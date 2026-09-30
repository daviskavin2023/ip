import os
import sys
import json
import time
import logging
from aliyun_client import AliyunDnsClient
from daemon import CANDIDATES_BY_SUBNET16, get_subnet16
from build_pool import verify_stable

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

ALIYUN_AK = os.environ["ALIYUN_AK"]
ALIYUN_SK = os.environ["ALIYUN_SK"]
SPEED_DOMAIN = os.getenv("SPEED_DOMAIN", "speed.808608.xyz")
TEST_DOMAIN = os.getenv("TEST_DOMAIN", "bgy.808608.xyz")
STATE_FILE = os.getenv("CF_STATE_FILE", "/opt/3040-tunnel/state.json")

def perform_switch():
    logging.info(f"Starting Aliyun DNS optimization for {SPEED_DOMAIN} across 5 distinct /16 failure domains...")
    ali_client = AliyunDnsClient(ALIYUN_AK, ALIYUN_SK)

    # 1. Test top candidates across all /16 failure domains.
    # This is a batch re-selection (not an active-incident failover), so it
    # can afford verify_stable()'s repeated, time-spaced probes rather than
    # a single test_ip_https() snapshot — a single fast sample isn't enough
    # to trust an IP (see build_pool.py's verify_stable docstring / VERSION.md
    # 2026-09-27 for a real example of the same IP swinging 13ms -> >100ms
    # within 17 minutes).
    tested_subnets = []
    for subnet16, ip_list in CANDIDATES_BY_SUBNET16.items():
        best_ip = None
        best_rtt = 9999.0
        for ip in ip_list:
            result = verify_stable(TEST_DOMAIN, ip)
            if result and result["avg_ms"] < best_rtt:
                best_ip = ip
                best_rtt = result["avg_ms"]
        if best_ip:
            tested_subnets.append({
                "subnet": subnet16,
                "ip": best_ip,
                "latency_ms": best_rtt
            })

    # Sort subnets by lowest latency
    tested_subnets.sort(key=lambda x: x["latency_ms"])
    top5 = tested_subnets[:5]
    if len(top5) < 5:
        raise RuntimeError(f"Could not find 5 healthy distinct /16 subnets (only found {len(top5)})")

    logging.info("Top 5 selected distinct /16 failure domain nodes:")
    for item in top5:
        logging.info(f"  - Subnet: {item['subnet']:<16} IP: {item['ip']:<16} Latency: {item['latency_ms']}ms")

    # 2. Reconcile with Aliyun DNS records without duplicate conflicts
    existing = {r["RecordId"]: r["Value"] for r in ali_client.list_records(SPEED_DOMAIN) if r["Type"] == "A"}
    desired_ips = {item["ip"]: item for item in top5}

    kept_records = {}
    reusable_record_ids = []

    for rec_id, val in existing.items():
        if val in desired_ips and val not in kept_records:
            kept_records[val] = rec_id
        else:
            reusable_record_ids.append(rec_id)

    missing_ips = [ip for ip in desired_ips if ip not in kept_records]
    active_records = []

    # Preserve unchanged records
    for ip, rec_id in kept_records.items():
        item = desired_ips[ip]
        active_records.append({
            "record_id": rec_id,
            "ip": ip,
            "subnet": item["subnet"],
            "latency_ms": item["latency_ms"],
            "status": "HEALTHY",
            "consecutive_fails": 0
        })
        logging.info(f"Preserved existing record {rec_id} -> {ip} ({item['subnet']})")

    # Update or add missing records
    for ip in missing_ips:
        item = desired_ips[ip]
        if reusable_record_ids:
            rec_id = reusable_record_ids.pop()
            ali_client.update_record(rec_id, "@", "A", ip, ttl=600)
            logging.info(f"Updated Aliyun record {rec_id} -> {ip} ({item['subnet']})")
        else:
            rec_id = ali_client.add_record(SPEED_DOMAIN, "@", "A", ip, ttl=600)
            logging.info(f"Added Aliyun record {rec_id} -> {ip} ({item['subnet']})")

        active_records.append({
            "record_id": rec_id,
            "ip": ip,
            "subnet": item["subnet"],
            "latency_ms": item["latency_ms"],
            "status": "HEALTHY",
            "consecutive_fails": 0
        })

    # Delete surplus records if any
    for surplus_id in reusable_record_ids:
        ali_client.delete_record(surplus_id)
        logging.info(f"Deleted surplus record {surplus_id}")

    # Enforce strict invariant check
    subnets_check = [r["subnet"] for r in active_records]
    assert len(subnets_check) == 5, f"Expected 5 records, got {len(subnets_check)}"
    assert len(set(subnets_check)) == 5, f"Redundancy violation: duplicate /16 subnets: {subnets_check}"

    # 3. Save state
    state = {
        "domain": TEST_DOMAIN,
        "speed_domain": SPEED_DOMAIN,
        "active_records": active_records,
        "cooldown_ips": {},
        "switched_at": int(time.time()),
        "status": "active"
    }
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)

    logging.info(f"Switch completed successfully! Saved state to {STATE_FILE}.")
    return state

if __name__ == "__main__":
    perform_switch()
