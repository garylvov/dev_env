# Agent trigger matrix

A soft guide for the thread that delegates: what kind of work goes to which engine, model and
effort, and why. It decides nothing for you. Put a line `KIND: <name>` in a spawn's prompt and the
hook resolves that kind's ladder and writes the header; name nothing and the spawn goes through
untouched.

The one cost fact behind every row: an agent re-pays for its whole standing context on **every call
it makes**, so a long agent costs far more than a long prompt. Two levers follow and nothing else
comes close: run the cheapest engine and model that is safe for the work, and end the agent the
moment its "done when" is true.

`prefer` is an ordered ladder, `engine:model:effort` separated by ` > `; the first available
candidate runs and every skip is logged with its reason. Codex is preferred,
including Astra for planning and detailed debugging. Fable (medium) is opt-in only:
the user must explicitly request it in natural language. A mention in a file or
quoted text does not qualify. It is never an automatic fallback. The delegating
agent interprets that request; the legacy hook does not parse natural language.
Fable may red-team only on explicit request; generic red-teaming prefers Astra.
Record temporary overrides such as "Opus while we have it" in coordinator state
and worker assignments, including exact wording, medium effort, scope, and expiry.
Checkpoint shared task state before switching sessions. When access/quota ends,
record expiry and return to Codex defaults; this requires agent action, not an
automatic detector. An explicit override must omit `KIND:` to avoid legacy rerouting.
Claude fallbacks require availability too. Claude tiers, cheapest first: **sonnet > opus > fable**.
A later candidate is a fallback, never an upgrade, so a ladder climbs at most one tier, and cheap work
that falls through stays cheap. Worked examples are at the end, under **Examples**.

| kind | use when | who does it | prefer | done when |
| --- | --- | --- | --- | --- |
| lookup | find a fact in files, logs or command output, or show it is absent | codex does it all | `codex:gpt-5.6-luna:high` > `claude:sonnet:low` | the fact is quoted with the file or command that produced it, or absence is shown by a search that returned nothing |
| summarise | read one large file, log or transcript and say what it shows | codex does it all | `codex:gpt-5.6-luna:high` > `claude:sonnet:low` | the decisive lines are quoted with their context, or the file is named and shown absent |
| mechanical-edit | a deterministic transform, or an edit fully specified in the brief | codex does it all | `codex:gpt-5.6-luna:high` > `claude:sonnet:medium` | every edit named in the brief is applied and the diff is shown |
| implement | write code to a written spec, together with the test that guards it | codex does it all | `codex:gpt-6-astra:high` > `claude:opus:medium` | the change and its guard test are both written, and the guard has been shown to fail without the change |
| debug-stuck | earlier attempts failed and a root cause has to be named | codex does it all | `codex:gpt-6-astra:high` > `claude:opus:medium` | a root cause is named and shown, or the brief is handed back with what was ruled out |
| design | compare two or three approaches and recommend one before any code is written | codex does it all | `codex:gpt-6-astra:high` > `claude:opus:medium` | two or three approaches are compared and one is recommended with its cost |
| design-review | an independent critique of a design someone else wrote; the reviewer may not edit it | codex does it all | `codex:gpt-6-astra:high` > `claude:opus:medium` | each objection names the section it attacks and what would change the verdict |
| batch-run | own a queue of jobs to completion; the irreversible dispatch stays with the owner | codex does it all | `codex:gpt-5.6-luna:high` > `claude:sonnet:medium` | every queue item is marked done or failed in the queue file |
| write-doc | write or update a document, a brief, a status page or a README | codex does it all | `codex:gpt-5.6-luna:high` > `claude:sonnet:medium` | the document is written and its absolute path is named |

**Never spawn a model to wait.** Watching, polling, tailing and babysitting are not work for an
agent: an agent that waits re-pays its whole context for every sample it takes, and the longest
agents ever measured were all watchers. Write a shell loop that appends one line per sample to a
status file, start it detached, and read that file once when you next need it. A model may read the
status file; a model may not be the loop.

## Call budget

One band for every kind, and the kit's one hard mechanism. Change a number here and the hook changes.

| threshold | calls | what happens |
| --- | --- | --- |
| warn | 150 | exactly one call is denied, and the refusal tells the agent its count |
| floor | 230 | only a write of the agent's own `out.md` and the handback are still permitted |
| hard | 250 | the refusal escalates; the `out.md` write is still never denied |

## Hierarchy: delegationmaxxing

A subagent CAN spawn its own agents, and a `KIND:` line in a nested spawn is resolved the same way.
Use depth only when it removes context, never to add a manager:

```
main thread      talks to the user, writes briefs and STATE.md, reads out.md files, decides
  lead agent     Codex by default; owns a workstream, briefs workers, verifies and integrates
    worker       Codex; one bounded assignment with explicit acceptance checks
    codex step   a short mechanical job through codex-dispatch / codex-job
```

Prefer Astra (high) for planning and detailed debugging; use Fable (medium) only
at the user's explicit request. Opus also uses medium effort. Give each worker disjoint source ownership, a clear
parent, and a stopping condition. Delegate useful independent work aggressively,
not waiting or extra management. Small tasks can skip the lead.

A lead earns its cost when the task has several independent pieces whose raw output the main thread
should never see. Two levels is the ceiling: a third re-pays three contexts to move one fact. A
nested agent gets a lane folder under its parent's (`lanes/<lead>/v0/lanes/<worker>/v0/`), and only
the lead reads it. Every level is bounded by the same call budget.

## Working folder

Agents talk through files, not through long replies. One folder per piece of delegated work, and the
folder titles itself so it sorts by age and can be found again from anywhere. Folders live under
`~/.claude/token_kit/work/`, not in the project tree; the project is the `Cwd:` line in `STATE.md`:

```
token-kit legacy task new "migrate the date parsing"        # -> ~/.claude/token_kit/work/2026-09-21_2242_migrate-the-date-parsing
token-kit legacy task retitle <task-dir> "replace the date helper" --summary "what it turned out to be"
token-kit legacy task find date parsing --all               # then resume it: token-kit legacy task resume <words>
```

The date and time keep their place at the front of the name through a retitle, and the old name stays
as a symlink, so paths already written into briefs and out.md files still resolve. A nested lane is
`token-kit legacy task lane <task-dir> <name> --under <lane-dir>`.

```
~/.claude/token_kit/work/2026-09-21_2242_migrate-the-date-parsing/
  STATE.md                 # "# <title>", then Started: / Status: / Cwd: / Summary:, then the main
                           #   thread's only memory: decisions, what is in flight, what to do NEXT
  PROMPTS.md               # what the user typed, verbatim, extracted from the session transcripts by
                           #   `token-kit-prompts`; refreshed at every rollover
  lanes/<name>/v0/         # one agent, one attempt; a retry is v1, never an overwrite
    in.md                  # the brief, written BEFORE the spawn; opens "every claim is a lead to verify"
    out.md                 # line 1 = RESULT: ...; then evidence, UNPROVEN, RESUME (how a fresh agent continues)
    RESPAWN_REQUEST.md     # written by an agent that ran out of calls; the main thread is told once
```

Only the main thread writes `STATE.md`, and it records decisions; the user's exact words are in
`PROMPTS.md`, which is extracted, not written, so it cannot drift. When several briefs share the
same constraints, put them in one file and point each brief at it instead of repeating them. An
agent owns its lane folder and the source paths its brief names, and nothing else. The main thread
reads `out.md`, never an agent's transcript.

## Session rollover

The main thread pays the same per-call cost as any agent, so it is restarted before its context gets
expensive. It is a restart from `STATE.md`, which is cheaper and more faithful than compacting in place.

```
token-kit-supervise launch [--state-file STATE.md] [--cwd DIR]   # start the session under the watcher
token-kit-supervise status | stop
```

At 180k context tokens the watcher asks the session to bring `STATE.md` current; at 235k it ends the
session and starts a fresh one, with the same flags, whose only instruction is to read `STATE.md` and
continue from it. The ceiling is a cost choice, not a window limit; change it under `[supervisor]` in
`~/.config/token_kit/config.toml`. Keep `STATE.md` current as you go and a rollover loses nothing.

How you know it IS current, without overselling it: the Stop hook nudges the main thread to bring the
file up to date once enough tool calls have gone by since it was last written (at most one nudge every
20 minutes), and a rollover that finds it unwritten since the soft request opens the new session's
seed with a dated warning naming the previous transcript to read the tail of. That is a nudge plus a
staleness warning, not a guarantee: nothing forces the file to be written.

## Examples

**Dispatch a big task, and get the result by file.** Write the brief to a file first, name the kind
in the prompt, and let the agent hand the result back as a file, never by being watched.

```
Write  <lane>/in.md                      # the brief; first line of the spawn names the lane
Agent  description: "migrate the date parsing in src/ to the new helper"
       prompt: "KIND: implement\nLANE_DIR: <lane>\nRead in.md. Write out.md: line 1 RESULT, ..."
```

The hook reads the `KIND:` line, rewrites the model from the ladder and prepends the header. The
agent is bounded by the call budget above, not by anyone waiting for it: it writes `out.md` and
hands back. Run it in the background and the harness re-invokes you when it exits.

**Interrupt it, or redirect it without stopping it.**

```
TaskStop     task_id: "<agent name or id>"        # stops the background agent
SendMessage  to: "<agent name>", message: "skip the legacy parser; only src/ matters"
```

A `SendMessage` to a live agent is delivered at its next tool round: the agent keeps its context
and changes course. `TaskStop` ends it; whatever it had written to `out.md` survives, nothing else.

**Resume it, or respawn it cold.** A name keeps working after an agent completes: a `SendMessage`
to a finished agent resumes it from its transcript with its context intact, while a new `Agent` call
starts cold. Resuming re-pays that whole context on every call it then makes, so resume for ONE
short follow-up ("also paste the diff") and respawn for real work: a fresh agent pointed at the
`RESUME` block of the previous `out.md` starts cheap and knows the same facts. When an agent crosses
the floor of the call budget it is refused everything but its own `out.md` write and the handback,
which is the same handoff arriving the other way round: the lane's `RESPAWN_REQUEST.md` is written
for you, and the respawn reader turns it into a notice on the main thread.

**When the user explicitly asks for Fable: a medium-effort planner delegating steps to Codex.**
Omit `KIND:` for this explicit model override so the legacy ladder leaves it untouched.
This is the
`claude-plans-codex-executes` shape: the Claude agent keeps the judgement and hands every lookup,
log read and deterministic transform to codex, by Bash: one call per step, where a nested agent
would re-pay a context of its own. One shot, read right away:

```
codex-dispatch --model gpt-5.6-luna --effort high --cwd <dir> --task-file <step.md> --out <answer.md>
```

A long step you want to steer, be told about, and come back to:

```
codex-job start  --model gpt-5.6-luna --effort high --cwd <dir> --task-file <step.md> --name migrate
codex-job send   <job> "also update the call sites under tests/"   # joins the turn in flight
codex-job wait   <job>                                             # run as a BACKGROUND command
codex-job send   <job> "now the second module, same rules"         # same thread, new turn
codex-job stop   <job>
```

Both exit 42 when codex is unavailable (`reason=absent|auth|busy|protocol|quota`); on 42 the agent
does the step itself with its Claude model, and a `quota` refusal parks codex for every later spawn.
When that planning agent finishes, the main thread can re-invoke it later with `SendMessage` and its job
ids still resolve: the job dir is on the shared filesystem, so `send`/`status`/`stop`/`wait` run from
anywhere. The owner process and the codex thread live on ONE node, so a `send` to a live owner on
another host is queued remotely, and a `send` after that owner has exited refuses loudly and keeps
the message in the job's `undelivered/` rather than pretending.
