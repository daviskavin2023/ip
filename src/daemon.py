import os
import sys
import time
import json
import logging
from cf_client import test_ip_https
from aliyun_client import AliyunDnsClient
from rollback import rollback
from web_ui import start_server_thread
from build_pool import verify_stable

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("/opt/3040-tunnel/daemon.log", encoding="utf-8")
    ]
)

ALIYUN_AK = os.environ["ALIYUN_AK"]
ALIYUN_SK = os.environ["ALIYUN_SK"]
SPEED_DOMAIN = os.getenv("SPEED_DOMAIN", "speed.808608.xyz")
TEST_DOMAIN = os.getenv("TEST_DOMAIN", "bgy.808608.xyz")

# Not used directly in this module anymore (rollback() reads its own copy) — validated here
# anyway so a missing/misconfigured CF token fails fast at daemon startup, not mid-outage
# when the circuit breaker actually needs it.
os.environ["CF_API_TOKEN"]
os.environ["CF_ZONE_ID"]

STATE_FILE = "/opt/3040-tunnel/state.json"
STATUS_FILE = "/opt/3040-tunnel/live_status.json"

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))
FAILURE_THRESHOLD = int(os.getenv("FAILURE_THRESHOLD", "2"))
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "7200"))
# How often to PROACTIVELY re-test the ENTIRE candidate pool and swap to a
# genuinely better set of 5, independent of whether anything is failing.
# Without this, the daemon only ever reacts to an active IP already having
# failed FAILURE_THRESHOLD times (find_replacement_ip) — it never asks "is
# there a better set of 5 than what's active right now?"
# Originally defaulted to 7200s (PRD module 2's stated cadence), but a live
# A/B on CT160 (2026-09-27) showed a /16 subnet flip from "best candidate
# passing at 900ms" to "zero candidates passing at all" within ~20 minutes —
# 2h is too slow to track real drift at that speed. 1800s balances
# responsiveness against the extra background HTTPS load each run adds.
REOPTIMIZE_INTERVAL_SEC = int(os.getenv("REOPTIMIZE_INTERVAL_SEC", "1800"))

def get_subnet16(ip: str) -> str:
    parts = ip.split(".")
    return f"{parts[0]}.{parts[1]}.0.0/16"

CANDIDATE_POOL_FILE = os.getenv("CANDIDATE_POOL_FILE", "/opt/3040-tunnel/candidate_pool.json")

# Bootstrap seed pool: ONLY used when candidate_pool.json doesn't exist yet
# (e.g. first run before any CloudflareST scan + build_pool.py has completed).
# Run `python3 build_pool.py` after a scan to replace this with real,
# latency-verified candidates — see src/build_pool.py.
_BOOTSTRAP_POOL = {
    "104.17.0.0/16": ["104.17.31.34", "104.17.44.94", "104.17.49.185"],
    "172.64.0.0/16": ["172.64.229.1", "172.64.146.38", "172.64.230.1"],
    "198.41.0.0/16": ["198.41.208.220", "198.41.196.7", "198.41.200.116"],
    "172.66.0.0/16": ["172.66.201.108", "172.66.210.43", "172.66.194.79", "172.66.204.199", "172.66.205.172"],
    "104.24.0.0/16": ["104.24.7.81", "104.24.54.114", "104.24.162.235"],
    "104.25.0.0/16": ["104.25.235.184", "104.25.52.100", "104.25.80.113"],
    "104.16.0.0/16": ["104.16.180.36", "104.16.213.143", "104.16.173.5"],
    "141.101.0.0/16": ["141.101.114.86"],
    "104.27.0.0/16": ["104.27.60.133", "104.27.100.7"],
    "104.19.0.0/16": ["104.19.227.244"],
    "104.20.0.0/16": ["104.20.34.201"]
}


def load_candidate_pool():
    if os.path.exists(CANDIDATE_POOL_FILE):
        try:
            with open(CANDIDATE_POOL_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            pool = data.get("pool") or {}
            if pool:
                logging.info(
                    f"已从 {CANDIDATE_POOL_FILE} 加载候选池：{len(pool)} 个 /16 网段 "
                    f"(生成于 {data.get('generated_at_str', '未知时间')}，延迟阈值 {data.get('max_latency_ms', '?')}ms)"
                )
                return pool
            logging.error(f"{CANDIDATE_POOL_FILE} 存在但 pool 字段为空，回退到内置种子池")
        except Exception as e:
            logging.error(f"读取 {CANDIDATE_POOL_FILE} 失败: {e}，回退到内置种子池")
    else:
        logging.warning(
            f"{CANDIDATE_POOL_FILE} 不存在，当前使用内置种子池（未经过本次实测的延迟验证）。"
            f"运行一次 CloudflareST 全网扫描 + `python3 build_pool.py` 可以生成真正的验证过的候选池。"
        )
    return _BOOTSTRAP_POOL


CANDIDATES_BY_SUBNET16 = load_candidate_pool()

def load_or_init_state(ali_client: AliyunDnsClient):
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass

    logging.info(f"Synchronizing active records from Aliyun for {SPEED_DOMAIN}...")
    ali_records = ali_client.list_records(SPEED_DOMAIN)
    active = []
    for r in ali_records:
        if r["Type"] == "A":
            ip = r["Value"]
            active.append({
                "record_id": r["RecordId"],
                "ip": ip,
                "subnet": get_subnet16(ip),
                "status": "INITIALIZING",
                "latency_ms": 0,
                "consecutive_fails": 0
            })

    state = {
        "domain": TEST_DOMAIN,
        "speed_domain": SPEED_DOMAIN,
        "active_records": active,
        "cooldown_ips": {},
        "status": "active",
        "updated_at": int(time.time())
    }
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    return state

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)

def write_live_status(state, probe_results):
    status_data = {
        "domain": TEST_DOMAIN,
        "speed_domain": SPEED_DOMAIN,
        "updated_at": int(time.time()),
        "updated_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        "status": state.get("status", "unknown"),
        "active_nodes": probe_results,
        "cooldown_count": len(state.get("cooldown_ips", {})),
        "cooldown_ips": state.get("cooldown_ips", {})
    }
    with open(STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump(status_data, f, indent=2, ensure_ascii=False)

def find_replacement_ip(failing_ip: str, active_records: list, cooldown_ips: dict):
    """
    Deliberately uses a single test_ip_https() probe per candidate, NOT
    build_pool.py's multi-sample verify_stable() — this runs mid-incident
    (the active IP just failed FAILURE_THRESHOLD times) and needs to react
    fast, not spend 3x longer confirming stability before failing over.
    The very next health-check cycle (CHECK_INTERVAL seconds later) probes
    whatever gets picked here anyway, so a flaky replacement still gets
    caught and rotated out quickly — this function only needs "good enough
    right now", the periodic loop is what enforces "stays good over time".
    """
    now = time.time()
    active_ips = {r["ip"] for r in active_records}
    target_subnet = get_subnet16(failing_ip)

    # Priority 1: Try alternative healthy candidates in the SAME /16 failure domain
    candidates_in_same_subnet = CANDIDATES_BY_SUBNET16.get(target_subnet, [])
    for ip in candidates_in_same_subnet:
        if ip in active_ips:
            continue
        if ip in cooldown_ips and now < cooldown_ips[ip]:
            continue
        code, _, _ = test_ip_https(TEST_DOMAIN, ip, timeout_sec=3.0)
        if code == 200:
            return ip, target_subnet

    # Priority 2: If entire /16 is degraded, choose from an UNUSED /16 failure domain
    active_subnets = {get_subnet16(r["ip"]) for r in active_records if r["ip"] != failing_ip}
    for sub, ip_list in CANDIDATES_BY_SUBNET16.items():
        if sub in active_subnets:
            continue  # HARD INVARIANT: strictly prevent any /16 overlap
        for ip in ip_list:
            if ip in active_ips:
                continue
            if ip in cooldown_ips and now < cooldown_ips[ip]:
                continue
            code, _, _ = test_ip_https(TEST_DOMAIN, ip, timeout_sec=3.0)
            if code == 200:
                return ip, sub

    return None, None


def full_reoptimize(ali_client: AliyunDnsClient, state: dict):
    """
    Periodic PROACTIVE full re-selection across the ENTIRE candidate pool —
    not just re-checking the 5 IPs already active. Mirrors switch.py's
    perform_switch() logic; duplicated here (rather than imported) because
    switch.py imports CANDIDATES_BY_SUBNET16 from THIS module, so importing
    switch.py back into daemon.py would be circular. If this drifts out of
    sync with switch.py, that's the reason — worth consolidating into a
    shared module later.

    Reloads candidate_pool.json from disk on every call (not just once at
    process startup) — build_pool.py is expected to run on its own schedule
    (systemd timer) and refresh that file independently of this daemon's
    lifetime. Without the reload, a freshly-rebuilt pool on disk would sit
    unused until the next full daemon restart.
    """
    global CANDIDATES_BY_SUBNET16
    CANDIDATES_BY_SUBNET16 = load_candidate_pool()
    logging.info("[定时全量优选] 开始对候选池全部 /16 网段重新测速排序（每个候选走 verify_stable 多次复测，不是单次快照）...")
    tested_subnets = []
    for subnet16, ip_list in CANDIDATES_BY_SUBNET16.items():
        best_ip, best_ms = None, float("inf")
        for ip in ip_list:
            result = verify_stable(TEST_DOMAIN, ip)
            if result and result["avg_ms"] < best_ms:
                best_ip, best_ms = ip, result["avg_ms"]
        if best_ip:
            tested_subnets.append({"subnet": subnet16, "ip": best_ip, "latency_ms": best_ms})

    tested_subnets.sort(key=lambda x: x["latency_ms"])
    top5 = tested_subnets[:5]
    if len(top5) < 5:
        logging.error(f"[定时全量优选] 只找到 {len(top5)} 个合格 /16 网段（要求5个），本轮放弃更新，维持现有 active_records 不变。")
        return state

    new_ips = {item["ip"] for item in top5}
    old_ips = {r["ip"] for r in state.get("active_records", [])}
    if new_ips == old_ips:
        logging.info("[定时全量优选] 全量重选结果和当前生产使用的一致，无需更新DNS。")
        return state

    logging.info(f"[定时全量优选] 发现更优组合，准备更新Aliyun DNS: {sorted(old_ips)} -> {sorted(new_ips)}")

    existing = {r["RecordId"]: r["Value"] for r in ali_client.list_records(SPEED_DOMAIN) if r["Type"] == "A"}
    desired_ips = {item["ip"]: item for item in top5}
    kept_records, reusable_record_ids = {}, []
    for rec_id, val in existing.items():
        if val in desired_ips and val not in kept_records:
            kept_records[val] = rec_id
        else:
            reusable_record_ids.append(rec_id)

    missing_ips = [ip for ip in desired_ips if ip not in kept_records]
    active_records = []
    for ip, rec_id in kept_records.items():
        item = desired_ips[ip]
        active_records.append({"record_id": rec_id, "ip": ip, "subnet": item["subnet"],
                                "status": "HEALTHY", "last_latency_ms": item["latency_ms"], "consecutive_fails": 0})
    for ip in missing_ips:
        item = desired_ips[ip]
        if reusable_record_ids:
            rec_id = reusable_record_ids.pop()
            ali_client.update_record(rec_id, "@", "A", ip, ttl=600)
        else:
            rec_id = ali_client.add_record(SPEED_DOMAIN, "@", "A", ip, ttl=600)
        logging.info(f"[定时全量优选] 写入 {ip} ({item['subnet']}) -> record {rec_id}")
        active_records.append({"record_id": rec_id, "ip": ip, "subnet": item["subnet"],
                                "status": "HEALTHY", "last_latency_ms": item["latency_ms"], "consecutive_fails": 0})
    for surplus_id in reusable_record_ids:
        ali_client.delete_record(surplus_id)
        logging.info(f"[定时全量优选] 删除多余记录 {surplus_id}")

    state["active_records"] = active_records
    state["cooldown_ips"] = {}
    save_state(state)
    logging.info(f"[定时全量优选] 完成，新的5个生效IP: {sorted(new_ips)}")
    return state


def run_daemon():
    logging.info(f"Starting 3040-tunnel Aliyun automated health daemon for {TEST_DOMAIN}...")
    logging.info(f"Check interval: {CHECK_INTERVAL}s | Failure threshold: {FAILURE_THRESHOLD} | Cooldown: {COOLDOWN_SECONDS}s")

    try:
        start_server_thread()
        logging.info("3040 Web Dashboard launched on port 3040 successfully.")
    except Exception as e:
        logging.warning(f"Could not start Web Dashboard thread: {e}")

    ali_client = AliyunDnsClient(ALIYUN_AK, ALIYUN_SK)

    while True:
        state = load_or_init_state(ali_client)
        now = int(time.time())
        cooldown_ips = state.get("cooldown_ips", {})

        # 1. Clean expired cooldowns
        expired = [ip for ip, expire_ts in cooldown_ips.items() if now >= expire_ts]
        for ip in expired:
            logging.info(f"IP {ip} cooldown expired. Returned to candidate pool.")
            del cooldown_ips[ip]
        state["cooldown_ips"] = cooldown_ips

        # 1.5 Periodic PROACTIVE full re-optimization — independent of whether
        # anything is currently failing (that's what find_replacement_ip below
        # is for). Without this, a genuinely faster candidate elsewhere in the
        # pool would never get picked up unless an active IP outright failed.
        last_reopt = state.get("last_reoptimize_at", 0)
        if now - last_reopt >= REOPTIMIZE_INTERVAL_SEC:
            try:
                state = full_reoptimize(ali_client, state)
            except Exception as e:
                logging.error(f"[定时全量优选] 本轮执行异常，跳过，维持现状: {e}")
            state["last_reoptimize_at"] = now
            save_state(state)
            active_records = state.get("active_records", [])

        # 2. Probe active records
        active_records = state.get("active_records", [])
        probe_results = []
        all_failed = True
        state_modified = False

        for rec in list(active_records):
            ip = rec["ip"]
            code, tcp_cost, total_cost = test_ip_https(TEST_DOMAIN, ip, timeout_sec=3.0)
            cost_ms = round(tcp_cost * 1000, 1)

            if code == 200:
                rec["consecutive_fails"] = 0
                rec["last_latency_ms"] = cost_ms
                rec["status"] = "HEALTHY"
                all_failed = False
                state["status"] = "active"
                logging.info(f"Probe OK: {ip:<15} ({rec['subnet']}) -> 200 OK (TCP: {cost_ms}ms, Total: {round(total_cost*1000, 1)}ms)")
            else:
                rec["consecutive_fails"] = rec.get("consecutive_fails", 0) + 1
                rec["status"] = f"FAIL_{rec['consecutive_fails']}"
                # code == 0 means test_ip_https() itself errored/timed out (see cf_client.py's
                # except-block sentinel of 9.99s) — that's not a real 9990ms measurement, it's
                # "couldn't connect at all". Showing it as a literal ms number on the dashboard
                # is misleading (looked like a real, if bad, latency reading). null it instead.
                rec["last_latency_ms"] = None if code == 0 else cost_ms
                logging.warning(f"Probe FAIL: {ip:<15} ({rec['subnet']}) -> Code {code} (Consecutive: {rec['consecutive_fails']})")

                if rec["consecutive_fails"] >= FAILURE_THRESHOLD:
                    logging.error(f"IP {ip} exceeded failure threshold ({FAILURE_THRESHOLD}). Replacing on Aliyun...")
                    cooldown_ips[ip] = now + COOLDOWN_SECONDS
                    state_modified = True

                    new_ip, new_subnet = find_replacement_ip(ip, active_records, cooldown_ips)

                    if new_ip:
                        logging.info(f"Selected healthy replacement IP: {new_ip} ({new_subnet})")
                        try:
                            ali_client.update_record(rec["record_id"], "@", "A", new_ip, ttl=600)
                            logging.info(f"Successfully replaced Aliyun DNS record {rec['record_id']} with {new_ip}")
                            rec["ip"] = new_ip
                            rec["subnet"] = new_subnet
                            rec["consecutive_fails"] = 0
                            rec["status"] = "HEALTHY"
                            all_failed = False
                        except Exception as e:
                            logging.error(f"Failed to update Aliyun DNS record: {e}")
                    else:
                        logging.critical(f"No candidate IP available to replace {ip}!")

            probe_results.append({
                "ip": rec["ip"],
                "subnet": rec["subnet"],
                "status": rec.get("status", "UNKNOWN"),
                "latency_ms": rec.get("last_latency_ms", 0),
                "consecutive_fails": rec.get("consecutive_fails", 0),
                "record_id": rec["record_id"]
            })

        # 3. Check circuit breaker
        if all_failed and len(active_records) > 0:
            logging.critical("CIRCUIT BREAKER: ALL active IPs failed! Rolling back to official Tunnel CNAME!")
            try:
                rollback()  # shared with src/rollback.py — single source of truth, see its own atomicity note
                state["status"] = "CIRCUIT_BREAKER_TRIGGERED"
                save_state(state)
                write_live_status(state, probe_results)
                time.sleep(300)
                continue
            except Exception as e:
                logging.error(f"Circuit breaker rollback failed: {e}")

        if state_modified:
            save_state(state)

        write_live_status(state, probe_results)
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    run_daemon()
