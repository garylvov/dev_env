# Agent trigger matrix

A soft guide for the thread that delegates: how to classify work, apply the
[canonical trigger pyramid](trigger_pyramid.md), and bound workers. Shared Token Kit
sessions apply these preferences through agent instructions. Only the legacy router
resolves a `KIND: <name>` line and writes a routing header; that hook is not required
for native delegation below.

The one cost fact behind every row: an agent re-pays for its whole standing context on **every call
it makes**, so a long agent costs far more than a long prompt. Two levers follow and nothing else
comes close: run the cheapest engine and model that is safe for the work, and end the agent the
moment its "done when" is true.

## Legacy `KIND:` compatibility

This static projection exists only for the legacy router. It does not read
`TASK/trigger_pyramid.md`. `prefer` is an ordered list of
`engine:model:effort` entries separated by ` > `. Omit `KIND:` and select manually
when a task-session map, provider restriction, or explicit worker assignment applies.

| kind | use when | who does it | prefer | done when |
| --- | --- | --- | --- | --- |
| lookup | find a fact in files, logs or command output, or show it is absent | codex does it all | `codex:gpt-5.6-luna:xhigh` > `claude:sonnet:medium` > `codex:gpt-5.6-terra:medium` | the fact is quoted with the file or command that produced it, or absence is shown by a search that returned nothing |
| summarise | read one large file, log or transcript and say what it shows | codex does it all | `codex:gpt-5.6-luna:xhigh` > `claude:sonnet:medium` > `codex:gpt-5.6-terra:medium` | the decisive lines are quoted with their context, or the file is named and shown absent |
| mechanical-edit | a deterministic transform, or an edit fully specified in the brief | codex does it all | `codex:gpt-5.6-luna:xhigh` > `claude:sonnet:medium` > `codex:gpt-5.6-terra:medium` | every edit named in the brief is applied and the diff is shown |
| implement | write code to a written spec, together with the test that guards it | codex does it all | `codex:gpt-5.6-luna:high` > `claude:sonnet:medium` > `codex:gpt-5.6-terra:medium` | the change and its guard test are both written, and the guard has been shown to fail without the change |
| debug-stuck | earlier attempts failed and a root cause has to be named | codex does it all | `codex:gpt-6-astra:medium` > `claude:opus:medium` | a root cause is named and shown, or the brief is handed back with what was ruled out |
| design | compare two or three approaches and recommend one before any code is written | codex does it all | `codex:gpt-6-astra:medium` > `claude:opus:medium` | two or three approaches are compared and one is recommended with its cost |
| design-review | an independent critique of a design someone else wrote; the reviewer may not edit it | codex does it all | `codex:gpt-6-astra:medium` > `claude:opus:medium` | each objection names the section it attacks and what would change the verdict |
| batch-run | own a queue of jobs to completion; the irreversible dispatch stays with the owner | codex does it all | `codex:gpt-5.6-luna:high` > `claude:sonnet:medium` > `codex:gpt-5.6-terra:medium` | every queue item is marked done or failed in the queue file |
| write-doc | write or update a document, a brief, a status page or a README | codex does it all | `codex:gpt-5.6-sol:high` | the document is written and its absolute path is named |

**Never spawn a model to wait.** Watching, polling, tailing and babysitting are not work for an
agent. Dedicated watcher agents and repeated model wakeups re-pay context without
advancing the task. Prefer client completion or message notifications. The coordinator
keeps useful independent work moving and may park on an interruptible completion wait
when none remains. If the client lacks notifications, bounded process tooling may record
status for the coordinator to read when needed; do not create a polling worker.

## Tier use policy

The [trigger pyramid](trigger_pyramid.md) owns the live model and effort lists.
Choose the lowest capable tier and start with Mid. Use Medium for scoped work that
is harder than Mid, including substantive documentation; Smart for complex planning,
debugging, and independent review; and Smartest only for especially difficult or
high-consequence decisions. Tell the user why whenever Smartest is selected.

A tier describes the assignment, not the agent's place in the hierarchy. Mechanical
edits do not move up a tier merely because they change source code.
Each brief must name the role, allowed paths, expected output, completion check,
and escalation boundary. Workers must not widen scope or promote their own model.
If complexity exceeds the brief, checkpoint and report to the main thread via the
parent: blocker, evidence, attempted approaches, and proposed scope/model change.
The coordinator decides whether to split, clarify, or assign a higher tier within
the user's constraints, and announces the change and reason. Escalate for actual
complexity, not elapsed time. Availability fallback stays within the assigned tier
and, when chosen, must still be reported and checkpointed.

## Feature intake

Before starting, the coordinator tells the user the simple/complex route, a short
reason, and chosen models. If complexity is unclear, announce a bounded audit first.
Update the user when phases, scope, or model choices change; these are progress
updates, not extra approval gates.

**Simple:** small, bounded behavior with clear requirements and limited interaction
with other components. One scoped worker can design, implement, and verify directly,
without separate audit/review agents. Use Mid for the direct design, implementation,
and check.

**Complex:** cross-cutting behavior, ambiguous requirements, architectural changes,
or shared-state risk. Follow this sequence:

| Phase | Tier | Done when |
| --- | --- | --- |
| Audit | Mid | affected paths, behavior, tests, dependencies, and uncertainties are identified through bounded reads |
| Plan | Smart | acceptance criteria, interfaces, source ownership, dependencies, and validation are specified |
| Independent red-team | Smart | a separate reviewer names failure cases and required fixes without editing the plan |
| Revise | Smart | findings have evidence or reasoned dispositions; blocking findings are resolved |
| Delegate implementation | Mid | bounded assignments pass relevant checks and return changed-file evidence |

The coordinator integrates results against acceptance criteria; serialize overlapping
writes. Workers checkpoint and report redesign needs to their parent, without silently
widening scope or promoting models. The coordinator handles and announces any tier
change. Exact model, effort, and provider restrictions take precedence.

For additions during ongoing work, record a concise scoped queue with dependencies
and ownership. Classify each addition, update affected briefs, and revisit review when
the design changes. Clarify incompatible scope or priority conflicts; otherwise continue
authorized work without unrelated improvements or restarting unaffected phases.

## Session and worker overrides

Apply the snapshot and override lifecycle in the [trigger pyramid](trigger_pyramid.md).
At task start, copy the file to `TASK/trigger_pyramid.md`; this session snapshot
governs workers, rollovers, and resumes until reset or replaced. Repository-default
updates do not rewrite active snapshots, and other sessions are unaffected.

For a session-wide request, the user may override rows with a compact instruction
such as `smartest opus-med smart astra-high medium sol-med mid luna-xhigh`. The
coordinator interprets it and edits only the current session copy; no natural-language
parser does this automatically. Each supplied tier replaces that row, and omitted
tiers stay as they were. Exact user effort beats the scouting/mechanical default. Exact provider
restrictions such as "Use Codex" filter rows without reordering them; ask if the
selected tier becomes empty instead of promoting. An explicit worker assignment
wins over the session map and remains scoped to that worker. Omit `KIND:` on legacy
spawns that use either override path.

## Call budget

Legacy-router limits only; shared native-worker sessions do not enforce this table.

One band for every kind, and the kit's one hard mechanism. Change a number here and the hook changes.

| threshold | calls | what happens |
| --- | --- | --- |
| warn | 150 | exactly one call is denied, and the refusal tells the agent its count |
| floor | 230 | only a write of the agent's own `out.md` and the handback are still permitted |
| hard | 250 | the refusal escalates; the `out.md` write is still never denied |

## Hierarchy: delegationmaxxing

The main thread is the orchestrator, not an execution worker. It owns user intent,
task decomposition, scoped briefs, model selection, durable coordination state,
acceptance decisions, and the final synthesis. It normally uses Smart under the
current task snapshot, typically Astra or Opus; session overrides still apply.
Delegate source exploration, session/transcript summaries, implementation,
debugging, tests, documentation, and independent verification to bounded workers
using the trigger pyramid.
Do not start a substantial investigation in the main thread before delegating it.
Workers execute their assignments; this rule does not require workers to recursively
delegate every action. Small tasks can use a single worker without a lead.

The coordinator may read applicable instructions, inspect task/worker status, write
briefs and its own state, manage lifecycle tickets, and inspect returned evidence.
It should not duplicate the worker's investigation or ingest full transcripts.
For complex work, split bounded audit, design, implementation, review, validation,
and documentation workstreams when their dependencies allow. Parallelize ready
work with disjoint source ownership; bound worker count by useful independent work,
not an arbitrary headcount. Serialize overlapping edits. The coordinator advances
other ready coordination while workers run and does not create duplicate managers.

If site rules, client capabilities, or permitted model availability block delegation,
explain the specific restriction before substantial execution. Ask for a scoped
direct-work exception or propose an authorized execution environment; do not silently
take over the worker's job or bypass a restriction. Explicit user instructions can
change this role preference, but cannot override higher-priority safety constraints.

Every logical agent, including workers, owns its own `TASK/agents/ID/STATE.md`
with Objective, Completed, Evidence, Unresolved, and Next sections. Checkpoint after
meaningful milestones and before returning, with changed-file evidence and incorporated
message IDs. Parents read `out.md` and verification evidence, not entire transcripts.
Keep `STATE.md` a current snapshot: replace superseded status instead of appending
progress logs and aim for about 200 words while retaining Objective, Completed,
Evidence, Unresolved, and Next. Preserve unresolved operations, next actions,
decisions, active user overrides, and evidence references; the 32 KiB hard limit
still applies. Create or append `historical_state.md` only when STATE is full enough
to need compression. Append the prior STATE as a dated verbatim snapshot before
rewriting it. Do not create it for a new agent or append it at every checkpoint or
recovery. Read older
history selectively; checkpoints capture the optional file when it exists.
The coordinator's state records decisions, worker ownership, dependencies, and next
actions. The main-thread-only state rule in the legacy layout below does not apply
to these shared per-agent records.

Prefer the current client's native delegation tools for same-engine work, including
nested delegation where the client and repository policy permit it. Do not use raw
CLI subprocesses as a substitute for available native children. If nesting is not
supported, ask the coordinator to spawn the worker; do not invent a native tool.
Use depth only when it removes context, never to add a manager:

```
main thread      talks to the user, writes briefs and STATE.md, reads out.md files, decides
  lead agent     optional Smart integrator for a workstream
    worker       Mid or Medium assignment; may be spawned by a useful Smart lead
    child        native delegation in the selected client; track its logical parent
```

Smart leads are optional and may delegate Mid workers when that removes context or
coordinates a real workstream. Use Medium for scoped work between Mid and Smart.
Smartest remains an exceptional escalation, not a default complex-feature stage.
Give each worker disjoint source ownership, a clear parent, and a stopping condition.
Small tasks can skip the lead.

A lead earns its cost when it integrates independent pieces the main thread need
not read in full. Prefer at most two delegation levels. Shared records live at
`TASK/agents/ID/`; `parent_agent` records the hierarchy, not nested lane directories.

### Native delegation and parent tracking

Use the compact creation path instead of handwritten Python to write/register briefs:

```bash
token-kit worker prepare TASK --agent scout --parent coordinator --engine codex --model MODEL --brief "Role: scout. Read only PATH. Output: summary with evidence. Stop when QUESTION is answered; escalate broader work."
```

`--assignment-file FILE` is an alternative for a longer brief. These options create
a new agent only; never overwrite an existing assignment. For an existing worker,
omit them. Save the returned ticket and spawn prompt; use `native_task_name` for
the native spawn, then bind its returned ID. Do not reconstruct paths or tickets
from memory. Reserve and bind remain separate because spawning is a client action.
If binding a `/root/...` task path, `hook_identity_status: pending_metadata` means
UUID-based tracking is not yet confirmed. The hook maps it only from a matching
child transcript metadata header. Never guess UUIDs from timing or model names.

Use automatic completion or message notifications when the client supports them;
do not assume every client can wake an idle turn. Send messages for changed scope,
blockers, decisions, or meaningful new evidence, not merely to ask whether a worker
is done. Do not re-read unchanged state or repeat CLI help each turn. Integrate concise worker
results instead of duplicating their searches. A context rollover target is not a
cumulative-spend budget: inspect the ledger at milestones, not in a polling loop.
Cached input still counts as reported usage; do not describe it as free or as billing.

1. Register the child: `token-kit agent TASK CHILD --parent PARENT --assignment-file BRIEF`.
2. Reserve: `token-kit worker prepare TASK --agent CHILD --engine ENGINE`.
   Spawn with the native tool only when `spawn_authorized` is true, passing its
   `spawn_prompt` and scoped model preferences. Bind the returned native ID with
   `token-kit worker bind TASK --agent CHILD --ticket TICKET --native-id NATIVE_ID`.
3. Use native messaging/completion tools for live interaction. For durable messages,
   use `token-kit send TASK --agent CHILD "message"`; queueing does not mean live delivery.
4. Parent recovery: `token-kit resume TASK --agent PARENT` includes direct children
   and pending notices. `token-kit status TASK` lists all agents, parent links, and
   native attempts, including grandchildren. No transcript scanning is needed.
5. The child checkpoints and requests rollover/completion using its ticket. The
   parent confirms native closure and reconciles external jobs before `worker stopped`,
   or uses verified orphan retirement below for a dead managed owner.
   Replace only if work remains, under the same logical ID; never blindly repeat a spawn.
   For an orphan of a dead managed runner, use the recovery retirement path below.

Every nested lead follows this protocol. Hooks can notify an active parent; an idle
parent sees durable notices on resume. Registration/binding remain agent actions,
not automatic interception of every native spawn. Lifecycle visibility and token
accounting are separate: missing client usage is unknown, not zero.

For cross-engine work, respect the user's model/provider choice rather than silently
substituting a same-engine child. Instrumented `codex-dispatch` / `codex-job` report
Codex usage inside managed sessions; raw CLI calls do not. A corresponding one-shot
Claude wrapper and unified `token-kit exec` are not implemented. Do not claim that
cross-engine jobs automatically share native attempt tracking.

### Continuing a task

```bash
token-kit continue parser
token-kit continue /path/to/task
token-kit continue --task /path/to/task --rollover-perc 80
```

`continue QUERY` launches a fresh continuation: one fuzzy match is selected
automatically; multiple matches show a chooser. Use `--select N` for scripts or
`--print` to preview without stopping or launching anything. An exact task path
also works. Latest launch settings are inherited unless explicitly overridden.
`resume TASK` remains a read-only state export, not a client launcher.

For a live managed runner on the same host, continuation requests a cooperative
stop and waits a bounded time for verified shutdown before starting recovery.
It does not attach to the old native UI. Unsupported runners, uncertain process
identity, or a shutdown timeout leave a blocker instead of launching duplicates.
An interrupted recovery can itself be continued; reconcile pending predecessor
runs oldest first. Preserve checkpoints and any newer working state. Never edit
run records manually to force continuation or infer operation success from a
runner exit. Reconcile external operations before ordinary work or replacements;
continuation does not authorize replay or release retries.

### Managed compaction recovery

Managed token-kit launches in all projects attempt compaction recovery even without
rollover flags. Eligible stops have a structured compaction marker or legacy run and
runtime records with exact matching compaction evidence. Unknown stops, generic
errors, and manual interrupts are excluded. Process uncertainty or an explicitly configured
restart limit can prevent recovery. Restarts are unlimited by default.
`--max-rollovers N` sets a cap; `0` disables automatic restarts, and
`--max-rollovers unlimited` removes the cap. Continuation preserves explicit new
caps. An untagged legacy value of 10 migrates as the old default to unlimited.

Recovery starts a fresh session. Read the committed checkpoint plus any newer working
`STATE.md`, inspect children and external operations, and verify every uncertain
outcome before taking a fresh checkpoint, explicitly closing the previous run, and
continuing. A dead PID does not prove an external operation finished, and a parent
exit does not prove a native child closed.

For workers orphaned by a verified dead managed runner, a linked recovery session
uses `worker retire` instead of requesting closure from that vanished runner.
Inspect the worker's state, evidence, and external operations; record outcomes and
remaining work, then take a fresh worker checkpoint. Retire the exact attempt:

```bash
token-kit worker retire TASK --agent CHILD --ticket TICKET --note "Evidence and remaining work" --operations-reconciled --recovery-agent PARENT --recovery-run RUN
```

Recovery identity flags default to the managed session environment. The command
checks the dead owner, linked recovery, and fresh checkpoint; the explicit
`--operations-reconciled` attests that no live or uncertain operations remain.
Its `retired` phase does not claim native closure or task success. A verified partial
or failed publication remains partial or failed; retirement authorizes no retry.
Only reserve a replacement when work remains and its actions are authorized.
If an operation is live or uncertain, retain the blocker and report it once; do not
poll unchanged evidence or repeatedly ask a vanished runner to confirm closure.

### CodeGraph: worktree ownership and freshness

One index per worktree, not per task, branch name, remote URL, or Git commit.
Independent worktrees and independent clones must not share a writable index,
even at the same HEAD. Tasks using the same worktree should reuse its index.

Resolve the assignment's explicit source workspace before querying. For Git work:

```bash
git -C /path/to/worktree rev-parse --show-toplevel
git -C /path/to/worktree rev-parse --absolute-git-dir
```

Canonicalize these paths (resolve symlinks). The physical worktree root plus its
per-worktree Git directory distinguishes linked worktrees; the shared Git common
directory alone does not. Record the resolved workspace, Git directory, index
location, and index generation/provenance in the assignment and STATE.md. For
non-Git sources, record the physical workspace root and explicit index provenance.
Do not infer a child's workspace from its parent's cwd or select an index by branch
name. A moved, copied, or recreated checkout requires identity revalidation; if its
index provenance is uncertain, rebuild deliberately rather than reuse blindly.

| Event | Rule |
| --- | --- |
| First use of a worktree | Initialize its own index after confirming scope and indexing authority |
| Edits in that worktree | Use incremental sync; HEAD alone misses dirty and untracked source changes |
| Session start or rollover | Reuse the matching index; check freshness before relying on results |
| Checkout, merge, rebase, reset, or edits from another session | Reconcile changed source before querying; do not assume watcher delivery |
| Several agents in one worktree | Share one coordinated writer; do not launch competing rebuilds |
| Identity or freshness uncertain | Read the assigned source directly; label graph evidence stale/unknown |

Independent workers query their own worktree's graph and return findings with the
worktree identity, source path, and revision or content evidence. The parent treats
those findings as branch-specific, not as facts about its own checkout. Integrate
source changes using the agreed Git workflow, resolve source conflicts, then sync
the destination worktree's index and revalidate findings there. Never merge SQLite
index databases or copy a worker's graph over the parent's graph.

Indexes are disposable derived data; assignments and checkpoints remain durable
task state. Token Kit currently stores one workspace per task and does not implement
automatic per-agent worktree routing or index identity/freshness enforcement. For
independent worktrees, use separate tasks with explicit --workspace paths and record
their task paths in the coordinating parent's state. A native worker ticket alone
does not isolate a filesystem or switch its workspace.

On shared HPC storage, do not assume cross-host SQLite/watcher safety. Coordinate
one writer in a supported storage/host setup; large indexing belongs in a compute
allocation. Do not silently start daemons or scan a broad directory. This is agent
policy, not an implemented Token Kit index supervisor.

The call-budget hooks, lane layout, supervisor, and examples below describe legacy
compatibility, not the shared native-worker lifecycle above.

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
