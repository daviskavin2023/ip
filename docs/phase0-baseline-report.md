# Phase 0 现场勘测与事实基线报告

> **执行时间**：2026-09-27 00:15  
> **执行性质**：纯只读探测（Read-Only Probe），零配置修改，零业务影响。

---

## 1. 勘测成果总表

| 核验项目 | 预设推论 | 实测真实事实 | 影响与后续行动 |
| :--- | :--- | :--- | :--- |
| **CT104 出网路由** | 担心走了旁路由代理，测速失真 | **纯正真实直连宽带！**<br>默认网关为 `10.10.10.2`（主路由），出口 IP 为 `120.239.168.196`（广东中山移动）。 | **测速环境极度理想**，可直接部署 `CloudflareST`，测出最真实的家庭宽带优选 IP。 |
| **容器宿主机与 ID** | 原以为是 10.10.10.1 CT104 | **全部运行在 PVE10 (`10.10.10.10`)！**<br>Tunnel 容器为 **VMID 160 (`tunnel-10.10.10.160`)**；<br>Lucky 为 **VMID 105 (`lucky-05` / `10.10.10.5`)**；<br>AdGuardHome 为 **VMID 114 (`10.10.10.4`)**。 | 彻底校准资产清单，以后统一通过 PVE10 (`10.10.10.10`) 管理。 |
| **Tunnel 客户端配置** | 预备在 Phase 2 改为 http2 | **当前已经是 `--protocol http2 --edge-ip-version 4` 运行！** | 用户前期已完成该项加固，Phase 2 协议部分已达成，风险大幅降低。 |
| **`cloudtunnel` 面板机制** | 担心是未知复杂架构 | **单文件 Python 微服务 (`/opt/tunnel-panel/tunnel-panel.py`)**。<br>直接调用 Cloudflare 官方 API 增删 Ingress；自带备份机制 (`/etc/tunnel-panel/backups`)。 | 代码透明、逻辑简洁，具备极佳的免侵入监听或轻量钩子对接条件。 |
| **Cloudflare SaaS 与 API 权限** | 以为现有 Token 可直接调 SaaS | **当前 API Token 权限受限 (403 Authentication error)**。<br>当前 Token 仅有 Tunnel 和部分 DNS 权限，缺少 `Custom Hostnames (SSL and Certificates: Edit)` 权限。 | **关键阻断项**：需在 Cloudflare 网页端重新生成一个包含 Custom Hostnames 权限的 Token，并确认账户已绑定支付方式。 |
| **域名权威 DNS 与记录现状** | 曾讨论 DNSPod / 阿里云免费 TTL 限制 | **`808608.xyz` 权威 DNS 直接托管在 Cloudflare 官方！**<br>`bgy.808608.xyz` 当前为 CNAME 到 `0333ec4f-06b3-45ee-8607-b10da69a5195.cfargotunnel.com` (proxied=true)。 | 业务域名在 Cloudflare 官方，SaaS 回退源接入链路最平滑。 |
| **Lucky (10.10.10.5) 角色** | 需确认是否已在反代 bgy | **Lucky 正在监听 `*:443` 与 `*:16601`！**<br>内网解析中 `bgy.808608.xyz` 已经成功指向 `10.10.10.5`，Lucky 就是现成的内网反代着陆点！ | 完美验证了架构推论，内网流量完全由 Lucky 接管。 |

---

## 2. 核心技术定论与决策

1. **测速引擎部署**：直接部署在 **PVE10 CT160 (`10.10.10.160`)**，直连广东移动宽带出口，毫无代理污染。
2. **SaaS 开通的唯一阻断点**：
   - 现有的 `CF_API_TOKEN`（在 `/etc/tunnel-panel/config.env`）缺少 **`Zone - Custom Hostnames: Edit`** 和 **`Zone - SSL and Certificates: Edit`** 权限，导致 API 调用报 403。
   - 需要在 Cloudflare 仪表盘生成一个带该权限的 Token，并确认账户已经绑定支付方式开通了 Cloudflare for SaaS（前 100 个免费）。
