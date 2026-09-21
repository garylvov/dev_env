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

It is one command and it is safe to run twice: a second run changes nothing and says so. It picks a
profile from `vibe_coding/token_kit/profiles/` by looking at the machine (Slurm plus `/oscar` means the
cluster, anything else means a workstation); pass `--profile <name>` to choose. Add `--dry-run` to see
exactly what it would do without it doing anything.

What it installs:

- the settings keys the measurements showed actually take effect, merged into `~/.claude/settings.json`
  (a dated backup is taken first, your existing keys keep their values and their order, and a key of
  yours that disagrees is reported, never overwritten);
- a `PreToolUse` hook, registered once, that carries the per-agent call cap and the routing table;
- the agents from `vibe_coding/token_kit/agents/` symlinked into `~/.claude/agents/` (a real file of
  yours with the same name is reported as a conflict and left alone);
- `agent_trigger_matrix.toml`, the table that decides which agent and model a spawn gets.

Everything is a symlink back into the clone, so `git pull` updates the machine. Nothing is written
outside `~/.claude`, `~/.config/token_kit`, and the clone itself.

It needs `uv` (it installs it if missing) and the `claude` CLI; `codex` is optional. The kit is Python
on top of a small bash bootstrap, standard library only, so there is no environment to build.

To remove it:

```
bash vibe_coding/token_kit/install.sh --uninstall
```

That reads the manifest of what was added and removes exactly that.

# Other People's Setups that I borrow:
