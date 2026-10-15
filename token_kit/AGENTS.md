- I hate emojis. Don't use them.
- Always use the pixi envs. Check the pixi.toml, e.g. if you need ros2.

## Claude Code only
- For codebase exploration and research, always use the henry-hudson-codebase-explorer agent instead of doing manual Grep/Search calls. Only use Grep for tiny, single-purpose lookups (e.g. confirming a specific line changed after an edit). For anything requiring understanding structure, tracing code paths, or multi-file exploration, dispatch henry-hudson.
- GPT-6 subagents (local Shunt/Codex gateway; needs `systemctl --user status shunt`): `astra-gpt-6-astra`, `sol-gpt-6-sol`, `luna-gpt-6-luna`. Delegate difficult debugging, substantial implementation, architectural analysis, independent reviews and second opinions to them with the Agent tool, and whenever the user asks for Astra, Sol or Luna by name.
- Launch them WITHOUT a `model` override. Default effort is high (set in each agent's frontmatter); if the Agent tool accepts an `effort` parameter, pass low, medium, high or xhigh to match the task.
- Always set the Agent tool's `name` as `<agent>-<effort>-<short-task>` (e.g. `astra-gpt-6-astra-xhigh-auth-bug`) and a specific `description`.
