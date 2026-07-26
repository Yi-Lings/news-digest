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
        "fetch", help="抓取真实新闻源并入库（同时写 var/data/fetched 快照）"
    )
    fetch.add_argument(
        "--window-hours",
        type=int,
        default=None,
        metavar="N",
        help="覆盖抓取时间窗口（默认 24 小时或 NEWS_FETCH_WINDOW_HOURS）",
    )

    build = subparsers.add_parser(
        "build", help="生成静态站点：默认使用数据库版次，--fixtures 使用演示数据"
    )
    build.add_argument(
        "--fixtures",
        metavar="DIR",
        default=None,
        help="演示数据目录，例如 tests/fixtures/demo",
    )

    translate = subparsers.add_parser(
        "translate", help="翻译当日选题主文章（默认只显示计划，--yes 才真实调用）"
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
        help="强制重翻指定主文章（可多次使用），不受 --limit 约束",
    )
    translate.add_argument(
        "--yes", action="store_true", help="确认执行真实 API 调用（会产生费用）"
    )

    run = subparsers.add_parser(
        "run", help="完整每日流水线：抓取→选题→翻译（需 --yes）→构建"
    )
    run.add_argument("--window-hours", type=int, default=None, metavar="N")
    run.add_argument(
        "--yes", action="store_true", help="包含真实翻译调用；缺省只做抓取+选题+构建"
    )

    preview = subparsers.add_parser(
        "preview", help="本地预览站点并提供模型供应商切换面板（仅 127.0.0.1）"
    )
    preview.add_argument("--port", type=int, default=8618)

    preview_email = subparsers.add_parser(
        "preview-email", help="生成当日简报邮件预览（.eml + .html 到 var/mail，不联网）"
    )
    preview_email.add_argument("--date", default=None, metavar="YYYY-MM-DD")

    send_email = subparsers.add_parser(
        "send-email", help="发送当日简报（需 --yes；站点已生成且当日未发送过）"
    )
    send_email.add_argument("--date", default=None, metavar="YYYY-MM-DD")
    send_email.add_argument(
        "--resend", action="store_true", help="忽略防重记录，强制再次发送"
    )
    send_email.add_argument(
        "--yes", action="store_true", help="确认真实发送（缺省只显示发送计划）"
    )
    send_email.add_argument(
        "--smoke",
        action="store_true",
        help="只发一封无链接的最小测试信，验证 SMTP 通道（不检查站点、不写防重记录）",
    )

    admin = subparsers.add_parser(
        "admin", help="生产模型切换面板（仅 127.0.0.1；密钥不经网页，需外层反代加认证）"
    )
    admin.add_argument("--port", type=int, default=8619)
    admin.add_argument(
        "--config-dir",
        default="/config",
        metavar="DIR",
        help="含 .env 与 providers.json 的目录（容器内通常为 /config）",
    )
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
    if args.command == "run":
        return _run_daily(args.window_hours, args.yes)
    if args.command == "preview":
        return _run_preview(args.port)
    if args.command == "preview-email":
        return _run_preview_email(args.date)
    if args.command == "admin":
        return _run_admin(args.port, Path(args.config_dir))
    if args.command == "send-email":
        if args.smoke:
            return _run_send_smoke(args.yes)
        return _run_send_email(args.date, args.resend, args.yes)
    parser.print_help()
    return 0


def _email_payload(date: str | None):
    """定位日期与选题后版次，渲染邮件三件套；失败打印原因返回 None。"""
    from news_digest.config import build_config_from_env
    from news_digest.rendering.email import render_email

    fetch_config = _fetch_config(None)
    located = _translate_edition_for(date, fetch_config)
    if located is None:
        return None
    date, edition = located
    subject, text, html = render_email(edition, build_config_from_env().site_url)
    return fetch_config, date, edition, subject, text, html


def _run_preview_email(date: str | None) -> int:
    from news_digest.delivery.mailer import compose, write_eml

    payload = _email_payload(date)
    if payload is None:
        return 1
    fetch_config, date, _, subject, text, html = payload
    message = compose(subject, text, html, "preview@localhost", ("preview@localhost",))
    mail_dir = Path("var/mail")
    eml_path = write_eml(message, mail_dir, date)
    html_path = mail_dir / f"{date}.html"
    html_path.write_text(html, encoding="utf-8")
    print(f"主题：{subject}")
    print(f"邮件预览：{eml_path}（邮件客户端打开）")
    print(f"网页预览：{html_path}（浏览器打开看排版）")
    return 0


def _run_send_smoke(yes: bool) -> int:
    from news_digest.config import load_env_file, smtp_config_from_env
    from news_digest.delivery.mailer import MailError, compose, send, validate_smtp

    load_env_file()
    smtp = smtp_config_from_env()
    try:
        validate_smtp(smtp)
    except MailError as error:
        print(str(error))
        return 1
    print(f"服务器：{smtp.host}:{smtp.port}；收件人 {len(smtp.recipients)} 个")
    if not yes:
        print("通道测试预览模式；加 --yes 真实发送一封无链接测试信。")
        return 0
    message = compose(
        "Cheapcoding News 投递通道测试",
        "这是一封投递通道测试邮件。收到即说明 SMTP 配置与发信域名工作正常。",
        "<p>这是一封投递通道测试邮件。收到即说明 SMTP 配置与发信域名工作正常。</p>",
        smtp.sender,
        smtp.recipients,
    )
    try:
        send(message, smtp)
    except MailError as error:
        print(str(error))
        return 1
    print("通道测试邮件已发出，请检查收件箱与垃圾箱。")
    return 0


def _run_send_email(date: str | None, resend: bool, yes: bool) -> int:
    import datetime

    from news_digest.config import build_config_from_env, smtp_config_from_env
    from news_digest.delivery.mailer import MailError, compose, send, validate_smtp, write_eml
    from news_digest.storage import db

    payload = _email_payload(date)
    if payload is None:
        return 1
    fetch_config, date, _, subject, text, html = payload

    issues_dir = build_config_from_env().output_root / "current" / "issues" / date
    try:
        site_ready = issues_dir.is_dir()
    except OSError:  # current 链接在某些文件系统上不可遍历
        site_ready = False
    if not site_ready:
        print(f"站点尚未包含 {date}（{issues_dir} 不可达）；先运行 build 再发送。")
        return 1

    smtp = smtp_config_from_env()
    try:
        validate_smtp(smtp)
    except MailError as error:
        print(str(error))
        return 1

    conn = db.connect(fetch_config.database)
    try:
        sent = db.sent_detail(conn, date)
        if sent and not resend:
            print(f"{date} 已发送过（{sent}）；确需重发请加 --resend。")
            return 1
        print(f"主题：{subject}")
        print(f"服务器：{smtp.host}:{smtp.port}；收件人 {len(smtp.recipients)} 个")
        if not yes:
            print("当前为预览模式，未发送。确认无误后加 --yes 执行。")
            return 0
        message = compose(subject, text, html, smtp.sender, smtp.recipients)
        try:
            send(message, smtp)
        except MailError as error:
            print(str(error))
            return 1
        stamp = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
        db.mark_sent(conn, date, f"{stamp} -> {len(smtp.recipients)} 人")
        write_eml(message, Path("var/mail"), date)
        print(f"已发送，并归档 var/mail/{date}.eml；重复执行将被防重记录拦截。")
        return 0
    finally:
        conn.close()


def _fetch_config(window_hours: int | None):
    import dataclasses

    from news_digest.config import fetch_config_from_env, load_env_file

    load_env_file()
    config = fetch_config_from_env()
    if window_hours is not None:
        config = dataclasses.replace(config, window_hours=window_hours)
    return config


def _print_fetch(config, report) -> None:
    from news_digest.sources.http import proxy_active

    proxy_note = (
        "代理已生效，本地 DNS 公网校验交由代理处理"
        if proxy_active(config.proxy)
        else "未检测到代理，本地 DNS 公网校验生效"
    )
    print(f"抓取窗口：最近 {config.window_hours} 小时；时区：{config.timezone}；{proxy_note}")
    for source, status in report.per_source.items():
        print(f"  {source}: {status}")


def _run_fetch(window_hours: int | None) -> int:
    from news_digest.pipeline import fetch_daily

    config = _fetch_config(window_hours)
    edition, report = fetch_daily(config)
    _print_fetch(config, report)
    if edition is None:
        print("全部来源失败或窗口内无内容，未生成当日数据。")
        return 1
    print(
        f"完成：入库主文章 {report.articles} 篇（摘要降级 {report.degraded} 篇），"
        f"简讯 {report.briefs} 条；日期 {edition.date}"
    )
    print("下一步：uv run news-digest run --yes（或分步 translate + build）")
    return 0


def _run_build(fixtures: str | None) -> int:
    from news_digest.config import build_config_from_env, load_env_file
    from news_digest.pipeline import build_editions, build_site, load_db_editions

    load_env_file()
    config = build_config_from_env()
    if fixtures is not None:
        release = build_site(Path(fixtures), config)
    else:
        from news_digest.config import fetch_config_from_env

        release = build_editions(load_db_editions(fetch_config_from_env()), config)
    print(f"构建完成：{release}")
    print("本地预览：双击 preview.bat")
    return 0


def _translate_edition_for(date: str | None, config) -> tuple[str, object] | None:
    from news_digest.pipeline import latest_db_date, selected_mains_for_translation

    date = date or latest_db_date(config)
    if date is None:
        print("数据库无内容；先运行 news-digest fetch")
        return None
    edition = selected_mains_for_translation(config, date)
    if edition is None or not edition.articles:
        print(f"{date} 没有可翻译的主文章")
        return None
    return date, edition


def _run_translate(
    date: str | None, limit: int | None, yes: bool, redo: frozenset[str]
) -> int:
    from news_digest.config import translation_config_from_env
    from news_digest.pipeline import store_translated
    from news_digest.translation.client import ApiTranslator, TranslationError
    from news_digest.translation.service import translate_edition

    fetch_config = _fetch_config(None)
    located = _translate_edition_for(date, fetch_config)
    if located is None:
        return 1
    date, edition = located

    slugs = {a.slug for a in edition.articles}
    unknown = sorted(redo - slugs)
    if unknown:
        print(f"--redo 中不在当日主文章之列：{', '.join(unknown)}")
        return 1
    pending = [a for a in edition.articles if not a.translated_by and a.slug not in redo]
    planned = (len(pending) if limit is None else min(limit, len(pending))) + len(redo)
    config = translation_config_from_env()

    print(f"日期：{date}；主文章 {len(edition.articles)} 篇，其中未翻译 {len(pending)} 篇")
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

    store_translated(fetch_config, date, updated.articles)
    print(
        f"完成：成功 {report.succeeded} 篇（缓存命中 {report.cache_hits}），"
        f"API 请求 {report.api_calls} 次，失败 {report.failed} 篇，"
        f"此前已翻译 {report.already_done} 篇"
    )
    for slug, reason in report.failures:
        print(f"  失败 {slug}: {reason}")
    print("下一步：uv run news-digest build（或双击 preview.bat）")
    return 0 if report.failed == 0 else 1


def _run_daily(window_hours: int | None, yes: bool) -> int:
    from news_digest.config import build_config_from_env, translation_config_from_env
    from news_digest.pipeline import (
        build_editions,
        fetch_daily,
        load_db_editions,
        store_translated,
    )
    from news_digest.translation.client import ApiTranslator, TranslationError
    from news_digest.translation.service import translate_edition

    fetch_config = _fetch_config(window_hours)

    print("[1/3] 抓取")
    edition, report = fetch_daily(fetch_config)
    _print_fetch(fetch_config, report)
    if edition is None:
        print("抓取无结果；继续用数据库既有内容构建。")

    print("[2/3] 翻译")
    exit_code = 0
    if not yes:
        print("未加 --yes：跳过翻译，主文章将以英文原文成刊。")
    else:
        located = _translate_edition_for(None, fetch_config)
        if located is None:
            return 1
        date, mains = located
        try:
            translator = ApiTranslator(translation_config_from_env())
        except TranslationError as error:
            print(f"{error}；跳过翻译。")
        else:
            try:
                updated, t_report = translate_edition(
                    mains,
                    translator,
                    translation_config_from_env().cache_dir,
                    on_progress=print,
                )
            except KeyboardInterrupt:
                print("\n翻译被中断，改以当前状态成刊。")
                updated, t_report = mains, None
            finally:
                translator.close()
            store_translated(fetch_config, date, updated.articles)
            if t_report is not None:
                print(
                    f"翻译：成功 {t_report.succeeded}（缓存 {t_report.cache_hits}），"
                    f"失败 {t_report.failed}，此前已译 {t_report.already_done}"
                )
                if t_report.failed:
                    exit_code = 1

    print("[3/3] 构建")
    release = build_editions(load_db_editions(fetch_config), build_config_from_env())
    print(f"构建完成：{release}")
    print("本地预览：双击 preview.bat")
    return exit_code


def _run_admin(port: int, config_dir: Path) -> int:
    from news_digest.preview_server import create_server

    if not config_dir.is_dir():
        print(f"配置目录不存在：{config_dir}")
        return 1
    print(f"生产模型切换面板：http://127.0.0.1:{port}/admin/（密钥不经网页传输）")
    server = create_server(
        config_dir,
        config_dir,
        port,
        env_file=".env",
        profiles_file="providers.json",
        allow_key_input=False,
        serve_static=False,  # /config 含明文密钥，绝不提供静态文件回落
        htpasswd_file=config_dir / "htpasswd-admin",
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
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
