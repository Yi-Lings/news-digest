"""Command-line entry point."""

import argparse
from pathlib import Path

from news_digest import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="news-digest",
        description="Daily bilingual news digest generator for English learning.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    fetch = subparsers.add_parser(
        "fetch", help="抓取真实新闻源并保存当日候选（写入 var/data/fetched）"
    )
    fetch.add_argument(
        "--window-hours",
        type=int,
        default=None,
        metavar="N",
        help="覆盖抓取时间窗口（默认 24 小时或 NEWS_FETCH_WINDOW_HOURS）",
    )

    build = subparsers.add_parser(
        "build", help="生成静态站点：默认使用已抓取数据，--fixtures 使用演示数据"
    )
    build.add_argument(
        "--fixtures",
        metavar="DIR",
        default=None,
        help="演示数据目录，例如 tests/fixtures/demo",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "fetch":
        return _run_fetch(args.window_hours)
    if args.command == "build":
        return _run_build(args.fixtures)
    parser.print_help()
    return 0


def _run_fetch(window_hours: int | None) -> int:
    import dataclasses

    from news_digest.config import fetch_config_from_env
    from news_digest.pipeline import fetch_daily

    config = fetch_config_from_env()
    if window_hours is not None:
        config = dataclasses.replace(config, window_hours=window_hours)

    from news_digest.sources.http import proxy_active

    proxy_note = (
        "代理已生效，本地 DNS 公网校验交由代理处理"
        if proxy_active(config.proxy)
        else "未检测到代理，本地 DNS 公网校验生效"
    )
    print(f"抓取窗口：最近 {config.window_hours} 小时；时区：{config.timezone}；{proxy_note}")
    edition, report = fetch_daily(config)
    for source, status in report.per_source.items():
        print(f"  {source}: {status}")
    if edition is None:
        print("全部来源失败或窗口内无内容，未生成当日数据。")
        return 1
    print(
        f"完成：主文章 {report.articles} 篇（其中摘要降级 {report.degraded} 篇），"
        f"简讯 {report.briefs} 条 -> var/data/fetched/{edition.date}.json"
    )
    print("下一步：uv run news-digest build")
    return 0


def _run_build(fixtures: str | None) -> int:
    from news_digest.config import build_config_from_env, fetch_config_from_env
    from news_digest.pipeline import build_editions, build_site, load_fetched_editions

    config = build_config_from_env()
    if fixtures is not None:
        release = build_site(Path(fixtures), config)
    else:
        editions = load_fetched_editions(fetch_config_from_env().data_dir)
        release = build_editions(editions, config)
    print(f"构建完成：{release}")
    print(f"当前版本：{config.output_root / 'current'}")
    print("本地预览：双击 preview.bat")
    return 0
