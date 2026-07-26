"""Minimal toolchain-verification tests for the CLI."""

import pytest

from news_digest import __version__
from news_digest.cli import build_parser, main


def test_version_option_reports_package_version(capsys):
    with pytest.raises(SystemExit) as excinfo:
        build_parser().parse_args(["--version"])
    assert excinfo.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_bare_invocation_prints_help_and_succeeds(capsys):
    assert main([]) == 0
    assert "news-digest" in capsys.readouterr().out


def test_subcommands_parse():
    parser = build_parser()
    args = parser.parse_args(["build", "--fixtures", "tests/fixtures/demo"])
    assert args.command == "build"
    assert args.fixtures == "tests/fixtures/demo"
    args = parser.parse_args(["build"])
    assert args.fixtures is None
    args = parser.parse_args(["fetch", "--window-hours", "12"])
    assert args.command == "fetch"
    assert args.window_hours == 12
