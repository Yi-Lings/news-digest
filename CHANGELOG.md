# 更新日志

版本纪律:tag = `v` + `__version__`(CI 强校验二者一致);**已推送的 tag 永不移动**,重打即升号。
部署脚本运行时从 `src/news_digest/__init__.py` 派生版本,该文件是唯一真源。

## [1.2.4] - 2026-08-01

- 修复生产 Admin 将翻译探测或单篇操作写入持久队列后没有 worker 消费，界面长期显示
  `provider probe is already queued` 的问题。
- Admin 入队后通过 systemd path 唤醒独立恢复 worker；恢复过程不重新抓取，只续接最新未完成刊期。
- 每日 worker 与恢复 worker 通过同一宿主锁串行；重复探测保持幂等，只重新唤醒 worker，
  不新增探测审计或第二次 provider 请求。

## [1.2.3] - 2026-07-28

- 将 Linux、WSL 与 Windows 自托管流程统一改为显式 `ND_*` 部署目标，移除仓库中的个人服务器、
  SSH key 路径、域名、证书邮箱和安装目录默认值；非法或缺失目标在联网前失败。
- 首次安装只生成关闭状态的安全配置，API/SMTP 密钥改由 HTTPS Admin 管理；部署不读取本地
  `.env.local`，不传输运行密钥，也不在已有当日刊物时触发抓取、翻译、构建或投递。
- 修复共享 SQLite volume 权限、非默认 Admin 端口和幂等复跑检查，补充通用部署文档、产品说明、
  Admin/订阅/自动化监控/移动端截图及部署工件回归。
- 保留 `v1.2.2` tag、镜像 digest 和 Release 资产不可变；`v1.2.3` 作为包含上述修复的新部署版本。

## [1.1.1] - 2026-07-28

- 修正 CI 流水线中的旧翻译测试替身，使其符合 p3 标题长度与学习内容数量 schema；生产行为不变。

## [1.1.0] - 2026-07-28

- 新增 OpenAI Chat 与 Anthropic Messages 双协议供应商档案、固定 `Hi` 连接测试和唯一默认档案。
- 接通每日四阶段流水线与 Admin 邮件工作台，提供 SMTP 分阶段诊断、逐账号投递状态、
  `unknown` 防重闩锁、纯文本刊物邮件和统一订阅名单。
- 新增公开 double opt-in 订阅、一键退订、隐私页、首页订阅与管理入口，并收口本地 loopback
  验收边界、DNS rebinding 防护和移动端排版。
- 部署链增加不可变 release manifest、迁移前 SQLite online backup、镜像 digest 绑定、
  worker 配置权限与发布工件校验。

## [1.0.0] - 2026-07-27

Cheapcoding News 首个正式版。0.6.0rc1–rc6 六个部署候选在线打磨,加上终审 8 项 P0 安全加固后转正。
正式站点 <https://news.cheapcoding.top>,每日 08:00(Asia/Shanghai)自动更新。

### 基线功能

- **每日全自动流水线**:RSS 抓取 → 选题 → AI 翻译与学习解析 → 静态站构建 → 原子发布 → 邮件简报,
  systemd timer 驱动,发布序号当日递增且永不复用,releases 保留 5 版自动修剪。
- **双语学习阅读**:段落级中英对照、词汇/搭配/句子解析、阅读时长;已译落选文章自动转双语简讯
  (翻译跨选题保留);`/issues/` 按日归档,`current` 原子切换;邮件与站点同源渲染。
- **管理面板**:网页登录(会话 Cookie);供应商档案管理与热切换(密钥只落服务器文件、页面永远掩码);
  改口令须验当前口令且失败恒定延迟;`import-edition` 支持往期导入。
- **部署链**:本地一键 `deploy-all` → GitHub Actions 构建多阶段镜像(GHCR,非 root + 只读根 + 内存上限)
  → 服务器 `bootstrap.sh` 幂等拉起(nginx + compose + systemd),`preflight.sh` 只读预检。

### 安全与部署加固(终审 8 项 P0,全部落地)

- **存储型 XSS 双层防线**:RSS 入库仅收 http/https 链接(`is_web_url` 白名单);渲染层 `safe_url`
  过滤器兜底全部外部 href/src(3 href + 2 img src),恶意 `javascript:`/`data:` URL 即使入库也无法成为可执行链接。
- **SMTP 强制证书校验**:`ssl.create_default_context()` 注入 465 隐式 SSL 与 STARTTLS 两条路径,
  杜绝中间人窃取邮件凭据。
- **服务器 GHCR 凭据降权**:弃用全权限 `gh auth token`,改用只读 `read:packages` PAT
  (`ND_GHCR_TOKEN` 或隐藏交互输入),token 只走 stdin、不上 argv、不进日志。
- **`.env` 定点合并**:部署改经 `.env.incoming` + awk 逐键 upsert,保留服务器上运维手工添加的
  键与注释,不再整包覆盖;密钥仅经 scp 文件通道下发。
- **镜像 digest 纪律**:每次部署把实际解析的 worker/web digest 追加写入 `backups/DEPLOYED.log`
  回滚台账;支持 `ND_WORKER_DIGEST`/`ND_WEB_DIGEST` 按不可变 digest 固定;已固定 digest 的机器
  拒绝静默降级回 tag(`ND_ALLOW_TAG_DOWNGRADE=1` 显式放行)。
- **certbot 续期闭环**:自动安装 `renewal-hooks/deploy/10-reload-nginx.sh` 目录钩子
  (对存量证书同样生效),证书续期后 nginx 自动重载,消除 90 天后 HTTPS 静默失效隐患。
- **版本号单源 + tag 预检**:deploy 脚本硬编码版本全部删除,运行时从 `__init__.py` 派生并经
  `ND_VERSION` 贯通到服务器;推 tag 前预检本地 tag 指向与远端一致性(禁移已发布 tag);
  bootstrap 部署后核对运行镜像 OCI version label 与 TAG。
- **凭据全量轮换**(运维项):SSH 口令、SMTP 密码、翻译 API key、GHCR token 吊销换只读 PAT。

### 测试与审计

- 单元/集成测试 106 → **111 项全绿**(新增 5 项渲染安全测试,含端到端恶意 URL 注入断言)。
- 终审与复审报告归档:`docs/v1.0.0-final-review.md`、`docs/v1.0.0-rereview.md`
  (7 项代码 P0 逐项裁决通过,遗留一行级文档失实已随本版修正)。

## [0.6.0rc1–rc6] - 2026-07-25 ~ 2026-07-27(部署候选摘要)

- **rc1**:首个部署候选;部署工件 8 件成型,95→96 项测试,许可证与敏感信息审计干净。
- **rc2**:生产上线主链路与管理面板;修复改口令接口未验当前口令的会话夺权风险。
- **rc3–rc4**:面板口令文件权限收紧(640→600);rc4 经历标签分叉事故——由此固化
  「已推送 tag 永不移动」纪律。
- **rc5**:干净重发;`import-edition` 落地,创刊号 44 篇(18 篇双语)+ 简讯 10 条入归档。
- **rc6**:修复发布器自删缺陷(序号复用 + 修剪误删组合);面板三连验收 + 外网安全复核 7 项全绿;
  发布序号递增与 `current` 自动接管在线实证。
