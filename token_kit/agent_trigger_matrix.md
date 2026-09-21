# Agent trigger matrix

A soft guide for the thread that delegates: what kind of work goes to which engine, model and
effort, and why. It decides nothing for you — put a line `KIND: <name>` in a spawn's prompt and the
hook resolves that kind's ladder and writes the header; name nothing and the spawn goes through
untouched.

The one cost fact behind every row: an agent re-pays for its whole standing context on **every call
it makes**, so a long agent costs far more than a long prompt. Two levers follow and nothing else
comes close — run the cheapest engine and model that is safe for the work, and end the agent the
moment its "done when" is true.

`prefer` is an ordered ladder, `engine:model:effort` separated by ` > `; the first available
candidate runs and every skip is logged with its reason. Every ladder ends in a Claude candidate so
a maxed-out codex can never block a kind. Claude tiers, cheapest first: **sonnet > opus > fable**.
A later candidate is a fallback, never an upgrade, so a ladder climbs at most one tier — cheap work
that falls through stays cheap. Worked examples are at the end, under **Examples**.

| kind | use when | who does it | prefer | done when |
| --- | --- | --- | --- | --- |
| lookup | find a fact in files, logs or command output, or show it is absent | codex does it all | `codex:gpt-5.6-luna:high` > `claude:sonnet:low` | the fact is quoted with the file or command that produced it, or absence is shown by a search that returned nothing |
| summarise | read one large file, log or transcript and say what it shows | codex does it all | `codex:gpt-5.6-luna:high` > `claude:sonnet:low` | the decisive lines are quoted with their context, or the file is named and shown absent |
| mechanical-edit | a deterministic transform, or an edit fully specified in the brief | codex does it all | `codex:gpt-5.6-luna:high` > `claude:sonnet:medium` | every edit named in the brief is applied and the diff is shown |
| implement | write code to a written spec, together with the test that guards it | Claude plans, codex executes | `claude:sonnet:high` > `claude:opus:high` | the change and its guard test are both written, and the guard has been shown to fail without the change |
| debug-stuck | earlier attempts failed and a root cause has to be named | Claude plans, codex executes | `claude:opus:high` > `claude:fable:high` | a root cause is named and shown, or the brief is handed back with what was ruled out |
| design | compare two or three approaches and recommend one before any code is written | Claude does it all | `claude:opus:high` > `claude:fable:high` | two or three approaches are compared and one is recommended with its cost |
| design-review | an independent critique of a design someone else wrote; the reviewer may not edit it | codex does it all | `codex:gpt-6-astra:high` > `claude:fable:high` > `claude:opus:high` | each objection names the section it attacks and what would change the verdict |
| batch-run | own a queue of jobs to completion; the irreversible dispatch stays with the owner | Claude does it all | `claude:sonnet:medium` > `claude:opus:medium` | every queue item is marked done or failed in the queue file |
| write-doc | write or update a document, a brief, a status page or a README | Claude does it all | `claude:sonnet:medium` | the document is written and its absolute path is named |

**Never spawn a model to wait.** Watching, polling, tailing and babysitting are not work for an
agent: an agent that waits re-pays its whole context for every sample it takes, and the longest
agents ever measured were all watchers. Write a shell loop that appends one line per sample to a
status file, start it detached, and read that file once when you next need it. A model may read the
status file; a model may not be the loop.

## Call budget

One band for every kind — the kit's one hard mechanism. Change a number here and the hook changes.

| threshold | calls | what happens |
| --- | --- | --- |
| warn | 150 | exactly one call is denied, and the refusal tells the agent its count |
| floor | 230 | only a write of the agent's own `out.md` and the handback are still permitted |
| hard | 250 | the refusal escalates; the `out.md` write is still never denied |

## Hierarchy

A subagent CAN spawn its own agents, and a `KIND:` line in a nested spawn is resolved the same way.
Use depth only when it removes context, never to add a manager:

```
main thread      talks to the user, writes briefs and STATE.md, reads out.md files, decides
  lead agent     opus; owns one large task; splits it, keeps the judgement, merges the results
    worker       sonnet or codex; one bounded piece; hands back by file
    codex step   a mechanical step through codex-dispatch / codex-job: one call, no context of its own
```

A lead earns its cost when the task has several independent pieces whose raw output the main thread
should never see. Two levels is the ceiling: a third re-pays three contexts to move one fact. A
nested agent gets a lane folder under its parent's (`lanes/<lead>/v0/lanes/<worker>/v0/`), and only
the lead reads it. Every level is bounded by the same call budget.

## Working folder

Agents talk through files, not through long replies. One folder per piece of delegated work:

```
<task>/
  STATE.md                 # the main thread's only memory: what the user said (quoted, dated), decisions,
                           #   what is in flight, and a last line saying what to do NEXT
  RULES.md                 # optional: constraints shared by every agent of this task, so each brief
                           #   points at one file instead of repeating them
  lanes/<name>/v0/         # one agent, one attempt; a retry is v1, never an overwrite
    in.md                  # the brief, written BEFORE the spawn; opens "every claim is a lead to verify"
    out.md                 # line 1 = RESULT: ...; then evidence, UNPROVEN, RESUME (how a fresh agent continues)
    RESPAWN_REQUEST.md     # written by an agent that ran out of calls; the main thread is told once
```

Only the main thread writes `STATE.md`. The user's instructions go into it in their own words the
moment they are given — a rollover or a fresh agent sees only what is on disk, and a paraphrase
drifts. An agent owns its lane folder and the source paths its brief
names — nothing else. The main thread reads `out.md`, never an agent's transcript.

## Session rollover

The main thread pays the same per-call cost as any agent, so it is restarted before its context gets
expensive — a restart from `STATE.md`, which is cheaper and more faithful than compacting in place.

```
token-kit-supervise launch [--state-file STATE.md] [--cwd DIR]   # start the session under the watcher
token-kit-supervise status | stop
```

At 180k context tokens the watcher asks the session to bring `STATE.md` current; at 235k it ends the
session and starts a fresh one, with the same flags, whose only instruction is to read `STATE.md` and
continue from it. The ceiling is a cost choice, not a window limit; change it under `[supervisor]` in
`~/.config/token_kit/config.toml`. Keep `STATE.md` current as you go and a rollover loses nothing.

## Examples

**Dispatch a big task, and get the result by file.** Write the brief to a file first, name the kind
in the prompt, and let the agent hand the result back as a file — never by being watched.

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

A `SendMessage` to a live agent is delivered at its next tool round — the agent keeps its context
and changes course. `TaskStop` ends it; whatever it had written to `out.md` survives, nothing else.

**Resume it, or respawn it cold.** A name keeps working after an agent completes: a `SendMessage`
to a finished agent resumes it from its transcript with its context intact, while a new `Agent` call
starts cold. Resuming re-pays that whole context on every call it then makes, so resume for ONE
short follow-up ("also paste the diff") and respawn for real work — a fresh agent pointed at the
`RESUME` block of the previous `out.md` starts cheap and knows the same facts. When an agent crosses
the floor of the call budget it is refused everything but its own `out.md` write and the handback,
which is the same handoff arriving the other way round: the lane's `RESPAWN_REQUEST.md` is written
for you, and the respawn reader turns it into a notice on the main thread.

**An opus agent that plans and runs the mechanical steps through codex.** This is the
`claude-plans-codex-executes` shape: the Claude agent keeps the judgement and hands every lookup,
log read and deterministic transform to codex, by Bash — one call per step, where a nested agent
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
When that opus agent finishes, the main thread can re-invoke it later with `SendMessage` and its job
ids still resolve: the job dir is on the shared filesystem, so `send`/`status`/`stop`/`wait` run from
anywhere. The owner process and the codex thread live on ONE node, so a `send` to a live owner on
another host is queued remotely, and a `send` after that owner has exited refuses loudly and keeps
the message in the job's `undelivered/` rather than pretending.
