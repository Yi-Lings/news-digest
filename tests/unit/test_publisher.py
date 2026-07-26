"""Release switching must be safe on both POSIX (symlink) and Windows (junction)."""

from pathlib import Path

from news_digest.delivery.publisher import publish, switch_current


def _make_release(root: Path, name: str) -> Path:
    release = root / "releases" / name
    release.mkdir(parents=True)
    (release / "index.html").write_text(name, encoding="utf-8")
    return release


def test_switch_current_points_at_latest_and_keeps_previous(tmp_path):
    first = _make_release(tmp_path, "2026-07-25-01")
    switch_current(tmp_path, first)
    current = tmp_path / "current"
    assert (current / "index.html").read_text(encoding="utf-8") == "2026-07-25-01"

    second = _make_release(tmp_path, "2026-07-26-01")
    switch_current(tmp_path, second)
    assert (current / "index.html").read_text(encoding="utf-8") == "2026-07-26-01"
    # 上一版本目录必须原样保留，供失败回退
    assert (first / "index.html").read_text(encoding="utf-8") == "2026-07-25-01"


def test_publish_moves_build_dir_into_releases(tmp_path):
    build = tmp_path / ".build-tmp"
    build.mkdir()
    (build / "index.html").write_text("x", encoding="utf-8")

    target = publish(build, tmp_path, "r-01")

    assert target == tmp_path / "releases" / "r-01"
    assert not build.exists()
    assert (tmp_path / "current" / "index.html").read_text(encoding="utf-8") == "x"
