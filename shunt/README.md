# shunt: GPT-6 Astra / Sol / Luna inside Claude Code

[pleaseai/shunt](https://github.com/pleaseai/shunt) is a local gateway on `127.0.0.1:3001`. Claude models pass through to Anthropic with your normal login; the effort-suffixed ids `gpt-6-<astra|sol|luna>-<low|medium|high|xhigh>` go to the Codex provider using your existing `codex login` (ChatGPT) credential. No tokens live in this folder.

## Contents
- `shunt.toml`: upstreams (anthropic passthrough, codex) and the 12 effort-suffixed model ids, each pinning its own effort. No silent fallback to another GPT model.
- `shunt.service`: systemd user unit.
- `agents/{astra-gpt-6-astra,sol-gpt-6-sol,luna-gpt-6-luna}.md`: native Claude Code subagents on the unpinned `gpt-6-<model>` ids with `effort: high` in frontmatter (Shunt honors the client effort on unpinned ids; the `-low/-medium/-high/-xhigh` ids pin it and are for the picker). Delegation and naming rules live in `token_kit/AGENTS.md`.
- `install.sh`: checksum-verified install of the pinned release (default `v0.55.1`), config, service and agents. Backs up anything it overwrites.

## Claude Code settings (merge into `~/.claude/settings.json`)
```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:3001",
    "ANTHROPIC_CUSTOM_MODEL_OPTION": "gpt-6-astra-high",
    "ANTHROPIC_CUSTOM_MODEL_OPTION_NAME": "GPT-6 Astra (high)",
    "ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION": "Astra pinned to high effort via local Shunt and Codex"
  },
  "modelPicker": {
    "options": [
      { "model": "gpt-6-astra-xhigh", "label": "GPT-6 Astra (xhigh)" },
      { "model": "gpt-6-sol-high", "label": "GPT-6 Sol (high)" },
      { "model": "gpt-6-luna-high", "label": "GPT-6 Luna (high)" }
    ]
  }
}
```
Only one `ANTHROPIC_CUSTOM_MODEL_OPTION` slot exists, hence `modelPicker` for the rest. Start a new `claude` session afterwards.

## Notes
- Every GPT model id carries its effort: `gpt-6-<astra|sol|luna>-<low|medium|high|xhigh>`. They are `[[routes]]` that pin effort and forward to the real upstream model. There are deliberately no bare `gpt-6-astra`/`-sol`/`-luna` ids, so effort is never left to the slider. The three agents use the `-high` ids.
- Effort is not echoed in gateway headers; routing and upstream model are. Reasoning output tokens differ between low and xhigh, which is consistent with it working.
- Claude Code assumes a 200k window for non-`claude-` ids. `CLAUDE_CODE_MAX_CONTEXT_TOKENS` is global, so set it only once the real window is known.
- Launch the agents without a `model` override.

## Rollback
```sh
systemctl --user disable --now shunt.service
rm ~/.config/systemd/user/shunt.service ~/.local/bin/shunt ~/.claude/agents/{astra,sol,luna}-gpt-6-*.md
rm -r ~/.config/shunt
```
Then remove the keys above from `settings.json`.
