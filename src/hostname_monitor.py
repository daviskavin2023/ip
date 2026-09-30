import os
import json
import time
import logging
from cf_client import CloudflareClient, test_ip_https

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("/opt/3040-tunnel/hostname_monitor.log", encoding="utf-8")
    ]
)

CF_TOKEN = os.environ["CF_API_TOKEN"]
CF_ZONE_ID = os.environ["CF_ZONE_ID"]
STATE_FILE = os.getenv("MANAGED_HOSTNAMES_FILE", "/opt/3040-tunnel/managed_hostnames.json")
ACTIVE_STATE_FILE = os.getenv("CF_STATE_FILE", "/opt/3040-tunnel/state.json")
CHECK_INTERVAL = int(os.getenv("HOSTNAME_CHECK_INTERVAL", "300"))
FAILURE_THRESHOLD = int(os.getenv("HOSTNAME_FAILURE_THRESHOLD", "2"))


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
    tmp = f"{STATE_FILE}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


def check_hostname(hostname, ips):
    """
    A converted hostname must serve valid HTTP responses on EVERY currently active
    optimized IP — not just one of them. Real clients round-robin across
    all of the active A records, so a hostname that's fine on 4 IPs but
    broken on the 5th still causes real partial-outage traffic.

    Valid HTTP codes indicating traffic reached origin server through Anycast edge:
    2xx, 3xx (Redirects e.g. login pages like Joplin, HA), 401 (Auth required), 404 (Root not mapped).
    Failure codes: 0 (Timeout/Connect fail), 403 (SaaS inactive/blocked), 5xx (Gateway error).
    """
    for ip in ips:
        code, _, _ = test_ip_https(hostname, ip, timeout_sec=4.0)
        if code not in (200, 204, 301, 302, 307, 308, 401, 404):
            return False, ip
    return True, None


def run_monitor():
    cf = CloudflareClient(CF_TOKEN, CF_ZONE_ID)
    logging.info(f"Starting hostname health monitor: interval={CHECK_INTERVAL}s, failure_threshold={FAILURE_THRESHOLD}")

    while True:
        state = load_state()
        managed = state.setdefault("managed", {})
        active_ips = get_active_ips()

        if not active_ips:
            logging.error(f"{ACTIVE_STATE_FILE} 里没有 active_records，本轮跳过")
            time.sleep(CHECK_INTERVAL)
            continue

        checked = 0
        for hostname, info in list(managed.items()):
            if info.get("status") != "CONVERTED":
                continue
            checked += 1

            ok, bad_ip = check_hostname(hostname, active_ips)
            if ok:
                if info.get("consecutive_fails", 0) > 0:
                    logging.info(f"{hostname}: 恢复正常，清零失败计数")
                info["consecutive_fails"] = 0
                info["last_checked"] = int(time.time())
                continue

            info["consecutive_fails"] = info.get("consecutive_fails", 0) + 1
            logging.warning(f"{hostname}: 在优选IP {bad_ip} 上探测失败（连续 {info['consecutive_fails']}/{FAILURE_THRESHOLD} 次）")

            if info["consecutive_fails"] >= FAILURE_THRESHOLD:
                original = info.get("original_cname")
                if not original:
                    logging.critical(f"{hostname}: 连续失败达到阈值，但没有记录 original_cname，无法自动回滚！需要人工立刻介入")
                    continue
                try:
                    recs = cf.list_dns_records(name=hostname)
                    if not recs:
                        logging.critical(f"{hostname}: 查不到DNS记录，无法自动回滚，需要人工立刻介入")
                        continue
                    cf.update_dns_record(
                        record_id=recs[0]["id"], name=hostname, r_type="CNAME",
                        content=original, ttl=1, proxied=True,
                        comment="3040-auto-hostname-monitor-rollback"
                    )
                    info["status"] = "AUTO_ROLLED_BACK"
                    info["rolled_back_at"] = int(time.time())
                    info["rolled_back_reason"] = f"连续{FAILURE_THRESHOLD}次在优选IP {bad_ip} 上探测失败"
                    logging.critical(f"{hostname}: 已自动回滚 -> {original}（原因: 连续失败）")
                except Exception as e:
                    logging.critical(f"{hostname}: 自动回滚失败: {e}，需要人工立刻介入")

        # Merge with fresh disk state so we don't overwrite concurrent PINNED_DIRECT or rollout changes
        disk_state = load_state()
        disk_managed = disk_state.setdefault("managed", {})
        for h, info in managed.items():
            if info.get("status") == "CONVERTED" and h in disk_managed and disk_managed[h].get("status") == "CONVERTED":
                disk_managed[h]["consecutive_fails"] = info.get("consecutive_fails", 0)
                disk_managed[h]["last_checked"] = info.get("last_checked", disk_managed[h].get("last_checked"))
            elif info.get("status") == "AUTO_ROLLED_BACK" and h in disk_managed:
                disk_managed[h] = info
        save_state(disk_state)
        logging.info(f"本轮巡检完成: 共监控 {checked} 个已转换域名")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    run_monitor()
