# 更新日志

版本纪律:tag = `v` + `__version__`(CI 强校验二者一致);**已推送的 tag 永不移动**,重打即升号。
部署脚本运行时从 `src/news_digest/__init__.py` 派生版本,该文件是唯一真源。

## [Unreleased]

（下一版本尚未记录变更。）

## [1.2.14] - 2026-08-29

- 单篇数据缺失或 schema 失败时继续处理同刊期其他可调度任务，队列耗尽后再进入人工处理状态。
- 保留失败任务和审计动作，不再因单篇失败提前中断整期恢复。

## [1.2.13] - 2026-08-29

- 将正式翻译提示改为逐句 `P#S#` 编号输入，并明确每段句数，减少模型合并英文句子导致的 schema 拒绝。
- 提示版本升级至 `p6`，旧版译文缓存自动隔离并按新契约重新生成。

## [1.2.12] - 2026-08-29

- 修复 OpenAI 流式响应在 gateway 中途断开后仍被当作完整文本解析，避免半截 JSON 被误报为 `SCHEMA_VALIDATION_FAILED`。
- 未收到 `[DONE]` 或收到流式错误事件时记录为可重试的上游响应失败。

## [1.2.11] - 2026-08-29

- 修复 schema/响应格式失败任务的人工重试在 worker 启动恢复时被误归一化，导致动作长期停留在 `requested` 且任务重新显示为阻断的问题。
- 启动恢复会保留新的人工重试动作，并修复已被旧逻辑改回 `failed` 的遗留人工重试；不改变正式 schema 校验规则。

## [1.2.10] - 2026-08-28

- 修复 schema/响应格式错误被无限自动退避的问题；失败会保留为可见的 `failed` 任务并提供人工重试。
- 修复翻译失败阶段固定显示为 `waiting_model` 的问题，schema 错误现在记录为 `schema_validation`。
- 修复任务引用原文缺失时遗留 `running` lease 的问题，并在 worker 启动时归一化旧版遗留退避任务。
- 新增 `TASK_DATA_MISSING` 脱敏错误代码，用于标识任务与原文数据不一致。

## [1.2.9] - 2026-08-28

- 修复翻译任务处于 `pending` 时显示可调度却没有操作按钮的问题，新增持久化“立即调度”动作。
- 按任务所属 provider 计算恢复动作，补齐熔断探测幂等、配置阻断恢复和取消 lease 回收。
- Admin 翻译状态模块在任务行显示“立即探测”，并保留错误代码、失败阶段、退避时间和动作审计。
- SQLite schema 升级至 v7；已有文章、翻译尝试、发布与投递审计保持不变。

## [1.2.6] - 2026-08-11

- 将新闻翻译提示词升级为 `p4`：明确要求逐句、逐段完整翻译，禁止摘要化、合并段落、跳过
  句子、删减事实或弱化限定条件；要求保留人物、机构、地点、时间、数字、比例、金额、
  否定、条件、引语和归因，并在输出前逐段自检。
- 提示词版本升级会隔离旧 `p3` 缓存；没有当前 `p4` 有效缓存的既有译文会在下一次翻译链路
  中重新生成，避免旧译文被静默复用。

## [1.2.5] - 2026-08-01

- 修复旧刊投递失败后始终被恢复 worker 优先领取、随后又被投递时间门禁拒绝，导致新刊永远
  无法进入 SMTP 且 `news-digest-resume.service` 每 15 秒重启的问题。
- 自动投递只领取当前 worker 的刊期；开始处理新刊时，更早的未投递 `complete` 刊期标记为
  `DELIVERY_EXPIRED`，保留刊物、订阅与投递审计，但不再自动补发或进入恢复队列。
- 当日目标文章仍须满足 `target_count = succeeded_count = online_count` 并完成最终 build 后
  才投递；Admin 刊期摘要增加封闭错误码，不回显 SMTP/provider 原始内容。
- systemd 将确定性人工处理终态 `10` 与临时锁竞争 `75` 分离；前者停止恢复重启，后者仍可
  稍后重试。

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
## v1.2.8

- Added p5 sentence-aligned translation validation and cache migration.
- Added conservative news sentence-boundary handling and resume service start limits.

## v1.2.7

- Unified upstream HTTP 403 failures as `UPSTREAM_ERROR`.
- Added controlled provider probe and recovery for configuration-blocked tasks.
