# Token Kit simplification plan

Status: revised after two independent red-team reviews; ready to guide implementation.
Planning only; no runtime changes or live rollover validation.
Baseline inspected: `30ff036` (2026-09-25 handoff), clean working tree before this document.

## 1. Product contract

Token Kit provides ordinary folders and a launcher that carries a session through
automatic context rollovers. It does not approve assignments, enforce a delegation
method, certify completion, or require the agent to administer its own lifecycle.

The user should start/resume once, then work normally. An agent should write notes
and results using normal file operations. Ending work requires no Token Kit command.
Task completion is descriptive prose or an optional display label, never a gate.

This plan replaces the protocol, rather than hiding its commands or changing more
validation errors to warnings. Existing user/site permissions remain in force.
Being permissive about bookkeeping does not enable `--yolo` automatically.

Scope: current shared-task workflow, automatic rollover, native-worker interaction,
existing folders, installed instructions/hooks, CLI compatibility, and regression
coverage. No unrelated Retread work, publication, job submission, CodeGraph indexing,
provider upgrade, or live-session takeover is part of implementation by default.

## 2. What the source actually does today

| Area | Current coupling | Change required |
| --- | --- | --- |
| `core/store.py:Store.__init__`, `create`, `add_agent` | Task schema, model pyramid, coordinator scaffolding and checkpoints underpin ordinary folders | Separate readable working files from launcher metadata |
| `core/store.py:update_task` | `done` refuses unresolved lifecycle attempts and runs | Labels independent of process state |
| `core/lifecycle.py` | Tickets, prepare/bind, completion requests, stop reconciliation, retirement | Remove from new execution path; retain only bounded legacy readers as needed |
| `runtime.py:_handle`, `wait_segment` | Rollover demands new committed checkpoint, unchanged state/evidence/HEAD and reconciled children | Automatic handoff capture; no semantic completion/fingerprint gate |
| `core/store.py:claim_run`, planned-rollover/close/recovery methods | Launch/recovery depends on worker reconciliation and checkpoint chains | Replace with narrow launcher ownership and process-exit checks |
| `continuation.py:handoff` | Cooperative handoff plus old store/recovery dependency | Reuse cooperative stop and identity logic behind the new launch path |
| `worker_policy.py`, `project_install.py`, adapters | Inject lifecycle commands and large workflow rules | Short folder guidance, model preferences optional |
| `__main__.py`, `workflow.py`, legacy `cli.py` | Broad overlapping command surfaces | Small normal interface; temporary explicit compatibility |
| `inbox.py`, `core/ledger.py` | Durable steering/telemetry share bookkeeping machinery | Preserve useful data independently; no acknowledgment/checkpoint obligations |
| Legacy router, supervisor, installer | Independent caps, hooks and restart paths | Remove enforcement from default installs; prevent competing supervisors |

These are local-source observations, not claims about current provider APIs.
Adapter behavior must be verified against the installed client before claiming a
live rollover works. The old tests are evidence of intended legacy behavior, not
requirements to preserve all of it.

## 3. Decisions and intended daily experience

### Normal command surface

- `token-kit`: bounded picker with a create-new choice.
- `token-kit run "Title"`: create a named session, idle until the user types;
  `--prompt` begins work immediately, preserving existing behavior.
- `token-kit continue QUERY_OR_PATH`: resume; ambiguous matches require a chooser.
- Optional `status`: a short read-only diagnostic, never required for work.

Keep engine/model/workspace and rollover options. Keep preview/dry-run and
noninteractive explicit selection. Do not add a configuration wizard or natural
language settings parser. Keep existing task-root defaults and descriptive names;
changing storage location is not part of simplification.

Do not overload a title as a fuzzy lookup: `run "Title"` creates, `continue` resumes.
Existing `run --task PATH` remains a deprecated resume alias, outside normal help.
No matches must not silently create an unintended task. Noninteractive ambiguity
returns candidates and a selection instruction without launching.

New tasks default to 80% rollover, unlimited successful rollovers. Explicit off,
thresholds, context-window overrides, restart limits, engine/model/effort, workspace,
and permissions survive continuation. Preserve explicit old settings; an old absent
threshold adopts the new default, announced once. Add an explicit `--no-rollover`
to distinguish off from unspecified. Preserve `--max-rollovers 0` meaning
no automatic restart, including emergency compaction recovery.

| Resolved setting | Threshold rollover | Recognized parent-compaction restart |
| --- | --- | --- |
| Unspecified on a new task | 80%, when measurable | Enabled when supported |
| Explicit threshold | That threshold | Enabled when supported |
| `--no-rollover` | Off | Off |
| `--max-rollovers 0` | No restart; do not request a handoff just to stop | Off |
| Positive restart limit | Enabled up to the limit | Shares the same limit |

Reject `--no-rollover` combined with an explicit threshold or positive restart cap
in the same invocation. CLI overrides beat saved settings; saved explicit settings
beat new defaults. Distinguish a missing/null legacy threshold (previous default)
from a recorded explicit disable/cap-zero. Do not infer user intent from an untagged
historical default value. A finite exhausted cap leaves the client in native mode
where supported instead of manufacturing a failed closure. Unlimited is the default
budget, not permission to restart after a manual stop. With rollover off, no Token Kit
compaction veto or rollover hook trust requirement is installed; optional observation
must not become a prerequisite to ordinary launch.

Unlimited rollovers must not mean unlimited crash retries. Unknown errors, quota/
authentication failures, manual interrupts, and normal completion do not auto-replay.

### Folder layout

New tasks:

```text
task/
  task.md                 assignment/objective, ordinary editable prose
  STATE.md                current context and next steps, any useful format
  out.md                  optional overall result
  artifacts/              optional, created when used
  agents/                 optional, created when used
    descriptive-name/
      in.md
      STATE.md
      out.md
      artifacts/          optional
  .token-kit/             launcher-owned internals
    session.json          schema, workspace, settings, current process reference
    launch.lock           scoped ownership; never a global user lock
    runs/                 small per-segment process records and handoff snapshots
```

Do not eagerly create empty directories, tickets, assignment histories, ledgers or
checkpoints. No required STATE headings, word count, RESULT prefix or evidence list.
History is optional. Recommend preserving useful detail before shortening notes;
do not require archiving verbatim before every edit. Ordinary file ownership and
scoped coordination for overlapping source writes remain the agents' responsibility.

New layout does not require moving old files. Existing tasks continue using
`agents/coordinator/STATE.md` and their existing assignments/results. A layout reader
selects existing locations; it never maintains two writable copies of current STATE.
Old checkpoints already mirrored coordinator STATE to task-root STATE. Identify the
established working path from the layout, not mtime. If the alternate copy differs,
preserve it and reference both in recovery with their roles explained. Do not silently
discard, merge or overwrite either copy. For missing canonical state, the alternate
is a fallback, explicitly identified as such.
Old `task.json`, `trigger_pyramid.md`, checkpoints, messages, runs and artifacts remain
available. Legacy `lanes/` layouts are readable in place when passed explicitly.

### Guidance and model preferences

Inject at most a small paragraph: task path, state path, assignment path, keep useful
notes, write results when useful, and consult uncertain external outcomes before
repeating them. No instructions to invoke checkpoint, bind, close, retire or reconcile.
No mandatory planning/review sequence or per-action tier announcements.

Preserve explicit user model/effort/provider choices and existing task model maps.
Treat a preferences file as optional guidance; absence or malformed formatting must
not prevent launch or invent a replacement model. Exact launch flags remain exact.
Repository model defaults do not overwrite a user's task-specific preferences.
Include the path to existing task preferences in brief/recovery text so preserving
the file also preserves its discoverability. Precedence: current user instructions,
then explicit scoped launch/worker overrides, then task preferences, then defaults.
Add optional explicit effort to `LaunchRequest` and persisted settings: current
adapters instead derive and inject effort from model names. Unspecified effort should
inherit native settings rather than silently overriding them; exact choices survive
every rollover. Engine changes are allowed on explicit continuation after the old
client stops. Carry only portable settings; do not pass the old provider's model or
effort value blindly to a different provider or silently translate permissions.

Historical generated policy is not current policy. The recovery preface explicitly
supersedes Token Kit's old checkpoint/ticket/closure/reconciliation-only instructions,
including those inside old STATE, assignments, recovery-input files and notices.
Recognized generated wrappers are omitted from the recovery view; raw files remain
unchanged. Preserve actual assignment text and user/site constraints. Do not strip
arbitrary prose by keyword. Old lifecycle notices are historical observations, never
fresh obligations. Surface available later results/messages when old STATE still
reports a bookkeeping blocker; do not claim those results were independently verified.

## 4. Minimal internal architecture

Keep the code organized around five responsibilities, without building a new plugin
system or generic workflow engine:

1. **Task files:** resolve layout, read bounded prose, preserve existing files.
2. **Launcher:** settings, single-owner reservation, subprocess start/stop, exit status.
3. **Rollover controller:** usage observation, one handoff request, successor launch.
4. **Adapters:** provider-specific argv, hooks/events, identity and capabilities.
5. **Compatibility:** old layout/settings readers and temporary command/hook shims.

Prefer extracting reusable functions from existing modules over an all-at-once
rewrite. New control flow must not import `core.lifecycle` or route through old
`Store.claim_run`/closure gates. Compatibility may read old records; it may not
resurrect their authority over new work.

Internal run IDs remain useful for distinguishing processes and ignoring late
events. They are generated automatically, never tickets the agent must handle.
Internal records describe processes, not task success. Persist atomically and keep
the launch claim through publication of successor identity. Do not hold a file lock
while waiting for model text, callbacks, or user input.

Separate a short metadata transaction lock from a lifetime slot-ownership lock; the
latter may stay held by the supervisor while work runs, without preventing hooks or
file edits. Slot identity is `(canonical task, agent)`, not the whole task. Before
spawn, durably record intent under ownership. After spawn, publish verified child
identity before considering launch complete. If a crash leaves a pre-spawn claim
without identity, it means "launch may have happened," not "safe to retry."
Prefer a small child-start handshake that cannot execute the client until its identity
is published, and exits if the parent disappears before release. Test that exact
window with real fake-client subprocesses. If the platform cannot establish this,
retain the uncertain claim and block only that slot until positive process evidence
resolves it; never turn owner death or elapsed time into proof of child death.

Use scoped locking for a managed session and brief metadata updates. Distinct workers
can run concurrently. Canonical task identity must prevent alias paths creating two
owners. PID alone is insufficient: retain host and process-start identity; use the
owned subprocess handle where available. Cross-host uncertainty is not local death.

Only the owning launcher stops its verified child. Do not kill unrelated processes,
the surviving supervisor, detached jobs, or arbitrary descendants. No distributed
lease service, background sweeper, or heartbeat farm is needed.

## 5. Rollover behavior

### Ordinary successful rollover

1. Observe current context usage at supported event/turn boundaries. Count current
   context, not cumulative billed tokens. Threshold crossing is a request, not an
   instruction to kill a tool midway through an operation.
2. Ask once for a brief update to the working STATE: what changed, what remains,
   important user constraints, and known active/uncertain work. No required command.
3. At the next supported safe boundary, capture a launcher-owned recovery bundle:
   assignment, current state, explicit settings/preferences, recent pending steering,
   and available in-flight-work observations. Missing prose is marked missing.
4. Stop the owned client cooperatively and verify that this client stopped. Capture
   final working files after exit so late writes are included. Files remain authoritative
   for newer changes; a snapshot is a fallback, not permission to overwrite them.
5. Start exactly one fresh successor with a short recovery prompt and file paths.
   Carry forward user constraints and settings; reset usage accounting for the new
   segment. The launcher, not the model, handles process transitions.

No fresh checkpoint ID, unchanged Git HEAD, unchanged output fingerprint, artifact
approval, child completion closure, or operations-reconciled attestation is required.
Unrelated file edits are ordinary progress. Rollover does not mark tasks complete.

### Failure and fallback policy

| Situation | Behavior |
| --- | --- |
| STATE missing, empty, free-form or stale | Warn once; use assignment, last readable snapshot and bounded available recent context; resume with uncertainty explicit |
| No usable objective or context at all | Open an idle session; do not invent work or replay a guessed instruction |
| Agent ignores handoff request | Use existing files at a safe boundary; do not demand repeated checkpoint turns |
| Usage/window temporarily unavailable | Warn once; keep work running; no fabricated capacity; resume proactive rollover if valid telemetry returns |
| Unsupported hooks/telemetry at launch | Clearly report degraded rollover capability and allow ordinary client behavior; never advertise guaranteed rollover |
| Supported compaction event | Capture available context and roll over if owned-client shutdown is verifiable; otherwise retain native fallback where possible |
| Unknown error, interrupt, normal completion | Save best-effort diagnostic state and stop; no automatic task replay |
| Shutdown timeout or uncertain client identity | Do not start a duplicate; report precise process limitation, leave files usable |
| Disk full, unreadable metadata, invalid JSON | Keep existing user files intact; isolate optional-data errors; inability to establish exclusive ownership blocks only launch |
| Successor startup/auth failure | Preserve handoff and stop with actionable error; no paid retry loop |
| Late hook from predecessor | Ignore for current run; do not overwrite successor ownership |

Define an adapter capability result rather than assuming all hooks exist. Native
compaction must not be unconditionally disabled if managed rollover is unavailable.
Remove current global strict-no-compaction requirement from the default path.
When telemetry disappears after launch, report the degradation; do not halt useful
work merely because context accounting failed. Restoring a provider's native fallback
mid-session may not be supported; document that limitation rather than inventing it.

A successor that immediately asks to roll over before any useful progress is a
restart failure, not a successful rollover. Detect repeated startup/no-progress cycles
and stop with one diagnostic. Tests must define the signal (fresh-segment activity and
valid new usage), rather than using wall time alone or imposing a lifetime rollover cap.

Use a direct current-run/settings pointer, not historical run enumeration on each
launch. Remove inherited history-length limits (including the current 256-run inference
limit). Each automatic bundle has bounded text size and references artifacts/history;
it never embeds previous bundles recursively. Retain old bundles by default. Optional
future retention must not delete user prose/artifacts or become required maintenance.

## 6. Workers and operations across rollover

Native worker creation and messaging use the client's native tools. No Token Kit
reservation or binding ceremony. Worker folders can be created by `mkdir` and normal
writes; they need not exist before native spawning. Completion is ordinary output.

Distinguish workers from external jobs. Parent process exit does not establish either
worker completion or job success. The recovery notes must describe uncertain work
locally: do not blindly respawn that assignment or retry its publication/submission.
Other authorized work continues without a global reconciliation phase.

Observe native child IDs automatically when the client exposes them. Use this bounded
adapter decision at a rollover boundary:

- If the adapter establishes worker survival and output/reconnection paths, roll the
  parent and carry those observations forward without respawning the workers.
- If native workers would be interrupted, or their fate is unknown, defer managed
  rollover for that segment and leave native compaction available. Reconsider on a
  real worker-finished event; do not poll or inject repeated drain requests.
- Missing stop events do not mean workers are complete. Display one notice that
  managed rollover is paused for this client capability; ordinary work continues.
- If native fallback is unavailable too, report that limitation and use only a
  supported safe boundary stop. At context exhaustion, retain partial state and
  surface the native error; do not promise uninterrupted recovery or kill a tool
  midway through its operation. An adapter with no viable fallback must declare
  that before starting a managed session.

Never infer an unknown worker is gone merely because its parent exited. An uncertain
assignment is not automatically replaced; unrelated work continues. This is a local
client limitation, not a requirement to certify worker closure. No polling agents
or new distributed worker scheduler. Default operation must not disable native
compaction globally, since that would defeat these fallback choices.

Independently launched cross-engine workers may outlive their coordinator. Preserve
their output paths and process observations; coordinator rollover does not terminate
or relaunch them. Support the currently implemented managed Claude worker launch
without requiring a ticket, using the same launcher ownership checks. Do not promise
new Codex print-mode support as part of this change.

Do not deliberately assign a parent's session identity to a child. Clients can still
inherit environment/hooks, so environment variables alone are insufficient identity.
Bind controlling events to an automatically generated run nonce and verified parent
session identity from an adapter-supported handshake. Unknown/child-first events
cannot establish parent ownership, trigger parent rollover, veto compaction, or stop
the parent. Late predecessor events remain noncontrolling. If a provider cannot
distinguish parent events, managed control degrades visibly; it must not guess.
Native workers may use native
compaction; automatic Token Kit rollover applies to sessions actually owned by its
launcher. Explain this coverage rather than claiming arbitrary native-worker recovery.

## 7. Messages, usage and recovery input

Retain existing pending user steering and worker results during transition. Use native
messaging for new live coordination where available. Cross-engine durable messages
can remain an optional utility; saving/delivering them never requires checkpoints.

Delivery records mean presented, not understood or executed. Persist an automatic
presentation cursor across segments, independent of checkpoint records. Bound recovery
excerpts and preserve full files/stable IDs. Already-presented messages remain
discoverable as context but are not reissued as fresh instructions on every rollover.
If a crash leaves delivery uncertain, redisplay once marked as possibly already seen;
never auto-execute a message as a shell command or assume an external action is owed.
Legacy incorporated IDs must still be read so old acknowledged messages stay quiet.
Where the client offers no delivery acknowledgment, mark injection as presentation
attempted and carry the uncertainty explicitly; do not claim exactly-once delivery.
No user/agent acknowledgment ceremony replaces the old checkpoint obligation.

Extract the small amount of telemetry needed for rollover from ledger presentation.
Optional accounting failure must not stop a client. Keep historical ledger files;
do not require a token ledger for a task. Bound transcript reads to the exact managed
session with incremental cursors; no recursive workspace or home-directory scans.

Recovery prompts load concise current state and relevant assignment/settings first.
Large history/artifacts are referenced, not pasted wholesale. Truncation must be
visible and include paths to full text. Never execute commands obtained from a saved
artifact merely because it was included in recovery context.

## 8. Compatibility and installation transition

1. **Read old tasks directly.** Select paths automatically, retain original bytes and
   timestamps when reading, and do not copy/move user prose into a new hierarchy.
   Missing optional old records do not invalidate a task.
2. **Resolve workspace deliberately.** Use explicit override, new metadata, valid old
   metadata, then a valid legacy `Cwd:`. If none is usable, request a workspace or
   open idle without executing project work; never guess a different repository.
3. **Handle old process records separately.** Read sufficient process identity to
   avoid duplicate launch. Old completion/checkpoint states do not gate notes or
   task labels. Uncertain process ownership blocks only a conflicting launch.
4. **Do not hot-swap running supervisors.** The installed CLI is checkout-backed.
   Existing hooks capture absolute `runtime.py` paths, and old PATH entries point at
   checkout shims. Keep that entire original source/import dependency closure intact
   while old sessions exist. Build the new implementation in a separate release path
   and switch only new invocations to it. Copying old code elsewhere does not fix
   already-captured paths. Future hooks use immutable release paths. Do not update
   the original checkout in place until its consumers have exited; a tested dispatcher
   at every captured path is an alternative only if an in-place update is unavoidable.
5. **Installed policy cleanup.** Enumerate exact Token Kit-owned markers/hooks in
   explicit project/user config locations. Replace owned guidance and remove owned
   call-budget/lifecycle enforcement. Preserve user/site instructions and unrelated
   hooks. Back up edited configuration. No whole-home search or blind text removal.
   Preserve the contract of existing old sessions: cleanup that would affect their
   live hooks waits for their exit. New sessions must exclude only positively identified
   obsolete Token Kit hooks; if safe exclusion is unsupported, defer that cutover and
   explain the exact config conflict. Never describe a session as simplified while
   the old global cap or lifecycle enforcement remains active.
6. **Capability and trust.** Preserve client hook trust/approval controls. Unsupported
   or unapproved hooks lead to a clear degraded-capability launch, not an attempt to
   bypass the client's controls.
7. **Compatibility commands.** Keep `run`, `continue`, `pick`, `launch`, `resume`,
   `status` aliases with their existing meanings during transition. In particular,
   existing `resume` remains read-only. Old `checkpoint` may save an optional snapshot;
   old `done`/`reopen` only label. In the new CLI, obsolete worker/close-run commands
   exit nonzero, perform no mutations and briefly name the ordinary replacement
   action; they cannot mint fake authorization or report work complete. Old running
   clients use their pinned old CLI, where their protocol still functions. These are
   distinct callers, not a single shim pretending to satisfy incompatible contracts.
8. **Old installer/router/supervisor.** Stop adding them to new installs. Keep exact
   installed hook shims usable until cleanup; do not leave dangling commands. Ensure
   an existing legacy supervisor and new launcher cannot own the same session.
9. **Historical documentation.** Keep dated handoffs as history. Replace current
   README/matrix/install guidance rather than appending contradictory overrides.
   Clearly separate legacy docs from the normal entry point.

For current-schema tasks, use a concrete old/new launch fence in addition to new slot
ownership: new supervisors hold a shared lock on the existing `.continue.lock` for
their entire lifetime, including rollover gaps. Old explicit continuation requires
an exclusive lock there; old ordinary `Store.claim_run` also attempts an exclusive
lock under `.lock`. Thus multiple new slots can coexist while old launch routes
cannot claim the task. Acquire the shared fence nonblocking, then inspect old run
claims under the existing `.lock` before publishing new ownership. A launch already
claimed by the old code must be observed and resolved before task cutover. Require
no active/uncertain old managed owner anywhere in that task before entering this
mixed-version fence; do not strand an old worker's automatic restart. Keep the shared
fence across successor publication, not just around the initial scan. New metadata
locks remain independent so this fence cannot block new notes, hooks or other slots.

A supervisor-held lock alone is not crash-safe: it disappears if the supervisor dies
while its client survives. Therefore, under the same old `.lock`, also publish a narrow
legacy-readable run claim in the existing agent's `runs/` before spawning. It contains
only compatible run/process identity fields and a starting/running status old admission
honors, plus provenance identifying it as a compatibility claim. Keep it active across
all successor gaps, update child identity atomically, and release it only after verified
client death and final ownership release. On supervisor crash, leave it active/uncertain
until process evidence resolves it. New code must not interpret its status as task or
artifact approval, and no checkpoint/lifecycle ticket is required to maintain it.
Old code may refuse to recover an unsupported compatibility claim, but must not launch
past it or signal a mismatched process. Test crash with a surviving client, then invoke
the frozen old executable; it must refuse a duplicate. If the old schema cannot support
that narrow claim reliably, defer task cutover rather than relying on lock inheritance.

This bridge is specific to the audited current workflow's launch routes. Verify all
supported old entry points honor it with concurrent old/new executable tests. For
older standalone supervisors that do not honor this lock, retire/route their exact
launch entry points before adopting that task, or defer its cutover. Do not claim a
record scan is a substitute for fencing. The fence is launcher-owned compatibility;
it adds no worker protocol, agent command or completion certification.

Phase A records each retained component's exact consumer and removal condition:
old-runtime shims last only while captured consumers exist; install/uninstall helpers
last while owned installed references remain; user-facing deprecated aliases can be
removed in a documented later breaking release. Old-layout reading remains supported
without the old execution protocol. No maintenance command is required from agents.

Compatibility is a transition boundary, not a second permanent implementation of
the new workflow. Old records remain readable; old lifecycle execution code can be
removed once no supported installed/running entry point depends on it. Do not delete
historical task data to achieve that removal.

## 9. Implementation sequence and source ownership

Each phase ends in an inspectable diff and meaningful offline tests. Development
uses an isolated checkout because edits to this checkout affect installed invocations.
Integrate serially; parallel workers may own disjoint source/tests after interfaces
are settled. No implementation or paid live-client launch is performed by this plan.

| Phase | Work and principal files | Exit condition |
| --- | --- | --- |
| A. Characterize | Tests/fixtures for old layouts, settings, messages, process cases; exact installed-entry inventory | Preserved behavior and behavior to remove are explicit |
| B. Plain task files | New small layout/metadata reader; `core/store.py` extraction; `display.py`, picker integration | Plain/old folders usable without lifecycle/pyramid/checkpoint validation |
| C. Launcher ownership | `workflow.py`, `continuation.py`, adapters; minimal process records | One managed owner; cooperative restart; no closure protocol |
| D. Automatic handoff | `runtime.py`, `rollover.py`, telemetry extraction, recovery builder | Fake clients roll repeatedly with ordinary STATE writes and no checkpoint commands |
| E. Workers/messages | `worker_policy.py`, `inbox.py`, managed-worker launch | Native delegation unregistered; cross-engine output/steering preserved; no completion ceremony |
| F. Guidance/install/CLI | `__main__.py`, `project_install.py`, `cli.py`, settings merge, docs and shell shims | Simple help and instructions; owned old gates removed; unrelated configuration intact |
| G. Remove dead machinery | `core/lifecycle.py`, old store gates, router/cap paths and obsolete tests | New path independent; compatibility inventory explains every retained legacy component |
| H. Validate and cut over | Offline suite, adapter capability check, bounded live smoke when explicitly scoped, rollback notes | Evidence distinguishes offline success from actual live behavior |

Phases B–E must land together as a coherent candidate before changing the installed
entry point. Removing instructions first while retaining old gates would strand
agents. Phase F cleanup is part of acceptance, not an optional later polish task.

## 9a. Parallel execution plan

Use one integrator and up to four bounded implementation workers. Four is a ceiling,
not a quota: start a worker only when its inputs are ready. This is an implementation
arrangement, not a workflow to install into Token Kit or impose on future users.
Keep process ownership and rollover under one owner; they share the most consequential
invariants and should not be developed as competing state machines.

### First agree on the small shared interfaces

The integrator resolves these before parallel implementation. Record concrete Python
signatures and a few representative fixtures in the candidate checkout; avoid building
a generic service layer or configuration framework.

| Interface | Contract to settle | Producer → consumer |
| --- | --- | --- |
| Task view | Canonical task/workspace and assignment/STATE/preferences paths; alternate legacy state; diagnostics; read-only loading | Files → recovery, CLI |
| Launch options | Engine, explicit optional model/effort, workspace, permissions, threshold/off, restart budget and provenance | CLI/settings → adapters, runner |
| Adapter events | Verified parent/child/unknown identity, segment identity, context sample, safe boundary and exit reason; unsupported capability is explicit | Adapters → runner |
| Recovery input | Bounded selected text, full-file references, pending/presented steering, uncertainties; no semantic approval fields | Files/messages → runner |
| Process ownership | Slot identity, claim/publication/exit and compatibility-fence responsibilities; unknown outcome is not retry authority | Runner only; CLI requests actions |

Shared definitions have one editor: the integrator. Workers propose necessary changes
with a failing example; the integrator updates producers and consumers together.
Do not make teams coordinate through independently invented JSON schemas, repeated
file moves, or placeholder functions that silently report success.

### Independent ownership

Paths below refer to the isolated candidate checkout. New module names are suggested
boundaries, not a requirement to proliferate files. Existing production paths remain
untouched during candidate development.

| Worker | Owns | Can start independently | Completion evidence |
| --- | --- | --- | --- |
| A: files and recovery | Plain/legacy layout reader and recovery builder (e.g. `task_files.py`), `inbox.py`, `worker_policy.py`, corresponding private test modules | Legacy/state/message fixtures and wrapper recognition; implementation after task/recovery contract | Free-form notes work; divergent state preserved; old rules do not revive; three rollovers do not repeat steering |
| B: runner and rollover | `runtime.py`, `continuation.py`, `rollover.py`, narrow new process-ownership module, subprocess tests | Crash/fence fixtures and process ownership design; controller against agreed fake events | One successor; ordinary STATE writes suffice; off/errors respected; old/new crash fencing holds |
| C: provider adapters | `adapters/`, telemetry extraction from `core/ledger.py`, provider fake-event fixtures/tests | Capability audit, argv/effort behavior and identity cases | Explicit effort/settings retained; child/unknown events cannot control parent; unsupported rollover has an honest fallback |
| D: CLI and installed guidance | CLI parsing/presentation helper, `display.py`, `project_install.py`, owned-settings cleanup helpers, README/matrix, focused config/CLI tests | Fixture inventory, current instruction cleanup drafts and command semantics | Small public surface; old hooks preserved/excluded correctly; no unowned config changes; exact rollback preview |

Integrator owns `workflow.py`, `__main__.py`, shared contracts, shared fake-client
harness changes, integration tests and final dead-code removal. Existing
`core/store.py`/`core/lifecycle.py` are reference/legacy code initially; workers extract
needed small primitives into their owned modules rather than concurrently modifying
the old Store. The integrator removes obsolete paths only after the new path works.
Worker D supplies parsing functions for the integrator to wire; worker B supplies
runner entry points; neither independently rewrites the central workflow.

Each worker owns its behavior tests too. A separate test-only worker is not a substitute
for testing implementation. Independent adversarial review happens after the first
integrated candidate, using a freed worker slot rather than more concurrent agents.

### Waves and dependencies

1. **Characterization in parallel:** A builds old-folder fixtures; B captures process
   races and legacy admission behavior; C establishes adapter event/capability facts;
   D inventories exact owned hook/config variants. These are bounded local tasks,
   not project-wide indexing or live-client experiments.
2. **Contract checkpoint:** integrator settles the interfaces above, ownership and
   settings truth table. This is a small code/design integration step, not a user
   approval gate or a new Token Kit checkpoint command.
3. **Parallel implementation:** A, B, C and D work against those contracts. B initially
   uses fake adapter events and fixture recovery inputs; C need not wait for B's loop;
   D tests parsing/config changes without invoking a live launcher.
4. **Early vertical integration:** as soon as A/B/C have working slices, integrator
   wires one fake-client task through launch → ordinary STATE update → rollover →
   successor. D continues config/CLI work in parallel. Test missing state, absent
   telemetry and manual stop here, not only on the happy path. Do not wait until all
   workers declare their entire assignment complete to discover interface mismatch.
5. **Compatibility integration:** add D's entry points, old/new fencing and exact
   captured hook/shim paths. Run mixed-version subprocess tests against a frozen old
   baseline. Shared entry-point changes and merges remain serialized.
6. **Independent review and cleanup:** one reviewer attacks the integrated behavior
   and user experience. Original owners fix their findings in their own files. The
   integrator deletes disconnected machinery, runs the combined relevant suite and
   prepares the cutover/rollback record. No parallel deployment or live-task migration.

Critical path: shared contracts → B's ownership/rollover plus C's identity events →
integrated fake-client handoff → mixed-version crash tests → cutover. A and D shorten
elapsed time by proceeding alongside that path. Adding more workers to B's lock and
process code is unlikely to shorten it and increases integration risk.

### Workspace, integration and resource rules

- Use a candidate checkout separate from the checkout-backed installed CLI. Each
  worker gets an isolated worktree/branch with the ownership above; the integrator
  alone changes the integration branch. Do not switch the shared checkout's branch.
- Workers return a commit/diff, meaningful test results and unresolved interface
  issues. No bespoke task ledger, ticket service, closure or approval protocol.
- Merge small coherent changes serially. Integration failing because a contract
  changed is resolved once by the contract owner, not separately in every branch.
- Test from git-listed copies under `/tmp`; use distinct temporary task/config roots
  and process slots. Never exercise fake recovery against the user's real tasks.
- At most two lightweight targeted test suites at once on the login node; run the
  integrated suite once after the candidate settles. No paid provider launches,
  indexing, job submission, real settings cleanup or process signals during fixture
  tests. Heavy testing, if needed, follows site allocation/submission rules.
- Freeze the old baseline used by compatibility tests. Worker progress must not
  change that executable beneath another worker's mixed-version test.
- Deployment and installed configuration changes have one owner and happen only
  after integration evidence exists. Parallelizable preparation does not imply
  simultaneous cutover of running sessions.

This decomposition permits four useful streams without turning implementation into
another permanent orchestration system. If a stream has no independent work left,
finish it; do not spawn agents just to wait for dependencies.

## 10. Acceptance tests

Tests assert user-visible outcomes and failure containment, not a replacement state
machine's internal names. Retain relevant quoting, permissions, path safety and
subprocess tests. Rewrite tests whose purpose was to enforce obsolete ceremony.

### Folder and workflow tests

- Create/resume, idle title launch, explicit prompt, fuzzy chooser, exact paths,
  noninteractive selection, dry-run no writes/no processes.
- Free-form/missing STATE, missing/invalid pyramid, changed HEAD and artifacts,
  absent/nonempty/revised out.md all work without lifecycle commands.
- Task label updates succeed with active workers and interrupted runs, without
  killing anything or claiming process completion.
- Old coordinator layout, legacy lanes, moved task paths, partial metadata and
  workspace override; never overwrite a newer working file with a snapshot.
- Existing explicit model/effort/provider/permission/threshold/restart preferences
  survive; an explicit off remains off; no automatic permission escalation.
- Divergent legacy root/coordinator STATE with misleading timestamps retains both;
  old wrapped assignment/recovery-input/matrix/lifecycle notices cannot reinstate
  the protocol. A later saved result is discoverable despite stale blocked STATE.

### Rollover and process tests

- Fake Claude and Codex adapters: repeated rollovers, 80% threshold, absolute targets,
  explicit window override, unknown window, delayed telemetry, normal completion.
- No model-issued checkpoint command; missing/ignored handoff note still produces
  best-effort recovery; unrelated writes do not veto rollover.
- Crash injection before/after snapshot, stop, process-exit observation, successor
  claim, subprocess creation and identity publication; never two successors.
- Simultaneous continue, alias paths, PID reuse, changed host, unknown identity,
  surviving supervisor, child alive/supervisor dead, cooperative timeout.
- Manual interruption, auth/quota failure, unknown error and immediate repeated
  rollover do not create restart storms. Explicit cap and unlimited behavior differ.
- Late predecessor hooks cannot stop/modify successor. No signal reaches unrelated
  processes. Permission flags/cwd/engine/model survive every segment.
- Child-before-parent, absent IDs, inherited parent environment and stale nonce are
  noncontrolling. Native-child survival/fallback/unknown-stop cases follow section 6.
- Explicit effort round-trips for both adapters; unspecified effort respects native
  settings. Engine changes after shutdown do not reuse incompatible provider flags.
- More than 256 completed segments remains resumable without an unbounded history
  scan or recursively growing recovery bundle.
- Native child remains live or uncertain: no automatic duplicate; independent
  cross-engine worker survives parent rollover and its output remains discoverable.
- External job continues or publication is uncertain: no replay based on PID exit;
  unrelated assignments remain usable.

### Compatibility and installation tests

- Old pending messages preserved, old acknowledged messages quiet, crash around
  delivery produces bounded possible-duplicate notice, no checkpoint acknowledgment.
- Three successive rollovers do not reissue already-presented steering as new work;
  undelivered and uncertain messages remain available without a manual cursor repair.
- Existing hooks and marker blocks upgraded idempotently; unrelated config unchanged;
  malformed/ambiguous ownership is reported without destructive cleanup.
- Old process invokes versioned old hook while new task runs new hook; neither reads
  the other's control schema. Unsupported client hooks degrade visibly.
- Execute an already-generated absolute old hook command and old checkout shim after
  cutover; both still load their original dependency closure. New sessions exclude
  obsolete global gates without removing unrelated hooks.
- Race old ordinary launch and old continuation against new launch and each rollover
  gap; only one implementation owns the task. Multiple new worker slots still run
  concurrently. Unfenced older supervisors cannot enter concurrent task cutover.
- Obsolete commands in the new CLI fail clearly without writes; old pinned CLI remains
  compatible; `run --task PATH` resumes rather than creating another task.
- Legacy installed shims still resolve; no call-count tool denial or child-compaction
  veto leaks into new sessions. No duplicate legacy/new supervisor ownership.
- Recovery bundle respects size limits, preserves full source paths, does not expose
  unrelated transcripts/secrets, and never shells interpolated task text.
- Disk-full/partial writes preserve prior valid notes and process ownership records;
  optional telemetry/message failures remain local.

Run Python 3.11 offline tests from a git-listed copy under `/tmp`, not by recursively
discovering the shared workspace. Run targeted suites per phase, then the relevant
combined suite once integration settles, plus `git diff --check`. No HPC computation
or scheduler submission needed. Live provider behavior remains unverified until an
explicitly scoped smoke test records client version, capability and observed handoff.

## 11. Cutover, rollback and definition of done

Before activating: inspect exact installed path/config targets and active managed
sessions once; do not poll. Preserve the old executable/package path for old sessions.
Switch new invocations only after integrated offline tests pass. Existing sessions
finish on their original runtime; a later user continuation can enter the new path
after conflicting process shutdown is established.

Rollback restores the previous launcher/config backups for old tasks; new prose
remains ordinary files and is not deleted. Do not point an old binary at new-only
internal metadata and promise compatibility. The new launcher's read-only preview
must provide an exact native-client recovery recipe: workspace, engine, explicit
model/effort/permission settings, and assignment/STATE paths. The launcher generates
and saves that recipe for new tasks using argv serialization rather than shell
interpolation; agents do not maintain it manually.
It provides ordinary native continuation if the new launcher is rolled back, without
promising automatic rollover. Test rollback with one old and one new-layout task.

Done means:

- A user starts/resumes and works through repeated rollovers without issuing a
  bookkeeping command or repairing a closure.
- Agents freely create/edit folders and report results through normal files/tools.
- Native delegation works without registration, and native-compaction limitations
  versus managed-rollover coverage are honestly documented.
- Existing tasks remain usable with their state, preferences, steering and artifacts.
- Installed current guidance contains no required prepare/bind/complete/retire/
  close-run/checkpoint ceremony or hardcoded planning pipeline.
- Remaining launch blockers have a concrete process, workspace, filesystem or client
  capability reason; no semantic task-completion or evidence-freshness gate survives.
- A missing telemetry feature cannot masquerade as functioning automatic rollover.
- Normal README/help fit the small product; historical machinery is not presented
  as something users or agents need to learn.

## 12. Review record

See `SIMPLIFICATION-RED-TEAM-2026-09-26.md` for the independent findings and explicit
dispositions. The revisions above address the design findings; implementation tests
and adapter smoke tests remain future work, not evidence already obtained.
