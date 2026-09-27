# v1.4.0t36：公网验收与诊断收口计划

日期：2026-09-27。状态：**待实施**。以 t35 的本机 SNI/TLS 门禁、可终止默认 DNS、schema 14 翻译诊断为基线。本文件只规划未收口的问题，不改变 t35 代码或发布结论。所有检查使用部署者输入的 `ND_DOMAIN`，不得预设任何生产域名、服务器 IP 或第三方代理配置。

## 待修问题与实施标准

| 优先级 | 触发条件与影响 | 最小改动 | 可验证验收标准 |
|---|---|---|---|
| P2：公网 TLS 路径 | 本机 `--resolve ND_DOMAIN:443:127.0.0.1` 验收通过，但公网 DNS/CDN 指向另一台证书错误或返回异常的主机；现有 DNS 体检只警告，部署仍可能报告完成。 | 保留 t35 本机硬门禁，增加共用的公网 HTTPS GET 门禁。使用正常 DNS、系统信任链和原始 SNI 检查 `/`、`/healthz`、`/admin/` 均为 200，使用 `--noproxy '*'`，禁止 `--resolve` 与 `-k`，每条请求设置有限时。新增 `ND_PUBLIC_TLS_POLICY=required|report-only`，默认 `required`；PowerShell 入口对应 `-PublicTlsPolicy`。`report-only` 仅供 CDN、分流或回环路由无法从部署主机验证的环境显式选择，失败时清楚标记“公网未验证”，不得输出公网验收通过。HTTPS 部署在恢复 timer 前执行；`required` 失败按 t35 流程回滚，首次 HTTP-only 安装只报告无 HTTPS，不执行该门禁。各部署入口调用同一检查，不复制判断逻辑。 | 本机 TLS 正常而公网证书错配、过期、状态码错误或超时时，`required` 阻断并恢复原配置与 timer；`report-only` 仅告警且最终状态明确为未验证。公网经 CDN 指向不同 IP、但证书和三条路径有效时通过，不要求 DNS 等于服务器 IP。两个不同测试域名均通过隔离测试。 |
| P3：取消 attempt 丢失诊断 | 翻译请求已收到响应后被确认取消时，Runner 已采集安全阶段耗时，但 `confirm_translation_task_cancelled` 不写入 attempt 的 `timings_json` 或 `http_status`。 | 给取消完成函数增加可选 `timings` 参数，复用 schema 14 的安全字段校验；与 task、attempt、Admin action、probe lease 的取消结果在同一 SQLite 事务写入。旧调用方不传参数时行为不变，task 仍按取消策略进入 `retry_wait`。 | 模拟流式响应开始后取消、取消前收到 504、尚未发请求即取消：attempt 分别保留已有阶段/状态或 `null`，不出现 URL、密钥、正文；故障注入提交前中断全部回滚；旧调用方测试继续通过。无需 schema 15。 |
| P3：自定义 DNS resolver 无期限 | 非默认 Admin 集成给 provider 校验注入阻塞的 `resolver` 时，该 callable 当前同步运行，可占住 HTTP 请求线程。生产默认 resolver 已由 t35 隔离。 | 把 provider 域名校验的可注入 resolver 限定为测试内部：公开 Admin 保存、连接测试路径始终使用 t35 可终止解析；提取纯公网地址校验函数供单元测试直接传入地址。SMTP 的独立 resolver 注入不在本项修改。保留翻译客户端固定目标 IP 连接与所有地址必须为公网的规则。 | 阻塞 DNS 在 10 秒期限加有界清理宽限后安全失败，紧接着第二次测试可成功；Admin API 不存在绕过可终止解析的 callable 路径；私网、混合公网/私网、非法 IP 仍被拒绝，成功连接只访问已校验地址。 |
| P3：迁移演练时区固定 | 非 `Asia/Hong_Kong` 的开源部署使用 `verify-content-migration.py --build` 时，构建演练仍以固定时区读取刊期，与目标实例配置不一致。 | `--build` 同时要求显式 `--timezone`（IANA 时区）和现有 `--site-url`；用 `zoneinfo.ZoneInfo` 校验后传入 `FetchConfig`。不读取或修改生产 `.env`，无 `--build` 的纯数据核对保持原用法。 | 缺失或非法时区在读取快照前报错；以 `UTC` 与 `Asia/Shanghai` 的隔离快照分别演练并核对刊期及历史页面哈希；脚本不含实例域名、IP 或固定时区。 |

## 实施顺序与发布验收

1. 先完成公网检查的隔离 TLS/Nginx 测试与 `required`/`report-only` 回滚路径，再接入 `install.sh`、`server-push.ps1`、`deploy-all.ps1` 共用检查。外部探测与本机探测分别报告，不把公网失败伪装为成功。
2. 增加取消 attempt 的事务回归，再移除 Admin provider 校验的同步注入路径；验证阻塞 DNS 后立即重试。最后加入迁移时区参数及文档示例。
3. 跑完整离线测试、Ruff、脚本语法及隔离 Nginx/TLS 测试；公网 RSS 仍单独运行。候选发布后用实际 `ND_DOMAIN` 核对本机与公网证书、三个 GET 状态、Admin 连接测试、取消诊断和 timer；记录上一镜像、Nginx 配置与 timer 状态作为回滚依据。

保持现有单机 Nginx/Certbot、SQLite 和 Docker Compose 结构。本版不修改第三方 CDN/provider 网关，也不自动迁移域名、重写历史品牌内容或引入新的数据库 schema。
