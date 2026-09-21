# Agent trigger matrix (fixture)

A stand-in for the shipped guide, small enough that a case can rewrite one line of it. The prose
around the tables is deliberately chatty, and it carries a `|` pipe and the word kind so that a
parser which located its tables by scanning for pipes would pick this paragraph up instead.

| kind | use when | who does it | prefer | done when |
| --- | --- | --- | --- | --- |
| lookup | find a fact, or show it is absent | codex does it all | `codex:gpt-5.6-luna:high` > `claude:sonnet:low` | the fact is quoted with its source |
| implement | write code to a spec with its guard test | Claude plans, codex executes | `claude:sonnet:high` > `claude:opus:high` | the change and its failing-first guard are both written |
| design | compare approaches before any code | Claude does it all | `claude:opus:high` > `claude:fable:high` | one approach is recommended with its cost |
| write-doc | write or update a document | Claude does it all | `claude:sonnet:medium` | the document is written and its path named |

**Never spawn a model to wait.** A model may read a status file; a model may not be the loop.

## Call budget

| threshold | calls | what happens |
| --- | --- | --- |
| warn | 150 | exactly one call is denied, and the refusal tells the agent its count |
| floor | 230 | only a write of the agent's own `out.md` and the handback are still permitted |
| hard | 250 | the refusal escalates; the `out.md` write is still never denied |

## Examples

Prose down here must never reach the parsed rows, code fences and pipe tables included:

```
codex-dispatch --model gpt-5.6-luna --effort high --cwd <dir> --task-file <step.md> --out <answer.md>
```

| kind | use when | who does it | prefer | done when |
| --- | --- | --- | --- | --- |
| not-a-real-kind | this table is an EXAMPLE of the chart, not the chart | Claude does it all | `claude:fable:high` | never, because it is never parsed |
