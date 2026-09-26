# Simplified release cutover

Build and test the candidate in a separate release path. Do not replace the original
checkout while existing launchers or installed hooks reference it. The source switch
and installed-policy cleanup are separate operations.

1. Preview the exact command/PATH switch, explicitly selected project guidance and
   client configuration paths. Inventory old hooks at those paths. Do not scan home
   directories or delete settings based on a substring match.
2. Keep old sessions on their pinned source and CLI. If inherited old global hooks
   cannot be selectively excluded safely, defer the affected new launch until old
   sessions exit and cleanup is possible. Never bypass client trust or site hooks.
3. Preview optional guidance cleanup with `simple_install.preview`. It replaces only
   complete recognized generated blocks, validates existing ownership hashes, and
   removes only exact generated hook objects recorded by the old TSV manifest.
   Customized blocks require an explicit manual decision; plain launch needs no
   new instruction installation. Unrelated MCP and permission settings stay intact.
4. Once affected old sessions have exited, apply the reviewed preview with
   `simple_install.apply`, using a new backup directory. It backs up all originals
   before editing and writes `rollback.sh`. A changed preview is rejected. Config
   writers must be serialized by the caller while applying.
5. Verify effective hooks and launch settings before describing a launch as simplified.
   Test a fake-client rollover, ordinary stop, and existing-task continuation. Use
   separately authorized live smoke checks only when needed.

The deployment tool must emit the concrete release-switch reversal and per-task
recovery commands in addition to the configuration `rollback.sh`. A rollout is not
complete while those recipes remain prose placeholders. The integrator owns the
release switch and its preview; `simple_install` only edits explicitly named configs.

For rollback, stop candidate sessions first, inspect changes since installation, then
run the generated config recipe and restore the previous command selection. A recipe
restores the exact backed-up bytes; it can overwrite later user edits, so inspect them
before running it. Old-layout tasks can use the pinned old client after process
ownership is clear. New-layout tasks must instead receive a tool-generated direct
native-client command pointing at their workspace and saved notes. Do not promise
old automatic rollover for a layout the old launcher does not understand. Preserve
all task files in either case.

Old hook targets and installation helpers can be removed only after no installed or
running consumer references them. Reading historical task files remains supported.
