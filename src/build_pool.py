import csv
import os
import re
import sys
import json
import time
import logging
import subprocess
from collections import defaultdict
from cf_client import test_ip_https

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

DOMAIN = os.getenv("TEST_DOMAIN", "bgy.808608.xyz")
CSV_PATH = os.getenv("SCAN_CSV", "/opt/3040-tunnel/result_all.csv")
OUT_PATH = os.getenv("CANDIDATE_POOL_FILE", "/opt/3040-tunnel/candidate_pool.json")
# Actively-maintained community source (auto-updated every ~3h), found to
# produce meaningfully better real candidates than this project's static,
# years-old local ip.txt — see VERSION.md 2026-09-27 A/B numbers. This is
# now the PRIMARY candidate source; the local CloudflareST CSV (if present)
# is merged in as a supplement, not required.
EXTERNAL_SOURCES = [
    s.strip() for s in os.getenv(
        "EXTERNAL_IP_SOURCES",
        "https://raw.githubusercontent.com/LancelotRar/best-cf-ips/main/best-cf-ip-collected.txt"
    ).split(",") if s.strip()
]
EXTERNAL_FETCH_TIMEOUT_SEC = int(os.getenv("EXTERNAL_FETCH_TIMEOUT_SEC", "30"))
# NOTE: as of 2026-09-27 these thresholds are measured against total_cost
# (full HTTPS response time: TCP + TLS + TTFB + download), NOT bare TCP
# connect time. A live comparison on CT160 showed the two can disagree —
# an IP with a much faster TCP handshake still had a SLOWER total response
# than a "default" IP, because the extra hop is TLS/backend-tunnel time,
# not network path — bare TCP latency was misleading real end-user
# experience. See VERSION.md 2026-09-27 for the raw numbers.
MAX_LATENCY_MS = float(os.getenv("MAX_LATENCY_MS", "1500"))
MIN_SUBNETS = int(os.getenv("MIN_SUBNETS", "5"))
MAX_PER_SUBNET = int(os.getenv("MAX_PER_SUBNET", "3"))
STABILITY_SAMPLES = int(os.getenv("STABILITY_SAMPLES", "3"))
MAX_JITTER_MS = float(os.getenv("MAX_JITTER_MS", "500"))
SAMPLE_DELAY_SEC = float(os.getenv("SAMPLE_DELAY_SEC", "0.4"))


def get_subnet16(ip: str) -> str:
    parts = ip.split(".")
    return f"{parts[0]}.{parts[1]}.0.0/16"


def fetch_external_candidates():
    """
    Pulls IPs from actively-maintained community sources (see
    EXTERNAL_SOURCES). Best-effort: raw.githubusercontent.com access from
    this network has been observed to be flaky (works, then times out a
    minute later, then works again) — a failed fetch here just means fewer
    candidates this round, not a hard failure. The local CloudflareST CSV
    (if present) is still merged in separately, so one bad fetch doesn't
    starve the whole pool.
    """
    ips = set()
    for url in EXTERNAL_SOURCES:
        try:
            result = subprocess.run(
                ["curl", "-s", "--max-time", str(EXTERNAL_FETCH_TIMEOUT_SEC), url],
                capture_output=True, text=True, timeout=EXTERNAL_FETCH_TIMEOUT_SEC + 5
            )
            found = 0
            for line in result.stdout.splitlines():
                m = re.match(r"^(\d+\.\d+\.\d+\.\d+):", line.strip())
                if m:
                    ips.add(m.group(1))
                    found += 1
            logging.info(f"外部候选源 {url} 拉取到 {found} 个IP")
        except Exception as e:
            logging.warning(f"外部候选源 {url} 拉取失败(跳过,不影响本地CSV候选): {e}")
    return ips


def verify_stable(domain: str, ip: str, samples: int = STABILITY_SAMPLES,
                   max_latency_ms: float = MAX_LATENCY_MS, max_jitter_ms: float = MAX_JITTER_MS,
                   delay_between_sec: float = SAMPLE_DELAY_SEC):
    """
    Runs `samples` independent, time-spaced HTTPS probes against this IP —
    a single fast test_ip_https() call isn't enough to call an IP "good":
    the same IP can measure 13ms one minute and 170ms+ (or drop the
    connection) a few minutes later (observed directly on this project —
    see VERSION.md 2026-09-27). A single lucky sample would let a flaky IP
    through just as easily as selector.py's old no-threshold bug did.

    Rejects on the FIRST failed sample (any drop = unstable, not "mostly
    fine") and on excessive jitter between samples (a wildly swinging
    response time is not something you want backing production traffic
    even if every single sample happened to return 200).

    Judges on total_cost (full response: TCP+TLS+TTFB+download), not just
    the TCP handshake — a live A/B on CT160 showed an IP with much faster
    TCP connect still had a slower total response than the "default"
    fallback IP, because the extra time was in TLS/backend-tunnel hops the
    bare network path doesn't capture. tcp_ms is still recorded per-sample
    for visibility, it just isn't what gates pass/fail anymore.

    Returns None if unstable, else {"avg_ms", "jitter_ms", "samples",
    "tcp_ms_samples"} where avg_ms/jitter_ms/samples are all in terms of
    total response time.
    """
    latencies = []
    tcp_latencies = []
    for i in range(samples):
        code, tcp_cost, total_cost = test_ip_https(domain, ip, timeout_sec=3.0)
        if code != 200:
            return None
        latencies.append(round(total_cost * 1000, 1))
        tcp_latencies.append(round(tcp_cost * 1000, 1))
        if i < samples - 1:
            time.sleep(delay_between_sec)

    avg_ms = round(sum(latencies) / len(latencies), 1)
    jitter_ms = round(max(latencies) - min(latencies), 1)
    if avg_ms > max_latency_ms or jitter_ms > max_jitter_ms:
        return None
    return {"avg_ms": avg_ms, "jitter_ms": jitter_ms, "samples": latencies, "tcp_ms_samples": tcp_latencies}


def build_pool():
    """
    Merges candidates from two sources into a verified, /16-diverse pool:
      1. Actively-maintained external community IP lists (EXTERNAL_SOURCES) —
         the PRIMARY source as of 2026-09-27, since these are refreshed every
         ~3h and proved to contain meaningfully better real candidates than
         this project's static local ip.txt (see VERSION.md A/B numbers).
      2. The local CloudflareST full-scan CSV (result_all.csv), if present —
         kept as a supplement, not required.
    Every candidate must pass `verify_stable()` — repeated, time-spaced real
    HTTPS probes, not a single snapshot — before it's allowed into the pool.

    If too few subnets qualify, this refuses to overwrite the existing pool
    file and exits non-zero, rather than silently degrading to worse IPs.
    """
    candidates_by_subnet = defaultdict(list)

    external_ips = fetch_external_candidates()
    for ip in external_ips:
        candidates_by_subnet[get_subnet16(ip)].append(ip)
    logging.info(f"外部候选源合计 {len(external_ips)} 个IP，覆盖 {len(candidates_by_subnet)} 个 /16 网段")

    if os.path.exists(CSV_PATH):
        csv_added = 0
        with open(CSV_PATH, "r", encoding="utf-8", errors="ignore") as f:
            reader = csv.DictReader(f)
            for row in reader:
                ip = (row.get("IP 地址") or "").strip()
                loss_str = (row.get("丢包率") or "1").strip()
                if not ip:
                    continue
                try:
                    loss = float(loss_str)
                except ValueError:
                    continue
                if loss != 0.0:
                    continue
                sub = get_subnet16(ip)
                if ip not in candidates_by_subnet[sub]:
                    candidates_by_subnet[sub].append(ip)
                    csv_added += 1
        logging.info(f"本地CloudflareST扫描({CSV_PATH})补充了 {csv_added} 个候选IP")
    else:
        logging.info(f"本地扫描文件 {CSV_PATH} 不存在，仅使用外部候选源（不是硬性要求）")

    if not candidates_by_subnet:
        raise RuntimeError("外部源和本地CSV都没有拿到任何候选IP，无法构建候选池")

    logging.info(f"合并后共 {len(candidates_by_subnet)} 个 /16 网段有候选IP待验证")

    verified_pool = {}
    diagnostics = {}
    for subnet, ips in candidates_by_subnet.items():
        verified = []
        for ip in ips:
            result = verify_stable(DOMAIN, ip)
            if result is None:
                continue
            verified.append((ip, result))
            diagnostics[ip] = result
            if len(verified) >= MAX_PER_SUBNET:
                break
        if verified:
            verified.sort(key=lambda x: x[1]["avg_ms"])
            verified_pool[subnet] = [ip for ip, _ in verified]
            best_ip, best_info = verified[0]
            logging.info(
                f"{subnet}: {len(verified)} 个候选通过 {STABILITY_SAMPLES} 次复测验证 "
                f"(每次都HTTPS 200、平均总响应耗时<={MAX_LATENCY_MS}ms、抖动<={MAX_JITTER_MS}ms) | "
                f"最优 {best_ip} 平均{best_info['avg_ms']}ms 抖动{best_info['jitter_ms']}ms 采样{best_info['samples']}"
            )

    if len(verified_pool) < MIN_SUBNETS:
        logging.error(
            f"候选池不合格: 只有 {len(verified_pool)} 个 /16 网段同时满足 "
            f"[连续{STABILITY_SAMPLES}次HTTPS 200 + 平均总响应耗时<={MAX_LATENCY_MS}ms + 抖动<={MAX_JITTER_MS}ms]"
            f"（要求至少 {MIN_SUBNETS} 个）。拒绝覆盖现有候选池文件，保留上一次的有效候选池不变，"
            f"避免用不合格/不稳定的 IP 静默污染生产。请检查网络状况或适当放宽阈值后重跑。"
        )
        return None

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump({
            "generated_at": int(time.time()),
            "generated_at_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            "domain": DOMAIN,
            "max_latency_ms": MAX_LATENCY_MS,
            "max_jitter_ms": MAX_JITTER_MS,
            "stability_samples": STABILITY_SAMPLES,
            "external_sources": EXTERNAL_SOURCES,
            "external_ip_count": len(external_ips),
            "source_csv": CSV_PATH if os.path.exists(CSV_PATH) else None,
            "pool": verified_pool,
            "diagnostics": diagnostics
        }, f, indent=2, ensure_ascii=False)
    logging.info(f"候选池已写入 {OUT_PATH}：共 {len(verified_pool)} 个 /16 网段（每个IP都经过{STABILITY_SAMPLES}次复测验证，非单次快照）")
    return verified_pool


if __name__ == "__main__":
    result = build_pool()
    sys.exit(0 if result else 1)
