import http.server
import socketserver
import json
import os
import subprocess
import time

PORT = int(os.getenv("WEB_PORT", "3040"))
STATUS_FILE = os.getenv("CF_STATUS_FILE", "/opt/3040-tunnel/live_status.json")
STATE_FILE = os.getenv("CF_STATE_FILE", "/opt/3040-tunnel/state.json")
HOSTNAMES_FILE = os.getenv("MANAGED_HOSTNAMES_FILE", "/opt/3040-tunnel/managed_hostnames.json")

_D = os.path.dirname(os.path.abspath(__file__))
UI_FILES = [os.path.join(_D, "ui", "index.html"), os.path.join(_D, "app", "optimizer", "index.html"), os.path.join(_D, "..", "app", "optimizer", "index.html")]  # 部署=ui/index.html；仓库=app/optimizer/index.html


def load_ui():
    try:
        with open(next(p for p in UI_FILES if os.path.exists(p)), "rb") as f:
            return f.read()
    except Exception as e:
        return ("<!doctype html><meta charset=\"utf-8\"><title>优选 IP</title><p>页面文件读取失败：%s</p>" % e).encode("utf-8")

UPTIME_FILE = os.getenv("TUNNEL_UPTIME_FILE", "/var/lib/tunnel-panel/uptime.json")


def read_uptime():
    """只读：tunnel-panel 每轮巡检累加的每日 [up,total]（北京时间切日）；文件不存在=还没有样本，如实返回空。"""
    try:
        with open(UPTIME_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    days = [time.strftime("%Y-%m-%d", time.gmtime(time.time() + 8 * 3600 - i * 86400)) for i in range(6, -1, -1)]
    return {"success": True, "days": days, "data": {h: {k: v for k, v in d.items() if k in days} for h, d in data.items()}}


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self.send_response(200)
            body = load_ui()
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/status":
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            if os.path.exists(STATUS_FILE):
                with open(STATUS_FILE, "rb") as f:
                    self.wfile.write(f.read())
            elif os.path.exists(STATE_FILE):
                with open(STATE_FILE, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self.wfile.write(b"{}")
        elif self.path == "/api/uptime":
            body = json.dumps(read_uptime(), ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/hostnames":
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            if os.path.exists(HOSTNAMES_FILE):
                with open(HOSTNAMES_FILE, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self.wfile.write(b'{"managed": {}}')
        elif self.path == "/api/sync":
            try:
                res = subprocess.run([
                    "python3", "/opt/3040-tunnel/rollout_manager.py", "--sync"
                ], capture_output=True, text=True, timeout=25)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(res.stdout.encode("utf-8") if res.stdout.strip() else b'{"success":true}')
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path in ("/api/action", "/api/sync"):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                act = "sync" if self.path == "/api/sync" else "unknown"
                target_host = ""
                if body:
                    try:
                        data = json.loads(body.decode("utf-8"))
                        act = data.get("action", act)
                        target_host = data.get("hostname", "")
                    except Exception:
                        pass
                msg = "Unknown action"
                if act == "rollback":
                    subprocess.run(["python3", "/opt/3040-tunnel/rollback.py"])
                    msg = "已切回原生 Tunnel CNAME"
                elif act == "rescan":
                    subprocess.run(["python3", "/opt/3040-tunnel/switch.py"])
                    msg = "已重测并同步 5 个异构优选节点"
                elif act == "sync":
                    res = subprocess.run([
                        "python3", "/opt/3040-tunnel/rollout_manager.py", "--sync"
                    ], capture_output=True, text=True, timeout=25)
                    out = res.stdout.strip()
                    try:
                        r_data = json.loads(out)
                        p_cnt = r_data.get("panel_count", 0)
                        msg = f"已完成实时双向同步！面板有效域名: {p_cnt} 个，已清理并对齐状态"
                    except Exception:
                        msg = "已完成与 Cloudtunnel 域名状态同步"
                elif act == "pin_direct":
                    if target_host:
                        res = subprocess.run([
                            "python3", "/opt/3040-tunnel/rollout_manager.py", "--pin", target_host
                        ], capture_output=True, text=True, timeout=25)
                        msg = f"域名 {target_host} 已手动固定为不优选 (直连原生Tunnel)"
                    else:
                        msg = "未指定域名"
                elif act == "unpin_direct":
                    if target_host:
                        res = subprocess.run([
                            "python3", "/opt/3040-tunnel/rollout_manager.py", "--unpin", target_host
                        ], capture_output=True, text=True, timeout=10)
                        msg = f"域名 {target_host} 已解除固定，重新进入自动优选队列"
                    else:
                        msg = "未指定域名"
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"success": True, "message": msg}).encode("utf-8"))
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode("utf-8"))

def run():
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("0.0.0.0", PORT), Handler) as httpd:
        print(f"3040 Web UI listening on http://0.0.0.0:{PORT}")
        httpd.serve_forever()

def start_server_thread():
    import threading
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t

if __name__ == "__main__":
    run()

