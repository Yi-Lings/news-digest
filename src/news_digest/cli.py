"""Command-line entry point."""

import argparse
import os
import time
import types
from pathlib import Path

from news_digest import __version__

_AUTOMATION_ACTION_REQUIRED = 10


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
    translate.add_argument("--yes", action="store_true", help="确认执行真实 API 调用（会产生费用）")

    run = subparsers.add_parser("run", help="完整每日流水线：抓取→选题→翻译（需 --yes）→构建→投递")
    run.add_argument("--window-hours", type=int, default=None, metavar="N")
    run.add_argument("--yes", action="store_true", help="包含真实翻译调用；缺省只做抓取+选题+构建")

    resume = subparsers.add_parser(
        "resume-automation", help="恢复数据库中未完成的自动化刊期（不重新抓取；需 --yes）"
    )
    resume.add_argument("--yes", action="store_true", help="确认执行真实翻译与后续构建/投递")

    import_edition = subparsers.add_parser(
        "import-edition", help="把一期版次 JSON 併入数据库（翻译成果原样保留，幂等）"
    )
    import_edition.add_argument("file", metavar="FILE", help="fetched 快照或裸版次 JSON 文件路径")

    preview = subparsers.add_parser(
        "preview", help="本地预览站点并提供模型供应商切换面板（仅 127.0.0.1）"
    )
    preview.add_argument("--port", type=int, default=8618)
    preview.add_argument(
        "--automation-demo",
        action="store_true",
        help="启用隔离的阶段 8 fake automation 状态（不调用 provider 或 SMTP）",
    )

    preview_email = subparsers.add_parser(
        "preview-email", help="生成当日简报邮件预览（.eml + .html 到 var/mail，不联网）"
    )
    preview_email.add_argument("--date", default=None, metavar="YYYY-MM-DD")

    send_email = subparsers.add_parser(
        "send-email", help="发送当日简报（需 --yes；站点已生成且当日未发送过）"
    )
    send_email.add_argument("--date", default=None, metavar="YYYY-MM-DD")
    send_email.add_argument(
        "--resend", action="store_true", help="兼容别名：仅重试 failed，不重发 sent"
    )
    send_email.add_argument(
        "--retry-unknown",
        action="store_true",
        help="重试可能已送达的 unknown（必须同时加 --confirm-unknown-risk）",
    )
    send_email.add_argument(
        "--confirm-unknown-risk",
        action="store_true",
        help="确认 unknown 重试可能产生重复邮件",
    )
    send_email.add_argument("--yes", action="store_true", help="确认真实发送（缺省只显示发送计划）")
    send_email.add_argument(
        "--smoke",
        action="store_true",
        help="发送带[测试]标识的当前已发布刊物给 saved Admin 收件人（不写正式状态）",
    )

    admin = subparsers.add_parser(
        "admin", help="生产模型切换面板（仅 127.0.0.1；登录页认证，经外层反代 HTTPS 暴露）"
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
    if args.command == "resume-automation":
        return _run_automation_resume(args.yes)
    if args.command == "preview":
        return _run_preview(args.port, automation_demo=args.automation_demo)
    if args.command == "preview-email":
        return _run_preview_email(args.date)
    if args.command == "admin":
        return _run_admin(args.port, Path(args.config_dir))
    if args.command == "import-edition":
        return _run_import(Path(args.file))
    if args.command == "send-email":
        if args.smoke:
            return _run_send_smoke(args.yes)
        return _run_send_email(
            args.date,
            args.resend,
            args.yes,
            retry_unknown=args.retry_unknown,
            confirm_unknown=args.confirm_unknown_risk,
        )
    parser.print_help()
    return 0


def _email_payload(date: str | None):
    """Load one retained immutable release manifest and render its configured preview."""
    from news_digest.config import build_config_from_env, fetch_config_from_env
    from news_digest.delivery.delivery_service import (
        DeliveryServiceError,
        preview_published,
    )

    build_config = build_config_from_env()
    fetch_config = fetch_config_from_env()
    try:
        preview = preview_published(
            output_root=build_config.output_root,
            database=fetch_config.database,
            site_url=build_config.site_url,
            edition_date=date,
        )
    except (DeliveryServiceError, ValueError) as error:
        print(str(error))
        return None
    rendered = preview.rendered
    return (
        fetch_config,
        preview.release.release_date,
        preview.release.edition,
        rendered.subject,
        rendered.text,
        rendered.html,
    )


def _run_preview_email(date: str | None) -> int:
    from news_digest.config import load_env_file
    from news_digest.delivery.mailer import compose, write_eml

    load_env_file()
    payload = _email_payload(date)
    if payload is None:
        return 1
    fetch_config, date, _, subject, text, html = payload
    message = compose(
        subject,
        text,
        html,
        "preview@invalid.example",
        ("preview@invalid.example",),
    )
    mail_dir = Path("var/mail")
    eml_path = write_eml(message, mail_dir, date)
    html_path = mail_dir / f"{date}.html"
    html_path.write_text(html, encoding="utf-8")
    print(f"主题：{subject}")
    print(f"邮件预览：{eml_path}（邮件客户端打开）")
    print(f"网页预览：{html_path}（浏览器打开看排版）")
    return 0


def _run_send_smoke(yes: bool) -> int:
    from news_digest.config import (
        build_config_from_env,
        fetch_config_from_env,
        load_env_file,
        smtp_config_from_env,
    )
    from news_digest.delivery.delivery_service import (
        DeliveryServiceError,
        deliver_published,
        preview_published,
    )

    load_env_file()
    build_config = build_config_from_env()
    fetch_config = fetch_config_from_env()
    try:
        smtp = smtp_config_from_env()
        preview = preview_published(
            output_root=build_config.output_root,
            database=fetch_config.database,
            site_url=build_config.site_url,
            smtp_config=smtp,
            test=True,
        )
    except (DeliveryServiceError, ValueError) as error:
        print(str(error))
        return 1
    print(
        f"测试刊期：{preview.release.release_name}；服务器：{smtp.host}:{smtp.port}；"
        f"已保存收件人 {len(smtp.recipients)} 个"
    )
    if not yes:
        print("测试邮件预览模式；加 --yes 后仅发送给已保存 Admin 收件人，不写正式状态。")
        return 0
    try:
        report = deliver_published(
            "test",
            output_root=build_config.output_root,
            database=fetch_config.database,
            site_url=build_config.site_url,
            timezone=fetch_config.timezone,
            smtp_config=smtp,
        )
    except (DeliveryServiceError, ValueError) as error:
        print(str(error))
        return 1
    print(
        f"测试邮件：成功 {report.sent_count}，失败 {report.failed_count}，"
        f"unknown {report.unknown_count}；不污染正式投递状态。"
    )
    return 0 if report.succeeded else 1


def _run_send_email(
    date: str | None,
    resend: bool,
    yes: bool,
    *,
    retry_unknown: bool = False,
    confirm_unknown: bool = False,
) -> int:
    from news_digest.config import (
        build_config_from_env,
        fetch_config_from_env,
        load_env_file,
        smtp_config_from_env,
    )
    from news_digest.delivery.delivery_service import (
        DeliveryServiceError,
        deliver_published,
        preview_published,
    )

    load_env_file()
    if retry_unknown and resend:
        print("--resend 与 --retry-unknown 不能同时使用。")
        return 1
    if confirm_unknown and not retry_unknown:
        print("--confirm-unknown-risk 只能与 --retry-unknown 同时使用。")
        return 1
    build_config = build_config_from_env()
    fetch_config = fetch_config_from_env()
    try:
        smtp = smtp_config_from_env()
        preview = preview_published(
            output_root=build_config.output_root,
            database=fetch_config.database,
            site_url=build_config.site_url,
            smtp_config=smtp,
            edition_date=date,
        )
    except (DeliveryServiceError, ValueError) as error:
        print(str(error))
        return 1

    mode = "retry_unknown" if retry_unknown else "retry_failed" if resend else "manual"
    print(f"主题：{preview.rendered.subject}")
    print(
        f"刊期：{preview.release.release_name}；服务器：{smtp.host}:{smtp.port}；"
        f"已保存收件人 {len(smtp.recipients)} 个"
    )
    if retry_unknown and not confirm_unknown:
        print("unknown 可能已由 SMTP 接受；重试有重复风险，需加 --confirm-unknown-risk。")
        return 1
    if not yes:
        print("当前为预览模式，未发送。确认无误后加 --yes 执行。")
        return 0
    try:
        report = deliver_published(
            mode,
            output_root=build_config.output_root,
            database=fetch_config.database,
            site_url=build_config.site_url,
            timezone=fetch_config.timezone,
            smtp_config=smtp,
            edition_date=date,
            confirm_unknown=confirm_unknown,
        )
    except (DeliveryServiceError, ValueError) as error:
        print(str(error))
        return 1
    print(
        f"投递结果：成功 {report.sent_count}，失败 {report.failed_count}，"
        f"unknown {report.unknown_count}，跳过 {report.skipped_count}；"
        f"归档 {report.archive_status}"
    )
    return 0 if report.succeeded else 1


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


def _runtime_translation_config():
    profiles_file = os.environ.get("TRANSLATION_PROVIDERS_FILE", "").strip()
    from news_digest.admin_providers import PROFILES_FILE, runtime_translation_config

    if not profiles_file:
        profiles_file = PROFILES_FILE

    return runtime_translation_config(Path(profiles_file), os.environ)


def _run_translate(date: str | None, limit: int | None, yes: bool, redo: frozenset[str]) -> int:
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
    try:
        config = _runtime_translation_config()
    except ValueError as error:
        print(f"翻译接口配置错误：{error}")
        return 1

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
    from news_digest.config import (
        build_config_from_env,
        email_delivery_enabled_from_env,
        smtp_config_from_env,
    )
    from news_digest.delivery.delivery_service import (
        DeliveryServiceError,
        deliver_published,
    )
    from news_digest.pipeline import (
        build_editions,
        fetch_daily,
        load_db_editions,
        store_translated,
    )
    from news_digest.translation.client import ApiTranslator, TranslationError
    from news_digest.translation.service import translate_edition

    fetch_config = _fetch_config(window_hours)

    print("[1/4] 抓取")
    edition, report = fetch_daily(fetch_config)
    _print_fetch(fetch_config, report)
    if edition is None:
        print("抓取无结果；继续用数据库既有内容构建。")

    if yes:
        return _run_automation_daily(fetch_config, edition)

    print("[2/4] 翻译")
    exit_code = 0
    if not yes:
        print("未加 --yes：跳过翻译，主文章将以英文原文成刊。")
    else:
        located = _translate_edition_for(None, fetch_config)
        if located is None:
            exit_code = 1
        else:
            date, mains = located
            try:
                translation_config = _runtime_translation_config()
                translator = ApiTranslator(translation_config)
            except (TranslationError, ValueError) as error:
                print(f"{error}；跳过翻译。")
                exit_code = 1
            else:
                try:
                    updated, t_report = translate_edition(
                        mains,
                        translator,
                        translation_config.cache_dir,
                        on_progress=print,
                    )
                except KeyboardInterrupt:
                    print("\n翻译被中断，改以当前状态成刊。")
                    exit_code = 1
                else:
                    store_translated(fetch_config, date, updated.articles)
                    print(
                        f"翻译：成功 {t_report.succeeded}（缓存 {t_report.cache_hits}），"
                        f"失败 {t_report.failed}，此前已译 {t_report.already_done}"
                    )
                    if t_report.failed:
                        exit_code = 1
                finally:
                    translator.close()

    print("[3/4] 构建")
    build_config = build_config_from_env()
    try:
        release = build_editions(load_db_editions(fetch_config), build_config)
    except Exception as error:
        print(f"构建失败：{error}")
        print("[4/4] 投递：构建未成功，未发送邮件。")
        return 1
    print(f"构建完成：{release}")

    print("[4/4] 投递")
    try:
        delivery_enabled = email_delivery_enabled_from_env()
    except ValueError as error:
        print(f"邮件配置错误：{error}")
        return 1
    if not delivery_enabled:
        print("邮件未启用，已跳过。")
        print("本地预览：双击 preview.bat")
        return exit_code
    try:
        smtp = smtp_config_from_env()
    except ValueError as error:
        print(f"邮件配置错误：{error}")
        return 1
    try:
        delivery = deliver_published(
            "auto",
            output_root=build_config.output_root,
            database=fetch_config.database,
            site_url=build_config.site_url,
            timezone=fetch_config.timezone,
            smtp_config=smtp,
            just_built_release_name=release.name,
        )
    except (DeliveryServiceError, ValueError) as error:
        print(f"邮件投递失败：{error}")
        return 1
    print(
        f"邮件投递：成功 {delivery.sent_count}，失败 {delivery.failed_count}，"
        f"unknown {delivery.unknown_count}，跳过 {delivery.skipped_count}；"
        f"归档 {delivery.archive_status}"
    )
    print("本地预览：双击 preview.bat")
    return exit_code if delivery.succeeded else 1


def _run_automation_daily(
    fetch_config,
    fetched_edition,
    *,
    clock=None,
    sleep=None,
) -> int:
    import datetime as dt
    import time

    from news_digest.config import (
        build_config_from_env,
        email_delivery_enabled_from_env,
        smtp_config_from_env,
    )
    from news_digest.delivery.delivery_service import (
        DeliveryServiceError,
        deliver_published,
    )
    from news_digest.models import DailyEdition
    from news_digest.pipeline import (
        build_editions,
        latest_db_date,
        load_db_editions,
        selected_mains_for_translation,
    )
    from news_digest.storage import db
    from news_digest.translation.automation import TranslationAutomationRunner
    from news_digest.translation.client import ApiTranslator, TranslationError

    date = getattr(fetched_edition, "date", None) or latest_db_date(fetch_config)
    if date is None:
        print("数据库无内容；无法创建逐篇翻译任务。")
        return 1
    edition = selected_mains_for_translation(fetch_config, date)
    if edition is None or not edition.articles:
        print(f"{date} 没有可翻译的主文章")
        return 1
    try:
        translation_config = _runtime_translation_config()
        translator = ApiTranslator(translation_config)
        delivery_enabled = email_delivery_enabled_from_env()
        smtp = smtp_config_from_env() if delivery_enabled else None
    except (TranslationError, ValueError) as error:
        print(f"自动化配置错误：{error}")
        return _AUTOMATION_ACTION_REQUIRED

    build_config = build_config_from_env()

    def build_callback(edition_date: str) -> str:
        editions = load_db_editions(fetch_config)
        visible_editions = []
        for current in editions:
            if current.date != edition_date:
                visible_editions.append(current)
                continue
            visible = [article for article in current.articles if article.translated_by]
            visible_editions.append(
                DailyEdition(
                    date=current.date,
                    articles=visible,
                    briefs=current.briefs,
                )
            )
        release = build_editions(visible_editions, build_config)
        print(f"增量构建完成：{release.name}")
        return release.name

    def delivery_callback(edition_date: str, delivery_key: str) -> bool:
        del delivery_key
        report = deliver_published(
            "auto",
            output_root=build_config.output_root,
            database=fetch_config.database,
            site_url=build_config.site_url,
            timezone=fetch_config.timezone,
            smtp_config=smtp,
            edition_date=edition_date,
        )
        print(
            f"自动投递：成功 {report.sent_count}，失败 {report.failed_count}，"
            f"unknown {report.unknown_count}，跳过 {report.skipped_count}"
        )
        error_category = getattr(report, "error_category", None)
        if error_category:
            print(f"自动投递错误分类：{error_category}")
        return report.succeeded or report.status == "skipped"

    clock = clock or (lambda: dt.datetime.now(dt.UTC))
    sleep = sleep or time.sleep
    runner = TranslationAutomationRunner(
        database=fetch_config.database,
        provider_id=f"default-{translator.cache_identity[:64]}",
        translator=translator,
        cache_dir=translation_config.cache_dir,
        build_callback=build_callback,
        delivery_callback=delivery_callback,
        clock=clock,
    )
    owner = f"daily-{os.getpid()}"
    recovery_conn = db.connect(fetch_config.database)
    try:
        # The daily service is protected by the same worker flock as resume;
        # stale leases can therefore be recovered before scheduling new work.
        db.recover_interrupted_translation_tasks(
            recovery_conn,
            now=clock().astimezone(dt.UTC).isoformat(),
            process_terminated=True,
        )
    finally:
        recovery_conn.close()
    runner.seed_edition(edition, now=clock())
    print(f"[2/4] 逐篇翻译自动化：{date}，任务 {len(edition.articles)} 篇")
    try:
        while True:
            now = clock()
            result = runner.run_ready(now=now, owner=owner, max_tasks=1)
            built = runner.flush_build(now=clock(), owner=owner)
            delivered = runner.flush_delivery(edition_date=date, now=clock())
            conn = db.connect(fetch_config.database)
            try:
                state = db.automation_edition(conn, date)
                tasks = db.list_translation_tasks(conn, date)
            finally:
                conn.close()
            if state is not None and state.status == "delivered":
                return 0
            if any(
                task.status == "configuration_blocked"
                or (task.status == "failed" and not task.auto_retry)
                for task in tasks
            ):
                print("自动化已安全停止：存在需要人工处理的翻译任务。")
                return _AUTOMATION_ACTION_REQUIRED
            if (
                delivery_enabled
                and state is not None
                and state.status == "complete"
                and not delivered
            ):
                print("自动投递失败；保留持久状态，未重复投递。")
                return _AUTOMATION_ACTION_REQUIRED
            if state is not None and state.status == "build_failed" and not built:
                print("增量构建失败；旧站点保持不变。")
                return _AUTOMATION_ACTION_REQUIRED
            if not (result.claimed or built or delivered):
                sleep(1.0)
    except (DeliveryServiceError, KeyboardInterrupt, ValueError) as error:
        print(f"自动化已停止：{error}")
        return 130 if isinstance(error, KeyboardInterrupt) else _AUTOMATION_ACTION_REQUIRED
    finally:
        translator.close()


def _run_automation_resume(yes: bool) -> int:
    import datetime as dt

    if not yes:
        print("未加 --yes：未恢复自动化任务，也未调用 provider 或 SMTP。")
        return 0

    fetch_config = _fetch_config(None)
    from news_digest.storage import db

    conn = db.connect(fetch_config.database)
    try:
        # The systemd flock guarantees the previous worker is no longer
        # running before this process takes ownership of stale leases.
        db.recover_interrupted_translation_tasks(
            conn,
            now=dt.datetime.now(dt.UTC).isoformat(),
            process_terminated=True,
        )
        unfinished = db.unfinished_automation_edition_dates(conn)
    finally:
        conn.close()
    if not unfinished:
        print("没有未完成的自动化刊期；无需恢复。")
        return 0
    return _run_automation_daily(
        fetch_config,
        types.SimpleNamespace(date=unfinished[0]),
    )


def _signal_translation_worker(path: Path) -> None:
    path.write_text(f"{time.time_ns()}\n", encoding="ascii")


def _run_import(file: Path) -> int:
    import json

    from news_digest.config import fetch_config_from_env, load_env_file
    from news_digest.models import edition_from_dict
    from news_digest.storage import db

    load_env_file()
    if not file.is_file():
        print(f"文件不存在：{file}")
        return 1
    data = json.loads(file.read_text(encoding="utf-8"))
    edition = edition_from_dict(data.get("edition", data))  # 兼容 fetched 快照与裸版次

    config = fetch_config_from_env()
    conn = db.connect(config.database)
    try:
        db.upsert_articles(conn, edition.date, edition.articles)
        db.upsert_briefs(conn, edition.date, edition.briefs)
    finally:
        conn.close()
    translated = sum(1 for article in edition.articles if article.translated_by)
    print(
        f"已导入 {edition.date}：文章 {len(edition.articles)} 篇"
        f"（含译文 {translated}），简讯 {len(edition.briefs)} 条"
    )
    print("下一步：news-digest build 重新成刊，归档即包含该日期")
    return 0


def _run_admin(port: int, config_dir: Path) -> int:
    from news_digest.config import (
        load_env_file,
        public_subscription_enabled_from_env,
        smtp_config_from_env,
    )
    from news_digest.delivery import subscriptions
    from news_digest.delivery.mailer import MailError, validate_smtp
    from news_digest.preview_server import create_server

    if not config_dir.is_dir():
        print(f"配置目录不存在：{config_dir}")
        return 1
    load_env_file(config_dir / ".env")
    site_url = os.environ.get("NEWS_SITE_URL", "").rstrip("/")
    if not site_url:
        print("配置缺少 NEWS_SITE_URL，公开订阅端点不会启动")
        return 1
    configured_database = os.environ.get("NEWS_DATABASE_PATH", "")
    configured_output = os.environ.get("NEWS_OUTPUT_PATH", "")
    container_database = Path("/data/news.db")
    container_output = Path("/site")
    if Path("/data").is_dir():
        database = container_database
    elif configured_database:
        database = Path(configured_database)
    else:
        database = config_dir / "news.db"
    if Path("/site").is_dir():
        output_root = container_output
    elif configured_output:
        output_root = Path(configured_output)
    else:
        output_root = Path("var/site")
    timezone = os.environ.get("NEWS_TIMEZONE", "Asia/Shanghai")
    public_subscription_enabled = False
    try:
        requested = public_subscription_enabled_from_env()
        if requested:
            subscriptions.public_https_base(site_url)
            smtp = smtp_config_from_env()
            if not smtp.delivery_enabled:
                raise ValueError("EMAIL_DELIVERY_ENABLED 必须为 true")
            validate_smtp(smtp, require_recipients=False)
            public_subscription_enabled = True
    except (MailError, ValueError) as error:
        print(f"公开订阅未就绪，表单与提交接口保持关闭：{error}")
    print(f"生产模型切换面板：http://127.0.0.1:{port}/admin/")
    server = create_server(
        config_dir,
        config_dir,
        port,
        env_file=".env",
        profiles_file="providers.json",
        serve_static=False,  # /config 含明文密钥，绝不提供静态文件回落
        htpasswd_file=config_dir / "htpasswd-admin",
        db_path=database,
        site_url=site_url,
        output_root=output_root,
        timezone=timezone,
        public_subscription_enabled=public_subscription_enabled,
        translation_wakeup_callback=lambda: _signal_translation_worker(
            config_dir / "automation.wake"
        ),
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def _run_preview(port: int, *, automation_demo: bool = False) -> int:
    from news_digest.config import (
        build_config_from_env,
        fetch_config_from_env,
        load_env_file,
    )
    from news_digest.preview_server import create_server

    root = Path.cwd()
    load_env_file()
    build_config = build_config_from_env()
    fetch_config = fetch_config_from_env()
    site_dir = build_config.output_root / "current"
    demo = None
    if automation_demo:
        from news_digest.translation.demo import (
            TranslationAutomationDemo,
            build_demo_edition,
        )

        demo_root = root / "var" / "data"
        demo = TranslationAutomationDemo(
            demo_root / f"automation-demo-{port}.db",
            build_demo_edition(),
            demo_root / f"automation-demo-{port}-cache",
        )
    if not (site_dir / "index.html").is_file():
        print(f"提示：{site_dir} 尚无站点，先运行 build（或双击 daily.bat）")
    print(f"站点预览：http://127.0.0.1:{port}/")
    print(f"模型设置：http://127.0.0.1:{port}/admin/")
    if demo is not None:
        print("翻译自动化：已启用隔离 fake demo（不会调用 provider 或 SMTP）。")
    print("按 Ctrl+C 停止。")
    server = create_server(
        root,
        site_dir,
        port,
        db_path=fetch_config.database,
        site_url=build_config.site_url,
        output_root=build_config.output_root,
        timezone=fetch_config.timezone,
        public_subscription_enabled=build_config.public_subscription_enabled,
        loopback_public_subscription=build_config.public_subscription_enabled,
        translation_db_path=demo.database if demo is not None else None,
        translation_wakeup_callback=demo.wakeup if demo is not None else None,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
