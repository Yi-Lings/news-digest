"""Restore legacy edition identity from retained publication facts without model calls."""

import datetime as dt
import json
import re
from pathlib import Path

from news_digest.delivery.publisher import load_publication_record, load_release_manifest
from news_digest.models import article_source_hash, article_to_dict
from news_digest.storage import db
from news_digest.translation.schema import (
    InvalidTranslation,
    build_sentence_snapshot,
    result_to_dict,
)
from news_digest.translation.service import _result_from_dict, cache_key


def restore_history(
    database: Path, output_root: Path, cache_dir: Path, *, latest_only: bool = False,
) -> dict[str, int]:
    publications = {}
    for record in (output_root / ".published").glob("*.json"):
        try:
            publication = load_publication_record(output_root, record.stem)
        except ValueError:
            continue
        publications[publication.release_date] = publication
    releases = output_root / "releases"
    if releases.is_dir():
        for release in sorted(releases.iterdir()):
            if not release.is_dir() or release.is_symlink():
                continue
            manifests = list((release / ".editions").glob("*.json"))
            if not manifests:
                manifests = [release / "release.json"]
            for manifest in manifests:
                if not manifest.is_file():
                    continue
                try:
                    publication = load_release_manifest(
                        release,
                        expected_release_name=release.name,
                        edition_date=manifest.stem if manifest.parent.name == ".editions" else None,
                    )
                except ValueError:
                    continue
                previous = publications.get(publication.release_date)
                if previous is None or publication.published_at >= previous.published_at:
                    publications[publication.release_date] = publication
    latest_date = max(publications, default=None) if latest_only else None
    if latest_date is not None:
        publications = {latest_date: publications[latest_date]}
    counts = {"editions": 0, "results": 0, "sentences": 0, "source_only": 0}
    conn = db.connect(database)
    now = dt.datetime.now(dt.UTC).isoformat()
    try:
        legacy_dates = set(db.list_dates(conn))
        for issue in (output_root / "current" / "issues").glob("????-??-??"):
            if issue.is_dir():
                try:
                    legacy_dates.add(dt.date.fromisoformat(issue.name).isoformat())
                except ValueError:
                    continue
        with conn:
            for date in legacy_dates:
                conn.execute(
                    "INSERT OR IGNORE INTO automation_editions"
                    " (edition_date, status, target_count, created_at, updated_at)"
                    " VALUES (?, 'complete', 0, ?, ?)",
                    (date, now, now),
                )
        for date, publication in publications.items():
            state = conn.execute(
                "SELECT * FROM automation_editions WHERE edition_date = ?",
                (date,),
            ).fetchone()
            if state is not None and state["history_status"] != "pending":
                continue
            tasks = db.list_translation_tasks(conn, date)
            if any(task.status == "running" for task in tasks):
                continue
            prepared = []
            for article in publication.edition.articles:
                matches = [t for t in tasks if t.article_id == article.url]
                matches.sort(key=lambda t: (t.status == "succeeded", t.updated_at), reverse=True)
                task = next((t for t in matches if t.status == "succeeded"), None)
                if not article.translated_by:
                    task = matches[0] if matches else None
                snapshot = build_sentence_snapshot([p.en for p in article.paragraphs])
                sentence_counts = [len(p["sentences"]) for p in snapshot["paragraphs"]]
                # A validated publication supplies the exact source; old task snapshots
                # may be counts-only, damaged, or refer to an earlier fetched revision.
                segmentation = json.dumps(snapshot, ensure_ascii=False)
                counts["sentences"] += 1
                result = None
                for candidate in matches:
                    if re.fullmatch(r"default-[0-9a-f]{64}", candidate.provider_id) is None:
                        continue
                    path = cache_dir / f"{cache_key(article, candidate.provider_id[8:])}.json"
                    if not path.is_file():
                        continue
                    try:
                        parsed = _result_from_dict(
                            json.loads(path.read_text(encoding="utf-8")),
                            article,
                            sentence_counts,
                        )
                    except (InvalidTranslation, ValueError):
                        continue
                    if (
                        parsed.title_zh == article.title_zh
                        and parsed.summary_zh == article.summary_zh
                        and parsed.paragraphs_zh == [p.zh for p in article.paragraphs]
                    ):
                        result = result_to_dict(parsed)
                        break
                prepared.append((article, task, segmentation, result))
            try:
                conn.execute("BEGIN IMMEDIATE")
                current = conn.execute(
                    "SELECT history_status FROM automation_editions WHERE edition_date = ?",
                    (date,),
                ).fetchone()
                if current is not None and current[0] != "pending":
                    conn.rollback()
                    continue
                conn.execute(
                    "INSERT OR IGNORE INTO automation_editions"
                    " (edition_date, status, target_count, created_at, updated_at)"
                    " VALUES (?, 'complete', ?, ?, ?)",
                    (date, len(prepared), now, now),
                )
                conn.execute("DELETE FROM edition_items WHERE edition_date = ?", (date,))
                for position, (article, task, segmentation, result) in enumerate(prepared):
                    task_id = (
                        task.task_id
                        if task
                        else db._translation_task_id(date, article.url, "legacy")
                    )
                    if task is None:
                        conn.execute(
                            "INSERT INTO translation_tasks (task_id, edition_date, article_id,"
                            " article_title, provider_id, status, build_status,"
                            " created_at, updated_at)"
                            " VALUES (?, ?, ?, ?, 'legacy', ?, 'online', ?, ?)",
                            (
                                task_id,
                                date,
                                article.url,
                                article.title_en,
                                "succeeded" if article.translated_by else "failed",
                                now,
                                now,
                            ),
                        )
                    conn.execute(
                        "UPDATE translation_tasks SET segmentation_json = ? WHERE task_id = ?",
                        (segmentation, task_id),
                    )
                    payload = json.dumps(article_to_dict(article), ensure_ascii=False)
                    conn.execute(
                        "INSERT INTO edition_items (edition_date, article_id, position,"
                        " source_json,"
                        " payload, source_hash, segmentation_json, active_task_id, result_json,"
                        " result_revision) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            date,
                            article.url,
                            position,
                            payload,
                            payload,
                            article_source_hash(article),
                            segmentation,
                            task_id,
                            json.dumps(result) if result else None,
                            int(result is not None),
                        ),
                    )
                    counts["results"] += int(result is not None)
                conn.execute(
                    "UPDATE automation_editions SET briefs_json = ?, history_status = 'manifest',"
                    " target_count = ?, succeeded_count = (SELECT COUNT(*) FROM edition_items i"
                    " JOIN translation_tasks t ON t.task_id = i.active_task_id"
                    " WHERE i.edition_date = ? AND t.status = 'succeeded'), online_count = ?"
                    " WHERE edition_date = ?",
                    (
                        json.dumps([vars(b) for b in publication.edition.briefs]),
                        len(prepared),
                        date,
                        len(prepared),
                        date,
                    ),
                )
                conn.commit()
                counts["editions"] += 1
            except Exception:
                conn.rollback()
                raise
        with conn:
            cursor = conn.execute(
                "UPDATE automation_editions SET history_status = 'source_only'"
                " WHERE history_status = 'pending' AND NOT EXISTS"
                " (SELECT 1 FROM translation_tasks t"
                " WHERE t.edition_date = automation_editions.edition_date"
                " AND t.status = 'running')",
            )
            counts["source_only"] = cursor.rowcount
            cursor = conn.execute(
                "UPDATE automation_editions SET status = 'complete',"
                " last_error_code = 'NO_ELIGIBLE_RECIPIENTS' WHERE status = 'delivered'"
                " AND (? IS NULL OR edition_date = ?)"
                " AND EXISTS (SELECT 1 FROM email_delivery_runs r"
                " WHERE r.edition_date = automation_editions.edition_date"
                " AND r.status IN ('completed', 'skipped') AND r.total_count = 0)"
                " AND NOT EXISTS (SELECT 1 FROM email_deliveries d"
                " WHERE d.edition_date = automation_editions.edition_date"
                " AND d.status IN ('sent', 'sending', 'unknown', 'failed'))",
                (latest_date, latest_date),
            )
            counts["zero_recipient_editions"] = cursor.rowcount
    finally:
        conn.close()
    return counts
