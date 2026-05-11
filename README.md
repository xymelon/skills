# Skills 集合

用于存放、迭代和测试可复用的 Claude Code / Codex skills。

## Skills

| Skill | 简介 | 触发 / 使用场景 |
| --- | --- | --- |
| [`project-token-insights`](./project-token-insights/SKILL.md) | 分析当前项目首轮冷启动 token 组成，生成中文优化报告，并可选安装项目级提醒 hook。 | 通过 `/project-token-insights` 或 Skill 工具显式调用，用于排查 `CLAUDE.md`、插件、skills、agents、Auto memory 和工具 schema 等常驻上下文开销。 |
| [`deploy-github-pages`](./deploy-github-pages/SKILL.md) | 将用户指定的单个自包含 HTML 文件部署到 `<user>.github.io/<slug>/`。 | 用户要求部署、发布或托管 HTML 到 GitHub Pages 时使用；源 HTML 可以位于任意目录。 |
