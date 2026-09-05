"""Static publishing: move a finished build into releases/ and switch current.

The switch is designed for both platforms:

- POSIX (production container): symlink + os.replace, fully atomic.
- Windows (local development): directory rename is blocked by open handles and
  symlinks may require privileges, so we try a real symlink first (works with
  Developer Mode) and fall back to an NTFS junction; the swap is remove + rename
  of the link entry only, never of the release directory itself.
"""

import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

from news_digest.models import DailyEdition, edition_from_dict, edition_to_dict

_KEEP_RELEASES = 5  # 保留最近版本数：满足回滚需要，防止磁盘无限增长
_MANIFEST_SCHEMA = 1
_MANIFEST_NAME = "release.json"
_MAX_MANIFEST_BYTES = 64 * 1024 * 1024
_RELEASE_NAME = re.compile(r"(?P<date>\d{4}-\d{2}-\d{2})-(?P<sequence>\d{2,})")


@dataclass(frozen=True)
class PublishedRelease:
    release_name: str
    release_date: str
    published_at: dt.datetime
    edition: DailyEdition
    path: Path
    edition_sha256: str


def _canonical_edition(edition: DailyEdition) -> bytes:
    return json.dumps(
        edition_to_dict(edition),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _validated_date(value: str, field: str) -> str:
    try:
        parsed = dt.date.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field} must be YYYY-MM-DD") from error
    if parsed.isoformat() != value:
        raise ValueError(f"{field} must be YYYY-MM-DD")
    return value


def _release_identity(name: str) -> tuple[str, int]:
    if not isinstance(name, str) or not (match := _RELEASE_NAME.fullmatch(name)):
        raise ValueError("release name is invalid")
    date = _validated_date(match.group("date"), "release date")
    return date, int(match.group("sequence"))


def write_release_manifest(
    build_dir: Path,
    release_name: str,
    edition: DailyEdition,
    *,
    published_at: dt.datetime | None = None,
    per_edition: bool = False,
) -> Path:
    """Write and re-read the self-contained immutable release identity before publication."""
    release_date, _ = _release_identity(release_name)
    if edition.date != release_date and not per_edition:
        raise ValueError("release name date does not match edition date")
    timestamp = published_at or dt.datetime.now(dt.UTC)
    if timestamp.tzinfo is None:
        raise ValueError("published_at must be timezone-aware")
    timestamp = timestamp.astimezone(dt.UTC)
    edition_payload = edition_to_dict(edition)
    payload = {
        "schema_version": _MANIFEST_SCHEMA,
        "release_name": release_name,
        "release_date": edition.date,
        "published_at": timestamp.isoformat(timespec="seconds"),
        "edition_sha256": hashlib.sha256(_canonical_edition(edition)).hexdigest(),
        "edition": edition_payload,
    }
    path = (
        build_dir / ".editions" / f"{edition.date}.json"
        if per_edition else build_dir / _MANIFEST_NAME
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, ensure_ascii=False, indent=1) + "\n").encode("utf-8")
    with path.open("xb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    load_release_manifest(
        build_dir, expected_release_name=release_name,
        edition_date=edition.date if per_edition else None,
    )
    return path


def load_release_manifest(
    release_dir: Path, *, expected_release_name: str | None = None,
    edition_date: str | None = None,
) -> PublishedRelease:
    """Validate one manifest and all release-relative identities without database fallback."""
    path = (
        release_dir / ".editions" / f"{_validated_date(edition_date, 'edition date')}.json"
        if edition_date else release_dir / _MANIFEST_NAME
    )
    return _load_manifest(path, release_dir, expected_release_name, edition_date)


def load_publication_record(output_root: Path, edition_date: str) -> PublishedRelease:
    """Read private durable metadata; it does not assert that HTML is currently online."""
    date = _validated_date(edition_date, "edition date")
    return _load_manifest(output_root / ".published" / f"{date}.json", None, None, date)


def _load_manifest(
    path: Path, release_dir: Path | None, expected_release_name: str | None,
    edition_date: str | None,
) -> PublishedRelease:
    if path.is_symlink() or not path.is_file():
        raise ValueError("published release manifest is missing or unsafe")
    raw = path.read_bytes()
    if not raw or len(raw) > _MAX_MANIFEST_BYTES:
        raise ValueError("published release manifest is empty or too large")
    try:
        data = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("published release manifest is damaged") from error
    if not isinstance(data, dict) or set(data) != {
        "schema_version",
        "release_name",
        "release_date",
        "published_at",
        "edition_sha256",
        "edition",
    }:
        raise ValueError("published release manifest fields are invalid")
    if data.get("schema_version") != _MANIFEST_SCHEMA:
        raise ValueError("published release manifest schema is unsupported")

    release_name = data.get("release_name")
    release_date, _ = _release_identity(release_name)
    if edition_date is not None:
        release_date = edition_date
    if expected_release_name is not None and release_name != expected_release_name:
        raise ValueError("published release identity does not match its directory")
    if data.get("release_date") != release_date:
        raise ValueError("published release dates are inconsistent")
    try:
        published_raw = data.get("published_at")
        if not isinstance(published_raw, str):
            raise TypeError
        published_at = dt.datetime.fromisoformat(published_raw)
    except (TypeError, ValueError) as error:
        raise ValueError("published_at is invalid") from error
    if published_at.tzinfo is None:
        raise ValueError("published_at must be timezone-aware")

    edition_data = data.get("edition")
    if not isinstance(edition_data, dict):
        raise ValueError("published edition is missing")
    try:
        edition = edition_from_dict(edition_data)
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("published edition is damaged") from error
    if edition.date != release_date:
        raise ValueError("published edition date does not match release date")
    digest = hashlib.sha256(_canonical_edition(edition)).hexdigest()
    if data.get("edition_sha256") != digest:
        raise ValueError("published edition digest does not match")
    if (
        type(edition.generation) is not int or edition.generation < 0
        or not isinstance(edition.result_revisions, dict)
        or any(type(v) is not int or v < 0 for v in edition.result_revisions.values())
    ):
        raise ValueError("published edition revision is invalid")

    issue_dir = release_dir / "issues" / release_date if release_dir else None
    if issue_dir is not None and not (issue_dir / "index.html").is_file():
        raise ValueError("published release issue page is missing")
    if len({a.url for a in edition.articles}) != len(edition.articles):
        raise ValueError("published article identity is duplicated")
    if len({a.slug for a in edition.articles}) != len(edition.articles):
        raise ValueError("published article path is duplicated")
    for article in edition.articles:
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", article.slug):
            raise ValueError("published article slug is unsafe")
        if issue_dir is not None and not (issue_dir / f"{article.slug}.html").is_file():
            raise ValueError("published release article page is missing")
    return PublishedRelease(
        release_name=release_name,
        release_date=release_date,
        published_at=published_at.astimezone(dt.UTC),
        edition=edition,
        path=release_dir or path.parent,
        edition_sha256=digest,
    )


def resolve_published_release(
    output_root: Path, *, edition_date: str | None = None
) -> PublishedRelease:
    """Resolve current, or a retained explicit edition, strictly below ``releases/``."""
    releases = output_root / "releases"
    try:
        releases_root = releases.resolve(strict=True)
    except OSError as error:
        raise ValueError("published releases directory is unavailable") from error
    if not releases_root.is_dir():
        raise ValueError("published releases directory is unavailable")

    if edition_date is None:
        current = output_root / "current"
        try:
            release_dir = current.resolve(strict=True)
        except OSError as error:
            raise ValueError("current published release is unavailable") from error
    else:
        wanted = _validated_date(edition_date, "requested edition date")
        current = (output_root / "current").resolve()
        if current.parent == releases_root and (current / ".editions" / f"{wanted}.json").is_file():
            return load_release_manifest(
                current, expected_release_name=current.name, edition_date=wanted,
            )
        candidates: list[tuple[int, Path]] = []
        for entry in releases.iterdir():
            if entry.is_symlink() or not entry.is_dir():
                continue
            try:
                date, sequence = _release_identity(entry.name)
            except ValueError:
                continue
            if date == wanted:
                candidates.append((sequence, entry.resolve(strict=True)))
        if not candidates:
            raise ValueError("requested edition has no retained release manifest")
        release_dir = max(candidates, key=lambda item: item[0])[1]

    if release_dir.parent != releases_root or not release_dir.is_dir():
        raise ValueError("published release path escapes releases directory")
    return load_release_manifest(release_dir, expected_release_name=release_dir.name)


def publish(build_dir: Path, output_root: Path, release_name: str) -> Path:
    """Move build_dir under releases/<release_name> and point current at it."""
    releases = output_root / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    target = releases / release_name
    target_exists = target.exists()
    if target.is_symlink() or target_exists:
        raise FileExistsError(f"release already exists: {release_name}")
    shutil.move(str(build_dir), str(target))
    switch_current(output_root, target)
    persist_publication_index(output_root)
    _prune_releases(releases, keep_name=target.name)
    return target


def persist_publication_index(output_root: Path) -> None:
    """Keep per-edition publication facts even after old HTML releases are pruned."""
    current = (output_root / "current").resolve()
    if not (current / _MANIFEST_NAME).is_file():
        return
    if current.parent != (output_root / "releases").resolve():
        raise ValueError("published release path escapes releases directory")
    manifests = list((current / ".editions").glob("*.json"))
    if not manifests:
        manifests = [current / _MANIFEST_NAME]
    index = output_root / ".published"
    index.mkdir(exist_ok=True)
    for manifest in manifests:
        date = manifest.stem if manifest.name != _MANIFEST_NAME else None
        publication = load_release_manifest(
            current, expected_release_name=current.name, edition_date=date,
        )
        destination = index / f"{publication.release_date}.json"
        temporary = index / f".{uuid.uuid4().hex}.tmp"
        try:
            with temporary.open("xb") as handle:
                handle.write(manifest.read_bytes())
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)


def _prune_releases(releases: Path, keep_name: str) -> None:
    """按名称排序删除最旧的多余版本；无论排序结果如何，绝不删除刚发布的版本。"""
    names = sorted(entry.name for entry in releases.iterdir() if entry.is_dir())
    for name in names[:-_KEEP_RELEASES]:
        if name == keep_name:
            continue
        shutil.rmtree(releases / name)


def switch_current(output_root: Path, target: Path) -> None:
    current = output_root / "current"
    tmp = output_root / f".current-tmp-{os.getpid()}"
    _remove_link(tmp)
    _create_dir_link(tmp, target)
    if sys.platform == "win32":
        # os.replace cannot overwrite a directory entry on Windows; the window
        # between remove and rename only affects the link, not the release data.
        _remove_link(current)
        os.rename(tmp, current)
    else:
        os.replace(tmp, current)


def _create_dir_link(link: Path, target: Path) -> None:
    try:
        os.symlink(Path("releases") / target.name, link, target_is_directory=True)
    except OSError:
        if sys.platform != "win32":
            raise
        # NTFS junction: no privileges needed. _winapi is private but stable
        # (exercised by CPython's own test suite) and avoids shelling out to
        # `mklink`, which is a cmd.exe builtin. Junctions store absolute paths.
        import _winapi

        _winapi.CreateJunction(str(target.resolve()), str(link))


def _remove_link(path: Path) -> None:
    """Remove a symlink or junction entry without touching what it points at."""
    if path.is_symlink():
        path.unlink()
    elif path.exists():
        os.rmdir(path)
