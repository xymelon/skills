#!/usr/bin/env python3
"""project-token-insights · 中文优化报告生成器。

基于 `first-turn-baseline.json` 与本地 settings/CLAUDE.md 等可观察数据源，
对齐 PLAN 的 2.1-2.6 六个优化方向生成中文 Markdown 报告。

每条建议包含：
  - title / 方向 / 置信度（高 / 中 / 低）
  - 当前估算 token
  - 节省估算（或量化说明）
  - 具体操作（settings.json 字段 / 文件路径 / 命令）

使用方法：
  python3 optimization_report.py                # stdout Markdown
  python3 optimization_report.py --json         # stdout JSON
  python3 optimization_report.py --out FILE     # 写入指定文件
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DATA_DIRNAME = ".project-token-insights"
BASELINE_FILENAME = "first-turn-baseline.json"
REPORT_FILENAME = "optimization-report.md"
GLOBAL_CLAUDE_MD = Path.home() / ".claude" / "CLAUDE.md"
GLOBAL_SETTINGS = Path.home() / ".claude" / "settings.json"
PROJECT_SETTINGS_RELS = (
    Path(".claude") / "settings.json",
    Path(".claude") / "settings.local.json",
)
AGENT_TEAMS_ENV = "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"
AUTO_MEMORY_DISABLE_ENV = "CLAUDE_CODE_DISABLE_AUTO_MEMORY"
AUTO_MEMORY_ENABLED_KEY = "autoMemoryEnabled"
CRON_DISABLE_ENV = "CLAUDE_CODE_DISABLE_CRON"

DIRECTIONS: dict[str, str] = {
    "top_heavy": "2.1 清理 CLAUDE.md / 插件 / Skills",
    "unused_agents": "2.2 清理未使用 Agent",
    "agent_team": "2.3 不开启实验 Agent Team",
    "auto_memory": "2.4 禁用 Auto memory",
    "cold_tools": "2.5 控制冷门高成本工具",
    "other": "2.6 其他可优化项",
}


# ── Helpers ───────────────────────────────────────────────────────────

def _approx(n: int) -> str:
    return f"约 {n:,} tok"


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return None


def _project_root(override: str | None = None) -> Path:
    root = override or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    return Path(root).resolve()


def _estimate_tokens(text: str) -> int:
    return max(1, round(len(text.encode("utf-8")) / 3.5))


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).lower() in {"1", "true", "yes", "on"}


def _project_settings(project_root: Path) -> list[dict]:
    settings: list[dict] = []
    for rel in PROJECT_SETTINGS_RELS:
        cfg = _read_json(project_root / rel)
        if isinstance(cfg, dict):
            settings.append(cfg)
    return settings


# ── 2.1 大头组件 Top 3 ────────────────────────────────────────────────

def recommend_top_heavy(baseline: dict | None) -> dict:
    rec: dict[str, Any] = {
        "direction": DIRECTIONS["top_heavy"],
        "title": "大头组件 Top 3",
        "confidence": "高",
        "current_tokens": 0,
        "estimated_savings_tok": 0,
        "findings": [],
        "actions": [],
    }
    if not baseline:
        rec["confidence"] = "低"
        rec["findings"].append("尚未生成首轮基线（缺少 first-turn-baseline.json）。")
        rec["actions"].append("运行 `/project-token-insights` 生成首轮基线后重试。")
        return rec
    rec["confidence"] = baseline.get("confidence") or rec["confidence"]

    component_labels = {
        "global_claude_md": "全局 CLAUDE.md",
        "project_claude_md": "项目 CLAUDE.md",
        "plugin_descriptions": "插件描述汇总",
        "skill_descriptions": "Skill 描述汇总",
        "agent_definitions": "Agent 定义汇总",
        "tool_schemas": "工具 schema 汇总",
        "auto_memory": "Auto memory 注入",
        "user_first_message": "用户首条消息",
    }
    items = [
        (label, int(baseline.get(key, 0) or 0), key)
        for key, label in component_labels.items()
    ]
    items.sort(key=lambda x: x[1], reverse=True)
    top3 = items[:3]
    rec["current_tokens"] = sum(i[1] for i in items)
    rec["estimated_savings_tok"] = max(0, sum(i[1] for i in top3) // 3)  # 保守 1/3 可压缩
    cold_stats = baseline.get("cold_start_cache_creation_stats") or {}
    calibration = baseline.get("calibration") or {}
    if cold_stats.get("count"):
        rec["findings"].append(
            "当前项目冷启动样本："
            f"{cold_stats['count']} 个 session，"
            f"p50 {_approx(int(cold_stats.get('p50') or 0))}，"
            f"p75 {_approx(int(cold_stats.get('p75') or 0))}。"
        )
    if calibration.get("actual_cache_creation_input_tokens"):
        actual = int(calibration.get("actual_cache_creation_input_tokens") or 0)
        diff = calibration.get("diff_ratio")
        if diff is not None:
            rec["findings"].append(
                f"基线校准：组件估算 {_approx(rec['current_tokens'])} / 实测 p50 {_approx(actual)}，"
                f"误差约 {int(float(diff) * 100)}%，置信度 {rec['confidence']}。"
            )
    for label, tok, key in top3:
        rec["findings"].append(f"{label}: {_approx(tok)}")
    rec["actions"].extend([
        "针对 Top 3 组件做压缩 30-50% 的精简：合并重复规则、去掉历史注释、把示例移到外部文档。",
        "编辑入口：`~/.claude/CLAUDE.md`、`$CLAUDE_PROJECT_DIR/CLAUDE.md`、项目/用户/启用插件的 `skills/*/SKILL.md`、`commands/*.md`、`agents/**/*.md`。",
        "提交后重新运行 `/project-token-insights`，对比新旧基线确认节省。",
    ])
    return rec


# ── 2.2 未使用 Agent 清单 ──────────────────────────────────────────────

def recommend_unused_agents(agent_defs: list[dict]) -> dict:
    rec: dict[str, Any] = {
        "direction": DIRECTIONS["unused_agents"],
        "title": "Agent 定义体量与待核查清单",
        "confidence": "中",
        "current_tokens": 0,
        "estimated_savings_tok": 0,
        "findings": [],
        "actions": [],
    }
    if not agent_defs:
        rec["confidence"] = "低"
        rec["findings"].append("未扫描到 agent 定义。")
        return rec

    rec["current_tokens"] = sum(int(a.get("tokens", 0) or 0) for a in agent_defs)

    def source_label(agent: dict) -> str:
        source = agent.get("source") or "plugin"
        if source == "builtin":
            return "Claude Code 内置 agent"
        if source == "project":
            return "项目 .claude/agents"
        if source == "user":
            return "用户 ~/.claude/agents"
        plugin = agent.get("plugin")
        return f"插件 {plugin}" if plugin else "插件 agents"

    source_counts: dict[str, int] = {}
    for a in agent_defs:
        source_counts[source_label(a)] = source_counts.get(source_label(a), 0) + 1
    breakdown = "、".join(f"{k} {v} 个" for k, v in sorted(source_counts.items()))

    cleanable_defs = [a for a in agent_defs if a.get("source") != "builtin"]
    builtin_count = len(agent_defs) - len(cleanable_defs)

    rec["findings"].append(
        f"共扫到 {len(agent_defs)} 个 agent 定义（{breakdown}），"
        f"其中 {len(cleanable_defs)} 个属于项目/用户/插件可调整范围。"
    )
    rec["findings"].append("当前精简版不读取历史轮次数据，因此只列出体量较大的待核查 agent，不能自动判定最近是否未使用。")
    if builtin_count:
        rec["findings"].append(f"另有 {builtin_count} 个 Claude Code 内置 agent 会出现在首轮列表中，但不属于项目可清理项。")
    if not cleanable_defs:
        rec["findings"].append("没有发现项目/用户/插件级 agent 可清理项。")
        return rec
    candidates = sorted(cleanable_defs, key=lambda x: int(x.get("tokens", 0) or 0), reverse=True)
    rec["estimated_savings_tok"] = sum(int(a.get("tokens", 0) or 0) for a in candidates)
    for a in candidates:
        name = a.get("name", "")
        tok = int(a.get("tokens", 0) or 0)
        rec["findings"].append(f"{name}: {_approx(tok)}（{source_label(a)}，待确认是否仍需要）")
    rec["actions"].extend([
        "按来源核查并清理低频 agent：项目 `.claude/agents/*.md`、用户 `~/.claude/agents/*.md`、启用插件 `agents/*.md`。",
        "若某个用户级 agent 只服务单一项目，迁到该项目 `.claude/agents/`；若使用 ccforge，可在 `plugins/ccforge/plugin.json` 的 `agents` 字段注释掉插件 agent。",
        "调整后重新运行 `/project-token-insights`，观察 `agent_definitions` 基线是否下降。",
    ])
    return rec


# ── 2.1 Skill 清单（随 Top 3 一起归入 2.1） ─────────────────────────────

def recommend_skill_inventory(skill_defs: list[dict]) -> dict:
    rec: dict[str, Any] = {
        "direction": DIRECTIONS["top_heavy"],
        "title": "Skill 清单",
        "confidence": "高",
        "current_tokens": sum(int(s.get("tokens", 0) or 0) for s in skill_defs),
        "estimated_savings_tok": 0,
        "findings": [],
        "actions": [],
    }
    if not skill_defs:
        rec["confidence"] = "低"
        rec["findings"].append("未扫描到启用的 skill 定义。")
        return rec

    def source_label(s: dict) -> str:
        source = s.get("source") or "plugin"
        if source == "builtin":
            return "Claude Code 内置入口"
        if source == "project":
            return "项目 .claude/skills"
        if source == "user":
            return "用户 ~/.claude/skills"
        plugin = s.get("plugin")
        if s.get("kind") == "plugin_command":
            return f"插件 {plugin} commands" if plugin else "插件 commands"
        return f"插件 {plugin} skills" if plugin else "插件 skills"

    source_counts: dict[str, int] = {}
    for s in skill_defs:
        source_counts[source_label(s)] = source_counts.get(source_label(s), 0) + 1
    breakdown = "、".join(f"{k} {v} 个" for k, v in sorted(source_counts.items()))
    rec["findings"].append(
        f"共扫到 {len(skill_defs)} 个模型可见 skill/command 入口（{breakdown}，`disable-model-invocation: true` 已排除）。"
    )
    for s in sorted(skill_defs, key=lambda x: int(x.get("tokens", 0) or 0), reverse=True):
        name = s.get("name") or s.get("rel_path") or ""
        tok = int(s.get("tokens", 0) or 0)
        full = int(s.get("full_tokens", 0) or 0)
        rec["findings"].append(
            f"{name}: {_approx(tok)} metadata / {_approx(full)} 全文（{source_label(s)}）"
        )
    rec["actions"].extend([
        "精简 SKILL.md frontmatter 的 `description` 和 `when_to_use`（只有这两项 + name 进入首轮常驻）。",
        "精简插件 `commands/*.md` frontmatter 的 `description`；这些 slash command 也会作为模型可见入口进入首轮。",
        "只在特定项目需要的 skill 不要放到 `~/.claude/skills/` 或全局插件；改装到项目 `.claude/skills/` 并在未启用时移除。",
        "低频 skill 可加 `disable-model-invocation: true` frontmatter，仅保留显式调用入口，避免 metadata 常驻首轮。",
    ])
    return rec


# ── 2.3 Agent 并发开销 ────────────────────────────────────────────────

def recommend_agent_team_cost(settings_sources: list[dict] | None = None) -> dict:
    rec: dict[str, Any] = {
        "direction": DIRECTIONS["agent_team"],
        "title": "实验 Agent Team 开启状态",
        "confidence": "中",
        "current_tokens": 0,
        "estimated_savings_tok": 0,
        "findings": [],
        "actions": [],
    }
    settings_sources = settings_sources or []
    enabled_locations: list[str] = []
    for idx, cfg in enumerate(settings_sources):
        env = cfg.get("env") if isinstance(cfg, dict) else {}
        if isinstance(env, dict) and _truthy(env.get(AGENT_TEAMS_ENV)):
            label = "全局 settings" if idx == 0 else "项目 settings"
            enabled_locations.append(label)
    if _truthy(os.environ.get(AGENT_TEAMS_ENV)):
        enabled_locations.append("当前环境变量")
    if enabled_locations:
        rec["confidence"] = "高"
        rec["findings"].append(
            f"`{AGENT_TEAMS_ENV}` 已在 {'、'.join(enabled_locations)} 中开启，当前会话可能加载实验 Agent teams。"
        )
    else:
        rec["findings"].append(
            f"未在已读取 settings 的 env 中发现 `{AGENT_TEAMS_ENV}`；按官方文档，Agent teams 默认关闭。"
        )

    rec["findings"].append("当前精简版不读取历史轮次数据，因此不统计单轮并发 Agent 调用次数。")
    rec["actions"].extend([
        "官方文档说明 Agent teams 是实验功能且默认关闭；不要在 `settings.json` 或环境变量里设置 `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS`。",
        "如果已开启，移除 `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS` 后重启 Claude Code；常规 subagent 调用仍可保留，但应避免一次性并发拉起多个 agent。",
        "在 workflow/prompt 里要求顺序调度，或合并为单个 agent 顺序完成多步，减少重复 system prompt / tools 重建。",
    ])
    return rec


# ── 2.4 Auto memory 禁用诊断 ──────────────────────────────────────────

def _auto_memory_disabled_from_settings(settings_sources: list[dict] | None = None) -> bool:
    if _truthy(os.environ.get(AUTO_MEMORY_DISABLE_ENV)):
        return True

    enabled_setting: Any = None
    for cfg in settings_sources or []:
        env = cfg.get("env") if isinstance(cfg, dict) else {}
        if isinstance(env, dict) and _truthy(env.get(AUTO_MEMORY_DISABLE_ENV)):
            return True
        if isinstance(cfg, dict) and AUTO_MEMORY_ENABLED_KEY in cfg:
            enabled_setting = cfg.get(AUTO_MEMORY_ENABLED_KEY)
    return enabled_setting is False


def recommend_auto_memory(settings_sources: list[dict] | None, baseline: dict | None) -> dict:
    rec: dict[str, Any] = {
        "direction": DIRECTIONS["auto_memory"],
        "title": "Auto memory 开销诊断",
        "confidence": "高",
        "current_tokens": int((baseline or {}).get("auto_memory", 0) or 0),
        "estimated_savings_tok": 0,
        "findings": [],
        "actions": [],
    }
    disabled_flag = _auto_memory_disabled_from_settings(settings_sources)
    if disabled_flag:
        rec["findings"].append("Auto memory 已通过 settings 或当前环境变量禁用，无需处理。")
        return rec
    rec["findings"].append("Auto memory 当前未禁用，若估算 > 0 表明已在首轮注入。")
    rec["estimated_savings_tok"] = rec["current_tokens"]
    rec["actions"].extend([
        "优先在项目 `.claude/settings.local.json` 设置 `\"autoMemoryEnabled\": false`；也可在 `env` 下设置 `\"CLAUDE_CODE_DISABLE_AUTO_MEMORY\": \"1\"`。",
        "重启 Claude Code 后运行 `/project-token-insights` 重新生成基线，核对 `auto_memory` 是否降为 0。",
    ])
    return rec


# ── 2.5 冷门高成本工具 ────────────────────────────────────────────────

def recommend_cold_tools(tool_schema_tokens: dict[str, int]) -> dict:
    rec: dict[str, Any] = {
        "direction": DIRECTIONS["cold_tools"],
        "title": "高成本工具 schema",
        "confidence": "中",
        "current_tokens": sum(tool_schema_tokens.values()),
        "estimated_savings_tok": 0,
        "findings": [],
        "actions": [],
    }
    cold: list[tuple[str, int]] = []
    for tool, tok in tool_schema_tokens.items():
        if tok >= 200:
            cold.append((tool, tok))
    cold.sort(key=lambda x: x[1], reverse=True)
    rec["findings"].append("当前精简版不读取历史轮次数据，因此只按 schema 体量列出待核查工具，不能自动判定最近调用频次。")
    for tool, tok in cold[:8]:
        rec["findings"].append(f"{tool}: schema 约 {tok} tok（待确认是否常用）")
    rec["estimated_savings_tok"] = sum(tok for _, tok in cold)
    rec["actions"].extend([
        "确认某些工具确实低频后，可在启动时用 `--disallowedTools` 把对应 schema 直接从 context 中剔除，例如："
        "`claude --disallowedTools NotebookEdit CronCreate`（空格分隔多项）。",
        "若冷门项集中在 Cron 工具，可按官方环境变量设置 `CLAUDE_CODE_DISABLE_CRON=1` 关闭 scheduled tasks / cron tools。",
        "需要白名单式精简时改用 `--tools \"Bash,Edit,Read,...\"` 仅保留高频工具，默认禁其余。",
        "若只想持久阻止调用/防误用，可用 `settings.json` 的 `permissions.deny`；官方未明确保证它会减少首轮 tool schema token，节省估算以 `--disallowedTools` / `--tools` 为准。",
    ])
    if not cold:
        rec["findings"].append("未识别到 schema 体量较大的工具。")
    return rec


# ── 2.6 其他可优化项 ──────────────────────────────────────────────────

def _detect_duplicate_paragraphs(text: str) -> list[str]:
    """朴素检测：含 >=50 字符且出现 >=2 次的段落。"""
    blocks = [b.strip() for b in text.split("\n\n") if len(b.strip()) >= 50]
    seen: dict[str, int] = {}
    for b in blocks:
        seen[b] = seen.get(b, 0) + 1
    return [b[:80] + ("…" if len(b) > 80 else "") for b, c in seen.items() if c >= 2]


def recommend_other(global_md: str | None, project_md: str | None) -> dict:
    rec: dict[str, Any] = {
        "direction": DIRECTIONS["other"],
        "title": "其他可优化项",
        "confidence": "中",
        "current_tokens": _estimate_tokens((global_md or "") + (project_md or "")),
        "estimated_savings_tok": 0,
        "findings": [],
        "actions": [],
    }
    dup_global = _detect_duplicate_paragraphs(global_md or "")
    dup_project = _detect_duplicate_paragraphs(project_md or "")
    if dup_global:
        rec["findings"].append(f"全局 CLAUDE.md 有 {len(dup_global)} 个重复段落（示例：{dup_global[0]}）。")
    if dup_project:
        rec["findings"].append(f"项目 CLAUDE.md 有 {len(dup_project)} 个重复段落（示例：{dup_project[0]}）。")
    if not (dup_global or dup_project):
        rec["findings"].append("未在 CLAUDE.md 中识别到明显重复段落。")
    rec["actions"].extend([
        "合并 CLAUDE.md 中重复的行为约束段落；",
        "审视 skill trigger phrase 是否堆叠（`SKILL.md` 的 description / when_to_use 越精越好）；",
        "若某些插件/skill 只需在特定项目生效，考虑迁移到项目级配置。",
    ])
    rec["estimated_savings_tok"] = (len(dup_global) + len(dup_project)) * 200
    return rec


# ── 汇总入口 ─────────────────────────────────────────────────────────

def build_report(project_dir: str | None = None) -> dict:
    root = _project_root(project_dir)
    data_dir = root / DATA_DIRNAME
    baseline = _read_json(data_dir / BASELINE_FILENAME)
    global_settings = _read_json(GLOBAL_SETTINGS)
    settings_sources = [global_settings or {}, *_project_settings(root)]
    global_md = _read_text(GLOBAL_CLAUDE_MD)
    project_md = _read_text(root / "CLAUDE.md")
    agent_defs: list[dict] = (baseline or {}).get("agent_definitions_detail") or []
    skill_defs: list[dict] = (baseline or {}).get("skill_descriptions_detail") or []
    tool_schema_tokens: dict[str, int] = (baseline or {}).get("tool_schema_detail") or {}

    recs = [
        recommend_top_heavy(baseline),
        recommend_skill_inventory(skill_defs),
        recommend_unused_agents(agent_defs),
        recommend_agent_team_cost(settings_sources),
        recommend_auto_memory(settings_sources, baseline),
        recommend_cold_tools(tool_schema_tokens),
        recommend_other(global_md, project_md),
    ]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "project_root": str(root),
        "project_name": root.name,
        "baseline_available": baseline is not None,
        "recommendations": recs,
    }


# ── Markdown 渲染 ─────────────────────────────────────────────────────

def render_markdown(report: dict) -> str:
    lines: list[str] = []
    lines.append(f"# project-token-insights · 优化报告 · {report['project_name']}")
    lines.append("")
    lines.append(f"- 生成时间：{report['generated_at']}")
    lines.append(f"- 项目路径：`{report['project_root']}`")
    lines.append(f"- 基线数据：{'已就绪' if report['baseline_available'] else '未就绪，部分章节降级'}")
    lines.append("")
    for rec in report["recommendations"]:
        lines.append(f"## {rec['title']} · 方向 {rec['direction']}")
        lines.append("")
        lines.append(f"- 置信度：**{rec['confidence']}**")
        if rec.get("current_tokens"):
            lines.append(f"- 当前估算：{_approx(int(rec['current_tokens']))}")
        if rec.get("estimated_savings_tok"):
            lines.append(f"- 预计节省：{_approx(int(rec['estimated_savings_tok']))}")
        lines.append("")
        if rec["findings"]:
            lines.append("**发现：**")
            for f in rec["findings"]:
                lines.append(f"- {f}")
            lines.append("")
        if rec["actions"]:
            lines.append("**操作：**")
            for a in rec["actions"]:
                lines.append(f"- {a}")
            lines.append("")
    lines.append("> 所有 token 数均为启发式估算（误差约 ±15%），以 `约` 前缀标注。")
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description="project-token-insights 中文优化报告生成器。")
    p.add_argument("--project-dir", default=None, help="指定项目根（默认 $CLAUDE_PROJECT_DIR 或 cwd）。")
    p.add_argument("--json", action="store_true", help="以 JSON 输出而不是 Markdown。")
    p.add_argument("--out", default=None, help="额外把 Markdown 报告写入此路径。")
    args = p.parse_args()
    report = build_report(args.project_dir)
    text = json.dumps(report, ensure_ascii=False, indent=2) if args.json else render_markdown(report)
    sys.stdout.write(text + ("\n" if not text.endswith("\n") else ""))
    if args.out:
        Path(args.out).write_text(render_markdown(report), encoding="utf-8")
    else:
        default_out = _project_root(args.project_dir) / DATA_DIRNAME / REPORT_FILENAME
        try:
            default_out.parent.mkdir(parents=True, exist_ok=True)
            default_out.write_text(render_markdown(report), encoding="utf-8")
        except OSError as e:
            print(f"warning: could not save report to {default_out}: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
