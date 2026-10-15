# shunt: GPT-6 Astra / Sol / Luna inside Claude Code

[pleaseai/shunt](https://github.com/pleaseai/shunt) is a local gateway on `127.0.0.1:3001`. Claude models pass through to Anthropic with your normal login; `gpt-6-astra`, `gpt-6-sol` and `gpt-6-luna` go to the Codex provider using your existing `codex login` (ChatGPT) credential. No tokens live in this folder.

## Contents
- `shunt.toml`: upstreams (anthropic passthrough, codex with effort pinned to high) and the three model ids. No silent fallback.
- `shunt.service`: systemd user unit.
- `agents/{astra,sol,luna}.md`: native Claude Code subagents, each pinned to its model.
- `install.sh`: checksum-verified install of the pinned release (default `v0.55.1`), config, service and agents. Backs up anything it overwrites.

## Claude Code settings (merge into `~/.claude/settings.json`)
```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:3001",
    "ANTHROPIC_CUSTOM_MODEL_OPTION": "gpt-6-astra",
    "ANTHROPIC_CUSTOM_MODEL_OPTION_NAME": "GPT-6 Astra",
    "ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION": "Astra via local Shunt and Codex"
  },
  "modelPicker": {
    "options": [
      { "model": "gpt-6-sol", "label": "GPT-6 Sol" },
      { "model": "gpt-6-luna", "label": "GPT-6 Luna" }
    ]
  }
}
```
Only one `ANTHROPIC_CUSTOM_MODEL_OPTION` slot exists, hence `modelPicker` for the rest. Start a new `claude` session afterwards.

## Notes
- `effort = "high"` in `shunt.toml` overrides the `/effort` slider for Codex models. Delete that line to let the slider control it.
- Claude Code assumes a 200k window for non-`claude-` ids. `CLAUDE_CODE_MAX_CONTEXT_TOKENS` is global, so set it only once the real window is known.
- Launch the agents without a `model` override.

## Rollback
```sh
systemctl --user disable --now shunt.service
rm ~/.config/systemd/user/shunt.service ~/.local/bin/shunt ~/.claude/agents/{astra,sol,luna}.md
rm -r ~/.config/shunt
```
Then remove the keys above from `settings.json`.
