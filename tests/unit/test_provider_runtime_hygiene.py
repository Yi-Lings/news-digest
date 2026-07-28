import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[2]
PROVIDER_RUNTIME_FILES = (
    ".provider-test-secret",
    "provider-tests.json",
    ".provider-tests.json.lock",
    "..provider-test-secret.lock",
    "..env.providers.local.lock",
    "..env.local.lock",
)


def test_provider_runtime_files_are_ignored_by_git():
    not_ignored = [
        path
        for path in PROVIDER_RUNTIME_FILES
        if subprocess.run(
            ["git", "check-ignore", "--quiet", "--", path],
            cwd=ROOT,
            check=False,
        ).returncode
        != 0
    ]

    assert not not_ignored, f"provider runtime files are not ignored: {not_ignored}"
