# Vibe Coding


Here, are some agents and configs that help me vibe code.
At Brown, I highly encourage my students and collaborators to vibe code; I think as long as the output is verified it's an incredibly powerful tool.
I currently primarily use Claude Code.
In my experience, *so far*, it's the best. 
I've also tried Codex, Cursor, Gemini CLI, and some local coding agents.
Most of my students at Brown use Codex as I think the $20 a month may get more tokens/milage (I'm not entirely sure about this tbh), and they already have ChatGPT Pro subscriptions. However, I still (at the time of writing) recommend Claude. 


# My Prompting Strategy
I download my agent prompts into ``~/.claude/agents``. I like to give my agents memorable names, I think it makes coding feel like playing pokemon. 
I also like to enable ``CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS``.
I demonstrate some of my older workflows in [this presentation](https://www.youtube.com/watch?v=dVa7uNDu1ig&t=1285s).


My typical workflow is typically along the following lines.

0.) I carefully spec the functionality that I want. Then, I specify the following agent pattern in plaintext.

1.) I have the Henry Hudson Codebase explorer agent explore the current state of the codebase, and I recommend what areas to look at closer. Critically, Henry shouldn't look in big folders like ``.pixi``, and should use timeouts when using the find command. Henry ideally shouldn't get as lost as the default explore command 

2.) If implementing something that uses an external API, I often have an agent look at the documentation pages (which I feed as a raw URL).

3.) I then have the solutions architect carefully design a solution based on my spec, which it writes to a ``<PLAN_NAME>.MD`` file.

4.) I have some combination of the bloat killer, the tightly coupled hunter, and the grizzly review the plan file.

5.) I review the plan file itself. Oftentimes, I iterate with the solutions architect several times. It's best to catch mistakes in this stage.

6.) I have the solutions architect revise the plan file based on their feedback

7.) I have the solutions architect identify what areas, if any, can be implemented in parallel, and update the plan file. 

8.) I have as swe-worker-bees work on implementation (often, just one, unless there are parts that can be parallelized). I have the worker bees write their progress and relevant things to the plan file.

9.) Once they finish, I have henry hudson recap the codebase.

10.) I have the grizzly review the changes for errors or missed parts

11.) I review and test the functionality. If stuff doesn't work, I ask the grizzly for help lol.

If returning to a work session, that wasn't completed or was interrupted, I have henry hudson evaluate the progress based on the plan file and codebase state, then I continue with the plan file. The grizzly is my favorite debugger (other than myself ;) ).









# The token kit (one-command setup)

If you want the same hooks, agents and settings on another machine, run this in a clone of this repo:

```
bash vibe_coding/token_kit/install.sh
```

It is one command and it is safe to run twice: a second run changes nothing and says so. Each profile
in `vibe_coding/token_kit/profiles/` says what machine it is for, and the installer picks the one whose
evidence this machine matches; pass `--profile <name>` to choose yourself. Add `--dry-run` to see
exactly what it would do without it doing anything.

What it installs:

- the settings keys the measurements showed actually take effect, merged into `~/.claude/settings.json`
  (a dated backup is taken first, your existing keys keep their values and their order, and a key of
  yours that disagrees is reported, never overwritten);
- four hook registrations, each exactly once: a `PreToolUse` hook carrying the per-agent call cap and
  the routing table, a `SessionStart` hook that prints the row menu, and a `PostToolUse` (on a spawn
  returning) plus a `Stop` hook sharing one reader, which is what notices that a background lane hit
  its cap and needs a fresh agent on the same brief;
- four commands in `~/.config/token_kit/bin/`: `respawn-reader` and `token-kit-supervise`, plus
  `codex-dispatch` and `codex-job`;
- the agents from `vibe_coding/token_kit/agents/` symlinked into `~/.claude/agents/` (a real file of
  yours with the same name is reported as a conflict and left alone — see `--adopt-personas` below);
- one generated agent file per row of the table and per model tier it names, so a spawn on one of
  those carries that row's model, effort, budget and stop condition without anything rewriting it;
- `agent_trigger_matrix.toml`, the table that decides which agent and model a spawn gets.

Everything is a symlink back into the clone, so `git pull` updates the machine. Nothing is written
outside `~/.claude`, `~/.config/token_kit`, and the clone itself.

It needs `uv` (it installs it if missing) and the `claude` CLI; `codex` is optional. The kit is Python
on top of a small bash bootstrap, standard library only, so there is no environment to build.

## Running codex from a session

`codex-dispatch` runs one codex turn and prints the answer — use it for a question you will read
right away. `codex-job` is the same turn in the background, and you can talk to it while it runs:

```
codex-job start --model <model> --effort <low|medium|high> --cwd <dir> --task-file <file> --name <short>
codex-job send  <job> "<text>"      # joins the turn already in flight, or starts the next one
codex-job wait  <job>               # run this one as a BACKGROUND command; it tells you when it ends
codex-job status <job>              # also: list, stop
```

Exit code 42 from either means codex is unavailable here (absent, not signed in, busy, or out of
quota) and the work should be done with a Claude model instead. The four commands are documented in
full in `vibe_coding/token_kit/src/token_kit/codex/USAGE.md`.

## Supervising a long session

`token-kit-supervise launch` starts a detached session on the campaign directory the profile names,
and watches how much context it has used. When it gets close to the ceiling it asks the session to
bring its handoff document current, waits for it to go quiet, then stops it and starts a fresh one on
that document — with no keystroke from you. `token-kit-supervise status` says what it is doing;
`token-kit-supervise once --session-pid <pid>` does exactly one pass and exits, which is how you step
it by hand.

```
~/.config/token_kit/bin/token-kit-supervise launch -- --model <model> --dangerously-skip-permissions
```

## If you already have agent files of your own

By default the installer refuses to touch a real file: if `~/.claude/agents/architect.md` is yours, it
says so and leaves it alone, and the kit's version is simply not installed. Pass `--adopt-personas`
and it moves each one into `~/.claude/agents/.pre_token_kit/` first and links the kit's version in its
place. Nothing is ever deleted, and `--uninstall` puts your files back where they were.

## On a new machine

Copy `profiles/workstation.toml` to `profiles/<yourmachine>.toml` and edit it: it holds every fact
that is about your machine rather than about the kit — where your data and campaign directories are,
what launches codex, whether Slurm is there, which settings keys to merge, and the evidence that
identifies the machine so `install.sh` can pick the profile without being told. No path, username or
hostname belongs anywhere else; `bash install.sh --census` fails if one appears in the code.

To remove it:

```
bash vibe_coding/token_kit/install.sh --uninstall
```

That reads the manifest of what was added and removes exactly that: every symlink, every generated
file, all four hook registrations, and any agent file of yours that `--adopt-personas` moved aside.

# Other People's Setups that I borrow:
