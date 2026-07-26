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

    translate = subparsers.add_parser(
        "translate", help="翻译已抓取内容（默认只显示调用计划，--yes 才真实调用）"
    )
    translate.add_argument(
        "--date", default=None, metavar="YYYY-MM-DD", help="要翻译的日期，默认最新一期"
    )
    translate.add_argument(
        "--limit", type=int, default=None, metavar="N", help="本次最多翻译几篇（受控测试用）"
    )
    translate.add_argument(
        "--redo",
        action="append",
        default=[],
        metavar="SLUG",
        help="强制重翻指定文章（可多次使用），不受 --limit 约束",
    )
    translate.add_argument(
        "--yes", action="store_true", help="确认执行真实 API 调用（会产生费用）"
    )

    preview = subparsers.add_parser(
        "preview", help="本地预览站点并提供模型供应商切换面板（仅 127.0.0.1）"
    )
    preview.add_argument("--port", type=int, default=8618)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "fetch":
        return _run_fetch(args.window_hours)
    if args.command == "build":
        return _run_build(args.fixtures)
    if args.command == "translate":
        return _run_translate(args.date, args.limit, args.yes, frozenset(args.redo))
    if args.command == "preview":
        return _run_preview(args.port)
    parser.print_help()
    return 0


def _run_preview(port: int) -> int:
    from news_digest.config import build_config_from_env
    from news_digest.preview_server import create_server

    root = Path.cwd()
    site_dir = build_config_from_env().output_root / "current"
    if not (site_dir / "index.html").is_file():
        print(f"提示：{site_dir} 尚无站点，先运行 build（或双击 daily.bat）")
    print(f"站点预览：http://127.0.0.1:{port}/")
    print(f"模型设置：http://127.0.0.1:{port}/admin/")
    print("按 Ctrl+C 停止。")
    server = create_server(root, site_dir, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def _run_translate(
    date: str | None, limit: int | None, yes: bool, redo: frozenset[str]
) -> int:
    import json

    from news_digest.config import (
        fetch_config_from_env,
        load_env_file,
        translation_config_from_env,
    )
    from news_digest.translation.client import ApiTranslator, TranslationError
    from news_digest.translation.service import translate_edition

    load_env_file()
    data_dir = fetch_config_from_env().data_dir
    fetched_dir = data_dir / "fetched"
    if date is None:
        paths = sorted(fetched_dir.glob("*.json"))
        if not paths:
            print(f"未在 {fetched_dir} 找到抓取数据；先运行 news-digest fetch")
            return 1
        path = paths[-1]
    else:
        path = fetched_dir / f"{date}.json"
        if not path.is_file():
            print(f"未找到 {path}")
            return 1

    from news_digest.models import edition_from_dict, edition_to_dict

    payload = json.loads(path.read_text(encoding="utf-8"))
    edition = edition_from_dict(payload["edition"])
    known_slugs = {a.slug for a in edition.articles}
    unknown = sorted(redo - known_slugs)
    if unknown:
        print(f"--redo 中不存在的 slug：{', '.join(unknown)}")
        return 1
    pending = [a for a in edition.articles if not a.translated_by and a.slug not in redo]
    planned = (len(pending) if limit is None else min(limit, len(pending))) + len(redo)
    config = translation_config_from_env()

    print(f"日期：{edition.date}；文章 {len(edition.articles)} 篇，其中未翻译 {len(pending)} 篇")
    if redo:
        print(f"强制重翻：{', '.join(sorted(redo))}")
    print(f"接口：{config.base_url or '（未配置）'}；模型：{config.model or '（未配置）'}")
    print(f"本次计划翻译：{planned} 篇；预计 API 请求 {planned} 次（缓存命中会减少）")
    if not yes:
        print("当前为预览模式，未产生任何调用。确认无误后加 --yes 执行。")
        return 0

    try:
        translator = ApiTranslator(config)
    except TranslationError as error:
        print(str(error))
        return 1
    try:
        updated, report = translate_edition(
            edition, translator, config.cache_dir, limit=limit, on_progress=print, redo=redo
        )
    except KeyboardInterrupt:
        print("\n已中断。已成功的篇目在缓存中，重跑同一命令会瞬时续接。")
        return 130
    finally:
        translator.close()

    payload["edition"] = edition_to_dict(updated)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(
        f"完成：成功 {report.succeeded} 篇（缓存命中 {report.cache_hits}），"
        f"API 请求 {report.api_calls} 次，失败 {report.failed} 篇，"
        f"此前已翻译 {report.already_done} 篇"
    )
    for slug, reason in report.failures:
        print(f"  失败 {slug}: {reason}")
    print("下一步：uv run news-digest build（或双击 preview.bat）")
    return 0 if report.failed == 0 else 1


def _run_fetch(window_hours: int | None) -> int:
    import dataclasses

    from news_digest.config import fetch_config_from_env, load_env_file
    from news_digest.pipeline import fetch_daily

    load_env_file()
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
