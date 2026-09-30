import urllib.request
import urllib.parse
import hmac
import hashlib
import base64
import time
import uuid
import json

class AliyunDnsClient:
    def __init__(self, ak_id: str, ak_secret: str):
        self.ak_id = ak_id
        self.ak_secret = ak_secret
        self.endpoint = "https://alidns.aliyuncs.com"

    def _request(self, action: str, params: dict):
        base_params = {
            "Format": "JSON",
            "Version": "2015-01-09",
            "AccessKeyId": self.ak_id,
            "SignatureMethod": "HMAC-SHA1",
            "Timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "SignatureVersion": "1.0",
            "SignatureNonce": str(uuid.uuid4()),
            "Action": action
        }
        all_params = {**base_params, **params}
        sorted_params = sorted(all_params.items())
        canonical_qs = urllib.parse.urlencode(sorted_params)
        string_to_sign = f"GET&%2F&{urllib.parse.quote(canonical_qs, safe='')}"
        key = (self.ak_secret + "&").encode("utf-8")
        h = hmac.new(key, string_to_sign.encode("utf-8"), hashlib.sha1)
        sig = base64.b64encode(h.digest()).decode("utf-8")
        url = f"{self.endpoint}/?{canonical_qs}&Signature={urllib.parse.quote(sig)}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def list_records(self, domain_name: str, rr_key_word: str = None):
        params = {"DomainName": domain_name, "PageSize": 100}
        if rr_key_word:
            params["RRKeyWord"] = rr_key_word
        res = self._request("DescribeDomainRecords", params)
        return res.get("DomainRecords", {}).get("Record", [])

    def add_record(self, domain_name: str, rr: str, r_type: str, value: str, ttl: int = 600):
        params = {
            "DomainName": domain_name,
            "RR": rr,
            "Type": r_type,
            "Value": value,
            "TTL": str(ttl)
        }
        res = self._request("AddDomainRecord", params)
        return res.get("RecordId")

    def update_record(self, record_id: str, rr: str, r_type: str, value: str, ttl: int = 600):
        params = {
            "RecordId": record_id,
            "RR": rr,
            "Type": r_type,
            "Value": value,
            "TTL": str(ttl)
        }
        res = self._request("UpdateDomainRecord", params)
        return res.get("RecordId")

    def delete_record(self, record_id: str):
        res = self._request("DeleteDomainRecord", {"RecordId": record_id})
        return res.get("RecordId")
