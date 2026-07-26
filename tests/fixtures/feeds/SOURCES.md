# 来源 fixture 说明

| 文件 | 来源 | 格式 | 样本性质 | 已验证要点 |
|---|---|---|---|---|
| bbc.xml | BBC News | RSS 2.0 | 真实样本（2026-07-26 抓取，裁剪 3 条） | CDATA 标题；`media:thumbnail` 图片；链接含 `at_medium/at_campaign` 跟踪参数需剥除；guid 非永久链接；RFC822 日期 |
| dw.xml | DW English | RDF/RSS 1.0 | 真实样本（2026-07-26 抓取，裁剪 2 条） | `dc:date` ISO 日期；链接含 `maca` 跟踪参数；条目无图片字段，需页面 og:image 兜底 |
| guardian.xml | The Guardian | RSS 2.0 | 依公开格式构造，待 network smoke 复核 | `media:content` 多尺寸图；`dc:creator` 作者 |
| npr.xml | NPR | RSS 2.0 | 依公开格式构造，待 network smoke 复核 | `content:encoded` 全文片段；`dc:creator` |
| aljazeera.xml | Al Jazeera English | RSS 2.0 | 依公开格式构造，待 network smoke 复核 | 基础字段为主，无条目图片 |
| france24.xml | France 24 English | RSS 2.0 | 依公开格式构造，待 network smoke 复核 | `enclosure` 图片附件 |
| nyt.xml | The New York Times | RSS 2.0 | 依公开格式构造，待 network smoke 复核 | `media:content` 图；仅作简讯（标题+链接），不抓正文 |

Reuters：公开 RSS 已停止提供；在确定合规聚合入口前不接入，接入前需补充本表条目与 fixture。

正文提取实现选择：trafilatura（阶段 2 决定，理由见 `src/news_digest/extractors/body.py` 模块注释）。

维护约定：`uv run pytest -m network` 可实测各来源连通性；来源格式变化时先更新本表与对应 fixture，再改适配逻辑。
