import urllib.request
import urllib.error
import json
import ssl
import subprocess
import time

class CloudflareClient:
    def __init__(self, api_token: str, zone_id: str):
        self.api_token = api_token
        self.zone_id = zone_id
        self.base_url = "https://api.cloudflare.com/client/v4"
        self.headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json"
        }

    def _request(self, method: str, endpoint: str, data: dict = None):
        url = f"{self.base_url}{endpoint}"
        body = json.dumps(data).encode("utf-8") if data else None
        req = urllib.request.Request(url, data=body, headers=self.headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8")
            try:
                err_json = json.loads(err_body)
                raise RuntimeError(f"Cloudflare API Error ({e.code}): {err_json.get('errors')}")
            except json.JSONDecodeError:
                raise RuntimeError(f"Cloudflare API Error ({e.code}): {err_body}")

    def list_dns_records(self, name: str = None, r_type: str = None):
        params = []
        if name:
            params.append(f"name={name}")
        if r_type:
            params.append(f"type={r_type}")
        query = ("?" + "&".join(params)) if params else ""
        res = self._request("GET", f"/zones/{self.zone_id}/dns_records{query}")
        return res.get("result", [])

    def create_dns_record(self, name: str, r_type: str, content: str, ttl: int = 60, proxied: bool = False, comment: str = "3040-tunnel-optimizer"):
        payload = {
            "type": r_type,
            "name": name,
            "content": content,
            "ttl": ttl,
            "proxied": proxied,
            "comment": comment
        }
        res = self._request("POST", f"/zones/{self.zone_id}/dns_records", payload)
        return res.get("result")

    def update_dns_record(self, record_id: str, name: str, r_type: str, content: str, ttl: int = 60, proxied: bool = False, comment: str = "3040-tunnel-optimizer"):
        payload = {
            "type": r_type,
            "name": name,
            "content": content,
            "ttl": ttl,
            "proxied": proxied,
            "comment": comment
        }
        res = self._request("PUT", f"/zones/{self.zone_id}/dns_records/{record_id}", payload)
        return res.get("result")

    def delete_dns_record(self, record_id: str):
        res = self._request("DELETE", f"/zones/{self.zone_id}/dns_records/{record_id}")
        return res.get("result")

    # --- Cloudflare for SaaS / Custom Hostnames (used against the kavin.fun
    # SaaS zone, NOT the 808608.xyz zone — instantiate a second CloudflareClient
    # with KAVIN_FUN_CF_API_TOKEN/KAVIN_FUN_ZONE_ID for these calls). ---

    def list_custom_hostnames(self, hostname: str = None):
        query = f"?hostname={hostname}" if hostname else ""
        res = self._request("GET", f"/zones/{self.zone_id}/custom_hostnames{query}")
        return res.get("result", [])

    def create_custom_hostname(self, hostname: str):
        payload = {"hostname": hostname, "ssl": {"method": "txt", "type": "dv", "bundle_method": "ubiquitous"}}
        res = self._request("POST", f"/zones/{self.zone_id}/custom_hostnames", payload)
        return res.get("result")

    def get_custom_hostname(self, custom_hostname_id: str):
        res = self._request("GET", f"/zones/{self.zone_id}/custom_hostnames/{custom_hostname_id}")
        return res.get("result")

    def delete_custom_hostname(self, custom_hostname_id: str):
        res = self._request("DELETE", f"/zones/{self.zone_id}/custom_hostnames/{custom_hostname_id}")
        return res.get("result")

def test_ip_https(domain: str, ip: str, timeout_sec: float = 3.0):
    """
    Directly test end-to-end HTTPS access via specific IP using curl --resolve.
    Returns (status_code: int, tcp_latency_sec: float, total_sec: float)
    """
    try:
        res = subprocess.run([
            "curl", "-k", "-s", "-o", "/dev/null",
            "-w", "%{http_code}|%{time_connect}|%{time_total}",
            "--resolve", f"{domain}:443:{ip}",
            "-A", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "-H", "Accept-Language: zh-CN,zh;q=0.9",
            f"https://{domain}/", "-m", str(int(timeout_sec))
        ], capture_output=True, text=True, timeout=timeout_sec + 1.0)
        if res.returncode == 0 and res.stdout.strip():
            parts = res.stdout.strip().split("|")
            if len(parts) == 3:
                return int(parts[0]), float(parts[1]), float(parts[2])
    except Exception:
        pass
    return 0, 9.99, 9.99

