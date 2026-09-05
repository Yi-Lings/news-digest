"""Resolve published pages and assets before applying their existing access rules."""

import re
from pathlib import Path

_ISSUE = re.compile(r"/issues/\d{4}-\d{2}-\d{2}/(?:[a-z0-9]+(?:-[a-z0-9]+)*\.html)?")
_ASSET = re.compile(
    r"/assets/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_.-]+\.(?:css|js|svg|png|jpe?g|webp|ico|woff2?)"
)


def resolve_static_resource(root: Path, path: str) -> tuple[Path, str] | None:
    if path in {"/", "/archive", "/archive/", "/privacy", "/privacy/"}:
        path = path.rstrip("/") + "/index.html"
    elif re.fullmatch(r"/issues/\d{4}-\d{2}-\d{2}/?", path):
        path = path.rstrip("/") + "/index.html"
    if not (
        path in {"/index.html", "/archive/index.html", "/privacy/index.html"}
        or _ISSUE.fullmatch(path) or _ASSET.fullmatch(path)
    ):
        return None
    target = root / path.lstrip("/")
    try:
        resolved = target.resolve(strict=True)
        relative = resolved.relative_to(root.resolve(strict=True))
    except (OSError, ValueError):
        return None
    # The resolved resource must have the same identity used for authorization.
    if relative.as_posix() != path.lstrip("/") or not resolved.is_file():
        return None
    return resolved, path
