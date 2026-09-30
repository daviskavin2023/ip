import json
import os
from cf_client import CloudflareClient

BROKEN = ["op", "qbt", "dapanel", "cloud", "dingyue", "alist", "sp", "ylw", "ylwht",
          "ylwsj", "ylwcb", "3d", "cjfx", "file", "flatnas", "dk", "yz", "cxyy",
          "18docker", "ha"]

cf = CloudflareClient(os.environ["CF_API_TOKEN"], os.environ["CF_ZONE_ID"])
state = json.load(open("managed_hostnames.json"))
managed = state.get("managed", {})

for prefix in BROKEN:
    hostname = f"{prefix}.808608.xyz"
    info = managed.get(hostname)
    if not info or "original_cname" not in info:
        print(f"{hostname}: 没有记录原始CNAME,跳过,需要人工处理")
        continue
    recs = cf.list_dns_records(name=hostname)
    if not recs:
        print(f"{hostname}: 查不到记录")
        continue
    rec = recs[0]
    original = info["original_cname"]
    cf.update_dns_record(record_id=rec["id"], name=hostname, r_type="CNAME",
                          content=original, ttl=1, proxied=True,
                          comment="3040-emergency-rollback")
    managed[hostname]["status"] = "ROLLED_BACK"
    print(f"{hostname}: 已回滚 -> {original}")

json.dump(state, open("managed_hostnames.json", "w"), indent=2, ensure_ascii=False)
print("全部处理完成")
