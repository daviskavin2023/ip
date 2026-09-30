import os
import sys
import json
import time
import logging
import subprocess
from cf_client import CloudflareClient, test_ip_https

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("/opt/3040-tunnel/rollout_manager.log", encoding="utf-8")
    ]
)

def _ensure_env():
    env_file = os.getenv("ENV_FILE", "/opt/3040-tunnel/.env")
    if os.path.exists(env_file):
        try:
            with open(env_file) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        k = k.strip()
                        if k not in os.environ:
                            os.environ[k] = v.strip().strip("'\"")
        except Exception:
            pass

_ensure_env()

# Main zone (808608.xyz) — owns the actual business hostnames' DNS records.
CF_TOKEN = os.environ.get("CF_API_TOKEN", "")
CF_ZONE_ID = os.environ.get("CF_ZONE_ID", "")
KAVIN_FUN_CF_API_TOKEN = os.environ.get("KAVIN_FUN_CF_API_TOKEN", "")
KAVIN_FUN_ZONE_ID = os.environ.get("KAVIN_FUN_ZONE_ID", "")

PANEL_API = os.getenv("PANEL_API", "http://10.10.10.160:39091/api/cache")
SHARED_ACCEL_DOMAIN = os.getenv("SHARED_ACCEL_DOMAIN", "speed.808608.xyz")
EXCLUDED_HOSTNAMES = set(
    h.strip() for h in os.getenv("EXCLUDED_HOSTNAMES", "cloudtunnel.808608.xyz").split(",") if h.strip()
)
STATE_FILE = os.getenv("MANAGED_HOSTNAMES_FILE", "/opt/3040-tunnel/managed_hostnames.json")
ACTIVE_STATE_FILE = os.getenv("CF_STATE_FILE", "/opt/3040-tunnel/state.json")

CUSTOM_HOSTNAME_POLL_INTERVAL_SEC = int(os.getenv("CUSTOM_HOSTNAME_POLL_INTERVAL_SEC", "15"))
CUSTOM_HOSTNAME_POLL_TIMEOUT_SEC = int(os.getenv("CUSTOM_HOSTNAME_POLL_TIMEOUT_SEC", "600"))

# Post-conversion sanity check — now a light double-check, not a workaround
# for flakiness. With Custom Hostname properly registered first, the earlier
# "fails ~60s after conversion" pattern should no longer occur at all; if it
# still does, that's a genuinely new problem worth seeing, not something to
# paper over with more rounds.
VERIFY_ROUNDS = int(os.getenv("VERIFY_ROUNDS", "2"))
VERIFY_ROUND_DELAY_SEC = int(os.getenv("VERIFY_ROUND_DELAY_SEC", "90"))
BETWEEN_HOSTNAMES_DELAY_SEC = int(os.getenv("BETWEEN_HOSTNAMES_DELAY_SEC", "30"))
MIN_BODY_SIZE = int(os.getenv("MIN_BODY_SIZE", "200"))
RETRY_COOLDOWN_SEC = int(os.getenv("RETRY_COOLDOWN_SEC", "1800"))
MAX_VERIFY_ATTEMPTS = int(os.getenv("MAX_VERIFY_ATTEMPTS", "3"))

UA_HEADERS = [
    "-A", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "-H", "Accept-Language: zh-CN,zh;q=0.9",
]


def fetch_panel_hostnames():
    # Priority 1: Direct live summary from Cloudflare via tunnel-panel (takes <2s, no stale cache)
    summary_api = PANEL_API.replace("/api/cache", "/api/summary")
    try:
        r = subprocess.run(["curl", "-s", "--max-time", "8", summary_api], capture_output=True, text=True, timeout=10)
        data = json.loads(r.stdout)
        if data.get("success"):
            seen, out = set(), []
            for e in data.get("result", []):
                h = e.get("hostname")
                if h and h not in seen:
                    seen.add(h)
                    out.append(h)
            return out
    except Exception as e:
        logging.warning(f"从 {summary_api} 实时获取域名失败: {e}，尝试读取缓存面板接口")

    # Priority 2: Fallback to /api/cache
    try:
        r = subprocess.run(["curl", "-s", "--max-time", "8", PANEL_API], capture_output=True, text=True, timeout=10)
        data = json.loads(r.stdout)
        seen, out = set(), []
        for e in data.get("result", []):
            h = e.get("hostname")
            if h and h not in seen:
                seen.add(h)
                out.append(h)
        return out
    except Exception as e:
        logging.error(f"从 {PANEL_API} 读取域名失败: {e}")
        return []


def get_active_ips():
    if not os.path.exists(ACTIVE_STATE_FILE):
        return []
    with open(ACTIVE_STATE_FILE, "r", encoding="utf-8") as f:
        return [r["ip"] for r in json.load(f).get("active_records", [])]


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"managed": {}}


def save_state(state):
    tmp_file = f"{STATE_FILE}.tmp"
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                disk = json.load(f)
            disk_managed = disk.get("managed", {})
            for h, info in disk_managed.items():
                if info.get("status") == "PINNED_DIRECT":
                    state.setdefault("managed", {})[h] = info
    except Exception:
        pass
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp_file, STATE_FILE)


def is_standard_tunnel_cname(content: str) -> bool:
    return content.endswith(".cfargotunnel.com")


def reconcile_deleted_hostnames(saas_cf, cf, panel_hostnames, managed, state):
    """
    Bi-directional synchronization: when a hostname is removed from cloudtunnel
    (or Cloudflare Zero Trust), automatically clean up all associated resources
    and drop it from the 3040 managed matrix.
    """
    panel_set = set(panel_hostnames)
    deleted = [h for h in list(managed.keys()) if h not in panel_set and h not in EXCLUDED_HOSTNAMES]
    if not deleted:
        return False

    changed = False
    for h in deleted:
        logging.info(f"Hostname [{h}] 已在 cloudtunnel/Cloudflare 面板中删除，开始全自动资源解绑与状态同步...")

        # 1. Clean SaaS Custom Hostname on kavin.fun if exists
        try:
            chs = saas_cf.list_custom_hostnames(hostname=h)
            for ch in chs:
                saas_cf.delete_custom_hostname(ch["id"])
                logging.info(f"{h}: 已自动删除 SaaS Custom Hostname (id={ch['id']})")
        except Exception as e:
            logging.warning(f"{h}: 清理 SaaS Custom Hostname 异常: {e}")

        # 2. Clean verification TXT records on 808608.xyz if exist
        try:
            txts = cf.list_dns_records(r_type="TXT")
            for r in txts:
                name = r.get("name", "")
                if name.endswith(h) and ("_cf-custom-hostname" in name or "_acme-challenge" in name):
                    cf.delete_dns_record(r["id"])
                    logging.info(f"{h}: 已自动清理验证 TXT 记录 {name}")
        except Exception as e:
            logging.warning(f"{h}: 清理验证 TXT 记录异常: {e}")

        # 3. Clean up dangling CNAME pointing to SHARED_ACCEL_DOMAIN
        try:
            cnames = cf.list_dns_records(name=h, r_type="CNAME")
            for r in cnames:
                if r.get("content") == SHARED_ACCEL_DOMAIN:
                    cf.delete_dns_record(r["id"])
                    logging.info(f"{h}: 已自动清理指向加速域名的孤儿 CNAME 记录")
        except Exception as e:
            logging.warning(f"{h}: 清理 CNAME 记录异常: {e}")

        # 4. Remove from managed state
        del managed[h]
        changed = True
        logging.info(f"{h}: 已成功从 3040 监控列表中移除并完成状态对齐")

    if changed:
        save_state(state)
    return changed


def sync_now(saas_cf=None, cf=None):
    """Programmatic entry point for on-demand immediate synchronization."""
    if cf is None:
        cf = CloudflareClient(CF_TOKEN, CF_ZONE_ID)
    if saas_cf is None:
        saas_cf = CloudflareClient(KAVIN_FUN_CF_API_TOKEN, KAVIN_FUN_ZONE_ID)

    panel_hostnames = fetch_panel_hostnames()
    state = load_state()
    managed = state.setdefault("managed", {})

    reconciled = reconcile_deleted_hostnames(saas_cf, cf, panel_hostnames, managed, state)
    return {
        "success": True,
        "panel_count": len(panel_hostnames),
        "managed_count": len(managed),
        "reconciled": reconciled,
    }


def pin_hostname_direct(hostname: str, saas_cf=None, cf=None):
    """
    Manually pins a hostname as non-accelerated (direct native Tunnel CNAME).
    1. Reverts Cloudflare DNS CNAME to official *.cfargotunnel.com with proxied=True.
    2. Deletes SaaS Custom Hostname if registered on kavin.fun.
    3. Cleans verification TXT records.
    4. Sets status = 'PINNED_DIRECT' in managed_hostnames.json.
    """
    if cf is None:
        cf = CloudflareClient(CF_TOKEN, CF_ZONE_ID)
    if saas_cf is None:
        saas_cf = CloudflareClient(KAVIN_FUN_CF_API_TOKEN, KAVIN_FUN_ZONE_ID)

    state = load_state()
    managed = state.setdefault("managed", {})
    info = managed.setdefault(hostname, {})

    target_cname = info.get("original_cname")
    if not target_cname or not is_standard_tunnel_cname(target_cname):
        summary_api = PANEL_API.replace("/api/cache", "/api/summary")
        try:
            r = subprocess.run(["curl", "-s", "--max-time", "8", summary_api], capture_output=True, text=True, timeout=10)
            data = json.loads(r.stdout)
            for it in data.get("result", []):
                if it.get("hostname") == hostname and it.get("tunnel_id"):
                    target_cname = f"{it['tunnel_id']}.cfargotunnel.com"
                    break
        except Exception:
            pass

    # 1. Update DNS record to point to target_cname (proxied=True)
    if target_cname:
        try:
            recs = cf.list_dns_records(name=hostname, r_type="CNAME")
            if recs:
                rec_id = recs[0]["id"]
                cf.update_dns_record(
                    record_id=rec_id, name=hostname, r_type="CNAME",
                    content=target_cname, ttl=1, proxied=True,
                    comment="3040-pinned-direct"
                )
                logging.info(f"{hostname}: DNS 已切回原生官方 Tunnel CNAME ({target_cname})")
        except Exception as e:
            logging.warning(f"{hostname}: 切回原生 DNS 失败: {e}")

    # 2. Delete SaaS Custom Hostname on kavin.fun if exists
    try:
        chs = saas_cf.list_custom_hostnames(hostname=hostname)
        for ch in chs:
            saas_cf.delete_custom_hostname(ch["id"])
            logging.info(f"{hostname}: 已删除 SaaS Custom Hostname (id={ch['id']})")
    except Exception as e:
        logging.warning(f"{hostname}: 删除 SaaS Custom Hostname 出错: {e}")

    # 3. Clean verification TXT records
    try:
        txts = cf.list_dns_records(r_type="TXT")
        for r in txts:
            name = r.get("name", "")
            if name.endswith(hostname) and ("_cf-custom-hostname" in name or "_acme-challenge" in name):
                cf.delete_dns_record(r["id"])
                logging.info(f"{hostname}: 已清理验证 TXT 记录 {name}")
    except Exception as e:
        logging.warning(f"{hostname}: 清理 TXT 记录出错: {e}")

    # 4. Update managed status
    for k in ("failure_reason", "verify_fail_count", "retry_after", "error"):
        info.pop(k, None)
    if target_cname:
        info["original_cname"] = target_cname
    info["status"] = "PINNED_DIRECT"
    info["note"] = "手动固定不优选 (直连原生Tunnel)"
    info["last_checked"] = int(time.time())

    save_state(state)
    logging.info(f"{hostname}: 已成功标记为固定不优选 (PINNED_DIRECT)")
    return {"success": True, "hostname": hostname, "status": "PINNED_DIRECT"}


def unpin_hostname(hostname: str):
    """
    Unpins a hostname, allowing rollout_manager to evaluate and optimize it again.
    """
    state = load_state()
    managed = state.setdefault("managed", {})
    if hostname in managed:
        managed[hostname]["status"] = "WAITING_ROLLOUT"
        managed[hostname].pop("failure_reason", None)
        managed[hostname].pop("verify_fail_count", None)
        managed[hostname].pop("retry_after", None)
        managed[hostname]["note"] = "已解除固定，等待自动优选评估"
        managed[hostname]["last_checked"] = int(time.time())
        save_state(state)
        logging.info(f"{hostname}: 已解除固定不优选，重新进入自动优选队列")
    return {"success": True, "hostname": hostname, "status": "WAITING_ROLLOUT"}


def real_content_check(hostname, ip, timeout_sec=6.0):
    try:
        r = subprocess.run([
            "curl", "-s", "-L", "--max-redirs", "3", "-o", "/dev/null", "-w", "%{http_code}|%{size_download}",
            "--resolve", f"{hostname}:443:{ip}",
            *UA_HEADERS,
            f"https://{hostname}/", "-m", str(int(timeout_sec))
        ], capture_output=True, text=True, timeout=timeout_sec + 2)
        parts = r.stdout.strip().split("|")
        if len(parts) != 2:
            return False, "no_response"
        code, size = parts[0], int(parts[1]) if parts[1].isdigit() else 0
        # Valid HTTP codes indicating traffic reached origin server through Anycast edge:
        # 2xx, 3xx (Redirects e.g. login pages), 401 (Auth required), 403 (Forbidden), 404 (Root not mapped)
        if code in ("200", "204", "301", "302", "307", "308", "401", "403", "404"):
            return True, "ok"
        return False, f"http_{code}"
    except Exception as e:
        return False, f"exception_{e}"


def verify_hostname(hostname, active_ips):
    for round_num in range(1, VERIFY_ROUNDS + 1):
        for ip in active_ips:
            ok, reason = real_content_check(hostname, ip)
            if not ok:
                return False, f"第{round_num}轮在IP {ip} 上失败({reason})"
        logging.info(f"{hostname}: 第{round_num}/{VERIFY_ROUNDS}轮验证通过(全部{len(active_ips)}个优选IP)")
        if round_num < VERIFY_ROUNDS:
            time.sleep(VERIFY_ROUND_DELAY_SEC)
    return True, "ok"


def register_custom_hostname(saas_cf, cf, hostname):
    """
    Registers `hostname` as a Custom Hostname on the kavin.fun SaaS zone and
    creates the ownership-verification TXT record on the main 808608.xyz zone.
    Does NOT wait for it to go active — see the module docstring note below
    for why: Cloudflare will not validate/activate a Custom Hostname while
    its DNS still CNAMEs to a *proxied* record under its own zone (verified
    2026-09-27: the API returns `verification_errors: ["custom hostname does
    not CNAME to this zone."]` until the CNAME is switched). The CNAME switch
    must happen BEFORE polling for active, not after — get this order backwards
    (as the first version of this script did) and it spins forever failing
    the exact same way regardless of which optimized IP is used.

    Returns the custom_hostname id. Safe to call repeatedly — returns the
    existing registration's id if one already exists.
    """
    existing = saas_cf.list_custom_hostnames(hostname=hostname)
    if existing:
        ch_id = existing[0]["id"]
        logging.info(f"{hostname}: Custom Hostname已存在(id={ch_id}, status={existing[0]['status']})")
    else:
        ch = saas_cf.create_custom_hostname(hostname)
        ch_id = ch["id"]
        ownership = ch.get("ownership_verification", {})
        txt_name, txt_value = ownership.get("name"), ownership.get("value")
        if not txt_name or not txt_value:
            raise RuntimeError("create_custom_hostname返回里没有ownership_verification信息")
        logging.info(f"{hostname}: 已注册Custom Hostname(id={ch_id})，写入所有权验证TXT记录 {txt_name}")
        cf.create_dns_record(
            name=txt_name, r_type="TXT", content=txt_value, ttl=60,
            proxied=False, comment="3040-custom-hostname-verify"
        )

    # Separate from ownership verification: the actual SSL certificate (DV)
    # needs its own _acme-challenge TXT record(s) — missing this leaves
    # ssl.status stuck at "pending_validation" forever even though `status`
    # itself goes "active" and routing already works.
    #
    # IMPORTANT: the CREATE response's ssl.validation_records is EMPTY right
    # after creation (ssl.status="initializing") — Cloudflare only populates
    # it a few seconds later. Reading it straight off the create response (the
    # first version of this function did) silently skips this step every
    # time. Must re-GET the custom hostname and retry a few times until it's
    # populated. (Found 2026-09-27: pve.808608.xyz's _acme-challenge TXT was
    # never created this way — confirmed via public DNS returning NXDOMAIN —
    # while fn/dsm got it manually patched in after the fact.)
    validation_records = []
    for _ in range(10):
        info = saas_cf.get_custom_hostname(ch_id)
        validation_records = info.get("ssl", {}).get("validation_records", [])
        if validation_records:
            break
        time.sleep(3)

    if not validation_records:
        logging.warning(f"{hostname}: 等了30秒 ssl.validation_records 还是空的，跳过SSL验证TXT这一步(routing可能仍然生效,但证书可能签不下来)")
    else:
        existing_txt = {r["content"].strip('"') for r in cf.list_dns_records(r_type="TXT")
                         if r.get("name", "").startswith("_acme-challenge.")}
        for v in validation_records:
            dv_name, dv_value = v.get("txt_name"), v.get("txt_value")
            if dv_name and dv_value and dv_value not in existing_txt:
                logging.info(f"{hostname}: 写入SSL证书验证TXT记录 {dv_name}")
                cf.create_dns_record(
                    name=dv_name, r_type="TXT", content=dv_value, ttl=60,
                    proxied=False, comment="3040-ssl-dv-validation"
                )

    return ch_id


def wait_custom_hostname_active(saas_cf, hostname, ch_id):
    """Call AFTER the CNAME has already been switched to the shared accel
    domain — see register_custom_hostname's docstring for why the order matters."""
    deadline = time.time() + CUSTOM_HOSTNAME_POLL_TIMEOUT_SEC
    status, ssl_status = None, None
    while time.time() < deadline:
        info = saas_cf.get_custom_hostname(ch_id)
        status, ssl_status = info.get("status"), info.get("ssl", {}).get("status")
        # Require BOTH: `status` (routing) active AND `ssl.status` (the actual
        # dedicated certificate) active — bgy has both; a hostname stuck with
        # routing active but ssl.status forever "pending_validation" doesn't
        # truly match bgy's working end state, even though HTTP already
        # returns 200 via Cloudflare's interim/universal cert in the meantime.
        if status == "active" and ssl_status == "active":
            logging.info(f"{hostname}: Custom Hostname完全就绪(status=active, ssl.status=active)")
            return True, None
        logging.info(f"{hostname}: 等待Custom Hostname生效中... status={status} ssl.status={ssl_status}")
        time.sleep(CUSTOM_HOSTNAME_POLL_INTERVAL_SEC)
    return False, f"等待超时({CUSTOM_HOSTNAME_POLL_TIMEOUT_SEC}s)，最后状态status={status} ssl.status={ssl_status}"


def process_one_hostname(saas_cf, cf, hostname, active_ips, managed):
    records = cf.list_dns_records(name=hostname)
    if not records:
        managed[hostname] = {"status": "SKIPPED_NO_DNS_RECORD", "last_checked": int(time.time())}
        logging.warning(f"{hostname}: Cloudflare上查不到DNS记录,跳过")
        return

    rec = records[0]
    current_content = rec.get("content", "")

    if current_content == SHARED_ACCEL_DOMAIN:
        managed.setdefault(hostname, {})["status"] = "CONVERTED"
        managed[hostname]["last_checked"] = int(time.time())
        return

    if not is_standard_tunnel_cname(current_content):
        managed[hostname] = {"status": "SKIPPED_NON_STANDARD", "last_checked": int(time.time()), "note": current_content}
        logging.info(f"{hostname}: 当前记录 '{current_content}' 不是标准 *.cfargotunnel.com 格式,跳过")
        return

    managed[hostname] = managed.get(hostname, {})
    managed[hostname]["status"] = "REGISTERING_CUSTOM_HOSTNAME"
    managed[hostname]["last_checked"] = int(time.time())
    save_state({"managed": managed})

    try:
        ch_id = register_custom_hostname(saas_cf, cf, hostname)
    except Exception as e:
        managed[hostname]["status"] = "CUSTOM_HOSTNAME_FAILED"
        managed[hostname]["failure_reason"] = str(e)
        managed[hostname]["last_checked"] = int(time.time())
        logging.error(f"{hostname}: Custom Hostname注册失败: {e}")
        return

    # CNAME switch MUST happen before polling for active — see
    # register_custom_hostname's docstring for the verified reason why.
    logging.info(f"{hostname}: 转换CNAME -> {SHARED_ACCEL_DOMAIN}（Custom Hostname注册后，验证前）")
    cf.update_dns_record(
        record_id=rec["id"], name=hostname, r_type="CNAME",
        content=SHARED_ACCEL_DOMAIN, ttl=rec.get("ttl", 1),
        proxied=False, comment="3040-rollout-manager"
    )
    managed[hostname]["status"] = "WAITING_CUSTOM_HOSTNAME_ACTIVE"
    managed[hostname]["original_cname"] = current_content
    managed[hostname]["converted_at"] = int(time.time())
    managed[hostname]["last_checked"] = int(time.time())
    save_state({"managed": managed})

    ch_ok, ch_err = wait_custom_hostname_active(saas_cf, hostname, ch_id)
    if not ch_ok:
        cf.update_dns_record(
            record_id=rec["id"], name=hostname, r_type="CNAME",
            content=current_content, ttl=1, proxied=True,
            comment="3040-rollout-manager-ch-timeout-rollback"
        )
        managed[hostname]["status"] = "CUSTOM_HOSTNAME_FAILED"
        managed[hostname]["failure_reason"] = ch_err
        managed[hostname]["last_checked"] = int(time.time())
        logging.error(f"{hostname}: {ch_err}，已回滚CNAME")
        return

    managed[hostname]["status"] = "VERIFYING"
    managed[hostname]["last_checked"] = int(time.time())
    save_state({"managed": managed})

    ok, detail = verify_hostname(hostname, active_ips)

    if ok:
        # Clear stale fields from any earlier failed attempt(s) — a CONVERTED
        # row showing a leftover failure_reason from a previous try that
        # since succeeded is confusing (looked broken in the UI 2026-09-27
        # even though the domain was fine).
        for stale_key in ("failure_reason", "verify_fail_count", "retry_after"):
            managed[hostname].pop(stale_key, None)
        managed[hostname]["status"] = "CONVERTED"
        managed[hostname]["last_checked"] = int(time.time())
        managed[hostname]["verified_rounds"] = VERIFY_ROUNDS
        logging.info(f"{hostname}: {VERIFY_ROUNDS}轮验证全部通过,正式标记为已优选")
    else:
        cf.update_dns_record(
            record_id=rec["id"], name=hostname, r_type="CNAME",
            content=current_content, ttl=1, proxied=True,
            comment="3040-rollout-manager-verify-failed-rollback"
        )
        fails = managed[hostname].get("verify_fail_count", 0) + 1
        managed[hostname]["status"] = "FAILED_VERIFICATION"
        managed[hostname]["last_checked"] = int(time.time())
        managed[hostname]["failure_reason"] = detail
        managed[hostname]["verify_fail_count"] = fails
        if fails >= MAX_VERIFY_ATTEMPTS:
            managed[hostname]["status"] = "NEEDS_MANUAL_REVIEW"
            logging.warning(f"{hostname}: 验证未通过({detail})，已连续失败{fails}次(达到上限{MAX_VERIFY_ATTEMPTS})，标记为待人工review，不再自动重试")
        else:
            managed[hostname]["retry_after"] = int(time.time()) + RETRY_COOLDOWN_SEC
            logging.warning(f"{hostname}: 验证未通过({detail})，已回滚到原始CNAME，第{fails}/{MAX_VERIFY_ATTEMPTS}次失败，{RETRY_COOLDOWN_SEC}秒后才会重试")


def run():
    cf = CloudflareClient(CF_TOKEN, CF_ZONE_ID)
    saas_cf = CloudflareClient(KAVIN_FUN_CF_API_TOKEN, KAVIN_FUN_ZONE_ID)
    logging.info(
        f"Starting rollout manager: 每次只处理1个域名，先注册Custom Hostname(kavin.fun)再转CNAME，"
        f"{VERIFY_ROUNDS}轮验证(间隔{VERIFY_ROUND_DELAY_SEC}s)，域名间隔{BETWEEN_HOSTNAMES_DELAY_SEC}s"
    )

    while True:
        try:
            panel_hostnames = fetch_panel_hostnames()
            state = load_state()
            managed = state.setdefault("managed", {})

            # 1. 实时双向对齐：清理在 Cloudtunnel / Cloudflare 中已删除的域名
            reconcile_deleted_hostnames(saas_cf, cf, panel_hostnames, managed, state)

            active_ips = get_active_ips()
            if not active_ips:
                logging.error(f"{ACTIVE_STATE_FILE} 里没有 active_records，daemon可能没跑，等待重试")
                time.sleep(BETWEEN_HOSTNAMES_DELAY_SEC)
                continue

            # 1b. 确保所有排除名单域名均标记为 PINNED_DIRECT
            excluded_changed = False
            for hostname in EXCLUDED_HOSTNAMES:
                if managed.get(hostname, {}).get("status") != "PINNED_DIRECT":
                    managed.setdefault(hostname, {})["status"] = "PINNED_DIRECT"
                    managed[hostname]["note"] = "手动固定不优选 (直连原生Tunnel)"
                    managed[hostname]["last_checked"] = int(time.time())
                    excluded_changed = True
            if excluded_changed:
                save_state(state)

            # 2. 检查是否有需要接入或重试的新域名
            candidate = None
            now = int(time.time())
            for hostname in panel_hostnames:
                if hostname in EXCLUDED_HOSTNAMES:
                    continue
                info = managed.get(hostname, {})
                status = info.get("status")
                if status in ("CONVERTED", "SKIPPED_NO_DNS_RECORD", "SKIPPED_NON_STANDARD",
                              "NEEDS_MANUAL_REVIEW", "CUSTOM_HOSTNAME_FAILED", "PINNED_DIRECT"):
                    continue
                if status == "FAILED_VERIFICATION" and now < info.get("retry_after", 0):
                    continue
                candidate = hostname
                break

            if candidate is None:
                logging.info("本轮没有需要处理的新域名（全部已优选、已同步或已跳过），休眠后再检查")
                time.sleep(15)  # 15秒空闲检查，保持快速实时响应
                continue

            try:
                process_one_hostname(saas_cf, cf, candidate, active_ips, managed)
            except Exception as e:
                logging.error(f"{candidate}: 处理过程异常: {e}")
                managed[candidate] = {"status": "ERROR", "last_checked": int(time.time()), "error": str(e)}

            # Merge with fresh disk state to avoid clobbering concurrent user actions (e.g. PINNED_DIRECT)
            disk_state = load_state()
            disk_managed = disk_state.setdefault("managed", {})
            if candidate in managed:
                disk_managed[candidate] = managed[candidate]
            state["managed"] = disk_managed
            save_state(state)
            time.sleep(BETWEEN_HOSTNAMES_DELAY_SEC)
        except Exception as e:
            logging.error(f"Rollout manager 运行异常: {e}")
            time.sleep(10)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "--pin" and len(sys.argv) > 2:
            print(json.dumps(pin_hostname_direct(sys.argv[2]), ensure_ascii=False))
        elif cmd == "--unpin" and len(sys.argv) > 2:
            print(json.dumps(unpin_hostname(sys.argv[2]), ensure_ascii=False))
        elif cmd == "--sync":
            print(json.dumps(sync_now(), ensure_ascii=False))
        else:
            print(f"Unknown command: {cmd}")
            sys.exit(1)
    else:
        run()
