# 3040-穿透优选-tunnel 版本与变更日志

## [界面重做] 2026-10-06 Tunnel 总览 + 优选 IP 大屏套用统一设计系统

- **只改界面，接口与逻辑零改动**：Tunnel 总览（`app/tunnel/index.html`，由 `src/tunnel-panel.py` 读取）与优选 IP 大屏（`app/optimizer/index.html`，由 `src/web_ui.py` 读取）改为独立 HTML 文件，按 `DESIGN-SYSTEM.md` 重做（薄荷绿、浅/深两套、字号 5 档、固定尺寸卡片、胶囊筛选、无下拉、骨架占位+淡入、危险操作二次确认）。
- 新增 `checked_ts`（数字时间戳，附加字段）：避免 CT160 为 UTC 时页面显示的"上次检查"与本地时间差 8 小时。
- 部署：CT160 手工 `pct push`（无 git/webhook）。`/opt/tunnel-panel/ui/index.html`、`/opt/3040-tunnel/ui/index.html`；改前备份 `*.bak.ui_<时间>`；重启 `tunnel-panel`、`tunnel-optimizer`。
- **Tunnel 卡片加归属标签**：项目只放编号（3011…）；PVE 内的机器放 `pve节点+编号`（如 pve10 109、pve1 116）；其他放主机胶囊（.6、飞牛 .9、.2）。规则在页面内 `tagOf()`，纯前端，无接口改动；新增项目端口落在 3010–3049 且在 .14/.18 上自动识别，其余机器要在 `HOST_TAG` 补一行。
- **peerbar 胶囊条**：两个页面最顶端加六页统一的相关控制台胶囊条（3024 项目中台 · 3040 Tunnel · 3040 优选 IP · 3031 抓取调度 · 3043 手机转发 · 3041 修复审批），桌面端与其他五页坐标逐像素一致。
- **7 天可用性**：`tunnel-panel.py` 巡检时每轮累加每日 `[up,total]`（北京时间）到 `/var/lib/tunnel-panel/uptime.json`；两个进程各加只读 `GET /api/uptime`；两个页面的域名卡片各加一行 7 色块 + 百分比，无样本显示灰块和 `--`。见 `docs/PRD-2026-10-06-7天可用性.md`。无法回填历史（旧日志只记失败），从 2026-10-06 起逐天积累。
- 胶囊条手机端只显示编号（3040 两个带两字），六页位置不动。
- 验证：375/390/412/1280 × 浅深零横向滚动；Tunnel 46 条、优选 IP 46 条 + 5 节点与改前一致。外链胶囊条为第二步。

## [并发保护与状态原子化] 2026-09-27 状态持久化并发安全加固与 CLI 支持

- **并发覆写防护 (Concurrency Safety)**：
  - `save_state` 在 `rollout_manager.py` 和 `hostname_monitor.py` 均改造为**原子替换写入**（先写入 `.tmp` 临时文件，再通过 `os.replace` 原子覆写），杜绝其他进程或 Web UI 在读取时读到半写状态。
  - `hostname_monitor.py` 与 `rollout_manager.py` 在持久化写入前均先从磁盘重新合并最新状态，确保用户在 Web 端点击【🚫 固定不优选】或通过 API 手动固定/解除时，不会被长时间巡检的后台进程覆写丢失。
- **CLI 命令行参数原生支持**：
  - `rollout_manager.py` 原生支持 `--pin <hostname>`、`--unpin <hostname>` 与 `--sync` 参数。
  - `web_ui.py` 直接调用标准 CLI 替代动态字符串注入执行，执行更加稳固。
- **现网运行状态**：
  - 域名总数：41
  - ✅ **已优选**：36
  - ⏳ **处理中**：0
  - ❌ **失败/需关注**：**0**（完全清零）
  - 🚫 **跳过 / 不优选 (手动固定)**：5 个（`github-webhook.808608.xyz`、`ik.808608.xyz`、`lk.808608.xyz`、`pve.017384.xyz`、`qunhui.017384.xyz`）

## [功能扩展] 2026-09-27 支持手动固定项目为不优选 (直连原生 Tunnel)

- **场景与需求**：对于某些确定无法/无需进行 Anycast 优选加速的项目（如 `github-webhook.808608.xyz` 仅接收外部 webhook POST、`ik.808608.xyz` iKuai 路由器管理后台、`lk.808608.xyz` 等），用户希望能够手动一键固定为“不优选”，直接走 Cloudflare 官方原生 Tunnel 接入，不再占用“失败/需关注”告警列表。
- **功能落地**：
  1. **一键固定不优选 (`pin_hostname_direct`)**：
     - 在 Web 控制台表格每行增加【操作】列。
     - 点击【🚫 固定不优选】：
       - 自动将该域名的 Cloudflare DNS CNAME 原地切回官方原生 Tunnel (`<tunnel_id>.cfargotunnel.com`) 并开启 `proxied=True` 官方橙云 CDN 代理。
       - 自动清理在 `kavin.fun` 上的 SaaS Custom Hostname 与验证 TXT 记录。
       - 将状态更新为 `PINNED_DIRECT`（标签：`🚫 固定不优选`），并归类至【跳过 / 不优选】分组。
       - 自动移出“失败/需关注”卡片统计（数量立减）。
  2. **守护进程防干扰保证**：
     - `rollout_manager.py` 将 `PINNED_DIRECT` 纳入白名单忽略集，不再对其轮询转换或探测。
     - `hostname_monitor.py` 仅巡检 `CONVERTED` 域名，完全不干扰固定直连域名。
  3. **随时恢复优选 (`unpin_hostname`)**：
     - 固定不优选的域名在操作列展示【⚡ 恢复优选】按钮，点击即可随时重新激活进入自动优选队列。

## [实时双向同步] 2026-09-27 与 Cloudtunnel (cloudtunnel.808608.xyz) 实时全自动同步闭环

- **问题根因**：
  1. `rollout_manager.py` 此前仅做单向增量轮询，**缺少“下线/删除同步（De-registration / Prune）”机制**。当用户在 `cloudtunnel.808608.xyz` 或 Cloudflare 官方控制台删除了某些 Hostname（如 `mihome.808608.xyz`、`pve.808608.xyz`、`tr.808608.xyz`）后，3040 这边永久停留在 `managed_hostnames.json` 中，持续显示在 Web 大屏且触发失败告警。
  2. 此前仅读取静态缓存接口 `/api/cache`，若用户在 Cloudflare 官方修改，面板自身的缓存若未强制刷新，3040 会继续使用陈旧数据。
  3. `cloudtunnel` 面板此前未提供删除 Ingress 按钮（提示"不提供删除"），且修改后未通知 3040。
- **全套自动化重构落地**：
  1. **全自动下线解绑与资源清理 (`reconcile_deleted_hostnames`)**：
     - `rollout_manager.py` 每轮循环自动对比当前 Tunnel 面板与本地托管列表，一旦发现已被删除的域名：
       - 自动调用 Cloudflare for SaaS API，删除在 `kavin.fun` zone 下注册的 Custom Hostname（释放配额、杜绝悬挂配置）。
       - 自动清理主域 `808608.xyz` 上的 `_cf-custom-hostname` 和 `_acme-challenge` TXT 验证记录。
       - 自动清理指向 `speed.808608.xyz` 的孤儿 CNAME。
       - 从 `managed_hostnames.json` 彻底注销，Web 大屏秒级同步剔除。
     - **生产实测**：已成功将已删除的 `pve`、`tr`（含SaaS Custom Hostname与TXT验证记录）以及 `mihome` 干净清理，总数从 44 精确同步至 41（排除 cloudtunnel 自身后的 41 个域名全部匹配）。
  2. **双通道数据源保证 100% 实时性**：
     - `fetch_panel_hostnames()` 优先请求 `/api/summary`（直连 Cloudflare 权威 API 实时列出，耗时 <2s，杜绝陈旧缓存），并在异常时自动降级读取 `/api/cache`。
  3. **实时事件驱动联动 (Webhook & Event Notify)**：
     - `3040` 的 Web 服务与守护进程开放 `/api/sync` 接口与 POST 动作。
     - `cloudtunnel` 面板 (`tunnel-panel.py`) 新增保存/删除后的即时回调 `notify_3040_sync()`，毫秒级通知 3040 进行增删对齐。
     - `tunnel-panel.py` 补充 60s 后台轻量自动刷新线程，并在 Web 弹窗中正式新增 **“删除此 Hostname”** 按钮与后台安全删除逻辑（自动配置备份、Ingress 移除、DNS CNAME 清理）。
  4. **大屏交互升级**：
     - 3040 Web 控制台新增【⚡ 立即同步 Cloudtunnel 域名】运维按钮，点击即刻执行全网双向对齐，大屏每 4 秒自动拉取最新状态无缝刷新。
     - 优化验证探针跟随重定向（`-L`）并支持 302/404 等常见合法状态，避免将正常登录跳转（如 iKuai、Joplin）误判为失效。

## [根因确认] 2026-09-27 真正原因找到：Custom Hostname注册顺序反了（此前几版结论全部作废）

**最终结论（已用`fn.808608.xyz`连续6分钟200验证，不是猜测）**：`bgy.808608.xyz`能用、其他域名一直失败，根因是——`bgy`当初被注册成了`kavin.fun`这个zone上的 **Cloudflare for SaaS Custom Hostname**（证据：`_cf-custom-hostname.bgy.808608.xyz` TXT记录、`kavin.fun` zone下`custom_hostnames` API能查到`bgy.808608.xyz`且`status=active`），其他域名都没有做这一步注册。

Cloudflare的硬性要求（API返回的`verification_errors`原话）：**"custom hostname does not CNAME to this zone"** —— Custom Hostname只有在域名的CNAME已经指向SaaS zone（即已经切到`speed.808608.xyz`）之后才能验证通过。**顺序必须是：先切CNAME，再注册/等待Custom Hostname生效**，反过来就会一直卡在`pending`+这条报错。之前"转换后能用60秒然后必然403"的现象，就是这60秒内edge还用着旧的橙云代理缓存，缓存过期后没有Custom Hostname兜底，边缘不知道转给谁。

**此前几轮排查方向全部错误，明确撤销**：
- ~~zone证书按域名区分覆盖~~（已证伪：通配符证书覆盖整个zone）
- ~~CT160资源不足(0.5 cpulimit)导致假阳性~~（调了CPU/内存没解决问题，这条不是主因，虽然资源调整本身没坏处保留）
- ~~curl请求模式触发Cloudflare机器人检测~~（加了浏览器UA也没解决，证伪）
- ~~某个具体优选IP有问题~~（换了全新IP，同样的60秒后403，证伪，问题跟IP完全无关）

**新增**：用户在Cloudflare后台生成了一个只有`kavin.fun`这一个zone权限（`SSL and Certificates:Edit` + `Zone:Read`）的独立Token（存入`.env`的`KAVIN_FUN_CF_API_TOKEN`/`KAVIN_FUN_ZONE_ID`），`cf_client.py`加了`create_custom_hostname`/`get_custom_hostname`/`list_custom_hostnames`方法，`rollout_manager.py`改成正确顺序：注册Custom Hostname+建TXT验证记录 → 切CNAME → 轮询等Custom Hostname变`active` → 验证 → 标记已优选。

## [事故+修复] 2026-09-27 CT160资源不足导致批量接入引发生产故障
- **事故**：Phase 5批量转换31个域名后，20个（含`ylw`主财务、`ylwht`合同管理、`ylwsj`收入看板、`ylwcb`成本看板、`dk`打卡等核心业务）出现HTTP 403，紧急回滚恢复。
- **⚠️ 更正（同一天晚些时候）**：最初把根因归为"优选IP没配这个域名的zone证书"——**这个结论是错的，已被用户质疑后彻查推翻**。实测证实 `808608.xyz` 是**zone级别的通配符证书**（`CN=808608.xyz`，覆盖全部`*.808608.xyz`子域名），同一个边缘IP不可能"认这个域名、不认那个域名"，机制上根本说不通。真正原因是下面这条CT160资源不足——`0.5 cpulimit + 512MB内存`导致测速/验证请求超时，看起来像"这个IP不行"，其实是机器扛不住。调整CT160资源到 `cpulimit:1 + 1024MB` 后，把之前"失败"的10个域名用同一个IP（`104.18.14.94`）逐个重测，**全部200，无一失败**，坐实了资源不足才是真凶。
- `sync_hostnames.py`转换前验证只抽测了5个优选IP里的2个（`samples=1`）这个方法论漏洞依然真实存在，已修复为要求全部当前优选IP都通过才转换——这条修复保留，只是不再是本次故障的主因。
- **新增 `hostname_monitor.py` + `hostname-monitor.service`**：常驻服务，每5分钟对全部已转换域名用当前全部优选IP做健康检查，连续失败2次自动回滚到原始CNAME（复用daemon.py的巡检+熔断模式，但监控对象从"bgy的5个优选IP"扩展成"每一个已转换域名"）。
- **意外发现的更根本问题**：CT160（`10.10.10.160`，PVE10宿主机）被限制在 `cpulimit: 0.5`（只有半个CPU核心）+ `memory: 512MB`（实测常驻可用内存只剩29MB）。这台机器同时跑着 `cloudflared`(管53个隧道)、`tunnel-panel.py`、`daemon.py`、新加的`hostname-monitor.py`，长期`load average`维持在2.8~3.9（1核机器上严重过载的信号，LXC cgroup CPU配额限流的典型表现——`top`瞬时CPU看着空闲但进程在排队等配额）。用户确认后已将CT160提升到 `cpulimit: 1`、`memory: 1024`（宿主机6核31G，余量充足，热生效未重启），预期能显著缓解甚至消除这类"看起来是某个IP坏了，实际是容器资源不够导致探测超时"的假阳性。

## [设计定稿] 2026-09-27 Phase 5 全域名自动接入方案 + tunnel-panel小修复
- 敲定 Phase 5 落地方案：共享加速域名（`edge.808608.xyz`，复用现有 speed.808608.xyz 机制），新增 `sync_hostnames.py` 定期拉 cloudtunnel 面板 `/api/cache`，自动把符合条件的 Hostname CNAME 改指向共享加速域名，含"面板自己同步把记录改回去"的自愈检测。用户确认：排除名单只留 `cloudtunnel.808608.xyz` 自己；先完善PRD再写代码；不需要按域名单独回滚，全局回滚够用。详见 PRD.md 第6章 Phase 5。
- 顺手修复 `tunnel-panel.py`（生产系统，改前已备份到 `/etc/tunnel-panel/backups/`）：新增Hostname表单的"内网Service"输入框加了 `value="http://"` 默认值，减少每次手动输入；已重启验证服务正常（真实API调用返回47个hostname数据），确认不影响编辑已有记录的正常取值逻辑。

## [修复] 2026-09-27 源码问题修复（P1/P2已闭环，本地+CT160同步）
- 新增 `src/build_pool.py`：把 CloudflareST 扫描结果真正接回候选池，逐IP真实HTTPS验证+延迟硬阈值(默认100ms)+`/16`多样性强制，不合格时拒绝覆盖旧候选池并报错退出（不静默降级）。`daemon.py`/`switch.py` 改为优先加载 `candidate_pool.json`，文件不存在才回退内置种子池并打WARNING。
- 在CT160上真实跑了一次 `build_pool.py`：当次扫描数据里13个候选/16网段只有1个通过100ms阈值验证，脚本正确拒绝覆盖（保留原有更好的种子池），daemon重启后运行正常——验证了"禁止静默失败"设计确实生效。顺带发现同一IP不同时间点测速结果有明显抖动（83ms→13.7ms→>100ms），后续可以考虑加多次复测取中位数，本次未做（避免扩大范围）。
- 删除 `src/selector.py`（本地+CT160同步）：无任何引用，是"170ms+的IP被选中"这个历史问题的代码级根因，已被 `build_pool.py`+`switch.py` 取代。
- `src/rollback.py`：改成"先删多余记录留1条survivor、最后UPDATE原地改CNAME"，替换掉"先全删再建"，消除了"删除成功但创建失败→域名零解析记录"的最坏情况窗口。
- `src/daemon.py`：断路器触发逻辑改为直接调用共享的 `rollback()`，删掉内联重复代码和死代码（未使用的 `cf_client`/`CloudflareClient` 引用）。
- 全部改动已同步部署到CT160并重启 `tunnel-optimizer.service` 验证正常运行（`systemctl is-active` = active，日志无异常）。

## [修复] 2026-09-27 密钥轮换与硬编码清理（P0已闭环）
- 用户在阿里云RAM/Cloudflare后台分别生成了新的AK/SK和API Token，旧凭据已作废。
- `src/daemon.py`、`src/switch.py`、`src/rollback.py`三处硬编码密钥默认值全部移除，改为`os.environ["X"]`强制读环境变量（读不到直接抛`KeyError`报错退出，不再有兜底）。
- 新增`.env`（本地+CT160生产环境各一份，600权限，`.gitignore`已排除）+ `.env.example`模板。
- CT160的`tunnel-optimizer.service`补了`EnvironmentFile=/opt/3040-tunnel/.env`，重启服务验证正常运行。
- 新Aliyun Key、新CF Token均已用真实API调用（`DescribeDomainRecords`/`list_dns_records`）验证生效，不是仅凭"没报错"判断。
- 本地仓库尚未`git init`，不存在历史泄露风险；CT160是通过PVE10宿主机`pct exec`代理修复的（直连CT160的SSH当前被拒绝，如需直连需要用户后续补充访问方式）。

## [审计] 2026-09-27 第三方只读复核（未改源码，仅文档）
- 修正 PRD.md 内基础设施编号错误：实测 `pct list` 确认 Tunnel 容器是 **VMID 160**（不是 CT104），Lucky 是 **VMID 105**（不是 CT106），PVE 宿主是 **PVE10 `10.10.10.10`**（不是 PVE1 `10.10.10.1`）。旧编号来自更早的 3019 项目文档，基础设施后来迁移过。
- 实测确认当前生产真实链路：`bgy.808608.xyz`（灰云CNAME）→ `speed.808608.xyz`（NS委托阿里云，分线路）→ 境外线路兜底 `origin.kavin.fun` / 默认线路直连优选IP池。与 PRD 第 3.4/6 章描述的 "Cloudflare for SaaS Custom Hostname" 方案不是同一条路径，且外部浏览器实测 `https://bgy.808608.xyz` 生产可用（页面正常加载百果园现金对账数据）。详见 PRD.md 3.5 节。
- 发现 `src/daemon.py`、`src/switch.py`、`src/rollback.py` 三个文件硬编码了真实阿里云 AK/SK 与 Cloudflare API Token 明文默认值，**未做任何代码改动**，详见 PRD.md 第 8 章，待用户本人处理（轮换密钥+改造代码）。
- 发现 `src/selector.py` 按 `/24` 分组且无延迟上限判断，是用户反馈"170ms+ IP被选中"问题的代码级根因；已被 `switch.py` 的 `/16` 逻辑取代但未删除。

## [1.1.0] - 2026-09-27
### 严格 `/16` 独立故障域坚守与 SaaS 永久权威校验
- **坚守 `/16` 独立故障域底线**：
  - 彻底杜绝节点落入同段风险，强制 5 个节点来自 5 个绝对不重叠的 `/16` 独立边缘网段：
    1. `172.64.0.0/16` (`172.64.229.1` -> **13.2 ms**)
    2. `104.17.0.0/16` (`104.17.31.34` -> **14.1 ms**)
    3. `198.41.0.0/16` (`198.41.208.220` -> **16.6 ms**)
    4. `172.66.0.0/16` (`172.66.204.199` -> **72.4 ms**)
    5. `104.20.0.0/16` (`104.20.34.201` -> **170.4 ms**)
  - 其中前 3 节点直通 Cloudflare 香港特快 Anycast 边缘，TCP 握手仅 **13~16 ms**！平均延迟压至 **58.4 ms**。
- **候选池与动态容灾重构**：
  - 构建覆盖 11 个独立 `/16` 故障域的候选池（`104.17/16`, `172.64/16`, `198.41/16`, `172.66/16`, `104.24/16`, `104.25/16`, `104.16/16`, `141.101/16`, `104.27/16`, `104.19/16`, `104.20/16`）。
  - 当节点故障时，优先在同段切换备用 IP；若整段劣化，则在全新未使用的 `/16` 故障域中动态晋升，运行时强校验断言 `len(set(subnets)) == 5`。
- **SaaS 永久所有权校验解决 `status: moved` 与 1034 错误**：
  - 在主域 `808608.xyz` 注入 `_cf-custom-hostname.bgy.808608.xyz` TXT 所有权证书记录。
  - 彻底解耦 CNAME 依赖，避免 Cloudflare 巡检判定域名外迁，Custom Hostname 状态永久锁定为 `active`。
- **阿里云智能分线路解析防御**：
  - 阿里云 `speed.808608.xyz` 配置 `oversea` 境外线路直连 `origin.kavin.fun`，`default` 默认线路多路并发解析至 5 个异构 Anycast 优选 IP。

## [1.0.0] - 2026-09-27
### 核心架构落地与全自动化交付
- **架构落地**：基于 Cloudflare for SaaS (`kavin.fun`) 结合阿里云子域委托 (`speed.808608.xyz`)，实现真正的无感穿透优选架构。
- **业务零变更与零风险**：
  - 业务访问域名 100% 保持为 `bgy.808608.xyz`（及未来任意反代域名）。
  - `808608.xyz`、`017384.xyz`、`kavin.fun` 主域权威 NS 100% 留在 Cloudflare，家庭现有核心隧道（iKuai、群晖、PVE、飞牛）0 影响。
- **阿里云 OpenAPI 100% 全自动运维**：
  - 接入 Aliyun RAM OpenAPI，守护进程每 60 秒真实 HTTPS 探测。
  - 坏 IP 连续 2 次失败自动打入 2 小时冷冻池，并通过 API 秒级热替换。
  - 极端全失效场景自动熔断切回官方原生 Tunnel CNAME。
- **可视化监控大屏**：
  - CT160 独立运行 Web 状态看板：`http://10.10.10.160:3040/`。

## [0.1.0] - 2026-09-27
### 完成
- **立项与 PRD 定稿**：完成项目建立，输出标准 PRD 文档与微步骤 Roadmap。
- **Phase 0 只读现场勘测完成**：
  - 实测确认：CT 实际运行于 PVE10 (`10.10.10.10`)，VMID 为 **160** (`tunnel-10.10.10.160`)。
  - 出网实测：网关为 `10.10.10.2`，出口 IP 为 `120.239.168.196`（广东中山移动直连），无任何旁路由代理污染，测速环境极佳。
  - `cloudflared` 现状：当前已运行 `--protocol http2 --edge-ip-version 4`，协议加固已前置达成。
  - `tunnel-panel.py` 审计：位于 `/opt/tunnel-panel/tunnel-panel.py`，结构清晰，自带 Ingress 变更备份。
  - `Lucky (10.10.10.5)` 现状：CT 105，正在监听 `*:443` 与 `*:16601`，已作为内网反代核心。
