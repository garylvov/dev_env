# Trigger pyramid

Defaults for task sessions:

| Tier | Preference |
| --- | --- |
| Smartest | `claude:opus:high > claude:fable:medium` |
| Smart | `codex:gpt-6-astra:medium > claude:opus:medium` |
| Medium | `codex:gpt-5.6-sol:high` |
| Mid | `codex:gpt-5.6-luna:high > claude:sonnet:medium > codex:gpt-5.6-terra:medium` |

Choose the lowest capable tier. Mid covers bounded work; Medium covers harder scoped
work and substantive docs; Smart covers complex planning, debugging, and review;
Smartest is a sparing escalation for difficult or consequential decisions. Scouts
and mechanical edits use Luna xhigh unless exact user/session effort overrides it.
Use Sol xhigh only when reasoning is justified.

Copy this file to `TASK/trigger_pyramid.md` for each new task. Its snapshot governs
workers, rollovers, and resumes; repository changes do not rewrite it. Session-wide
overrides replace only named rows. Provider restrictions filter rows; ask if one
becomes empty. Explicit worker assignments win. No automatic natural-language
parsing or provider fallback is promised.
