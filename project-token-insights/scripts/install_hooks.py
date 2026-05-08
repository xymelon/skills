#!/usr/bin/env python3
"""
project-token-insights · 项目级 hook 安装器。

支持两类 hook：
  - cache-bust（3 个）：SessionStart / Stop / UserPromptSubmit
      来源：assets/cache-hooks/
  - first-turn（1 个脚本，2 个事件）：SessionStart 计算 pending / UserPromptSubmit 前台提醒
      来源：assets/first-turn-hooks/

动作：
  install（默认）：将 hook 脚本复制到当前项目 `.project-token-insights/hooks/`，
                   并在当前项目 `.claude/settings.local.json` 注册 matcher。
  --status        仅打印当前项目安装状态（不改任何文件）。
  --uninstall     删除当前项目安装的 hook 文件 + 清项目 settings.local.json。
  --dry-run       仅打印将要做的变更。
  --only GROUP    仅操作指定组：cache-bust / first-turn。
  --project-dir   显式指定项目根，默认 $CLAUDE_PROJECT_DIR 或 cwd。

写入 settings.local.json 前会先备份为 settings.local.json.bak-<UTC-timestamp>。
本安装器不写入用户级 ~/.claude/hooks 或 ~/.claude/settings.json。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

SKILL_ROOT = Path(__file__).parent.parent
DATA_DIRNAME = ".project-token-insights"
PROJECT_SETTINGS_REL = Path(".claude") / "settings.local.json"
PROJECT_HOOKS_REL = Path(DATA_DIRNAME) / "hooks"
GITIGNORE_RULE = ".project-token-insights/"
LOCAL_SETTINGS_GITIGNORE_RULE = ".claude/settings.local.json"
PROJECT_DIR_ENV = "CLAUDE_PROJECT_DIR"

HOOK_GROUPS: dict[str, dict] = {
    "cache-bust": {
        "asset_dir": SKILL_ROOT / "assets" / "cache-hooks",
        "files": [
            "cache-resume-detect.py",
            "cache-warn-stop.py",
            "cache-expiry-warn.py",
        ],
        "events": {
            "SessionStart": "cache-resume-detect.py",
            "Stop": "cache-warn-stop.py",
            "UserPromptSubmit": "cache-expiry-warn.py",
        },
    },
    "first-turn": {
        "asset_dir": SKILL_ROOT / "assets" / "first-turn-hooks",
        "files": [
            "first-turn-budget-check.py",
        ],
        "events": {
            "SessionStart": "first-turn-budget-check.py",
            "UserPromptSubmit": "first-turn-budget-check.py",
        },
    },
}


class InstallError(Exception):
    """Raised by install()/uninstall() on hard failures."""


def _project_root(project_dir: str | Path | None = None) -> Path:
    root = project_dir or os.environ.get(PROJECT_DIR_ENV) or os.getcwd()
    return Path(root).expanduser().resolve()


def _hooks_dir(project_dir: str | Path | None = None) -> Path:
    return _project_root(project_dir) / PROJECT_HOOKS_REL


def _settings_path(project_dir: str | Path | None = None) -> Path:
    return _project_root(project_dir) / PROJECT_SETTINGS_REL


def _hook_path(script: str, project_dir: str | Path | None = None) -> Path:
    return _hooks_dir(project_dir) / script


def _hook_command(script: str, project_dir: str | Path | None = None) -> str:
    return f'python3 "${{CLAUDE_PROJECT_DIR:-$PWD}}/{PROJECT_HOOKS_REL}/{script}"'


def _session_block(script: str, project_dir: str | Path | None = None) -> dict:
    return {
        "matcher": "*",
        "hooks": [{"type": "command", "command": _hook_command(script, project_dir)}],
    }


def _find_hook_index(event_list: list[dict], substr: str) -> int:
    for i, matcher_block in enumerate(event_list):
        for hook in matcher_block.get("hooks", []):
            if substr in hook.get("command", ""):
                return i
    return -1


def _already_wired(event_list: list[dict], substr: str) -> bool:
    return _find_hook_index(event_list, substr) >= 0


def _selected_groups(only: str | None) -> dict[str, dict]:
    if not only:
        return HOOK_GROUPS
    if only not in HOOK_GROUPS:
        raise InstallError(f"Unknown --only group: {only!r} (available: {list(HOOK_GROUPS)})")
    return {only: HOOK_GROUPS[only]}


def _read_settings(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise InstallError(f"{path} is malformed: {exc}") from exc
    if not isinstance(cfg, dict):
        raise InstallError(f"{path} must contain a JSON object")
    return cfg


def _backup_settings(path: Path, tag: str) -> Path | None:
    if not path.exists():
        return None
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_suffix(f".json.bak-{tag}-{ts}")
    shutil.copy2(path, backup)
    return backup


def _ensure_gitignore(project_root: Path) -> None:
    gitignore = project_root / ".gitignore"
    try:
        current = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    except OSError:
        current = ""
    rules = {line.strip().rstrip("/") for line in current.splitlines() if line.strip()}
    needed = [
        rule for rule in (GITIGNORE_RULE, LOCAL_SETTINGS_GITIGNORE_RULE)
        if rule.rstrip("/") not in rules
    ]
    if not needed:
        return
    prefix = "" if not current or current.endswith("\n") else "\n"
    try:
        gitignore.write_text(f"{current}{prefix}" + "\n".join(needed) + "\n", encoding="utf-8")
    except OSError:
        return


def check_status(only: str | None = None, project_dir: str | Path | None = None) -> dict[str, dict]:
    result: dict[str, dict] = {}
    root = _project_root(project_dir)
    groups = _selected_groups(only)
    settings_path = _settings_path(root)
    try:
        cfg = _read_settings(settings_path)
    except InstallError:
        cfg = {}
    hooks_cfg = cfg.get("hooks", {}) if isinstance(cfg, dict) else {}
    result["scope/project"] = {"path": str(root)}
    result["scope/settings"] = {"path": str(settings_path)}
    result["scope/hooks_dir"] = {"path": str(_hooks_dir(root))}
    for gname, spec in groups.items():
        for fname in spec["files"]:
            dest = _hook_path(fname, root)
            result[f"{gname}/file/{fname}"] = {"exists": dest.exists()}
        for event, script in spec["events"].items():
            wired = _already_wired(hooks_cfg.get(event, []), script)
            result[f"{gname}/settings/{event}"] = {"wired": wired}
    return result


def install(only: str | None = None, dry_run: bool = False, project_dir: str | Path | None = None) -> bool:
    changed = False
    root = _project_root(project_dir)
    groups = _selected_groups(only)
    hooks_dir = _hooks_dir(root)
    settings_path = _settings_path(root)
    if not dry_run:
        hooks_dir.mkdir(parents=True, exist_ok=True)
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        _ensure_gitignore(root)

    # 1. Copy hook scripts into the current project.
    for gname, spec in groups.items():
        asset_dir: Path = spec["asset_dir"]
        for fname in spec["files"]:
            src = asset_dir / fname
            dest = _hook_path(fname, root)
            if not src.exists():
                raise InstallError(f"Asset not found: {src}")
            if dest.exists() and dest.read_bytes() == src.read_bytes():
                print(f"  [skip] {gname}/{fname} — already up to date")
                continue
            tag = "update" if dest.exists() else "install"
            print(f"  [{tag}] {gname}/{fname} -> {dest}")
            if not dry_run:
                shutil.copy2(src, dest)
                dest.chmod(0o644)
            changed = True

    # 2. Merge project .claude/settings.local.json.
    cfg = _read_settings(settings_path)
    hooks_cfg = cfg.setdefault("hooks", {})
    settings_changed = False
    for gname, spec in groups.items():
        for event, script in spec["events"].items():
            event_list = hooks_cfg.setdefault(event, [])
            idx = _find_hook_index(event_list, script)
            desired_block = _session_block(script, root)
            if idx >= 0 and event_list[idx] == desired_block:
                print(f"  [skip] settings.local.json {event} ← {gname}/{script}（已注册）")
                continue
            if idx >= 0:
                print(f"  [update] settings.local.json {event} ← {gname}/{script}")
                if not dry_run:
                    event_list[idx] = desired_block
            else:
                print(f"  [add] settings.local.json {event} ← {gname}/{script}")
                if not dry_run:
                    event_list.append(desired_block)
            settings_changed = True
            changed = True

    if settings_changed and not dry_run:
        backup = _backup_settings(settings_path, "install")
        if backup:
            print(f"  [backup] settings.local.json → {backup.name}")
        settings_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        print(f"  [wrote] {settings_path}")
    return changed


def uninstall(only: str | None = None, dry_run: bool = False, project_dir: str | Path | None = None) -> bool:
    changed = False
    root = _project_root(project_dir)
    groups = _selected_groups(only)
    settings_path = _settings_path(root)

    # 1. Delete project-local hook script files.
    for gname, spec in groups.items():
        for fname in spec["files"]:
            dest = _hook_path(fname, root)
            if dest.exists():
                print(f"  [remove] {gname}/{fname} -> {dest}")
                if not dry_run:
                    dest.unlink()
                changed = True
            else:
                print(f"  [skip] {gname}/{fname} — not installed")

    # 2. Remove project settings.local.json entries.
    try:
        cfg = _read_settings(settings_path)
    except InstallError as exc:
        print(f"  [warn] settings.local.json unavailable: {exc}")
        return changed

    hooks_cfg = cfg.get("hooks") or {}
    settings_changed = False
    for gname, spec in groups.items():
        for event, script in spec["events"].items():
            event_list = hooks_cfg.get(event) or []
            idx = _find_hook_index(event_list, script)
            if idx < 0:
                print(f"  [skip] settings.local.json {event} ← {gname}/{script}（未注册）")
                continue
            print(f"  [unwire] settings.local.json {event} ← {gname}/{script}")
            if not dry_run:
                del event_list[idx]
                if not event_list:
                    hooks_cfg.pop(event, None)
            settings_changed = True
            changed = True

    if settings_changed and not dry_run:
        backup = _backup_settings(settings_path, "uninstall")
        if backup:
            print(f"  [backup] settings.local.json → {backup.name}")
        if not hooks_cfg:
            cfg.pop("hooks", None)
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        print(f"  [wrote] {settings_path}")
    return changed


def main() -> None:
    parser = argparse.ArgumentParser(description="project-token-insights 项目级 hook 安装/卸载器")
    parser.add_argument("--status", action="store_true", help="打印当前项目安装状态并退出")
    parser.add_argument("--uninstall", action="store_true", help="卸载当前项目 hook：删除脚本 + 清 settings.local.json")
    parser.add_argument("--dry-run", action="store_true", help="仅打印将要做的变更")
    parser.add_argument("--project-dir", default=None, help="项目根路径（默认 $CLAUDE_PROJECT_DIR 或 cwd）")
    parser.add_argument(
        "--only",
        choices=sorted(HOOK_GROUPS.keys()),
        default=None,
        help="限定组：cache-bust / first-turn",
    )
    args = parser.parse_args()

    root = _project_root(args.project_dir)
    try:
        if args.status:
            status = check_status(args.only, project_dir=root)
            print("Hook 安装状态（项目级）：")
            for key, val in status.items():
                flags = ", ".join(f"{k}={v}" for k, v in val.items())
                print(f"  {key}: {flags}")
            return

        if args.dry_run:
            print("[dry-run] 不会写入任何文件。\n")

        print(f"项目根：{root}")
        print(f"项目 settings：{_settings_path(root)}")
        print(f"项目 hooks 目录：{_hooks_dir(root)}")

        if args.uninstall:
            changed = uninstall(args.only, dry_run=args.dry_run, project_dir=root)
            action = "卸载"
        else:
            changed = install(args.only, dry_run=args.dry_run, project_dir=root)
            action = "安装"
    except InstallError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)

    if not changed:
        print(f"\n无需{action}，当前项目状态已对齐。")
    elif args.dry_run:
        print(f"\n[dry-run 结束] 不带 --dry-run 再跑一次即可应用当前项目{action}。")
    else:
        if args.uninstall:
            print("\n当前项目 hook 卸载完成。重启 Claude Code 让 hook 生效变更。")
        else:
            print("\n当前项目 hook 安装完成。重启 Claude Code 让 hook 生效。")


if __name__ == "__main__":
    main()
