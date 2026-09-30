import os
import sys
import json
import time
import logging
import subprocess
from cf_client import CloudflareClient, test_ip_https

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

CF_TOKEN = os.environ["CF_API_TOKEN"]
CF_ZONE_ID = os.environ["CF_ZONE_ID"]
PANEL_API = os.getenv("PANEL_API", "http://10.10.10.160:39091/api/cache")
# Reusing speed.808608.xyz directly rather than standing up a brand-new
# "edge.808608.xyz" delegated zone — same effect (any hostname CNAME'd here
# rides the same Aliyun-maintained optimized-IP pool), zero extra Cloudflare/
# Aliyun NS delegation setup needed. See PRD.md Phase 5.0 for the reasoning.
SHARED_ACCEL_DOMAIN = os.getenv("SHARED_ACCEL_DOMAIN", "speed.808608.xyz")
EXCLUDED_HOSTNAMES = set(
    h.strip() for h in os.getenv("EXCLUDED_HOSTNAMES", "cloudtunnel.808608.xyz").split(",") if h.strip()
)
STATE_FILE = os.getenv("MANAGED_HOSTNAMES_FILE", "/opt/3040-tunnel/managed_hostnames.json")
ACTIVE_STATE_FILE = os.getenv("CF_STATE_FILE", "/opt/3040-tunnel/state.json")
# Starts in dry-run by design (PRD Phase 5.3 Step 2) — must be explicitly
# turned off (DRY_RUN=false) only after reviewing a dry-run's output.
DRY_RUN = os.getenv("DRY_RUN", "true").lower() in ("1", "true", "yes")


def fetch_panel_hostnames():
    """Pulls the full hostname list from the cloudtunnel panel's own API — no auth needed, already verified reachable."""
    r = subprocess.run(["curl", "-s", "--max-time", "10", PANEL_API], capture_output=True, text=True, timeout=15)
    data = json.loads(r.stdout)
    return data.get("result", [])


def get_active_ips():
    """ALL currently-active optimized IPs — not a sample. 2026-09-27 incident:
    checking only 2 of 5 gave a false-positive "verified" for 20 hostnames
    that actually failed on the other 3 (DNS round-robin means real traffic
    can land on ANY of the 5 — passing on 2 out of 5 proves nothing about
    what a real client will experience). A hostname is only safe to convert
    if it works on every single one of the currently active IPs."""
    if not os.path.exists(ACTIVE_STATE_FILE):
        return []
    with open(ACTIVE_STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)
    return [r["ip"] for r in state.get("active_records", [])]


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"managed": {}}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def is_standard_tunnel_cname(content: str) -> bool:
    return content.endswith(".cfargotunnel.com")


def sync():
    cf = CloudflareClient(CF_TOKEN, CF_ZONE_ID)
    active_ips = get_active_ips()
    if not active_ips:
        raise RuntimeError(f"{ACTIVE_STATE_FILE} 里没有 active_records，没有可用的优选IP做验证，先确保 daemon 已经跑起来")
    logging.info(f"本轮验证用全部 {len(active_ips)} 个当前优选IP: {active_ips}（要求全部通过才转换）")

    panel_hostnames = fetch_panel_hostnames()
    state = load_state()
    managed = state.setdefault("managed", {})

    seen = set()
    converted, pending, skipped, already_ok = 0, 0, 0, 0

    for entry in panel_hostnames:
        hostname = entry.get("hostname")
        if not hostname or hostname in seen:
            continue
        seen.add(hostname)

        if hostname in EXCLUDED_HOSTNAMES:
            skipped += 1
            continue

        records = cf.list_dns_records(name=hostname)
        if not records:
            logging.warning(f"{hostname}: 面板里有,但Cloudflare DNS上查不到记录,跳过")
            skipped += 1
            continue
        rec = records[0]
        current_content = rec.get("content", "")

        if current_content == SHARED_ACCEL_DOMAIN:
            managed.setdefault(hostname, {})["status"] = "CONVERTED"
            managed[hostname]["last_checked"] = int(time.time())
            already_ok += 1
            continue

        if not is_standard_tunnel_cname(current_content):
            logging.info(f"{hostname}: 当前记录 '{current_content}' 不是标准 *.cfargotunnel.com 格式,跳过(可能是手动配置过的,不碰)")
            skipped += 1
            continue

        # Require EVERY currently active IP to serve this hostname's zone
        # correctly — not "any one of them" — since DNS round-robin means a
        # real client can land on any of the 5. One bad IP in the set = a
        # real partial-outage risk for this hostname if we convert it.
        failing_ip = None
        for ip in active_ips:
            code, _, _ = test_ip_https(hostname, ip, timeout_sec=4.0)
            if code != 200:
                failing_ip = ip
                break
        verified = failing_ip is None

        if not verified:
            logging.warning(f"{hostname}: 在优选IP {failing_ip} 上未通过(可能没配这个zone证书),暂不接入,标记待人工确认")
            managed[hostname] = {
                "status": "PENDING_MANUAL_CHECK",
                "last_checked": int(time.time()),
                "original_cname": current_content
            }
            pending += 1
            continue

        if DRY_RUN:
            logging.info(f"[DRY-RUN] 会把 {hostname} 的CNAME从 '{current_content}' 改成 '{SHARED_ACCEL_DOMAIN}' (记录ID {rec['id']})")
            converted += 1
        else:
            cf.update_dns_record(
                record_id=rec["id"], name=hostname, r_type="CNAME",
                content=SHARED_ACCEL_DOMAIN, ttl=rec.get("ttl", 1),
                proxied=False,  # must be DNS-only, same lesson as bgy's Error 1000 incident
                comment="3040-auto-accel"
            )
            logging.info(f"{hostname}: 已转换CNAME -> {SHARED_ACCEL_DOMAIN} (原值: {current_content})")
            managed[hostname] = {
                "status": "CONVERTED",
                "original_cname": current_content,
                "converted_at": int(time.time()),
                "last_checked": int(time.time())
            }
            converted += 1

    save_state(state)
    logging.info(
        f"本轮同步完成{'（DRY-RUN，未实际修改任何DNS）' if DRY_RUN else ''}: "
        f"面板共 {len(seen)} 个Hostname | 已是优选状态 {already_ok} | "
        f"{'会转换' if DRY_RUN else '本轮转换'} {converted} | 待人工确认 {pending} | 跳过 {skipped}"
    )
    return {"total": len(seen), "already_ok": already_ok, "converted": converted, "pending": pending, "skipped": skipped}


if __name__ == "__main__":
    result = sync()
    sys.exit(0)
