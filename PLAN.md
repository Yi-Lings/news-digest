# Cheapcoding News Digest - 本地开发与服务器部署计划

## 1. 项目目标

建设一个面向个人英语学习的每日双语新闻站，最终发布到 `news.cheapcoding.top`。

系统从经过审核的英文新闻源获取最新内容，筛选当日重点新闻，通过 OpenAI 兼容接口完成翻译和英语学习解析，生成静态网页，并通过 SMTP 向指定邮箱发送当日简报。

开发环境与最终运行环境：

- 开发环境：当前 Windows 本机 `E:\new\news-digest`。
- 生产环境：用户现有 Linux 服务器。
- 最终入口：`https://news.cheapcoding.top`。
- 本地 `http://127.0.0.1:8618` 仅用于开发验收，不作为最终运行方式（原定 8000，因被本机常驻程序占用改为 8618）。

本计划采用以下执行顺序：

1. 所有功能先在 Windows 本地环境实现和验收。
2. 本地阶段仅在用户明确授权时调用真实翻译接口或发送真实邮件。
3. 本地功能全部验收后，将源码 version tag 推送到 GitHub，由 GitHub Actions 构建并推送 Docker 镜像到 GHCR。
4. Linux 服务器不检出源码，只拉取已验收的镜像并通过 Docker Compose 部署。
5. 服务器上的 Nginx、HTTPS、systemd timer 和资源限制只在最终部署阶段配置。

核心原则：

- 每个阶段完成后必须由用户验收，用户确认前不进入下一阶段。
- 先使用固定测试数据验收界面，再接入真实抓取和模型，避免无效 API 消耗。
- 正文抓取失败时降级为摘要，不影响当日其他内容。
- 不绕过付费墙，不公开转载无授权的受版权保护全文。
- 外部服务通过配置注入，禁止在源码中写死域名、IP、API Key、模型或 SMTP 凭据。
- 源码、测试和依赖锁文件是项目事实来源；本地与生产使用同一套业务代码。
- 本地开发不依赖 Docker；生产部署使用固定版本 Docker 镜像。

## 2. 已确认的本地环境

检测日期：2026-07-25。

| 项目 | 当前状态 | 规划用途 |
|---|---|---|
| 工作目录 | `E:\new\news-digest` | 项目根目录 |
| 操作环境 | Windows、PowerShell 5.1 | 本地命令和脚本执行环境 |
| Python | `3.13.1` | worker、静态生成器和测试运行时 |
| uv | `0.11.28` | Python 版本、虚拟环境和依赖管理 |
| Node.js / npm | `24.15.0` / `11.12.1` | 当前不作为项目依赖；仅在确有前端构建需求时重新评估 |
| Git | `2.54.0.windows.1` | 本地版本管理 |
| GitHub CLI | `2.92.0` | 远程仓库认证与同步 |
| Docker | 本机未安装 | 本地开发不需要；镜像由 GitHub Actions 构建 |
| Git 仓库 | 尚未初始化 | 阶段 0 初始化并绑定 GitHub 远程 |

本地环境与服务器环境必须分开处理：

| 能力 | 本地开发 | 生产服务器 |
|---|---|---|
| 应用运行 | `uv run ...` 原生运行 | Docker Compose |
| 页面访问 | `http://127.0.0.1:8618` | `https://news.cheapcoding.top` |
| 静态文件服务 | Python 本地 HTTP server | 容器内 Nginx + 宿主机 Nginx |
| 定时执行 | 手动 CLI；测试中模拟时间 | systemd timer |
| 数据 | 本地 SQLite 和生成目录 | 持久化 volume |
| 翻译接口 | fixture/mock；授权后小规模真实调用 | SUB2API |
| 邮件 | `.eml` 预览和 fake SMTP；授权后测试投递 | 现有 SMTP |
| 密钥 | `.env.local`，不提交 Git | 权限受限的环境文件或 Docker Secret |

计划发布链路：

```text
Windows 本地开发与测试
          |
          v
Git commit + version tag
          |
          v
GitHub 私有仓库
          |
          v
GitHub Actions 测试并构建镜像
          |
          v
GHCR 私有镜像（version tag + digest）
          |
          v
Linux 服务器 docker compose pull / up
          |
          v
宿主机 Nginx + HTTPS
          |
          v
https://news.cheapcoding.top
```

服务器宿主机不保存 Git 仓库或项目源码。应用代码只存在于预构建的 worker 镜像内；服务器只保留 Compose manifest、运行配置、持久化数据、生成归档和运维单元。生产密钥只在服务器注入，不进入源码仓库、GitHub Actions 日志或镜像。

## 3. 技术基线

采用单一 Python 项目，避免在静态站点上增加不必要的前端框架和常驻应用服务。

- Python：`>=3.13,<3.14`。
- 包与虚拟环境：uv，提交 `pyproject.toml` 和 `uv.lock`。
- 页面生成：Jinja2 模板生成静态 HTML。
- 浏览器交互：原生 CSS 和 JavaScript，不引入 Node 构建链。
- 数据：SQLite；业务模块通过 storage API 访问，不直接散布 SQL。
- HTTP 获取：httpx。
- RSS 解析：feedparser。
- 正文提取：trafilatura（阶段 2 选定，理由记录于 `extractors/body.py` 与 `tests/fixtures/feeds/SOURCES.md`）。
- HTML 清洗：nh3 或同等级白名单清洗器，阶段 2 用安全测试确定。
- 配置：环境变量和 `.env.local`；应用启动时统一校验。
- 测试：pytest；网络测试使用本地 fixture 或 mock，默认测试不得访问公网。
- 代码检查：Ruff。
- 时区：应用内部使用 IANA 时区和 UTC 时间戳；Windows 环境添加 `tzdata` 依赖。
- 编码：Windows 控制台默认代码页不是 UTF-8；本地开发统一设置 `PYTHONUTF8=1`，代码中所有文件读写显式指定 `encoding="utf-8"`。
- 行尾：提交 `.gitattributes` 强制源码与 fixture 使用 LF，防止 CRLF 转换改变文件内容哈希，破坏去重和翻译缓存的稳定性。
- 路径：统一使用 `pathlib` 构造路径，不硬编码分隔符，保证 Windows 开发与 Linux 生产容器行为一致。

计划提供单一 CLI 入口：

```text
news-digest fetch          获取并规范化候选新闻
news-digest translate      翻译已抓取内容并生成学习注解（阶段 3 增补；真实调用需显式确认）
news-digest preview        本地预览 + 模型供应商切换面板（阶段 3 增补；仅绑定 127.0.0.1）
news-digest build          生成指定日期的静态站点
news-digest run            执行完整的每日流水线
news-digest preview-email  生成本地邮件预览
news-digest send-email     显式发送已生成的简报
```

`send-email` 不包含在默认本地流水线中，避免开发时误发邮件。

## 4. 目录和模块边界

```text
news-digest/
├── pyproject.toml
├── uv.lock
├── .env.example
├── src/news_digest/
│   ├── cli.py                 CLI 入口
│   ├── config.py              配置读取与校验
│   ├── models.py              模块间共享的数据模型
│   ├── pipeline.py            完整流程的组合与调度
│   ├── sources/               RSS 获取和来源适配器
│   ├── extractors/            正文与媒体字段提取
│   ├── selection/             去重、评分和每日选题
│   ├── translation/           通用翻译接口和响应校验
│   ├── storage/               SQLite 状态与归档索引
│   ├── rendering/             页面和邮件模板渲染
│   ├── delivery/              静态发布和 SMTP 投递
│   ├── templates/             HTML 和邮件模板
│   └── static/                CSS、JavaScript 和站点资源
├── tests/
│   ├── fixtures/              固定输入、模型响应和来源样本
│   ├── unit/
│   └── integration/
├── var/                       本地运行数据，不提交 Git
│   ├── data/
│   ├── site/
│   └── mail/
└── deploy/                    最终部署阶段再创建
```

模块约束：

- `models.py` 只定义数据结构，不执行网络、数据库或文件操作。
- `pipeline.py` 是唯一组合完整流程的模块，业务模块之间不直接互相初始化。
- `sources` 只产出规范化候选文章，不调用翻译、模板或 SMTP。
- `extractors` 只接收允许访问的文章地址并返回清洗后的正文数据。
- `selection` 只依赖规范化文章数据，不了解抓取方式和页面模板。
- `translation` 只依赖通用接口配置和文章数据，不绑定 SUB2API 部署位置。
- `rendering` 只接收准备好的展示数据，不访问新闻源或模型接口。
- `delivery` 不参与文章处理，只负责投递已生成内容。
- `storage` 提供明确的读写方法，其他模块不得直接操作其表结构。
- 单模块行为由单元测试覆盖，跨模块行为由 pipeline 集成测试覆盖。
- 只在确有多个实现时定义接口，不为单一用途制造抽象。

## 5. 本地运行模型

```text
tests/fixtures 或真实 RSS
             |
             v
      news-digest CLI
  获取 -> 去重 -> 筛选 -> 提取
             |
             v
 fixture/mock 或翻译 API
             |
             v
     var/data/news.db
             |
       +-----+-----+
       |           |
       v           v
 var/site/      var/mail/
 静态网页       .eml 预览
       |
       v
 http://127.0.0.1:8618
```

本地生成先写入 `var/site/releases/<日期-序号>`，校验通过后将 `var/site/current` 切换到新版本；生成失败时 `current` 不变，保留上一版页面。

Windows 注意：目录重命名会被已打开的文件句柄阻塞（例如预览用的本地 HTTP server），且符号链接默认需要管理员权限或开发者模式。因此本地 `current` 切换采用 NTFS 目录联接（junction）或指针文件方案，阶段 1 实测后固定为单一实现；Linux 生产端由同一发布模块用符号链接完成切换。

## 6. 配置与敏感信息

仓库只提交无敏感值的 `.env.example`。本地真实值写入 `.env.local`，并由 `.gitignore` 排除。

```dotenv
# Local paths and site
NEWS_ENV=development
NEWS_SITE_URL=http://127.0.0.1:8000
NEWS_TIMEZONE=
NEWS_DATABASE_PATH=var/data/news.db
NEWS_OUTPUT_PATH=var/site

# Translation; optional until phase 3
TRANSLATION_API_BASE_URL=
TRANSLATION_API_KEY=
TRANSLATION_MODEL=
TRANSLATION_TIMEOUT_SECONDS=60

# SMTP; optional until phase 5
SMTP_HOST=
SMTP_PORT=
SMTP_USERNAME=
SMTP_PASSWORD=
SMTP_FROM=
SMTP_RECIPIENTS=
SMTP_USE_TLS=true
```

约束：

- 缺少当前命令不需要的配置时不得阻止启动。例如页面 fixture 构建不要求 SMTP 配置。
- 调用真实翻译接口前必须显示目标接口、模型、文章数量和预计调用次数。
- 发送真实邮件必须使用显式 `send-email` 命令，不得由页面预览或测试隐式触发。
- 测试不得读取 `.env.local`，防止测试意外访问真实服务。
- 日志不得输出 API Key、SMTP 密码、完整授权头或包含凭据的 URL。

## 7. 新闻来源范围

首批全文候选来源：

- BBC News
- The Guardian
- NPR
- DW English
- Al Jazeera English
- France 24 English

标题和摘要补充来源：

- The New York Times
- Reuters（仅使用合规可访问的 RSS 或聚合入口，并保留原始来源链接）

每个来源正式接入前必须分别记录和验证：RSS 地址、发布日期格式、正文可提取性、图片字段、访问条款、robots 约束、请求频率和降级策略。来源验证结果应成为测试 fixture 和简短的来源说明，而不是只保存在运行日志中。

## 8. 分阶段计划与验收门

### 阶段 0：本地项目骨架

状态：`已验收（2026-07-26 用户放行）；首次提交与 GitHub 远程绑定待补，最迟阶段 1 验收前完成`

工作范围：

- 初始化本地 Git 仓库、`main` 分支、`.gitignore` 和强制 LF 的 `.gitattributes`。
- 创建 `pyproject.toml`、`uv.lock`、Python package 和测试目录。
- 建立本计划约定的模块目录，但不填充未使用的业务抽象。
- 创建无敏感值的 `.env.example`。
- 创建最小 CLI，至少支持 `--help` 和版本输出。
- 添加一个最小单元测试和 Ruff 配置，验证本地工具链。
- 在用户提供仓库信息后完成 GitHub 认证、私有远程仓库绑定和首次推送。

验收标准：

```powershell
uv sync
uv run news-digest --help
uv run pytest
uv run ruff check .
```

上述命令在全新 checkout 中通过；`git status` 不包含虚拟环境、`.env.local`、SQLite、生成页面或日志。

验收门：用户确认项目结构和本地命令后进入阶段 1。GitHub 信息未提供时不阻塞本地骨架，但必须在阶段 1 验收前补齐远程同步。

### 阶段 1：本地前端视觉原型

状态：`已验收（2026-07-26）`

已确认的产品决定（2026-07-26）：站点名称 `Cheapcoding News`；每日主文章 6 篇；视觉方向为报纸编辑风（冷纸白/墨黑/朱砂红，Constantia+Georgia+宋体，中文以批注体呈现，印章作为品牌与预览标识）；界面控件纯中文。

工作范围：

- 仅使用 `tests/fixtures` 中的固定演示数据。
- 完成今日首页、文章阅读页和日期归档页。
- 完成英文、双语、中文三种阅读模式。
- 完成来源筛选、阅读时长、词汇、长难句和浏览器朗读控件。
- 使用明确标注为预览数据的标题、摘要和本地测试图片。
- 生成纯静态 HTML、CSS 和 JavaScript 到 `var/site/releases`，并按第 5 节方案切换 `var/site/current`。
- 使用本地 HTTP server 预览，不连接 RSS、翻译接口或 SMTP。

本地验收入口：

```powershell
uv run news-digest build --fixtures tests/fixtures/demo
uv run python -m http.server 8000 --directory var/site/current
```

验收标准：

- `http://127.0.0.1:8000` 可访问，页面无控制台错误和失败资源。
- 在手机、平板和桌面视口检查布局，无文本溢出、遮挡和意外位移。
- 三种阅读模式、筛选、朗读和日期导航可操作。
- 自动化测试验证生成页面的关键内容和链接。
- 不产生任何公网请求、API 费用或邮件投递。

验收门：用户在本地预览并确认视觉和交互后进入阶段 2。

### 阶段 2：真实新闻获取与正文提取

状态：`已验收（2026-07-26）`

工作范围：

- 先为每个来源保存最小 RSS/HTML fixture，再实现适配器。
- 获取最近 24 小时的候选文章，规范化时间、链接和来源字段。
- 按 canonical URL、标题相似度和内容哈希去重。
- 实现正文提取、清洗、HTML 安全过滤和图片引用。
- 实现域名 allowlist、私网地址阻断、超时、响应大小和重定向限制。
- 实现来源失败和正文失败的摘要降级。
- 使用真实英文内容生成本地站点，但暂不调用翻译接口。

验收标准：

- 默认测试完全离线并可重复执行。
- 单独标记的 network smoke test 能验证各来源当前可用性。
- 抓取结果包含来源、标题、作者、发布时间、canonical URL、摘要和可用图片信息。
- 单一来源失败时完整流水线仍能成功生成页面。
- 页面明确链接和标注原始来源，不公开不应转载的全文。

验收门：用户确认本地真实英文版和来源处理结果后进入阶段 3。

### 阶段 3：翻译与英语学习内容

状态：`已验收（2026-07-26）——全量 43/44 篇真实翻译完成（1 篇模型输出瑕疵经 --redo 修复），p2 官方文风获认可`

工作范围：

- 先使用固定模型响应 fixture 完成结构化输出解析、校验和页面渲染。
- 定义中文标题、中文摘要、段落翻译、重点词汇、固定搭配和长难句解析的 JSON schema。
- 按文章内容哈希、模型和 prompt 版本缓存结果。
- 实现失败重试、断点恢复和单篇人工重新生成。
- 清楚标识媒体原文、媒体摘要和 AI 生成内容。
- 用户确认接口、模型和调用规模后，使用少量真实文章执行一次受控测试。

验收标准：

- 无 API Key 时全部离线测试和 fixture 页面仍能通过。
- 非法或不完整模型响应被拒绝，且不覆盖已有有效结果。
- 同一内容、模型和 prompt 重复运行时命中缓存，不重复计费。
- 中英段落正确对齐，翻译和学习内容通过用户抽样验收。
- 真实调用前后记录文章数、请求数、成功数、失败数和缓存命中数，但不记录敏感值。

验收门：用户确认翻译风格和学习内容后进入阶段 4。

### 阶段 4：每日选题、SQLite 归档与完整流水线

状态：`已验收（2026-07-26）——用户确认首页收敛为 6 主文章 + 双语简讯，daily.bat 全流程跑通`

阶段 4 设计决定（2026-07-26）：流水线顺序为「抓取 → 入库 → 选题 6 篇 → 仅翻译选中篇目 → 构建」，将每日翻译成本从全量（40+ 篇）压到 6 篇；未选中的全文候选降为简讯（外链原文，不翻译不建页）。SQLite 保存文章池与版次（翻译结果随文章入库）；请求级翻译缓存维持既有文件实现，不迁库（记录为对计划文字的偏离，理由：已验证、键设计完备，迁移无收益）。时区采用代码现默认 `Asia/Shanghai`（与 Asia/Hong_Kong 同为 UTC+8，无实际差异，待确认项就此关闭）。修订：历史日期与新日期统一走选题规则展示（6 主文章 + 简讯），不为 2026-07-26 的 44 篇设特例——已译内容保留在库中，其中未入选者以双语简讯形式外链呈现；理由：不引入按日期的选择状态，构建规则单一。

工作范围：

- 建立每日来源配额、候选评分和主题多样性规则。
- 默认生成约 6 篇主文章和若干标题简讯，最终数量由用户确认。
- 使用 SQLite 保存文章、内容版本、任务、翻译缓存和发送状态。
- 生成每日首页和历史日期归档。
- 实现原子发布、任务幂等和失败后恢复。
- 支持 `--date` 和 fixture 模式，使历史日期与失败场景可重复测试。
- 本地仅手动运行，不配置 Windows Task Scheduler。

验收标准：

- 同一天连续执行两次不重复抓取已确认内容、不重复翻译、不产生重复归档。
- 生成失败时 `var/site/current` 仍指向上一完整版本。
- 可以从空数据库完整生成，也可以从中断状态继续。
- 单个来源或单篇翻译失败不阻止其余合格内容发布。
- 用户确认选题质量、来源比例、主题多样性和历史日期导航。

验收门：使用 fixture 完成至少两个日期的生成，并对一个真实日期执行受控生成；用户确认后进入阶段 5。

### 阶段 5：本地邮件预览与受控 SMTP 测试

状态：`暂缓（2026-07-26 用户决定砍掉邮件功能，站点为唯一交付物；本阶段整体推迟，未写入任何邮件代码。日后恢复时按本节原范围执行）`

工作范围：

- 生成适合桌面和手机邮件客户端的双语摘要邮件。
- 邮件包含当天标题、摘要、来源和网站入口。
- 默认输出 `.eml` 到 `var/mail`，便于本地检查。
- 使用 fake SMTP 完成集成测试，不访问真实服务器。
- 只允许在站点成功生成后创建发送任务。
- 记录发送状态并防止重复投递。
- 用户提供 SMTP 和收件地址并明确授权后，发送一封真实测试邮件。

验收标准：

- `.eml` 的纯文本和 HTML 两部分内容正确，链接完整。
- 自动测试验证重复运行不会重复发送。
- 真实测试邮件正常到达指定邮箱，用户确认桌面端、移动端和垃圾邮件表现。
- 日志、Git 和生成站点中不存在 SMTP 凭据或收件地址泄漏。

验收门：用户确认测试邮件后进入阶段 6。

### 阶段 6：本地发布候选版

状态：`进行中（与阶段 5 验收并行，用户指示并发推进）`

工作范围：

- 在全新虚拟环境执行依赖安装、代码检查和完整测试。
- 使用 fixture 执行不访问外部服务的端到端流水线。
- 使用受控真实配置完成一次完整生成；真实邮件发送仍需单独显式授权。
- 固定依赖版本，检查许可证、敏感信息和生成物边界。
- 测量 worker 峰值内存、生成耗时、SQLite 和站点目录大小。
- 编写本地运行、配置、故障恢复和数据备份说明。
- 确定第一个部署候选版本，例如 `0.6.0-rc.1`。

验收标准：

- `uv sync --locked`、测试、Ruff 和端到端 fixture 流程全部通过。
- 从空目录可以按文档重建本地站点。
- `.env.local`、数据库、归档、邮件和日志均未被 Git 跟踪。
- 用户确认本地发布候选版后才开始服务器变更。

验收门：用户明确批准部署候选版本后进入阶段 7。

### 阶段 7：CI 镜像构建与服务器部署

状态：`部署成功（2026-07-27 02:31 UTC 上线）——进入观察期：验收标准要求连续观察至少两个生成周期 + 一次回滚演练，完成后发布 1.0.0`

前置条件：

- 已获得服务器访问权限和当前 Nginx 配置的只读核对结果。
- 已确认服务器 Docker/Compose 版本、CPU 架构、可用内存和磁盘。
- DNS `news.cheapcoding.top` 仍指向目标服务器。
- GitHub 私有仓库已启用 Actions 和 GHCR，服务器具备 GHCR 只读凭据。
- 生产 SUB2API、SMTP、时区、收件地址和访问保护配置已确认。
- 已完成数据库和上一版站点的备份/回滚路径设计。

工作范围：

- 在源码仓库中创建固定基础镜像版本的 worker 和 web Dockerfile。
- 创建 GitHub Actions workflow：对 version tag 对应的提交在 Linux runner 上复跑完整测试（兼作 Windows 本地与 Linux 生产的平台差异守门），构建与服务器 CPU 架构匹配的镜像，并推送到私有 GHCR。
- 镜像写入 OCI `source`、`revision` 和 `version` labels，同时发布可读 version tag 和不可变 digest。
- 创建生产 Compose；只引用已发布的镜像，不包含 `build`、源码目录或源码 bind mount。
- 在服务器创建独立部署目录，仅保存 Compose manifest、非敏感配置、密钥引用和运维文件。
- 使用 GHCR 只读 token 登录，在服务器执行 `docker compose pull` 和 `docker compose up -d`；服务器不执行镜像构建。
- worker 为一次性任务，web 只读提供静态页面。
- web 仅绑定宿主机 `127.0.0.1`，公网统一经过宿主机 Nginx。
- 配置 Nginx、HTTPS、登录保护、`noindex`、CSP、安全响应头和限速。
- 配置 systemd timer 调用 `docker compose run --rm news-worker`。
- 部署生产模型切换面板（已确认形态：仅切换预置档案，密钥不经网页传输；复用本地 `/admin/` 面板裁剪版，置于登录保护与 HTTPS 之后，仅绑定宿主 `127.0.0.1` 由 Nginx 代理）。
- 一键 Docker 部署包（用户 2026-07-26 增补需求，类 sub2api）：CI 在发布时将 `deploy/` 打包为 Release 附件，配 `install.sh` 实现服务器端一条命令部署；排在首次部署跑通之后实现，避免在部署中途变更 CI。
- 使用非 root 用户、只读根文件系统、移除 capabilities 并启用 `no-new-privileges`。
- 设置 worker 256 MB、web 32 MB 的初始内存上限，并根据实测调整。
- 配置日志轮转、健康检查、持久化目录、备份和版本回滚。
- Compose 固定镜像 digest；version tag 用于可读性，不使用 `latest`。

验收标准：

- GitHub Actions 只允许从本地已验收的 version tag 构建，测试与构建使用该提交中的同一 `uv.lock`。
- 服务器部署目录中不存在 Git metadata、`src/`、`tests/`、`pyproject.toml` 或 `uv.lock`。
- 服务器运行的镜像 digest 与发布记录一致，镜像 OCI revision 可追溯到验收提交。
- HTTPS、访问保护、定时任务、SMTP、健康检查和异常页面正常。
- 密钥不进入镜像、Git、日志或网页。
- 完成一次备份恢复和上一镜像版本回滚演练。
- 连续观察至少两个生成周期，单次失败不会破坏上一版站点或重复发信。

验收门：用户最终确认后发布 `1.0.0` 并转入日常运行。

## 9. 通用本地验收流程

每个本地阶段统一执行：

1. 在阶段分支完成约定范围。
2. 执行该阶段的单元测试、集成测试、代码检查和页面检查。
3. 生成本地验收版本，并提供 `http://127.0.0.1:8000` 或相应本地产物。
4. 记录命令、提交、改动、测试结果和已知限制。
5. 暂停进入下一阶段，等待用户验收。
6. 在同一分支完成用户要求的阶段内微调并重新验证。
7. 用户明确确认后合并到 `main`，创建 `phase-N-accepted` 标签并同步 GitHub。

任何阶段不得以“测试通过”替代用户验收，也不得因本地验收需要而默认变更服务器。

## 10. 版本、数据和回滚策略

- 开发版本：`0.x.y`；首个生产正式版本：`1.0.0`。
- 每个阶段使用独立分支，例如 `phase/1-ui`、`phase/2-sources`。
- 阶段验收通过后合并到 `main` 并创建 `phase-N-accepted` 标签。
- 仓库不得提交 `.env.local`、API Key、SMTP 密码、数据库、生成站点、邮件、缓存或日志。
- 数据库 schema 变更必须有版本记录和向前迁移；生产回滚不得盲目回退数据库文件。
- 本地原子发布保留上一完整站点目录；生产部署保留上一镜像 digest 和站点版本。
- 生产 Compose 固定不可变镜像 digest，不使用 `latest` 或可被覆盖的 tag 作为唯一标识。
- 镜像和发布记录必须能追溯到 Git commit 与 `uv.lock`。

## 11. 资源目标

- 本地开发首先记录实际基线，不以服务器资源上限影响功能正确性测试。
- 生产 `news-web` 常驻内存目标低于 15 MB，初始上限 32 MB。
- 生产 `news-worker` 峰值内存目标低于 150 MB，初始上限 256 MB。
- 不使用 Chromium 或其他常驻浏览器参与生产生成流程。
- 不下载或代理存储新闻视频。
- 新闻图片优先使用来源提供的远程地址和明确的来源标识；本地 UI fixture 使用仓库内小型测试图片。
- 一年文字和索引归档目标低于 1 GB，阶段 6 根据样本量重新估算。

## 12. 待用户确认

以下项目不阻塞阶段 0；在对应阶段开始前确认即可：

- GitHub 私有仓库名称或 URL：已确认 `news-digest`（2026-07-26，滞后于原定的阶段 1 验收前）。
- 站点显示名称、默认主文章数量：阶段 1 前确认。
- 发布与发信时区：当前本地环境为 `Asia/Hong_Kong`，原服务器计划为 `Asia/Shanghai`；两者同为 UTC+8 无实际偏移差异，阶段 4 前指定其一写入配置即可。
- SUB2API base URL、模型、单次测试文章数和费用边界：阶段 3 真实调用前确认。
- SMTP 收件地址、发送时间和真实测试授权：阶段 5 前确认。
- 生产站点是否需要登录保护及初始用户名：阶段 7 前确认。
- 生产端模型切换面板：已确认（2026-07-26）采用「仅切换预置档案」方案——密钥经部署通道预置于服务器，网页面板只做供应商切换与模型名变更，不传输密钥；置于 HTTPS + 登录保护之后，阶段 7 实现。
- 服务器端 Docker Engine 和 Compose 版本、CPU 架构、权限及剩余资源：阶段 7 开始前核验。
- GHCR 镜像命名空间和服务器只读 token 的配置方式：阶段 7 前确认。

## 13. 进度记录

| 日期 | 阶段 | 状态 | 说明 |
|---|---|---|---|
| 2026-07-24 | 阶段 0 | 进行中 | 在服务器环境创建原始总计划和项目规则 |
| 2026-07-25 | 阶段 0 | 进行中 | 项目下载到 Windows 本地；确认 Python、uv、Git 和 GitHub CLI 可用，Docker 未安装；计划调整为本地开发、GitHub Actions 构建 GHCR 镜像、Linux 服务器仅拉取镜像部署 |
| 2026-07-26 | 阶段 0 | 进行中 | 用户确认生产架构维持 CI 构建镜像、服务器仅拉取部署；按本地开发环境完成计划修订：新增 Windows 编码/行尾/发布切换对策，统一 `var/site/current` 路径，CI Linux 测试兼作平台差异守门 |
| 2026-07-26 | 阶段 0 | 待验收 | 完成本地骨架：pyproject（hatchling、动态版本 0.1.0）、最小 CLI、单元测试、Ruff 配置、`.gitignore`/`.gitattributes`/`.python-version`/`.env.example`、`uv.lock`；Linux 沙箱验证 `uv sync`/pytest/ruff 全部通过；`git init -b main` 完成且 ignore 规则验证生效；首次提交与 GitHub 远程待用户执行 |
| 2026-07-26 | 阶段 0 | 已验收 | 用户放行进入阶段 1；产品决定确认：站点名 Cheapcoding News、主文章 6 篇、报纸编辑风、纯中文界面 |
| 2026-07-26 | 阶段 1 | 待验收 | 完成视觉原型：models/config/rendering/delivery/pipeline 与 `build` 子命令；今日首页、文章页、归档页模板；三种阅读模式、来源筛选、朗读控件、批注体译文与样张印章；两日期演示数据（6+2 篇、6 简讯）；14 项测试含端到端链接检查全部通过，沙箱 HTTP 预览关键页面均 200；`current` 切换在 Linux 为符号链接，Windows junction 路径待用户 pytest 实测 |
| 2026-07-26 | 阶段 1 | 已验收 | Windows 端 pytest 14 项全绿（junction 实测通过）、8618 端口预览确认；视觉两轮迭代后用户确认：印章方案被否（“俗”），改为抽象编辑风字组标（对照双栏隐喻）+ favicon，动效与页面质感获认可；`preview.bat` 作为日常预览入口；排查沉淀：PyPI 需阿里镜像、8080 被占固定用 8618 |
| 2026-07-26 | 阶段 2 | 进行中 | 开始真实新闻获取与正文提取；GitHub 远程与首次提交仍未补齐（已到期，等待用户提供 git 身份与仓库名） |
| 2026-07-26 | 阶段 2 | 待验收 | 完成：来源注册表（6 全文 + NYT 简讯，Reuters 因无公开 RSS 暂缓）、加固 HTTP 层（allowlist/私网阻断/重定向与大小上限）、feedparser 规范化与跟踪参数剥离（BBC/DW 真实样本实测）、三重去重、trafilatura+nh3 提取清洗、摘要降级、`fetch` 子命令与 var/data 落盘、EN-only 渲染兼容；42 项离线测试 + 7 项 network 冒烟（默认跳过）；ruff 干净 |
| 2026-07-26 | Git | 完成本地历史 | 配置身份 Yi-Lings；首次提交 `614cf46` 合并阶段 0-2 历史（远程补建较晚，单一提交入库，提交信息已注明）；仓库名确认 `news-digest`，推送与私有仓库创建由用户以 gh 执行；自阶段 3 起恢复阶段分支 + `phase-N-accepted` 标签流程 |
| 2026-07-26 | Git | 远程同步完成 | 账号下已存在服务器时期的同名旧仓库，采用覆盖方案：`614cf46` 强推至 `https://github.com/Yi-Lings/news-digest`（旧 `633e000` 被取代），`main` 已跟踪 `origin/main` |
| 2026-07-26 | 阶段 2 | 修复 | 用户首次真实抓取全源被阻断：本机代理为 fake-ip 模式（DNS 返回 198.18.0.0/15 假地址），私网阻断误拦。修复：`proxy_active` 检测显式/环境代理，代理生效时本地 DNS 公网校验交由代理（域名 allowlist 不变，生产无代理时防护完整）；pytest 临时目录迁至 `.pytest-tmp` 规避 Windows %TEMP% 清理崩溃；44 项离线测试全绿 |
| 2026-07-26 | 阶段 2 | 修复 | 第二次真实抓取成功（7/7 源、44 篇全文），抽样发现三类脏数据：BBC 页脚样板句、DW live 直播贴当文章、F24 视频页占位文案。修复：候选阶段按 URL 排除 live/视频/音频页，提取阶段过滤短样板句（正则 + 140 字符上限防误伤），全段重复视为提取失败转摘要；46 项离线测试全绿 |
| 2026-07-26 | 阶段 2 | 修复 | 第三次抓取数据复查：补排除 AJE `/news/liveblog/`、补剥 `traffic_source` 跟踪参数；样张印章与演示页脚改为仅 `--fixtures` 构建渲染，真实构建页脚为正式来源声明（`cb11b35`） |
| 2026-07-26 | 阶段 2 | 已验收 | 用户经 `daily.bat` 一键流程确认真实新闻页面（头条为柏林事件报道、无演示标识）；新增 `daily.bat`/`preview.bat` 一键脚本（用户多次漏跑 build 的流程教训固化为工具）；打 `phase-2-accepted` 标签，创建 `phase/3-translation` 分支 |
| 2026-07-26 | 阶段 3 | 进行中 | 开始 fixture 先行部分：模型输出 schema、严格校验、内容哈希+模型+prompt 版本缓存、`translate` 子命令（真实调用需 `--yes` 且先展示接口/模型/文章数/预计请求数）；SUB2API 接口、模型与费用边界待用户确认后才做受控真实调用 |
| 2026-07-26 | 阶段 3 | 修复×2 | 受控真实调用两轮排障：Claude 系后端必填 `max_tokens`（400 根因，`2daadb4`）；推理系模型拒绝 `temperature`，参数移除（`f6c5b32`）。错误信息带响应体片段便于诊断 |
| 2026-07-26 | 阶段 3 | 增补 | 本地模型供应商切换面板（用户要求，类 ccswitch）：`news-digest preview` 伺服站点 + `/admin/` 档案管理，启用即改写 `.env.local`；密钥仅本机、页面掩码；70 项测试（`bf5a54a`）。生产端面板列入阶段 7 待确认项 |
| 2026-07-26 | 阶段 3 | 真实调用跑通 | 排障链：网关 504（Nginx 读超时截断长文生成）→ 改流式 SSE 根治（`97f2353`）；逐篇进度输出 + 180s 超时 + Ctrl+C 续接（`8511417`）。用户验收译文质量通过，意见「文风更官方」→ prompt 升 p2（规范新闻书面语）+ `--redo` 单篇重翻（`cf64a35`）；71 项测试 |
| 2026-07-26 | 阶段 3 | 已验收 | 全量真实翻译 43/44 跑通（1 篇模型输出瑕疵 --redo 修复），p2 文风获认可；合并 main，`phase-3-accepted`（`cab25ef`） |
| 2026-07-26 | 阶段 4 | 待验收 | 并行子代理完成存储层（SQLite 文章池，抓取刷新不覆盖翻译）与选题层（时效+全文+篇幅评分、来源配额≤2、相似标题互斥、确定性）；集成 `run` 一键流水线（抓取→选题→仅译 6 篇→构建），溢出候选转双语外链简讯，旧 JSON 自动导入；`daily.bat` 即完整流水线；89 项离线测试（`ed30579`） |
| 2026-07-26 | 阶段 4 | 已验收 | 用户确认首页收敛（6 主文章 + 双语简讯）；合并 main，`phase-4-accepted` |
| 2026-07-26 | 阶段 5 | 待验收 | 用户曾指示砍掉阶段 5（邮件），实现中途干净回退；随后决定恢复。完成：双语摘要邮件（HTML+纯文本多部件）、`preview-email` 输出 .eml/.html 到 var/mail、`send-email` 三重门（--yes + 站点已含当日 + meta 防重记录，--resend 可越过）、SMTP 465 隐式 SSL / 587 STARTTLS、fake SMTP 测试；本地预览地址统一为 8618（含 NEWS_SITE_URL 默认值）；95 项离线测试 |
| 2026-07-26 | 阶段 5/6 | 已验收 | 阶段 5：SMTP 通道 `--smoke` 实测送达（554 根因=简报链接指向尚无证书的空子域，完整简报送达率待部署后复测）；阶段 6：用户批准 `0.6.0rc1` 为部署候选。两阶段合并 main 并打标；决定：生产站点公开访问（不启用 Basic Auth）|
| 2026-07-26 | 阶段 7 | 进行中 | 服务器 Docker 29.6.2 确认；SSH 仅公钥认证（沙箱代执行不可行，改为用户侧一键链）；子代理E交付 preflight.sh/bootstrap.sh/server-push.ps1；主线交付 deploy-all.ps1 + deploy.bat（推送→等 CI→注入 .env（密钥 Windows 直达服务器）→GHCR 登录（gh token 管道）→上传体检部署→冒烟）；定时器按用户要求定为每日 08:00 Asia/Shanghai |
| 2026-07-27 | 阶段 7 | 部署成功 | 首次部署排障三连：ssh-agent 需提权（脚本自提权 UAC）、一次推 6 个 tag 触发 GitHub 事件抑制（版本标签改单独推）、宿主 8080 被占（web 改绑 127.0.0.1:8618）。最终全链贯通：CI 构建推 GHCR、服务器拉取、web 起、worker 首刊（6 篇双语 + 49 简讯）、timer 就位（UTC 00:00 = 北京 08:00）、certbot 签发（至 2026-10-24 自动续期）、Nginx 接管、healthz 与公网 https 均 200；外网实测首页为 07-27 真实双语内容。preflight 的 IPv4/IPv6 出口对比误报待修正 |
| 2026-07-27 | 阶段 7 | 增补 | 一键 Docker 部署包（类 sub2api）：install.sh（远程下载 Release 附件或本地解包两模式）+ CI release-bundle 任务（拍平打包、显式 create/upload 分支）+ README 一键章节；QA 子代理审查出的 6 项问题（含 2 项阻断：包布局 vs bootstrap 扁平查找、preflight 端口角色颠倒）全部修复；随下一个 v* 标签自动发布附件 |
| 2026-07-26 | 阶段 6 | 进行中 | 用户指示并发推进。子代理C：docs/OPERATIONS.md 运维文档（日常/配置/踩坑/恢复/备份/重建，104 行）；子代理D：阶段 7 部署工件模板 8 件（Dockerfile×2、compose、systemd timer、nginx、GitHub Actions release、部署 README，全部固定版本+非 root+只读+内存上限，标注"服务器实测前不视为最终版"）；主线：`uv sync --locked` 通过、95→96 项测试、许可证核查（全为 BSD/MIT/Apache 宽松许可）、敏感信息扫描干净、fixture 构建 0.36s/峰值 50MB、实测单期站点 ~300KB 与 DB 400KB（年归档远低于 1GB 目标）、新增 releases 保留 5 版修剪、版本升至 `0.6.0rc1`；send-email 站点检查在特殊文件系统上的崩溃改为干净报错 |
