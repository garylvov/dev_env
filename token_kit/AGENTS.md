- I hate emojis. Don't use them.
- Python environments: primarily use uv (`uv sync`, `uv run`, `uv add`); also use pixi where a `pixi.toml` exists (e.g. for ros2). Check which the project uses before running anything. Never use bare pip or the system Python.

## Claude Code only
- Don't get lost in unfamiliar code: before changing it, map it first (entry points, how the modules connect, where the relevant code lives). Delegate broad exploration to an explorer subagent so the main context stays small, and use Grep/Read directly only for tiny, single-purpose lookups.
- GPT-6 subagents (local Shunt/Codex gateway; needs `systemctl --user status shunt`): `astra-gpt-6-astra`, `sol-gpt-6-sol`, `luna-gpt-6-luna`. Delegate difficult debugging, substantial implementation, architectural analysis, independent reviews and second opinions to them with the Agent tool, and whenever the user asks for Astra, Sol or Luna by name.
- Launch them WITHOUT a `model` override. Default effort is high (set in each agent's frontmatter); if the Agent tool accepts an `effort` parameter, pass low, medium, high or xhigh to match the task.
- Always name every subagent `<task>-<model>-<effort>` using the Agent tool's `name` parameter (e.g. `auth-bug-gpt-6-astra-xhigh`, `repo-map-sonnet-low`), and give it a specific `description`.
