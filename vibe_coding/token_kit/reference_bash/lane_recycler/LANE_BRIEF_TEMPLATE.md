# LANE_BRIEF_TEMPLATE.md — the in.md a recyclable lane gets, and the out.md it owes

The recycler needs exactly two things from the spawn prompt. Everything else is
the lane's own business.

## 1. The spawn prompt MUST carry one line, on its own line

    LANE_DIR: /absolute/path/to/the/lane/dir

`lane_recycler.sh` reads it out of the first record of the agent's own
transcript, so nothing has to be registered anywhere. Without it the hook falls
back to the first `/…/in.md` path in the prompt; with neither, the lane still
gets its WARN and its FLOOR but the only thing permitted past the floor is
`SubagentHandback` — its out.md write would be denied. Do not omit the line.

## 2. The brief's rules block MUST contain these four lines verbatim

    - Your state lives in out.md, not in this conversation. A fresh agent with
      this in.md and your out.md must be able to continue without asking anyone.
    - A hook counts your tool calls. At ~150 it denies ONE call to tell you the
      count; at 230 it denies everything except a Write to THIS lane's out.md
      and SubagentHandback. bash heredocs to out.md are denied there — use Write.
    - When you get the WARN: estimate the calls your remaining work needs. If it
      does not fit, write out.md and hand back asking for a fresh lane.
    - Write out.md in the contract below. A successor that needs more than ~10
      calls to orient has destroyed the point of recycling.

## 3. The out.md contract (what makes a restart cheap)

Break-even turns entirely on rework: a 150-call agent costs about the same as
two 80-call segments, so a restart that burns 30 calls re-orienting loses
outright. The successor's budget is <10 calls: one Read of in.md, one Read of
out.md, and at most a handful of verifications. That is only possible if out.md
answers, without a single exploratory call:

Line 1  `RESULT: <key>=<value> …` — the verdict, first, before any prose.
Line 2  every absolute path this lane has written or will write.
Then, in this order:
- `## RESUME` — its FIRST line is the single next command, copy-pasteable,
  absolute paths, no placeholders. Then the files already written with one line
  each saying what state they are in (done / half-written / stub).
- `## RULED OUT` — every dead end with the command that showed it dead. This is
  the part that stops the successor repeating the predecessor's spend.
- `## UNPROVEN` — every claim not yet backed by a command that was run.
- Last line: `### SESSION <n> status=CONTINUE|DONE next=<one line>`
  followed by at most 20 progress lines.

Hard limit 60 lines. If out.md exists, READ it first and APPEND a new
`### SESSION <n+1>` block — never rewrite the file (one writer, `cat >>` or a
Write that reproduces the whole prior text verbatim plus the new block).

## 4. The successor's spawn prompt

Identical to the predecessor's, plus one line:

    Your lane already has an out.md. Read in.md then out.md, start from the
    first line of its RESUME block, and append SESSION <n+1> to it.

## 5. What does NOT happen by itself (v0)

Nothing respawns a lane on its own. The hook writes a row into
`<LANE_DIR>/RESPAWN_REQUEST.md` and the dying lane's handback says it was
recycled, but a parent (or a human) must issue the replacement spawn. A lane
whose parent has exited is not recycled; it is simply stopped, with its state
saved. Naming that limit is the point: v0 is parent-dependent by design.
