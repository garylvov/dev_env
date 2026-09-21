# DEPRECATION — proposed, never performed

Nothing in this file has been run. No file in `/users/glvov/.claude/agents/`
has been moved, renamed, overwritten or deleted by this lane. Isolation is the
cage, not deletion: every command below is a `mv`, and every one has its undo
on the next line.

## What is ours to retire, and what is not

A session rooted at `/oscar/data/stellex/glvov` lists 11 selectable agent
types. Only **4** are ours:

| type | ours? | why |
|---|---|---|
| `architect` | YES | `~/.claude/agents/architect.md`, 6,802 B |
| `bloat-killer` | YES | `~/.claude/agents/bloat-killer.md`, 8,490 B |
| `grizzly-veteran` | YES | `~/.claude/agents/grizzly-veteran.md`, 6,706 B |
| `job-runner` | YES | `~/.claude/agents/job-runner.md`, 11,817 B |
| `general-purpose` | no | harness built-in |
| `Explore` | no | harness built-in |
| `Plan` | no | harness built-in |
| `claude` | no | harness built-in |
| `statusline-setup` | no | harness built-in |
| `claude-code-guide` | no | harness built-in |
| `codex:codex-rescue` | no | `codex@openai-codex` plugin, enabled in settings.json |

Two more exist on disk but are **not selectable from the work home**:
`gpu-probe` and `terse-auditor`, present only as PROJECT agents in
`agrescap/canonical/.claude/agents/` and `wbc/wbc-latest/.claude/agents/`
(2,147 B and 1,272 B, byte-identical in both trees). They are not ours to
deprecate because they are already inert here — and they must not be installed
as-is either: both declare `model: haiku` in frontmatter, which ruling R1
forbids.

## Recipe A — COMPRESS (this is what the operator asked for)

Isolate the four originals and install the compressed bodies in one step, so
the matrix rows that name a persona never point at nothing:

```bash
D=/users/glvov/.claude/agents; S=/oscar/data/stellex/glvov/agrescap/canonical/tools/matrix_final/agents; mkdir -p $D/deprecated && for b in architect bloat-killer grizzly-veteran job-runner; do mv "$D/$b.md" "$D/deprecated/$b.md" && cp "$S/$b.md" "$D/$b.md"; done && ls -l $D $D/deprecated
```

Undo:

```bash
D=/users/glvov/.claude/agents; for b in architect bloat-killer grizzly-veteran job-runner; do rm -f "$D/$b.md" && mv "$D/deprecated/$b.md" "$D/$b.md"; done && rmdir $D/deprecated 2>/dev/null; ls -l $D
```

The undo is lossless: the originals are only ever moved, and the thing `rm`'d
is the copy this repo still holds.

## Recipe B — RETIRE ENTIRELY (only if the operator wants no personas at all)

Isolate without installing anything. Requires re-pointing the four rows
(`design`, `stuck`, `harness-broker`, `batch-sweep`) and the
`training-code-change` reviewer to `general-purpose` first, or the guard fails
A2/A3 — which is the point of the guard.

```bash
D=/users/glvov/.claude/agents; mkdir -p $D/deprecated && mv $D/architect.md $D/bloat-killer.md $D/grizzly-veteran.md $D/job-runner.md $D/deprecated/ && ls -l $D $D/deprecated
```

Undo:

```bash
D=/users/glvov/.claude/agents; mv $D/deprecated/architect.md $D/deprecated/bloat-killer.md $D/deprecated/grizzly-veteran.md $D/deprecated/job-runner.md $D/ && rmdir $D/deprecated; ls -l $D
```

## What breaks the moment they move — and one unknown

Immediately, under Recipe B: any `Agent` call naming one of the four fails to
resolve, and four matrix rows plus one reviewer column go dangling. Under
Recipe A nothing dangles, because the name still resolves — to a body 71%
smaller.

**The unknown, stated plainly: it is NOT established that
`~/.claude/agents/deprecated/` is actually isolation.** If the harness scans
that directory recursively, the four personas stay selectable and the move
achieves nothing but a longer path. This lane did not test it, because testing
it means moving a file, which this lane is forbidden to do. The one-command
test, for whoever runs the recipe:

```bash
# after the move, in a NEW session rooted at the work home:
bash /oscar/data/stellex/glvov/agrescap/canonical/tools/matrix_final/guard_matrix.sh
```

Under Recipe B the guard MUST report A2/A3 failures naming the four. If it
passes, the subdirectory is still being scanned and the isolation is fake —
move them to `/users/glvov/.claude/agents-deprecated/` (a sibling, not a
child) instead, with the same `mv`/undo shape.
