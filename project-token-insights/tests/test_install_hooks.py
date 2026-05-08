#!/usr/bin/env python3
"""Project-local hook installer behavior."""

from __future__ import annotations

import json
import sys
from pathlib import Path


_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))

import install_hooks as installer  # noqa: E402


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_install_dry_run_does_not_create_hooks_dir_or_write_settings(tmp_path):
    changed = installer.install(only="first-turn", dry_run=True, project_dir=tmp_path)

    assert changed is True
    assert not (tmp_path / ".project-token-insights" / "hooks").exists()
    assert not (tmp_path / ".claude" / "settings.local.json").exists()
    assert not list((tmp_path / ".claude").glob("settings.local.json.bak-*"))


def test_install_writes_only_project_local_files(tmp_path):
    changed = installer.install(only="first-turn", project_dir=tmp_path)

    hook_file = tmp_path / ".project-token-insights" / "hooks" / "first-turn-budget-check.py"
    settings_file = tmp_path / ".claude" / "settings.local.json"
    settings = _read_json(settings_file)
    session_command = settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    prompt_command = settings["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
    expected = 'python3 "${CLAUDE_PROJECT_DIR:-$PWD}/.project-token-insights/hooks/first-turn-budget-check.py"'

    assert changed is True
    assert hook_file.exists()
    assert settings_file.exists()
    assert session_command == expected
    assert prompt_command == expected
    assert str(hook_file) not in session_command
    assert str(Path.home() / ".claude") not in session_command
    gitignore = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert ".project-token-insights/" in gitignore
    assert ".claude/settings.local.json" in gitignore


def test_uninstall_removes_project_entries_and_files(tmp_path):
    installer.install(only="first-turn", project_dir=tmp_path)

    changed = installer.uninstall(only="first-turn", project_dir=tmp_path)

    hook_file = tmp_path / ".project-token-insights" / "hooks" / "first-turn-budget-check.py"
    settings_file = tmp_path / ".claude" / "settings.local.json"
    settings = _read_json(settings_file)

    assert changed is True
    assert not hook_file.exists()
    assert "hooks" not in settings
    assert list((tmp_path / ".claude").glob("settings.local.json.bak-uninstall-*"))


def test_install_updates_existing_absolute_command_to_project_expr(tmp_path):
    settings_dir = tmp_path / ".claude"
    settings_dir.mkdir()
    settings_file = settings_dir / "settings.local.json"
    old_hook = tmp_path / ".project-token-insights" / "hooks" / "first-turn-budget-check.py"
    settings_file.write_text(json.dumps({
        "hooks": {
            "SessionStart": [{
                "matcher": "*",
                "hooks": [{
                    "type": "command",
                    "command": f"python3 {old_hook}",
                }],
            }],
        },
    }), encoding="utf-8")

    changed = installer.install(only="first-turn", project_dir=tmp_path)
    settings = _read_json(settings_file)
    session_command = settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    prompt_command = settings["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
    expected = 'python3 "${CLAUDE_PROJECT_DIR:-$PWD}/.project-token-insights/hooks/first-turn-budget-check.py"'

    assert changed is True
    assert session_command == expected
    assert prompt_command == expected
    assert str(tmp_path) not in session_command
    assert str(tmp_path) not in prompt_command


def test_status_reports_project_scope(tmp_path):
    status = installer.check_status(only="first-turn", project_dir=tmp_path)

    assert status["scope/project"]["path"] == str(tmp_path.resolve())
    assert status["scope/settings"]["path"] == str(tmp_path / ".claude" / "settings.local.json")
    assert status["scope/hooks_dir"]["path"] == str(tmp_path / ".project-token-insights" / "hooks")
    assert status["first-turn/settings/SessionStart"]["wired"] is False
    assert status["first-turn/settings/UserPromptSubmit"]["wired"] is False
