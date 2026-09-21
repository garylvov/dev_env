# codex jobs (paste-in surface: four commands)

A codex job is a background turn you can talk to. `codex-job` = `<kit>/src/token_kit/codex/bin/codex-job`.

    codex-job start --model gpt-5.6-luna --effort high --cwd <abs dir> --task-file <abs in.md> --name <short>
    codex-job send  <job> "<text>"
    codex-job wait  <job>            # run this one as a BACKGROUND command
    codex-job status <job> | codex-job list | codex-job stop <job>

- **start** prints a job id and returns in ~1 s; the turn runs in a detached owner process.
- **send** while the turn is RUNNING = a real mid-turn steer: the text joins the turn already in
  flight (proven on codex-cli 0.153.4). After it finishes, the same command continues the SAME
  thread as a new turn — within `--linger-s` (default 120 s) in the same owner, later by resuming
  the thread id. Every message gets a row: steered | queued-next-turn | resumed | failed(<why>).
  No message is ever dropped: it stays in the job's `inbox/` until its row is written.
- **wait** blocks, then prints the last answer and the delivery rows. Run it in the BACKGROUND:
  the harness re-invokes you when it exits, so the completion notice costs no polling call.
- **status/list** are cheap and read files only (never a codex turn). **stop** interrupts the turn.

Use a job instead of one-shot `codex-dispatch` when you expect to redirect the work or want to be
told it finished; use `codex-dispatch` for one question you will read right away. Exit codes: 0 answered · 2 usage · 3 a turn failed · **42 codex unavailable
(reason=absent|auth|busy|protocol|quota) — do the step yourself with a Claude model.**
`reason=quota` writes a cooldown marker and the next `start` refuses until it expires
(`--ignore-cooldown` overrides). Model and effort are REQUIRED and have no defaults.

**Many nodes, one job dir.** The job dir is on the shared filesystem, so `send/status/stop/wait`
run anywhere, but the owner process and the codex thread live on ONE node (`host=` in `list`).
Cross-host: `send` to a live owner writes the shared inbox and reports `queued-remote(<host>)`;
`send` after that owner has exited REFUSES loudly (the thread is on the other node) and keeps your
message in the job's `undelivered/`; `stop` writes a stop request the owner honours, never a kill.
**`codex-run`** is the interactive entry point (`codex-run`, `codex-run --reseed`, `codex-run
update`): it is the kit's own launcher, so CODEX_HOME lands on node-local disk wherever a profile
sets `node_local_home = true` — no dependency on any hand-written wrapper in a home directory.
