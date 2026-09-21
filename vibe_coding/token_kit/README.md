# token_kit

Cuts Claude Code spend. The cost of a session is calls × the context each call re-reads, so the
kit does three things: sends each subagent to the cheapest model that can do the job, caps how
long any one agent runs, and restarts a session before its context gets expensive.

Standard-library Python (run through `uv`, needs ≥3.11). Bash only as thin shims.

## Install

```
bash vibe_coding/token_kit/install.sh --dry-run   # show what would change
bash vibe_coding/token_kit/install.sh             # do it (backs up settings.json first)
bash vibe_coding/token_kit/install.sh --uninstall # restores settings.json byte for byte
```

`--adopt-personas` moves your existing agent files aside (never deletes) and links the kit's.
`--profile <name>` picks a file from `profiles/`; otherwise the installer detects the machine.
On a new machine, the profile file is the only thing to edit — all machine paths live there.

It writes only to `~/.claude` (hooks in `settings.json`, links in `agents/`) and `~/.config/token_kit`.

## What you get

| Part | What it does |
|---|---|
| `agent_trigger_matrix.toml` | One table: kind of task → model, effort, and a `prefer` list like `codex:gpt-5.6-luna:high, claude:opus:medium`. First available entry wins; every list ends in Claude, so a maxed-out Codex never blocks a spawn. |
| router hook | Reads the table on every subagent spawn and rewrites model / agent / prompt. Matches a `ROW:` line, then the spawn description, then file paths. Also caps tool calls per subagent (warn → write your result → stop). |
| row agents | Generated from the table, one per row, with `model` and `effort` in the frontmatter. |
| `codex-dispatch` | One Codex turn, on demand, over `codex app-server` stdio. No daemon, no port. Exit 42 = Codex unavailable (absent, auth, busy, quota). |
| `codex-job` | Codex jobs you can talk to: `start`, `send` (steers a running turn, or continues the thread after it), `wait` (run in the background to be told when it ends), `status`, `list`, `stop`. No message is ever dropped silently. |
| `token-kit-supervise` | Runs a session in tmux, watches its context size, and rolls it over to a fresh session that resumes from the campaign's `STATE.md`. Ceiling is a cost choice (defaults 180k soft / 235k hard), not a window limit. |
| `respawn-reader` | Tells the main thread, once, when a background agent asked to be restarted. |

Details for the Codex commands: `src/token_kit/codex/USAGE.md`.

## Test

```
cd vibe_coding/token_kit
uv run --python '>=3.11' --no-project -m unittest discover -s tests -t .
```

`reference_bash/` holds the original bash tools. They are frozen: the behaviour spec the Python passes case for case.
