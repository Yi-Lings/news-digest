"""Migration rehearsal CLI must not bake in an operator-specific site URL."""

import subprocess
import sys
from pathlib import Path


def test_build_requires_explicit_site_url(tmp_path):
    script = Path(__file__).resolve().parents[2] / "deploy" / "verify-content-migration.py"
    result = subprocess.run(
        [sys.executable, str(script), str(tmp_path), "--build"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "--build requires --site-url" in result.stderr
