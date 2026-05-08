#!/usr/bin/env python3
"""project-token-insights · 首轮组件重建与估算。

PLAN 说明：JSONL transcript 里 `message.container` 为 null，无法从单轮
直接拆 system/tools/memory。因此改为"双源重建"：

1. 从文件系统估算各组件（文件读取 + 扫描）
2. 与当前项目每个 session 冷启动首轮的 cache_creation_input_tokens p50 做校准
3. 写出 `$CLAUDE_PROJECT_DIR/.project-token-insights/first-turn-baseline.json`

估算：estimate_tokens(text) = round(bytes / 3.5)。误差约 ±15%，
定性用于"哪个组件占大头"。校准误差 > 20% 时在报告里标注"估算不可信"。

组件覆盖（PLAN 表）：
  - 全局 CLAUDE.md        ~/.claude/CLAUDE.md
  - 项目 CLAUDE.md        $CLAUDE_PROJECT_DIR/CLAUDE.md
  - 插件描述汇总          settings.enabledPlugins + 当前插件根 + plugin manifests
  - Skill 描述汇总        项目/用户/启用插件 skills/*/SKILL.md metadata
  - Agent 定义汇总        项目/用户/启用插件 agents/**/*.md frontmatter (name + description)
  - 工具 schema 汇总      硬编码查表（Claude Code 无官方 API 导出）
  - Auto memory 注入      项目 memory/MEMORY.md 首 200 行或 25KB
  - 用户首条消息          当前项目每个 session 第 1 条 user message 的平均值
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

DATA_DIRNAME = ".project-token-insights"
BASELINE_FILENAME = "first-turn-baseline.json"
GITIGNORE_RULE = ".project-token-insights/"
PROJECTS_DIR = Path.home() / ".claude" / "projects"
GLOBAL_CLAUDE_DIR = Path.home() / ".claude"
GLOBAL_CLAUDE_MD = GLOBAL_CLAUDE_DIR / "CLAUDE.md"
GLOBAL_SETTINGS = GLOBAL_CLAUDE_DIR / "settings.json"
PLUGINS_MANIFEST = GLOBAL_CLAUDE_DIR / "plugins" / "installed_plugins.json"
PLUGINS_CACHE_DIR = GLOBAL_CLAUDE_DIR / "plugins" / "cache"
PROJECT_SETTINGS_RELS = (
    Path(".claude") / "settings.json",
    Path(".claude") / "settings.local.json",
)
PLUGIN_ROOT_ENV = "CLAUDE_PLUGIN_ROOT"
AUTO_MEMORY_DISABLE_ENV = "CLAUDE_CODE_DISABLE_AUTO_MEMORY"
AUTO_MEMORY_ENABLED_KEY = "autoMemoryEnabled"
AUTO_MEMORY_DIRECTORY_KEY = "autoMemoryDirectory"
AUTO_MEMORY_ENTRYPOINT = "MEMORY.md"
AUTO_MEMORY_STARTUP_LINE_LIMIT = 200
AUTO_MEMORY_STARTUP_BYTE_LIMIT = 25 * 1024
SKILL_METADATA_CHAR_LIMIT = 1536

# Claude Code 内置工具 schema 大致 token 估算（Claude Code 2.1.117 首轮观测）。
# 查表来源：PLAN 说明"内置工具清单是硬编码的"；这里给出保守估值。
BUILTIN_TOOL_SCHEMA_TOKENS: dict[str, int] = {
    "Bash": 350,
    "Read": 220,
    "Write": 160,
    "Edit": 320,
    "Agent": 520,
    "AskUserQuestion": 260,
    "Skill": 150,
    "TaskCreate": 180,
    "TaskUpdate": 220,
    "TaskList": 130,
    "TaskGet": 130,
    "TaskOutput": 150,
    "TaskStop": 120,
    "WebFetch": 150,
    "WebSearch": 170,
    "NotebookEdit": 260,
    "CronCreate": 180,
    "CronDelete": 110,
    "CronList": 110,
    "ScheduleWakeup": 160,
    "EnterPlanMode": 110,
    "ExitPlanMode": 110,
    "EnterWorktree": 200,
    "ExitWorktree": 200,
}

BUILTIN_SKILL_ENTRIES: tuple[tuple[str, str], ...] = (
    (
        "update-config",
        'Use this skill to configure the Claude Code harness via settings.json. '
        'Automated behaviors ("from now on when X", "each time X", "whenever X", '
        '"before/after X") require hooks configured in settings.json. Also use '
        "for permissions, env vars, hook troubleshooting, and settings files.",
    ),
    (
        "keybindings-help",
        "Use when the user wants to customize keyboard shortcuts, rebind keys, "
        "add chord bindings, or modify ~/.claude/keybindings.json.",
    ),
    (
        "simplify",
        "Review changed code for reuse, quality, and efficiency, then fix any issues found.",
    ),
    (
        "fewer-permission-prompts",
        "Scan transcripts for common read-only Bash and MCP tool calls, then add "
        "a prioritized allowlist to project .claude/settings.json.",
    ),
    (
        "loop",
        "Run a prompt or slash command on a recurring interval. Use only for "
        "recurring tasks, polling, or repeated interval work.",
    ),
    (
        "claude-api",
        "Build, debug, and optimize Claude API / Anthropic SDK apps, including "
        "prompt caching, model migrations, tool use, batch, files, citations, and memory.",
    ),
)

BUILTIN_COMMAND_ENTRIES: tuple[tuple[str, str], ...] = (
    ("init", "Initialize a new CLAUDE.md file with codebase documentation"),
    ("review", "Review a pull request"),
    ("security-review", "Complete a security review of the pending changes on the current branch"),
)

BUILTIN_AGENT_DEFINITIONS: tuple[tuple[str, str], ...] = (
    (
        "general-purpose",
        "General-purpose agent for researching complex questions, searching for code, "
        "and executing multi-step tasks.",
    ),
    (
        "Explore",
        "Fast agent specialized for exploring codebases, finding files by patterns, "
        "searching code for keywords, and answering codebase questions.",
    ),
    (
        "Plan",
        "Planning agent for turning ambiguous requests into scoped implementation plans "
        "before code changes begin.",
    ),
    (
        "claude-code-guide",
        "Use for questions about Claude Code, Claude Agent SDK, and Claude API features, "
        "settings, hooks, slash commands, MCP servers, and integrations.",
    ),
    (
        "statusline-setup",
        "Use this agent to configure the user's Claude Code status line setting.",
    ),
)


# ── Tokenizer (启发式) ───────────────────────────────────────────────

def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    byte_len = len(text.encode("utf-8"))
    return max(1, round(byte_len / 3.5))


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return None


def _read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _project_root(override: str | None = None) -> Path:
    root = override or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    return Path(root).resolve()


def project_data_dir(project_dir: str | Path | None = None) -> Path:
    return _project_root(str(project_dir) if project_dir is not None else None) / DATA_DIRNAME


def ensure_gitignore(project_dir: str | Path | None = None) -> None:
    project_root = _project_root(str(project_dir) if project_dir is not None else None)
    gitignore = project_root / ".gitignore"
    try:
        current = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    except OSError:
        current = ""
    rules = {line.strip().rstrip("/") for line in current.splitlines() if line.strip()}
    if GITIGNORE_RULE.rstrip("/") in rules:
        return
    prefix = "" if not current or current.endswith("\n") else "\n"
    try:
        gitignore.write_text(f"{current}{prefix}{GITIGNORE_RULE}\n", encoding="utf-8")
    except OSError:
        return


def _encode_project_path(path: str) -> str:
    """PLAN: '/', '.', '_' 统一映射为 '-'。"""
    return re.sub(r"[/._]", "-", path)


def _project_settings_paths(project_root: Path) -> tuple[Path, ...]:
    return tuple(project_root / rel for rel in PROJECT_SETTINGS_RELS)


def _settings_chain(project_root: Path) -> list[dict]:
    settings: list[dict] = []
    for path in (GLOBAL_SETTINGS, *_project_settings_paths(project_root)):
        cfg = _read_json(path)
        if isinstance(cfg, dict):
            settings.append(cfg)
    return settings


# ── CLAUDE.md ─────────────────────────────────────────────────────────

def estimate_global_claude_md() -> int:
    return estimate_tokens(_read_text(GLOBAL_CLAUDE_MD) or "")


def estimate_project_claude_md(project_root: Path) -> int:
    return estimate_tokens(_read_text(project_root / "CLAUDE.md") or "")


# ── 插件清单 & skills / agents ───────────────────────────────────────

def _boolish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).lower() in {"1", "true", "yes", "on"}


def _enabled_plugin_ids(project_root: Path) -> tuple[set[str], bool]:
    """Return enabled plugin ids from global + project settings.

    The boolean says whether an enabledPlugins map was observed at all. Older
    Claude Code installs may not have the map; in that case we fall back to all
    installed plugins rather than undercounting.
    """
    enabled: dict[str, bool] = {}
    saw_map = False
    for cfg in _settings_chain(project_root):
        plugins = cfg.get("enabledPlugins")
        if not isinstance(plugins, dict):
            continue
        saw_map = True
        for plugin_id, is_enabled in plugins.items():
            enabled[str(plugin_id)] = _boolish(is_enabled)
    return {pid for pid, is_enabled in enabled.items() if is_enabled}, saw_map


def _plugin_manifest_path(root: Path) -> Path:
    for rel in (Path(".claude-plugin") / "plugin.json", Path("plugin.json")):
        candidate = root / rel
        if candidate.is_file():
            return candidate
    return root / "plugin.json"


def _current_plugin_root(project_root: Path) -> Path | None:
    env_root = os.environ.get(PLUGIN_ROOT_ENV)
    if env_root:
        p = Path(env_root).expanduser().resolve()
        if p.is_dir():
            return p

    script_path = Path(__file__).resolve()
    for parent in script_path.parents:
        if (parent / ".claude-plugin" / "plugin.json").is_file():
            try:
                parent.relative_to(project_root)
                return parent
            except ValueError:
                return None
    return None


def iter_plugin_roots(project_root: Path) -> Iterable[Path]:
    """枚举当前项目实际启用的插件根目录。

    只纳入 settings.enabledPlugins 标记启用的插件，以及当前正在运行本
    skill 的插件根，避免把 cache 目录里存在但未启用的插件算入冷启动。
    """
    seen: set[Path] = set()
    enabled_ids, saw_enabled_map = _enabled_plugin_ids(project_root)

    current_root = _current_plugin_root(project_root)
    if current_root and current_root not in seen:
        seen.add(current_root)
        yield current_root

    # installed_plugins.json
    manifest = _read_json(PLUGINS_MANIFEST) or {}
    plugins = manifest.get("plugins") or {}
    for plugin_id, versions in plugins.items():
        if saw_enabled_map and plugin_id not in enabled_ids:
            continue
        if not isinstance(versions, list):
            continue
        for entry in versions:
            install_path = (entry or {}).get("installPath")
            if not install_path:
                continue
            p = Path(install_path).expanduser().resolve()
            if p.is_dir() and p not in seen:
                seen.add(p)
                yield p

    # Older installs may not expose enabledPlugins. Only then use the cache as a
    # compatibility fallback; modern configs avoid this to stay project-scoped.
    if not saw_enabled_map and PLUGINS_CACHE_DIR.is_dir():
        for child in PLUGINS_CACHE_DIR.rglob("plugin.json"):
            p = (child.parent.parent if child.parent.name == ".claude-plugin" else child.parent).resolve()
            if p not in seen:
                seen.add(p)
                yield p


def estimate_plugin_descriptions(plugin_roots: list[Path]) -> tuple[int, list[dict]]:
    """插件本身的 description / 名称等注入 token。"""
    total = 0
    detail: list[dict] = []
    for root in plugin_roots:
        manifest = _read_json(_plugin_manifest_path(root)) or {}
        name = manifest.get("name") or root.name
        desc = manifest.get("description") or ""
        tok = estimate_tokens(f"{name}\n{desc}")
        total += tok
        detail.append({"name": name, "tokens": tok, "root": str(root)})
    return total, detail


def _user_skills_dir() -> Path:
    return GLOBAL_SETTINGS.parent / "skills"


def _strip_frontmatter(text: str) -> str:
    return re.sub(r"^---\s*\n.*?\n---\s*\n?", "", text, count=1, flags=re.DOTALL)


def _first_body_paragraph(text: str) -> str:
    body = _strip_frontmatter(text).strip()
    for block in re.split(r"\n\s*\n", body):
        lines = [
            line.strip()
            for line in block.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        paragraph = " ".join(lines).strip()
        if paragraph:
            return paragraph
    return ""


def _skill_metadata(text: str, fm: dict[str, str]) -> tuple[str, str]:
    description = fm.get("description") or _first_body_paragraph(text)
    description_source = "frontmatter" if fm.get("description") else "body"
    when_to_use = fm.get("when_to_use") or fm.get("when-to-use") or ""
    metadata = "\n".join(part for part in (description, when_to_use) if part).strip()
    return metadata[:SKILL_METADATA_CHAR_LIMIT], description_source


def _scan_skill_dir(skills_dir: Path, source: str, extra: dict[str, str] | None = None) -> tuple[int, list[dict]]:
    total = 0
    detail: list[dict] = []
    if not skills_dir.is_dir():
        return total, detail
    seen: set[Path] = set()
    visited_dirs: set[Path] = set()
    skill_mds: list[Path] = []
    for child in sorted(skills_dir.iterdir(), key=lambda p: p.name):
        if not child.is_dir():
            continue
        real_dir = child.resolve()
        if real_dir in visited_dirs:
            continue
        visited_dirs.add(real_dir)
        skill_md = child / "SKILL.md"
        if skill_md.is_file():
            skill_mds.append(skill_md)
    for skill_md in sorted(skill_mds):
        resolved = skill_md.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        text = _read_text(skill_md) or ""
        fm = _parse_frontmatter(text)
        if _boolish(fm.get("disable-model-invocation", False)):
            continue
        rel_path = str(skill_md.parent.relative_to(skills_dir))
        name = skill_md.parent.name
        metadata, description_source = _skill_metadata(text, fm)
        tok = estimate_tokens(f"{name}: {metadata}")
        total += tok
        item = {
            "name": name,
            "frontmatter_name": fm.get("name") or "",
            "rel_path": rel_path,
            "tokens": tok,
            "full_tokens": estimate_tokens(text),
            "metadata_chars": len(metadata),
            "description_source": description_source,
            "source": source,
            "kind": "skill",
            "path": str(skill_md),
        }
        if extra:
            item.update(extra)
        detail.append(item)
    return total, detail


def _builtin_skill_entries() -> tuple[int, list[dict]]:
    total = 0
    detail: list[dict] = []
    for kind, entries in (
        ("builtin_skill", BUILTIN_SKILL_ENTRIES),
        ("builtin_command", BUILTIN_COMMAND_ENTRIES),
    ):
        for name, description in entries:
            tok = estimate_tokens(f"{name}: {description}")
            total += tok
            detail.append({
                "name": name,
                "rel_path": name,
                "tokens": tok,
                "full_tokens": tok,
                "metadata_chars": len(description),
                "description_source": "builtin",
                "source": "builtin",
                "kind": kind,
            })
    return total, detail


def _scan_plugin_commands(root: Path) -> tuple[int, list[dict]]:
    commands_dir = root / "commands"
    if not commands_dir.is_dir():
        return 0, []
    manifest = _read_json(_plugin_manifest_path(root)) or {}
    plugin_name = manifest.get("name") or root.name
    total = 0
    detail: list[dict] = []
    seen: set[Path] = set()
    for command_md in sorted(commands_dir.rglob("*.md")):
        resolved = command_md.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        text = _read_text(command_md) or ""
        fm = _parse_frontmatter(text)
        description = fm.get("description") or _first_body_paragraph(text)
        rel = command_md.relative_to(commands_dir).with_suffix("")
        command_name = str(rel).replace(os.sep, ":")
        display_name = f"{plugin_name}:{command_name}"
        tok = estimate_tokens(f"{display_name}: {description}")
        total += tok
        detail.append({
            "name": display_name,
            "rel_path": str(rel),
            "tokens": tok,
            "full_tokens": estimate_tokens(text),
            "metadata_chars": len(description),
            "description_source": "frontmatter" if fm.get("description") else "body",
            "source": "plugin",
            "kind": "plugin_command",
            "plugin": plugin_name,
            "plugin_root": str(root),
            "path": str(command_md),
        })
    return total, detail


def estimate_skill_descriptions(project_root: Path, plugin_roots: list[Path]) -> tuple[int, list[dict]]:
    total, detail = _builtin_skill_entries()
    for source, skills_dir in (
        ("project", project_root / ".claude" / "skills"),
        ("user", _user_skills_dir()),
    ):
        tok, items = _scan_skill_dir(skills_dir, source)
        total += tok
        detail.extend(items)

    for root in plugin_roots:
        tok, items = _scan_skill_dir(
            root / "skills",
            "plugin",
            {"plugin": root.name, "plugin_root": str(root)},
        )
        total += tok
        detail.extend(items)
        tok, items = _scan_plugin_commands(root)
        total += tok
        detail.extend(items)
    return total, detail


def _user_agents_dir() -> Path:
    return GLOBAL_SETTINGS.parent / "agents"


def _scan_agent_dir(agent_dir: Path, source: str, extra: dict[str, str] | None = None) -> tuple[int, list[dict]]:
    total = 0
    detail: list[dict] = []
    if not agent_dir.is_dir():
        return total, detail
    seen: set[Path] = set()
    visited_dirs: set[Path] = set()
    agent_mds: list[Path] = []
    for dirpath, _dirnames, filenames in os.walk(agent_dir, followlinks=True):
        real_dir = Path(dirpath).resolve()
        if real_dir in visited_dirs:
            _dirnames[:] = []
            continue
        visited_dirs.add(real_dir)
        for fn in filenames:
            if fn.endswith(".md"):
                agent_mds.append(Path(dirpath) / fn)
    for agent_md in sorted(agent_mds):
        resolved = agent_md.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        text = _read_text(agent_md) or ""
        rel = agent_md.relative_to(agent_dir).with_suffix("")
        fm = _parse_frontmatter(text)
        if not (fm.get("name") and fm.get("description")):
            continue
        name = fm.get("name") or str(rel)
        description = fm.get("description") or ""
        tok = estimate_tokens(f"{name}: {description}")
        full_tok = estimate_tokens(text)
        total += tok
        item = {
            "name": name,
            "rel_path": str(rel),
            "tokens": tok,
            "full_tokens": full_tok,
            "source": source,
            "kind": "agent",
            "path": str(agent_md),
        }
        if extra:
            item.update(extra)
        detail.append(item)
    return total, detail


def _builtin_agent_definitions() -> tuple[int, list[dict]]:
    total = 0
    detail: list[dict] = []
    for name, description in BUILTIN_AGENT_DEFINITIONS:
        text = f"{name}: {description}"
        tok = estimate_tokens(text)
        total += tok
        detail.append({
            "name": name,
            "rel_path": name,
            "tokens": tok,
            "full_tokens": tok,
            "source": "builtin",
            "kind": "builtin_agent",
            "path": None,
        })
    return total, detail


def estimate_agent_definitions(project_root: Path, plugin_roots: list[Path]) -> tuple[int, list[dict]]:
    total, detail = _builtin_agent_definitions()

    for source, agent_dir in (
        ("project", project_root / ".claude" / "agents"),
        ("user", _user_agents_dir()),
    ):
        tok, items = _scan_agent_dir(agent_dir, source)
        total += tok
        detail.extend(items)

    for root in plugin_roots:
        tok, items = _scan_agent_dir(
            root / "agents",
            "plugin",
            {"plugin": root.name, "plugin_root": str(root)},
        )
        total += tok
        detail.extend(items)
    return total, detail


def _parse_frontmatter(text: str) -> dict[str, str]:
    m = re.match(r"---\s*\n(.*?)\n---", text, re.DOTALL)
    if not m:
        return {}
    out: dict[str, str] = {}
    lines = m.group(1).splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.startswith((" ", "\t")) or ":" not in line:
            i += 1
            continue
        key, _, raw_value = line.partition(":")
        key = key.strip()
        value = raw_value.strip()
        block_style = value[:1]
        if value == "" and key in {"description", "when_to_use", "when-to-use"}:
            i += 1
            block: list[str] = []
            while i < len(lines):
                next_line = lines[i]
                if next_line and not next_line.startswith((" ", "\t")) and ":" in next_line:
                    break
                block.append(next_line.strip())
                i += 1
            out[key] = " ".join(part for part in block if part).strip()
            continue
        if block_style in {">", "|"}:
            i += 1
            block: list[str] = []
            while i < len(lines):
                next_line = lines[i]
                if next_line and not next_line.startswith((" ", "\t")) and ":" in next_line:
                    break
                block.append(next_line.strip())
                i += 1
            if block_style == ">":
                out[key] = " ".join(part for part in block if part).strip()
            else:
                out[key] = "\n".join(block).strip()
            continue
        out[key] = value.strip().strip('"').strip("'")
        i += 1
    return out


# ── 工具 schema ───────────────────────────────────────────────────────

def estimate_tool_schemas() -> tuple[int, dict[str, int]]:
    return sum(BUILTIN_TOOL_SCHEMA_TOKENS.values()), dict(BUILTIN_TOOL_SCHEMA_TOKENS)


# ── Auto memory ───────────────────────────────────────────────────────

def _truthy(value: Any) -> bool:
    return _boolish(value)


def _git_repo_root(project_root: Path) -> Path:
    for candidate in (project_root, *project_root.parents):
        if (candidate / ".git").exists():
            return candidate
    return project_root


def _auto_memory_disabled(project_root: Path) -> bool:
    if _truthy(os.environ.get(AUTO_MEMORY_DISABLE_ENV)):
        return True

    enabled_setting: Any = None
    for cfg in _settings_chain(project_root):
        env = cfg.get("env") if isinstance(cfg, dict) else {}
        if isinstance(env, dict) and _truthy(env.get(AUTO_MEMORY_DISABLE_ENV)):
            return True
        if AUTO_MEMORY_ENABLED_KEY in cfg:
            enabled_setting = cfg.get(AUTO_MEMORY_ENABLED_KEY)
    return enabled_setting is False


def _auto_memory_dir(project_root: Path) -> Path:
    # 官方文档：autoMemoryDirectory 接受 user/local settings，不接受 shared project settings。
    candidates = [GLOBAL_SETTINGS, project_root / ".claude" / "settings.local.json"]
    for path in candidates:
        cfg = _read_json(path)
        if not isinstance(cfg, dict):
            continue
        directory = cfg.get(AUTO_MEMORY_DIRECTORY_KEY)
        if isinstance(directory, str) and directory.strip():
            return Path(directory).expanduser().resolve()

    repo_root = _git_repo_root(project_root)
    return PROJECTS_DIR / _encode_project_path(str(repo_root)) / "memory"


def _read_auto_memory_startup_text(memory_md: Path) -> str:
    text = _read_text(memory_md) or ""
    first_lines = "\n".join(text.splitlines()[:AUTO_MEMORY_STARTUP_LINE_LIMIT])
    raw = first_lines.encode("utf-8")[:AUTO_MEMORY_STARTUP_BYTE_LIMIT]
    return raw.decode("utf-8", errors="ignore")


def estimate_auto_memory(project_root: Path | None = None) -> tuple[int, bool]:
    project_root = (project_root or _project_root()).resolve()
    disabled = _auto_memory_disabled(project_root)
    if disabled:
        return 0, True

    memory_md = _auto_memory_dir(project_root) / AUTO_MEMORY_ENTRYPOINT
    return estimate_tokens(_read_auto_memory_startup_text(memory_md)), False


# ── 首轮 JSONL 样本 ────────────────────────────────────────────────────

def _is_cold_first_turn_record(record: dict) -> bool:
    usage = ((record.get("message") or {}).get("usage")) or {}
    return (
        usage.get("cache_read_input_tokens") == 0
        and int(usage.get("cache_creation_input_tokens") or 0) > 0
        and not bool(record.get("isSidechain"))
    )


def find_first_turn_records(project_root: Path) -> list[dict]:
    """每个顶层 session 取第一条 cache 完全未命中的 assistant record。"""
    encoded = _encode_project_path(str(project_root))
    proj_dir = PROJECTS_DIR / encoded
    if not proj_dir.is_dir():
        return []
    candidates: list[tuple[str, dict, Path]] = []
    for jf in proj_dir.glob("*.jsonl"):
        try:
            with jf.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if _is_cold_first_turn_record(record):
                        ts = record.get("timestamp") or ""
                        candidates.append((ts, record, jf))
                        break
        except OSError:
            continue
    candidates.sort(key=lambda x: x[0])
    return [record for _, record, _ in candidates]


def find_first_turn_record(project_root: Path) -> dict | None:
    """Backward-compatible representative: earliest cold first-turn sample."""
    records = find_first_turn_records(project_root)
    return records[0] if records else None


def extract_user_first_message_samples(project_root: Path) -> list[int]:
    """每个顶层 session 第一条 user message 的估算 token。"""
    encoded = _encode_project_path(str(project_root))
    proj_dir = PROJECTS_DIR / encoded
    if not proj_dir.is_dir():
        return []
    samples: list[int] = []
    sessions = sorted(proj_dir.glob("*.jsonl"))
    for jf in sessions:
        try:
            with jf.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if record.get("type") == "user":
                        msg = record.get("message") or {}
                        content = msg.get("content")
                        if isinstance(content, str):
                            samples.append(estimate_tokens(content))
                            break
                        if isinstance(content, list):
                            text = "".join(
                                part.get("text", "") for part in content
                                if isinstance(part, dict) and part.get("type") == "text"
                            )
                            samples.append(estimate_tokens(text))
                            break
        except OSError:
            continue
    return samples


def extract_user_first_message(project_root: Path) -> int:
    """Representative first user-message size: average across current project sessions."""
    samples = extract_user_first_message_samples(project_root)
    return round(sum(samples) / len(samples)) if samples else 0


def _stats(values: list[int]) -> dict[str, int | None]:
    if not values:
        return {"count": 0, "min": None, "p50": None, "avg": None, "p75": None, "max": None}
    ordered = sorted(values)

    def percentile(pct: float) -> int:
        idx = round((len(ordered) - 1) * pct)
        return ordered[idx]

    return {
        "count": len(ordered),
        "min": ordered[0],
        "p50": percentile(0.5),
        "avg": round(sum(ordered) / len(ordered)),
        "p75": percentile(0.75),
        "max": ordered[-1],
    }


# ── 聚合 ─────────────────────────────────────────────────────────────

def build_baseline(project_dir: str | None = None) -> dict:
    project_root = _project_root(project_dir)
    plugin_roots = list(iter_plugin_roots(project_root))

    global_md_tok = estimate_global_claude_md()
    project_md_tok = estimate_project_claude_md(project_root)
    plugin_total, plugin_detail = estimate_plugin_descriptions(plugin_roots)
    skill_total, skill_detail = estimate_skill_descriptions(project_root, plugin_roots)
    agent_total, agent_detail = estimate_agent_definitions(project_root, plugin_roots)
    tool_total, tool_detail = estimate_tool_schemas()
    auto_mem_tok, auto_mem_disabled = estimate_auto_memory(project_root)
    user_first_samples = extract_user_first_message_samples(project_root)
    user_first_tok = round(sum(user_first_samples) / len(user_first_samples)) if user_first_samples else 0

    sum_components = (
        global_md_tok + project_md_tok + plugin_total + skill_total
        + agent_total + tool_total + auto_mem_tok + user_first_tok
    )

    records = find_first_turn_records(project_root)
    cold_cache_creation_values: list[int] = []
    cold_input_values: list[int] = []
    cold_samples: list[dict] = []
    for record in records:
        usage = (record.get("message") or {}).get("usage") or {}
        cache_creation = int(usage.get("cache_creation_input_tokens") or 0)
        input_tokens = int(usage.get("input_tokens") or 0)
        cold_cache_creation_values.append(cache_creation)
        cold_input_values.append(input_tokens)
        cold_samples.append({
            "session_id": record.get("sessionId") or "",
            "timestamp": record.get("timestamp") or "",
            "model": (record.get("message") or {}).get("model"),
            "input_tokens": input_tokens,
            "cache_creation_input_tokens": cache_creation,
        })

    cold_cache_stats = _stats(cold_cache_creation_values)
    actual_cache_creation = int(cold_cache_stats.get("p50") or 0)
    confidence = "中"
    if actual_cache_creation > 0:
        diff_ratio = abs(sum_components - actual_cache_creation) / actual_cache_creation
        confidence = "高" if diff_ratio <= 0.2 else "低"
    else:
        confidence = "中"  # 无样本可校准

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "project_root": str(project_root),
        "project_name": project_root.name,
        "global_claude_md": global_md_tok,
        "project_claude_md": project_md_tok,
        "plugin_descriptions": plugin_total,
        "plugin_descriptions_detail": plugin_detail,
        "skill_descriptions": skill_total,
        "skill_descriptions_detail": skill_detail,
        "agent_definitions": agent_total,
        "agent_definitions_detail": agent_detail,
        "tool_schemas": tool_total,
        "tool_schema_detail": tool_detail,
        "auto_memory": auto_mem_tok,
        "auto_memory_disabled": auto_mem_disabled,
        "user_first_message": user_first_tok,
        "user_first_message_stats": _stats(user_first_samples),
        "first_turn_total": sum_components,
        "cold_start_samples": cold_samples,
        "cold_start_cache_creation_stats": cold_cache_stats,
        "cold_start_input_stats": _stats(cold_input_values),
        "calibration": {
            "actual_cache_creation_input_tokens": actual_cache_creation,
            "actual_cache_creation_input_tokens_basis": "p50 of current-project cold-start sessions",
            "sample_count": len(cold_cache_creation_values),
            "diff_ratio": (
                None if actual_cache_creation == 0
                else round(abs(sum_components - actual_cache_creation) / actual_cache_creation, 3)
            ),
            "note": (
                "无当前项目冷启动 JSONL 样本可对齐，估算置信度中等。"
                if actual_cache_creation == 0
                else "估算不可信，请以 JSONL 实际值为准。" if confidence == "低"
                else "估算与实测误差 ≤ 20%，可信。"
            ),
        },
        "confidence": confidence,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="生成首轮组件 token 估算基线。")
    parser.add_argument("--project-dir", default=None, help="项目根路径（默认 $CLAUDE_PROJECT_DIR 或 cwd）。")
    parser.add_argument("--pretty", action="store_true", help="stdout 输出缩进 JSON。")
    parser.add_argument("--no-write", action="store_true", help="不写入 baseline 文件，仅打印。")
    args = parser.parse_args()

    baseline = build_baseline(args.project_dir)
    text = json.dumps(baseline, ensure_ascii=False, indent=2 if args.pretty else None)
    sys.stdout.write(text + "\n")

    if not args.no_write:
        data_dir = project_data_dir(args.project_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        ensure_gitignore(args.project_dir)
        (data_dir / BASELINE_FILENAME).write_text(
            json.dumps(baseline, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
