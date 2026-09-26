# Simplified Token Kit release: 2026-09-26

The folder-first implementation is active for new `token-kit` launches.

- Source/worktree: `/oscar/data/stellex/glvov/dev_env-token-kit-simple`.
- Branch: `token-kit-simplify`; implementation and activation tooling through `a72b722`.
- Original runtime retained unchanged at `/oscar/data/stellex/glvov/dev_env`.
- User command: `/users/glvov/.local/bin/token-kit` points to a small release dispatcher.
- Shell startup has a scoped PATH block for that dispatcher. Existing managed legacy
  sessions retain their old command path; the dispatcher also recognizes legacy
  `TOKEN_KIT_RUN` environments and routes them to the original runtime.
- Rollback: `python3.11 /users/glvov/.local/share/token-kit/activation-20260926-163133/rollback.py`.
  This restores the exact earlier command link and shell configuration, and refuses
  to overwrite changes made after activation. It does not delete task data or stop
  sessions. Stop candidate sessions normally before reverting their command selection.

## Daily behavior

Use `token-kit`, `token-kit run "Title"`, or `token-kit continue QUERY_OR_PATH`.
Title-only starts remain idle; `--prompt` begins work. Existing folders are read in
place. STATE and results are ordinary editable files. No worker registration,
completion tickets, fixed STATE headings, checkpoint command, closure or retirement
is required. Task labels do not control process or artifact completion.

Rollover defaults to 80% when context is measurable, with unlimited successful
rollovers. `--no-rollover` disables automatic restarts. Permission bypass is never
enabled automatically. Errors and manual interrupts are not replayed. Saved worker
settings and workspaces are separate from coordinator settings.

An automatically generated native-client recovery recipe is stored under each
managed slot's `.token-kit/slots/AGENT/native-recovery.json`. It references current
task files and does not depend on this launcher for native continuation. A bounded
last-saved STATE fallback survives missing working notes without imposing a checkpoint
protocol. Process records and snapshots never certify external operations or results.

Startup and recovery prompts explicitly carry the user's bounded-parallelism
preference, overriding only the blanket concurrency/global-lock restrictions in
`/oscar/AGENTS.md`. Scoped ownership, compute-allocation requirements, job-submission
approval and remaining site rules still apply. The optional installation guidance
uses the same wording. After this addition, all 39 affected tests passed and the
active release's startup, recovery and installation text were checked directly.

## Validation and limits

115 selected Python 3.11 tests passed together in the isolated `/tmp` checkout.
These cover the new CLI/files/settings/guidance/adapters/runner, meaningful retained
context/continuation/process tests, shell setup, and reversible release activation.
The end-to-end fake client exercised actual adapter argv, hook subprocesses, two
rollovers from ordinary notes, and normal exit. Independent code reviews found and
corrected path escapes, customized-policy stripping, settings leakage, failed-start
poisoning, stale readiness, cancellation races and partial-activation rollback.

After adding the final saved-state fallback, 43 affected tests passed; the final
115-test combined run includes that change and all release-switch tests. This is a
selected relevant suite, not a claim that every historical test for the removed
public protocol was run. `git diff --check` passed. Installed new help and legacy
environment dispatch were checked without launching models.

No paid live-client rollout, HPC job, real task continuation, or provider hook-setting
change was performed. Live provider rollover remains unverified. Unsupported or
untrusted hooks, missing telemetry, unknown parent identity, or conflicting existing
Codex hook arrays produce a visible native fallback. Nonoverlapping policy hooks are
preserved without disabling managed rollover. A live session with no safe-boundary
hooks must be stopped normally before fresh continuation; it is never killed on an
invented boundary. Native workers can use native compaction and need no registration.

Historical runtime/install modules remain for pinned compatibility and exact-owned
configuration cleanup, not as dependencies of the new worker lifecycle. Do not replace
the original checkout or delete captured hook paths while old sessions reference them.
The reviewed design and its red-team record are next to this document.
