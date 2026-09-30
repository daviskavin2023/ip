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

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>3040 穿透优选与高可用健康大屏</title>
    <style>
        :root {
            --bg-color: #0f172a;
            --card-bg: rgba(30, 41, 59, 0.7);
            --border-color: rgba(255, 255, 255, 0.1);
            --primary: #38bdf8;
            --success: #10b981;
            --warning: #f59e0b;
            --danger: #ef4444;
            --text-main: #f8fafc;
            --text-muted: #94a3b8;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background: linear-gradient(135deg, #0b0f19 0%, #0f172a 50%, #1e1b4b 100%);
            color: var(--text-main);
            min-height: 100vh;
            padding: 24px;
        }
        .container { max-width: 1100px; margin: 0 auto; }
        header {
            display: flex; justify-content: space-between; align-items: center;
            border-bottom: 1px solid var(--border-color);
            padding-bottom: 20px; margin-bottom: 24px;
        }
        h1 { font-size: 22px; font-weight: 700; display: flex; align-items: center; gap: 10px; }
        .tag-status {
            display: inline-block; padding: 4px 12px; border-radius: 9999px;
            font-size: 13px; font-weight: 600; text-transform: uppercase;
        }
        .status-healthy { background: rgba(16, 185, 129, 0.2); color: #34d399; border: 1px solid #10b981; }
        .status-fallback { background: rgba(239, 68, 68, 0.2); color: #f87171; border: 1px solid #ef4444; }
        .grid-summary {
            display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 16px; margin-bottom: 24px;
        }
        .summary-card {
            background: var(--card-bg); border: 1px solid var(--border-color);
            border-radius: 12px; padding: 18px; backdrop-filter: blur(12px);
        }
        .summary-card .label { font-size: 13px; color: var(--text-muted); margin-bottom: 8px; }
        .summary-card .val { font-size: 24px; font-weight: 700; color: var(--text-main); }
        .section-title { font-size: 17px; font-weight: 600; margin-bottom: 14px; color: #cbd5e1; display: flex; align-items: center; gap: 8px; }
        .node-grid {
            display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 16px; margin-bottom: 28px;
        }
        .node-card {
            background: var(--card-bg); border: 1px solid var(--border-color);
            border-radius: 12px; padding: 18px; backdrop-filter: blur(12px);
            position: relative; transition: all 0.2s ease;
        }
        .node-card:hover { transform: translateY(-2px); border-color: rgba(56, 189, 248, 0.4); }
        .node-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
        .node-ip { font-family: monospace; font-size: 18px; font-weight: bold; color: var(--primary); }
        .badge {
            font-size: 11px; font-weight: 600; padding: 3px 8px; border-radius: 6px;
        }
        .badge-ok { background: rgba(16, 185, 129, 0.25); color: #34d399; }
        .badge-fail { background: rgba(239, 68, 68, 0.25); color: #f87171; }
        .node-detail { font-size: 13px; color: var(--text-muted); margin-bottom: 6px; display: flex; justify-content: space-between; }
        .node-detail span.val { color: #e2e8f0; font-weight: 500; }
        .latency-val { font-size: 15px; font-weight: 700; color: #38bdf8; }
        .actions-panel {
            background: var(--card-bg); border: 1px solid var(--border-color);
            border-radius: 12px; padding: 20px; display: flex; gap: 14px; align-items: center;
        }
        button {
            padding: 10px 20px; border-radius: 8px; border: none; font-size: 14px;
            font-weight: 600; cursor: pointer; transition: all 0.2s;
        }
        .btn-primary { background: #0284c7; color: white; }
        .btn-primary:hover { background: #0369a1; }
        .btn-danger { background: #dc2626; color: white; }
        .btn-danger:hover { background: #b91c1c; }
        .footer { margin-top: 30px; text-align: center; font-size: 12px; color: var(--text-muted); }
        .domain-table { width: 100%; border-collapse: collapse; background: var(--card-bg); border: 1px solid var(--border-color); border-radius: 12px; overflow: hidden; margin-bottom: 28px; }
        .domain-table th { text-align: left; font-size: 12px; color: var(--text-muted); padding: 10px 14px; background: rgba(255,255,255,0.03); font-weight: 600; }
        .domain-table td { padding: 10px 14px; font-size: 13px; border-top: 1px solid var(--border-color); }
        .domain-table tr:hover td { background: rgba(255,255,255,0.02); }
        .dom-name { font-family: monospace; color: #e2e8f0; }
        .pill { display: inline-block; padding: 3px 10px; border-radius: 9999px; font-size: 11px; font-weight: 700; }
        .pill-converted { background: rgba(16,185,129,0.2); color: #34d399; }
        .pill-verifying { background: rgba(245,158,11,0.2); color: #fbbf24; }
        .pill-failed { background: rgba(239,68,68,0.2); color: #f87171; }
        .pill-pending { background: rgba(148,163,184,0.2); color: #94a3b8; }
        .pill-skipped { background: rgba(100,116,139,0.15); color: #64748b; }
        .pill-pinned { background: rgba(100, 116, 139, 0.25); color: #cbd5e1; border: 1px dashed rgba(148, 163, 184, 0.5); }
        .btn-sm { padding: 4px 10px; font-size: 11px; border-radius: 6px; font-weight: 600; cursor: pointer; border: 1px solid transparent; transition: all 0.15s; }
        .btn-danger-outline { background: rgba(239, 68, 68, 0.12); border-color: rgba(239, 68, 68, 0.35); color: #f87171; }
        .btn-danger-outline:hover { background: #ef4444; color: white; border-color: #ef4444; }
        .btn-ghost { background: rgba(56, 189, 248, 0.12); border-color: rgba(56, 189, 248, 0.35); color: #38bdf8; }
        .btn-ghost:hover { background: #0284c7; color: white; border-color: #0284c7; }
        .filter-tabs { display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 16px; }
        .filter-tab {
            background: var(--card-bg); border: 1px solid var(--border-color); color: #cbd5e1;
            border-radius: 9999px; padding: 8px 16px; font-size: 13px; font-weight: 600;
            cursor: pointer; display: flex; align-items: center; gap: 8px; transition: all 0.15s;
        }
        .filter-tab:hover { border-color: rgba(56, 189, 248, 0.5); }
        .filter-tab.active { background: #0284c7; color: white; border-color: #0284c7; }
        .filter-tab .count { background: rgba(255,255,255,0.15); border-radius: 9999px; padding: 1px 8px; font-size: 11px; }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div>
                <h1>⚡ 3040 穿透优选与高可用健康大屏</h1>
                <p style="font-size: 13px; color: var(--text-muted); margin-top: 4px;">业务域名: <strong id="dom-name">bgy.808608.xyz</strong> | 边缘容灾保护运行中</p>
            </div>
            <div>
                <span id="sys-status" class="tag-status status-healthy">初始化...</span>
            </div>
        </header>

        <div class="grid-summary">
            <div class="summary-card">
                <div class="label">当前活跃异构节点</div>
                <div class="val" id="active-count">5 / 5</div>
            </div>
            <div class="summary-card">
                <div class="label">平均实测延迟</div>
                <div class="val" id="avg-latency">-- ms</div>
            </div>
            <div class="summary-card">
                <div class="label">惩罚冷冻池 IP 数量</div>
                <div class="val" id="cooldown-count">0</div>
            </div>
            <div class="summary-card">
                <div class="label">最后巡检时间</div>
                <div class="val" style="font-size: 16px; line-height: 28px;" id="last-update">--</div>
            </div>
        </div>

        <div class="section-title">🌐 5 个跨网段异构节点状态 (Failure Domain Isolation)</div>
        <div class="node-grid" id="node-container">
            <!-- Dynamic nodes -->
        </div>

        <div class="section-title">📋 域名矩阵 (Domain Rollout Matrix)</div>
        <div class="grid-summary" id="domain-summary-cards"></div>
        <div class="filter-tabs" id="domain-filter-tabs"></div>
        <table class="domain-table">
            <thead>
                <tr><th>域名</th><th>优选状态</th><th>最后检查时间</th><th>备注</th><th style="width: 130px; text-align: center;">操作</th></tr>
            </thead>
            <tbody id="domain-table-body">
                <!-- Dynamic rows -->
            </tbody>
        </table>

        <div class="section-title">🛡️ 应急与运维指令</div>
        <div class="actions-panel">
            <button class="btn-primary" onclick="triggerAction('sync')">⚡ 立即同步 Cloudtunnel 域名</button>
            <button class="btn-primary" style="background: #0d9488;" onclick="triggerAction('rescan')">🔄 立即执行全网深度巡检与测速</button>
            <button class="btn-danger" onclick="triggerAction('rollback')">🚨 一键紧急回退至官方 Tunnel CNAME</button>
            <span id="action-msg" style="font-size: 13px; color: #38bdf8; margin-left: auto;"></span>
        </div>

        <div class="footer">
            3040-tunnel-optimizer · CT 160 (tunnel-10.10.10.160) · 严禁外部依赖字体 · 秒级自愈守护
        </div>
    </div>

    <script>
        async function fetchStatus() {
            try {
                const res = await fetch('/api/status');
                const data = await res.json();
                renderUI(data);
            } catch (e) {
                console.error("Fetch failed", e);
            }
            try {
                const res2 = await fetch('/api/hostnames');
                const data2 = await res2.json();
                renderDomainTable(data2);
            } catch (e) {
                console.error("Fetch hostnames failed", e);
            }
        }

        const STATUS_PILL = {
            CONVERTED: ['pill-converted', '✅ 已优选'],
            REGISTERING_CUSTOM_HOSTNAME: ['pill-verifying', '⏳ 注册SaaS证书中'],
            WAITING_CUSTOM_HOSTNAME_ACTIVE: ['pill-verifying', '⏳ 等待SaaS证书生效'],
            CUSTOM_HOSTNAME_FAILED: ['pill-failed', '❌ SaaS证书注册失败'],
            VERIFYING: ['pill-verifying', '⏳ 验证中'],
            FAILED_VERIFICATION: ['pill-failed', '❌ 验证失败(冷却重试中)'],
            NEEDS_MANUAL_REVIEW: ['pill-failed', '🛑 多次失败,需人工review'],
            AUTO_ROLLED_BACK: ['pill-failed', '❌ 巡检失败(已回滚)'],
            ROLLED_BACK: ['pill-pending', '↩️ 已回滚'],
            PENDING_MANUAL_CHECK: ['pill-pending', '⏸ 待人工确认'],
            PINNED_DIRECT: ['pill-pinned', '🚫 固定不优选'],
            ERROR: ['pill-failed', '⚠️ 异常'],
            SKIPPED_NO_DNS_RECORD: ['pill-skipped', '跳过(无DNS记录)'],
            SKIPPED_NON_STANDARD: ['pill-skipped', '跳过(非标准记录)'],
        };

        // Groups the many granular statuses into the handful of categories
        // the filter tabs/summary cards operate on — mirrors the cloudtunnel
        // panel's 全部/异常/正常 pattern so it's instantly recognizable.
        const STATUS_GROUP = {
            CONVERTED: 'ok',
            REGISTERING_CUSTOM_HOSTNAME: 'inprogress',
            WAITING_CUSTOM_HOSTNAME_ACTIVE: 'inprogress',
            VERIFYING: 'inprogress',
            CUSTOM_HOSTNAME_FAILED: 'failed',
            FAILED_VERIFICATION: 'failed',
            NEEDS_MANUAL_REVIEW: 'failed',
            AUTO_ROLLED_BACK: 'failed',
            ERROR: 'failed',
            ROLLED_BACK: 'pending',
            PENDING_MANUAL_CHECK: 'pending',
            PINNED_DIRECT: 'skipped',
            SKIPPED_NO_DNS_RECORD: 'skipped',
            SKIPPED_NON_STANDARD: 'skipped',
        };
        const GROUP_LABELS = {
            all: '全部', ok: '✅ 已优选', inprogress: '⏳ 处理中',
            failed: '❌ 失败/需关注', pending: '↩️ 已回滚/待处理', skipped: '跳过 / 不优选',
        };
        let currentFilter = 'all';
        let lastHostnameData = { managed: {} };

        function renderDomainTable(data) {
            lastHostnameData = data;
            const managed = data.managed || {};
            const names = Object.keys(managed).sort();

            // Summary cards
            const counts = { all: names.length, ok: 0, inprogress: 0, failed: 0, pending: 0, skipped: 0 };
            names.forEach(name => {
                const g = STATUS_GROUP[managed[name].status] || 'pending';
                counts[g] = (counts[g] || 0) + 1;
            });
            document.getElementById('domain-summary-cards').innerHTML = `
                <div class="summary-card"><div class="label">总数</div><div class="val">${counts.all}</div></div>
                <div class="summary-card"><div class="label">已优选</div><div class="val" style="color:#34d399">${counts.ok}</div></div>
                <div class="summary-card"><div class="label">处理中</div><div class="val" style="color:#fbbf24">${counts.inprogress}</div></div>
                <div class="summary-card"><div class="label">失败/需关注</div><div class="val" style="color:#f87171">${counts.failed}</div></div>
            `;

            // Filter tabs
            let tabsHtml = '';
            ['all', 'ok', 'inprogress', 'failed', 'pending', 'skipped'].forEach(g => {
                if (g !== 'all' && counts[g] === 0) return;
                tabsHtml += `<div class="filter-tab ${currentFilter === g ? 'active' : ''}" onclick="setDomainFilter('${g}')">
                    ${GROUP_LABELS[g]} <span class="count">${counts[g]}</span>
                </div>`;
            });
            document.getElementById('domain-filter-tabs').innerHTML = tabsHtml;

            // Table rows (filtered)
            let html = '';
            names.forEach(name => {
                const info = managed[name];
                const status = info.status || 'UNKNOWN';
                const group = STATUS_GROUP[status] || 'pending';
                if (currentFilter !== 'all' && group !== currentFilter) return;
                const [cls, label] = STATUS_PILL[status] || ['pill-pending', status];
                const lastChecked = info.last_checked ? new Date(info.last_checked * 1000).toLocaleString('zh-CN') : '--';
                const note = group === 'ok' ? '' : (info.failure_reason || info.error || info.note || '');

                let actionBtn = '';
                if (status === 'PINNED_DIRECT') {
                    actionBtn = `<button class="btn-sm btn-ghost" onclick="unpinDirect('${name}')">⚡ 恢复优选</button>`;
                } else {
                    actionBtn = `<button class="btn-sm btn-danger-outline" onclick="pinDirect('${name}')">🚫 固定不优选</button>`;
                }

                html += `<tr>
                    <td class="dom-name">${name}</td>
                    <td><span class="pill ${cls}">${label}</span></td>
                    <td>${lastChecked}</td>
                    <td style="color:#94a3b8;font-size:12px;">${note}</td>
                    <td style="text-align: center;">${actionBtn}</td>
                </tr>`;
            });
            document.getElementById('domain-table-body').innerHTML = html || '<tr><td colspan="5" style="color:#94a3b8;">该分类下暂无域名</td></tr>';
        }

        function setDomainFilter(g) {
            currentFilter = g;
            renderDomainTable(lastHostnameData);
        }

        function renderUI(data) {
            document.getElementById('dom-name').innerText = data.domain || 'bgy.808608.xyz';
            const statusBadge = document.getElementById('sys-status');
            if (data.status === 'active') {
                statusBadge.className = 'tag-status status-healthy';
                statusBadge.innerText = '● 优选加速中 (5段冗余)';
            } else {
                statusBadge.className = 'tag-status status-fallback';
                statusBadge.innerText = '⚠ ' + data.status;
            }

            const nodes = data.active_nodes || [];
            document.getElementById('active-count').innerText = nodes.length + ' 个异构网段';
            document.getElementById('cooldown-count').innerText = data.cooldown_count || 0;
            document.getElementById('last-update').innerText = data.updated_str || '--';

            let totalLat = 0;
            let validLatCount = 0;
            let html = '';

            nodes.forEach((n, idx) => {
                const isHealthy = n.status === 'HEALTHY';
                const hasLatency = n.latency_ms !== null && n.latency_ms !== undefined && n.latency_ms > 0;
                if (isHealthy && hasLatency) {
                    totalLat += n.latency_ms;
                    validLatCount++;
                }
                // null latency_ms means the probe itself errored/timed out (no connection at
                // all) — showing a fake ms number there would look like a real measurement.
                const latencyDisplay = hasLatency ? `${n.latency_ms} ms` : '超时/无法连接';
                html += `
                <div class="node-card">
                    <div class="node-header">
                        <div class="node-ip">${n.ip}</div>
                        <span class="badge ${isHealthy ? 'badge-ok' : 'badge-fail'}">${isHealthy ? '200 OK' : n.status}</span>
                    </div>
                    <div class="node-detail">
                        <span>/16 独立故障域:</span>
                        <span class="val" style="color: #a78bfa; font-family: monospace;">${n.subnet}</span>
                    </div>
                    <div class="node-detail">
                        <span>TCP 握手延迟 (RTT):</span>
                        <span class="val latency-val">${latencyDisplay}</span>
                    </div>
                    <div class="node-detail">
                        <span>连续失败次数:</span>
                        <span class="val">${n.consecutive_fails || 0} / 2</span>
                    </div>
                </div>
                `;
            });

            document.getElementById('node-container').innerHTML = html;
            if (validLatCount > 0) {
                document.getElementById('avg-latency').innerText = Math.round(totalLat / validLatCount) + ' ms';
            }
        }

        async function triggerAction(act) {
            const msgEl = document.getElementById('action-msg');
            msgEl.innerText = '正在执行: ' + act + '...';
            try {
                const res = await fetch('/api/action', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({action: act})
                });
                const r = await res.json();
                msgEl.innerText = r.message || '执行成功';
                setTimeout(fetchStatus, 1500);
            } catch(e) {
                msgEl.innerText = '执行失败: ' + e;
            }
        }

        async function pinDirect(hostname) {
            if (!confirm(`确定要将域名 [${hostname}] 手动固定为【不优选】吗？\n系统将自动切回官方原生 Tunnel 直连，不再对其进行加速和重试。`)) return;
            const msgEl = document.getElementById('action-msg');
            msgEl.innerText = `正在将 [${hostname}] 固定为不优选...`;
            try {
                const res = await fetch('/api/action', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({action: 'pin_direct', hostname: hostname})
                });
                const r = await res.json();
                msgEl.innerText = r.message || '已固定为不优选';
                setTimeout(fetchStatus, 800);
            } catch(e) {
                msgEl.innerText = '操作失败: ' + e;
            }
        }

        async function unpinDirect(hostname) {
            if (!confirm(`确定要解除 [${hostname}] 的固定不优选状态，恢复自动优选吗？`)) return;
            const msgEl = document.getElementById('action-msg');
            msgEl.innerText = `正在恢复 [${hostname}] 自动优选...`;
            try {
                const res = await fetch('/api/action', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({action: 'unpin_direct', hostname: hostname})
                });
                const r = await res.json();
                msgEl.innerText = r.message || '已恢复自动优选';
                setTimeout(fetchStatus, 800);
            } catch(e) {
                msgEl.innerText = '操作失败: ' + e;
            }
        }

        fetchStatus();
        setInterval(fetchStatus, 4000);
    </script>
</body>
</html>
"""

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_TEMPLATE.encode("utf-8"))
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

