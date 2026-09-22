# token_kit

An additive **portable-workflow preview** now supplies a shared task/checkpoint
core, Claude and Codex launch adapters, and project-local configuration for both
clients with optional CodeGraph. See [Portable workflows](docs/portable-workflows.md).
The existing commands documented below retain their legacy behavior. The preview
does not yet implement automatic rollover, failover, or combined budget enforcement.
Strict Codex launches are refused until compaction prevention is verified.

Cuts Claude Code spend. The cost of a session is calls × the context each call re-reads, so the
kit does three things: sends each subagent to the cheapest model that can do the job, caps how
long any one agent runs, and restarts a session before its context gets expensive.

**It avoids compaction.** Built-in auto-compact fires only when the window is nearly full. By then
every call has been re-reading a huge context for hours, and it then replaces the conversation with a
lossy summary. Start your session with `token-kit-supervise launch` instead: it restarts the
session early (235k tokens by default) from a `STATE.md` you keep current, plus `PROMPTS.md`, your
exact words. Nothing is summarised, so nothing drifts, and the session never gets big enough to
compact. A session started any other way is not watched and will still compact.

Standard-library Python (run through `uv`, needs ≥3.11). Bash only as thin shims.

## Install

```
bash token_kit/install.sh --dry-run   # show what would change
bash token_kit/install.sh             # do it (backs up settings.json first)
bash token_kit/install.sh --uninstall # removes exactly what it added, nothing else
bash token_kit/install.sh --config    # what this machine detected, and why
```

Nothing to edit on a new machine: every machine fact is detected at run time (whether your home
is on a network filesystem, where tmp is, where codex is). `~/.config/token_kit/config.toml` may
override any of it (tables `[codex]`, `[supervisor]`, `[router]`) and is never required.

It writes only to `~/.claude` (hooks in `settings.json`, links in `agents/`) and `~/.config/token_kit`.
It installs no agent prompts of its own and never touches yours.

## Working folder

Agents talk through files, not long replies.

`token-kit-task` names the folder and puts it under `~/.claude/token_kit/work/`, so no project tree
grows a folder it did not ask for (`--root DIR` puts one elsewhere). Which project a task belongs to
is its `Cwd:` line, and `resume` starts the session there:

```
token-kit-task new "migrate the date parsing"      # -> ~/.claude/token_kit/work/2026-09-21_2242_migrate-the-date-parsing
token-kit-task retitle <task-dir> "replace the date helper" --summary "one line"
token-kit-task find date parsing --all             # then: token-kit-task resume <words-or-path>
```

```
~/.claude/token_kit/work/2026-09-21_2242_migrate-the-date-parsing/
  STATE.md               # "# <title>", then Started: / Status: / Cwd: / Summary:, then your own text
  PROMPTS.md             # what you typed, verbatim; extracted by token-kit-prompts, refreshed at rollover
  lanes/<name>/v0/       # one agent, one attempt; a retry is v1, never an overwrite
    in.md                # the brief, written before the spawn
    out.md               # line 1 = RESULT; then evidence, what is unproven, how a fresh agent resumes
    RESPAWN_REQUEST.md   # written by an agent that ran out of calls; the main thread is told once
```

The date and time stay in the folder name through a `retitle`, so `ls` sorts by age and the old name
is left as a symlink to the new one; every task also lands in an append-only index under the XDG
state dir, which is what `list --all`, `find` and `resume` read.

Only the main thread writes `STATE.md`. It reads `out.md` files, never an agent's transcript. A
nested agent's folder goes under its parent's (`token-kit-task lane <task> <name> --under <lane>`).
How current is `STATE.md`? Two things watch it, and neither is a guarantee: the Stop hook nudges the
main thread once in a while to bring it up to date, and a rollover that finds it unwritten since the
soft request puts a dated staleness warning at the top of the new session's seed. More in
`agent_trigger_matrix.md`.

## What you get

| Part | What it does |
|---|---|
| `agent_trigger_matrix.md` | A markdown chart you edit by hand: kind of task → model, effort, and a `prefer` list like `codex:gpt-5.6-luna:high, claude:opus:medium`. First available entry wins; every list ends in Claude, so a maxed-out Codex never blocks a spawn. The chart and its worked examples live in that one file. |
| router hook | Reads the chart on every subagent spawn and rewrites model / agent / prompt. Also caps tool calls per subagent (warn → write your result → stop). |
| kind agents | Generated from the chart at install time, one per ladder candidate, with `model` and `effort` in the frontmatter. Nothing checked in is installed as an agent. |
| `codex-dispatch` | One Codex turn, on demand, over `codex app-server` stdio. No daemon, no port. Exit 42 = Codex unavailable (absent, auth, busy, quota). |
| `codex-job` | Codex jobs you can talk to: `start`, `send` (steers a running turn, or continues the thread after it), `wait` (run in the background to be told when it ends), `status`, `list`, `stop`. No message is ever dropped silently. |
| `codex-run` | The kit's own Codex launcher, used by both commands above and usable by hand. When your home is on a network filesystem it keeps `CODEX_HOME` on node-local `/tmp` (Codex's SQLite breaks on network filesystems), seeds it once per machine, syncs `auth.json` newer-wins with atomic writes, caps threads, and runs Codex with approvals bypassed. |
| `token-kit-supervise` | `launch [--state-file FILE] [--cwd DIR]`: runs a session in tmux, watches its context size, and rolls it over to a fresh session that resumes from that handoff file (default `./STATE.md`). Ceiling is a cost choice (defaults 180k soft / 235k hard), not a window limit. Its bookkeeping goes under the XDG state dir keyed by a hash of the file's path, so two projects never collide. |
| `respawn-reader` | Tells the main thread, once, when a background agent asked to be restarted. |
| `token-kit-prompts` | `--cwd DIR --out FILE [--since YYYY-MM-DD] [--session ID] [--stdout]`: writes what you actually typed, verbatim, from this directory's session transcripts into one markdown file (default `./PROMPTS.md`, mode 0600). Tool output, subagent transcripts, compaction summaries and harness notices are excluded; re-running rewrites the same bytes. A rollover refreshes it beside the handoff file. |
| `token-kit-task` | `new "<title>"` makes a titled, sortable working folder; `retitle` renames it once the work is understood, keeping the date stamp and leaving a symlink at the old name; `lane`, `list [--all]`, `find`, `resume`, `done`, `reopen`. Every task is in a machine-wide append-only index, so discovery does not depend on where you are standing. |
| `canary` | Proves mechanically whether an instruction file is really in a model's context: LOADED / NOT_LOADED / PROBE_BROKEN, never collapsed. |

Details for the Codex commands: `src/token_kit/codex/USAGE.md`.

## Test

```
cd token_kit
uv run --python '>=3.11' --no-project -m unittest discover -s tests -t .
```
