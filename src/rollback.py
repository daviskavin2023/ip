import os
import sys
import json
import logging
from cf_client import CloudflareClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

TOKEN = os.environ["CF_API_TOKEN"]
ZONE_ID = os.environ["CF_ZONE_ID"]
DOMAIN = "bgy.808608.xyz"
TUNNEL_CNAME = "0333ec4f-06b3-45ee-8607-b10da69a5195.cfargotunnel.com"

def rollback():
    """
    Rolls DOMAIN back to the official Tunnel CNAME.

    A CNAME can't coexist with any other record at the same name (standard DNS
    rule Cloudflare enforces too), so a plain "delete everything, then create
    the CNAME" is NOT atomic: if delete succeeds but create fails (rate limit,
    network blip, bad token...), the domain is left with ZERO DNS records —
    worse than before the rollback ran. Instead:
      1. Delete every existing record EXCEPT one "survivor" — the domain
         always has at least 1 record standing throughout this phase.
      2. UPDATE that survivor in place to become the target CNAME — a single
         atomic PUT, not a delete+create pair.
      3. Only if there were zero records to begin with do we fall back to a
         plain create.
    Worst case on partial failure: a stray extra record or two left over
    (visible in the Cloudflare dashboard, easy to clean up manually) — never
    a domain with no DNS record at all.
    """
    logging.info(f"Initiating rollback for {DOMAIN} to official Tunnel CNAME: {TUNNEL_CNAME}...")
    client = CloudflareClient(TOKEN, ZONE_ID)

    records = client.list_dns_records(name=DOMAIN)
    logging.info(f"Found {len(records)} existing DNS records for {DOMAIN}")

    if not records:
        logging.info(f"No existing records — creating fresh CNAME: {DOMAIN} -> {TUNNEL_CNAME}")
        cname_rec = client.create_dns_record(
            name=DOMAIN, r_type="CNAME", content=TUNNEL_CNAME,
            ttl=1, proxied=True, comment="3040-rollback-official-tunnel"
        )
        logging.info(f"Rollback completed successfully! New record ID: {cname_rec.get('id')}")
        return True

    survivor = records[0]
    extras = records[1:]

    for rec in extras:
        rec_id, rec_type, rec_val = rec["id"], rec["type"], rec["content"]
        logging.info(f"Deleting surplus record {rec_id} ({rec_type} -> {rec_val})...")
        try:
            client.delete_dns_record(rec_id)
        except Exception as e:
            logging.error(f"Failed to delete surplus record {rec_id}: {e} (leaving it — not fatal, domain still has a valid record throughout)")

    logging.info(f"Updating surviving record {survivor['id']} in place -> CNAME {TUNNEL_CNAME} (proxied=True)...")
    updated = client.update_dns_record(
        record_id=survivor["id"], name=DOMAIN, r_type="CNAME", content=TUNNEL_CNAME,
        ttl=1, proxied=True, comment="3040-rollback-official-tunnel"
    )
    logging.info(f"Rollback completed successfully! Record ID: {updated.get('id')}")
    return True

if __name__ == "__main__":
    rollback()
