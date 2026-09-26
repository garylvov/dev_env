# Token Kit simplification: independent red-team record

Reviewed artifact: `SIMPLIFICATION-PLAN-2026-09-26.md`.
Source baseline: `30ff036`. Date: 2026-09-26.

Two read-only auditors mapped runtime and policy dependencies. Two separate reviewers
then attacked the draft independently: `red_team` covered runtime/migration risks;
`red_team_usability` covered permissiveness, compatibility and product complexity.
Reviewers did not author the draft or edit source. No runtime changes, installs,
live client launches, scheduler jobs, or implementation tests occurred.

Both initial verdicts: **revise before implementation**. The lead revised the plan
to address every finding below. These are design dispositions, not claims that the
fixes have been implemented or experimentally proven.

## Runtime and migration review

| ID | Severity | Independent finding | Disposition in revised plan |
| --- | --- | --- | --- |
| R1 | High | Future versioned hooks do not protect already-running clients: current `runtime.hooks()` captures absolute checkout paths; old PATH entries point at checkout shims | Accepted. Sections 8/11 preserve the entire original checkout/import dependency closure; new invocations use a separate immutable release. Cleanup cannot break live old config. Test an already-generated old hook/shim after cutover. |
| R2 | High | Old tasks may already contain divergent coordinator and task-root STATE because checkpoint mirrors it; mtime is not authority | Accepted. Section 3 selects the established layout path and surfaces divergent alternate content without merging or overwriting. Missing canonical state uses an explicitly identified fallback. |
| R3 | High | Saved assignments/STATE/recovery inputs/notices can reinstate the deleted protocol | Accepted. Sections 3/8 omit recognized generated wrappers from views and explicitly supersede old Token Kit requirements while retaining raw text and user/site constraints. Stale blocked STATE plus later result gets a regression fixture. |
| R4 | High | Waiting for native workers can become an indefinite global rollover blocker | Accepted. Section 6 defines survival/reconnection, event-driven defer with native fallback, and unsupported-capability outcomes. No repeated drain requests, automatic duplicate workers, or closure attestation. |
| R5 | High | Child-first or unidentified inherited hooks can acquire/control parent identity | Accepted. Section 6 requires run nonce plus verified parent binding; unknown/child events are noncontrolling. Explicit tests cover early/missing/late/inherited identity cases. |
| R6 | High | No-duplicate-launch claim lacks a mechanism for crash between spawn and PID publication | Accepted. Section 4 distinguishes ownership/transaction locks, durable spawn intent, publish-before-release handshake, and an uncertain-slot fallback that never infers child death from supervisor death. Test the actual subprocess window. |
| R7 | Medium | `--no-rollover` and zero budget have ambiguous emergency-restart semantics | Accepted. Section 3 adds a truth table: off means no automatic restart, including compaction. Conflicting flags are rejected, explicit saved settings retained, native-mode behavior defined. |
| R8 | Medium | Today's adapter contract cannot preserve explicit effort; it injects model-derived effort | Accepted. Sections 3/9 require optional effort in the launch/settings contract and no inferred override of native settings. Both adapters and every rollover are tested. |
| R9 | Medium | Unlimited rollover may accumulate unbounded discovery work; old code has a 256-run inference limit | Accepted. Section 5 requires a direct current pointer, bounded nonrecursive bundles and no inherited history-length cap. Historical/user data is not automatically deleted. |
| R10 | Medium | Old live clients need functional lifecycle commands while new clients need those commands removed | Accepted. Section 8 separates pinned legacy CLI consumers from the new CLI. New obsolete commands are nonmutating failures; old captured shims remain functional until their consumers exit. |

Reviewer constraint retained: no new scheduler, distributed lease service, migration
wizard, or generic capability framework. A small adapter capability record and local
launcher ownership are enough. Process identity must never become task approval.

## Usability and permissiveness review

| ID | Severity | Independent finding | Disposition in revised plan |
| --- | --- | --- | --- |
| U1 | P1 | Legacy recovery can revive historical bookkeeping blockers despite shorter installed guidance | Accepted alongside R3. Explicit precedence for historical generated policy, later-result discovery, preserved raw notes and a stale-blocker fixture. |
| U2 | P1 | Removing checkpoint acknowledgments without a cross-segment cursor repeats steering forever | Accepted. Section 7 uses automatic persistent presentation state, full-file preservation and one bounded uncertain-delivery notice. Tests span three rollovers; no user acknowledgment requirement. |
| U3 | P2 | Preferences may survive on disk but become undiscoverable to workers | Accepted. Section 3 includes the preferences path in short guidance and makes user/override/task/default precedence explicit. Parse failure cannot block ordinary launch. |
| U4 | P2 | “run creates” contradicts preserving existing `run --task` behavior | Accepted. Title form creates; explicit `run --task PATH` remains a deprecated resume alias outside normal help. Test no duplicate task creation. |
| U5 | P2 | Obsolete commands have no defined exit status/side-effect contract | Accepted. New CLI returns nonzero, changes no records and names the ordinary replacement action. No fake success, ticket or completion. |
| U6 | P2 | Rollback cannot rely on the old binary understanding new folders | Accepted. Section 11 defines native-client recovery recipes with workspace/settings/state paths for new tasks, while restoring old runtime for old tasks. Test both layouts. Native fallback does not promise automatic rollover. |
| U7 | P3 | Compatibility could become the same permanent system hidden behind smaller help | Accepted. Section 8 requires a consumer/removal condition for each retained component. Old-format reading survives; obsolete execution machinery does not become a requirement for new work. |

Reviewer criterion retained: ordinary agents must not need to learn internal records,
repair their schema, or certify completion to continue unrelated authorized work.

## Source-backed additions from the audits

- `workflow.py` has independent injection paths for ordinary resumes, recovery-only
  reconciliation, idle-start loading of the full matrix, and ticketed worker completion.
  All must change; replacing `worker_policy.py` alone is insufficient.
- `core/store.py` permanently wraps saved assignments, makes model maps a launch
  dependency, gates task labels, validates recovery graphs and checkpoints, and mirrors
  coordinator STATE to root. New file access must not reuse those gates implicitly.
- `runtime.py` combines telemetry with child lifecycle observations, compaction veto,
  Stop blocking and checkpoint freshness. Tests must check child compaction is never
  mistaken for a reason to restart the parent.
- `inbox.py` currently depends on checkpoint manifests and incorporated message IDs.
  Presentation state must be independent while honoring old acknowledgments.
- `project_install.py`, legacy router hooks and installed shims can retain hard gates
  separately from normal shared launches. The installation transition is part of the
  feature, not a README-only cleanup.
- Adapter limitations are explicit: current managed print-mode support is Claude-only;
  native workers are not automatically interchangeable with managed clients. The plan
  does not promise new provider capabilities.

## Remaining implementation proof obligations

The revised design resolves the review objections on paper. The following still need
code and evidence before claiming the simplified tool is ready:

1. Real subprocess tests for slot ownership, crash windows, shutdown and stale events.
2. Legacy-layout/instruction/message/config fixtures demonstrating data preservation
   and absence of hidden ceremony.
3. Provider-specific capability checks for parent identity, safe boundaries, worker
   survival and compaction fallback; offline fakes alone cannot prove live support.
4. An exact installed-consumer inventory and cutover/rollback exercise, including
   absolute paths captured by existing sessions.
5. A normal-use demonstration: start, delegate, save ordinary notes, roll repeatedly,
   finish—without any checkpoint, completion ticket, closure or retirement command.

The planner should not answer these gaps by adding new agent-facing administration.

## Re-review

The usability reviewer re-read the revised plan and found **no remaining design
blockers**: U1–U7 addressed, with no new agent-facing ceremony. They emphasized that
rollback recipes must be generated by the tool, not maintained manually by agents.

The runtime reviewer confirmed R1–R10 materially resolved, then identified one further
coexistence race: old launchers do not honor the new `.token-kit` locks, so scanning old
records before claiming a new slot is insufficient. Section 8 now specifies a shared
lifetime fence on the old `.continue.lock`, which audited old ordinary/continuation
launch paths require exclusively. New slots share the fence; old claimed runs are
checked under `.lock` before admission. Older unfenced supervisors require explicit
entry-point retirement/routing before task cutover. Added concurrent old/new launch
tests cover initial admission and rollover gaps. This is a process compatibility
bridge, not restored lifecycle bookkeeping. Its implementation remains to be tested.

A final runtime check accepted the healthy admission fence but found its crash gap:
supervisor death releases the shared lock while the child may survive. Section 8 now
also requires a narrow legacy-readable process claim published before spawn, retained
across rollovers and until verified client death. Frozen old admission must honor it;
an actual old-executable crash test is required. If compatibility cannot be proven,
defer that task's cutover. This final design addition has not been experimentally
validated and does not imply another worker lifecycle or manual reconciliation step.
