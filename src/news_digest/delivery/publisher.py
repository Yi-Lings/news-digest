"""Static publishing: move a finished build into releases/ and switch current.

The switch is designed for both platforms:

- POSIX (production container): symlink + os.replace, fully atomic.
- Windows (local development): directory rename is blocked by open handles and
  symlinks may require privileges, so we try a real symlink first (works with
  Developer Mode) and fall back to an NTFS junction; the swap is remove + rename
  of the link entry only, never of the release directory itself.
"""

import os
import shutil
import sys
from pathlib import Path

_KEEP_RELEASES = 5  # 保留最近版本数：满足回滚需要，防止磁盘无限增长


def publish(build_dir: Path, output_root: Path, release_name: str) -> Path:
    """Move build_dir under releases/<release_name> and point current at it."""
    releases = output_root / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    target = releases / release_name
    if target.exists():
        shutil.rmtree(target)
    shutil.move(str(build_dir), str(target))
    switch_current(output_root, target)
    _prune_releases(releases, keep_name=target.name)
    return target


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
