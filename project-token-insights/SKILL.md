---
name: project-token-insights
disable-model-invocation: true
description: 通过 `/project-token-insights` 或 Skill 工具显式调用，检查当前项目首轮冷启动 token 组成并生成中文 Markdown 优化报告。
allowed-tools:
  - Bash(python3:*)
  - AskUserQuestion
---

# 项目 Token 洞察（project-token-insights）

面向**单个项目**的 token 优化项检查 skill：锚定 `$CLAUDE_PROJECT_DIR`，只扫描首轮冷启动里可优化的 token 组成，并生成一份中文 Markdown 优化报告；可选安装提醒 hook，在后续冷启动或 cache-bust 风险出现时提醒用户。

> 仅限显式调用：用户通过 `/project-token-insights` 或 Skill 工具主动触发。不会被模型隐式加载。

## 产物与存储

所有产物写入当前项目根的 `.project-token-insights/` 目录；该目录自动加入 `.gitignore`。

| 文件 | 产出脚本 | 说明 |
|------|---------|------|
| `first-turn-baseline.json` | `first_turn_breakdown.py` | 首轮组件估算基线 |
| `optimization-report.md` | `optimization_report.py` | 2.1-2.6 方向中文优化报告 |
| `cache-warn/<session_id>.json` | hook 运行时 | cache-bust 预警状态（安装 hook 后才有） |
| `first-turn-pending-<session_id>.json` | hook 运行时 | 首轮超阈待提醒状态（SessionStart 写入，首个 UserPromptSubmit 消费） |
| `first-turn-warned-<session_id>` | hook 运行时 | 首轮超阈 hook 去重标记 |

## Value Context（按需穿插 1-2 条，不要一次抛全部）

- Claude Code 用户通常看不到"这个项目首轮 token 被什么吃掉"；本 skill 只把可优化的常驻组件拆出来。
- 所有拆分基于 settings.json / JSONL transcript / 插件 manifest 等本地可观察机制，不编造 Anthropic 没公开的行为。
- 每条建议都标了置信度和节省估算，可以先按"高 + 大数"下手，再看"中置信度但可能有收益"。
- hook 不做报告生成，只做提醒：cache-bust 风险提醒、首轮超阈提醒。

---

## Step 1 · 生成首轮组件估算基线

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/first_turn_breakdown.py --pretty
```

默认行为：

- 项目根：`$CLAUDE_PROJECT_DIR`，未设置则用 `os.getcwd()`。
- 若 `.project-token-insights/` 不存在则创建，同时在项目根 `.gitignore` 追加一行 `.project-token-insights/`（若已有则跳过）。
- 仅扫描当前项目对应的 `~/.claude/projects/<encoded>/` JSONL（路径编码规则：`/` `.` `_` 全映射为 `-`），用于首轮校准和首条用户消息估算；不做轮次级全量分析，没有跨项目扫描模式。
- 输出 `.project-token-insights/first-turn-baseline.json`，stdout 同步打印 JSON。
- 非零退出则报错并终止后续步骤。

脚本会根据官方可观察面重建首轮 token 组成（启发式估算，误差约 ±15%）：

- 全局/项目 `CLAUDE.md`
- 当前启用插件清单 + 模型可见入口：Claude Code 内置 skill/command、项目/用户/启用插件的顶层 `skills/*/SKILL.md` metadata（description + when_to_use）、插件 `commands/*.md` description、以及带 `name + description` frontmatter 的 `agents/**/*.md`（`disable-model-invocation: true` 的 skill 不计入；skill/agent 全文字节数记入 `full_tokens` 字段，仅被调用时注入）
- Claude Code 内置工具 schema（查表硬编码）
- `autoMemoryEnabled` / `env.CLAUDE_CODE_DISABLE_AUTO_MEMORY` + 项目 memory 目录下 `MEMORY.md` 启动注入部分（首 200 行或 25KB）
- 本项目第一条用户消息

结果会与当前项目每个 session 冷启动首轮的 `cache_creation_input_tokens` p50 对比给出置信度：

- 误差 ≤20% → `confidence: 高`
- 误差 >20% → `confidence: 低`（估算不可信，建议以 JSONL 实际值为准）
- 无可校准样本 → `confidence: 中`

## Step 2 · 生成中文优化报告

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/optimization_report.py
```

报告写入 `.project-token-insights/optimization-report.md`，并同步 stdout（Markdown）；加 `--json` 可拿结构化输出。章节与 PLAN 的 2.1-2.6 一一对齐：

| 方向 | 章节 | 主数据源 | 置信度 |
|------|------|---------|-------|
| 2.1 清理 CLAUDE.md / 插件 / Skills | 大头组件 Top 3 + Skill 清单 | `first-turn-baseline.json` | 高/中 |
| 2.2 清理未使用 Agent | Agent 定义体量与待核查清单 | `first-turn-baseline.json` | 中 |
| 2.3 不开启实验 Agent Team | Agent Team 开启状态 | settings + env | 高/中 |
| 2.4 禁用 Auto memory | Auto memory 开销诊断 | settings + 当前项目 memory/MEMORY.md + baseline | 高 |
| 2.5 控制冷门高成本工具 | 高成本工具 schema | `first-turn-baseline.json` | 中 |
| 2.6 其他可优化项 | 其他可优化项 | 全局/项目 CLAUDE.md 段落重复检测 | 中 |

每条建议均含：当前估算 token、预计节省、具体操作（settings.json 字段 / 文件路径 / 命令）。

## Step 3 · Hook 安装选项（可选）

当 `first_turn_total` 或任一主要组件超阈，或用户明确想要后续提醒时，先打印状态：

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/install_hooks.py --status
```

然后用 AskUserQuestion 给出三个选项：

1. **安装全部 hook**（cache-bust 3 个 + first-turn 1 个脚本），推荐。
2. **仅安装 first-turn 预警**（适合只想盯首轮超阈的用户）：`install_hooks.py --only first-turn`。
3. **跳过**。

installer 只写当前项目目录：hook 脚本复制到 `.project-token-insights/hooks/`，注册写入 `.claude/settings.local.json`，并确保这两个本地路径进入项目 `.gitignore`。写入 settings 前自动生成 `settings.local.json.bak-install-<UTC>` 备份；`--uninstall` 可完整回滚当前项目安装。

### hook 行为速览

- **cache-bust 3 个 hook**：Stop 打时间戳 / SessionStart 检测 resume / UserPromptSubmit 超 5 分钟空闲时一次性提醒。状态文件落在 `$CLAUDE_PROJECT_DIR/.project-token-insights/cache-warn/`。
- **first-turn 预警 hook**：SessionStart 读取 baseline 并跳过 resume；超阈时写 pending，首个 UserPromptSubmit 用前台 `decision:block` 给出中文提示和 2.1-2.6 对应优化方向，每个 session 只提醒一次。阈值使用内置默认值，可用项目 `.project-token-insights/first-turn-budget.json` 覆盖。

安装/卸载成功后提醒用户重启 Claude Code（退出 + 重开）让当前项目 hook 生效。

若不确定是否安装，可直接告知用户："可以先不装，随时运行 `python3 ${CLAUDE_SKILL_DIR}/scripts/install_hooks.py` 再开启。"

## Step 4 · 中文呈现结果

把 Step 1 的基线字段和 Step 2 的 Markdown 拼成一份 **中文** 回复（无需反问，直接输出）：

### 4.1 总览

- 首轮 total 估算、置信度、当前项目冷启动 session 样本数、JSONL 实测 `cache_creation_input_tokens` p50/p75。
- 一句话结论：当前项目冷启动偏重 / 合理 / 超阈。

### 4.2 首轮大头组件 Top 3

来自 `first-turn-baseline.json`：每项含 `约 N tok` 和对应来源。

### 4.3 优化建议（按 2.1-2.6 顺序）

直接引用 Step 2 报告中的"发现 / 操作"条目，附上置信度和节省估算；每个方向选 1-2 条最有价值的展示，全部细节指引用户去看 `optimization-report.md`。

### 4.4 报告与 hook 状态

告知用户本地 Markdown 报告路径：`.project-token-insights/optimization-report.md`。
如果用户选择安装 hook，也告知安装结果与是否需要重启 Claude Code。

---

## 已决定的约束

- 只做优化项检查、Markdown/JSON 报告生成，以及可选项目级提醒 hook；不生成其他交互式产物。
- hook 安装只能写当前项目目录：`.claude/settings.local.json` 和 `.project-token-insights/hooks/`；不得写 `~/.claude/settings.json` 或 `~/.claude/hooks/`。
- 仅启发式估算，不引入 SDK、不要求 API Key，也不联网。
- 不跨项目读 memory 数据；默认只读取当前项目对应 memory 目录下的 `MEMORY.md` 启动注入部分。
- 不做轮次级数据库，不生成 HTML 报告；JSONL 只用于首轮校准和必要的提醒上下文。
- 所有优化建议必须对齐 Claude Code 官方可观察机制；做不到的地方在报告里明确标注"未找到官方依据"或"只能列为待核查"。
