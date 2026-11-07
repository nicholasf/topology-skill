# topology-skill


This topology skills lets an agent 

* maintain a TOML file for each machine on your network
* record common phrases for performing tasks for later reuse and analysis

You can either configure your network topology using the tailscale or manual providers. 


| Command | What it does |
|---|---|
| `/topology` | Reads the topology, shows machines and models, lets you start or swap a running model |
| `/topology discover` | Probes every machine over SSH and HTTP for GPU/VRAM, GGUF inventory, running backends, and agent endpoints |
| `/topology sync` | Refreshes IPs and online status from the current provider (Tailscale or manual) |
| `/topology benchmark <hostname> <model>` | Measures TTFT and token throughput over three runs, writes results to `topology.toml` |
| `/topology show` | Prints the full combined topology plus every sidecar file |
| `/topology docs` | Writes a per-file markdown breakdown into `$TOPOLOGIES_HOME/README.md`, between generated markers |
| `/topology run "<phrase>"` | Resolves a trigger phrase to a playbook and runs it, pausing for confirmation on tasks marked for oversight |
| `/topology playbook list` | Lists every playbook: name, description, aliases, source file |
| `/topology init` | First-time setup: choose Tailscale or manual, write `topology.toml`, run sync |
| `/topology hooks install --target claude\|pi\|goose` | Installs hooks so agents use the topology without being asked (see below) |
| `/topology help` | Lists all subcommands with a one-line description |


Topology lets you use your agent to do things with machines on your network. It is built for home labs and CONTAINS NO SECURITY guardrails.

I wrote it for my home lab. 

A TOPOLOGIES_HOME dir is set, where your topology files (kept in TOML) are kept and stored.

Playbooks are TOML configs that match a specific node in the topology. Right now I map machines to playbooks, but there's no reason you couldn't match a higher level construct, like an agent or service, that might move across machines. 

## Further concepts

* Sidecar files — dependent skills store their own data in `topology-{skill-name}.toml`, alongside `topology.toml`
* Playbook composition — a playbook task can `ref` another playbook instead of duplicating tasks per node
* Variables — `${VAR}` placeholders in a task's command, filled with `--var KEY=VALUE`
* Secrets — kept in `$SKILLS_HOME/.env` (see [manage-skills-skill](https://github.com/nicholasf/manage-skills-skill)), never in a topology file
* Privacy — keep `$TOPOLOGIES_HOME` private if it's a git repo; it's a recon map of your network
* Future — flat TOML files are a stopgap; `schema_version` exists for an eventual SQLite migration

## Getting agents to use the topology without being asked

Agents only see a skill's name and description until they decide to load it, and they often don't, especially smaller local models. Your hostnames, models and playbook aliases live in `$TOPOLOGIES_HOME`, so nothing tells the model that "start qwen on pond" is a topology request.

The fix is a hook: the agent harness runs a small command that matches your topology data and adds a hint to the conversation. All matching lives in Python (`src/topology/hooks.py`). Each harness only has a thin adapter.

```bash
topology hook session-start          # summary: machines, running models, playbooks, CLI usage
echo "start qwen on pond" | topology hook prompt   # hint, or nothing if the prompt is unrelated
```

The prompt hook matches playbook aliases, machine names and model names as whole words. It reads local TOML only (no network or SSH) and takes about 40 ms. On any error it prints nothing and exits 0, so it never blocks a prompt. It prints nothing for unrelated prompts because everything a hook adds stays in the context window for the rest of the session.

Install, remove or check the adapters with:

```bash
topology hooks install   --target claude|pi|goose [--scope user|project]
topology hooks uninstall --target claude|pi|goose [--scope user|project]
topology hooks status
```

Or use the Makefile: `make install-claude`, `make install-pi`, `make install-goose`, `make install` (all three), the matching `uninstall-*` targets, and `make status`. Add `SCOPE=project` for project scope.

(`topology` here means `PYTHONPATH="${SKILLS_HOME:-$HOME/.agents/skills}/topology-skill/src" python3 -m topology.cli`, or the `topology` script if the package is installed.)

| Harness | Mechanism | Per-prompt hints | Installed to (user scope) |
|---|---|---|---|
| Claude Code | `SessionStart` and `UserPromptSubmit` hooks in `settings.json`, returning `additionalContext` | Yes | `~/.claude/settings.json` |
| pi | TypeScript extension handling `session_start` and `before_agent_start` | Yes | `~/.pi/agent/extensions/topology.ts` (symlink) |
| goose | A generated block in `.goosehints`, which goose loads into every session | No | `~/.config/goose/.goosehints` |

### Claude Code

`install` adds two entries to `settings.json` and leaves your other hooks alone. Entries are recognised by the `topology.cli hook` command string, so installing twice is harmless and uninstall removes only these. The previous file is saved as `settings.json.topology-backup`. The file is rewritten with 2-space JSON indentation.

- `SessionStart` adds the summary once per session.
- `UserPromptSubmit` runs on every prompt and adds a hint only when something matches. It cannot change what you typed; it adds context next to it.

### pi

`install` symlinks `hooks/pi/topology.ts` into pi's extensions directory, so pulling this repo updates it. Run `/reload` in an open pi session. The extension fetches the summary on `session_start`, adds it with the first prompt, and adds a hint to any matching prompt.

Context is returned as a `message`, which is added at the end of the conversation, rather than by editing the system prompt. Editing the system prompt would change the start of every request and invalidate the prompt (KV) cache. On a local llama.cpp or Ollama server that means re-processing the whole conversation.

### goose

goose 1.1.1 has no lifecycle hooks or skills, so there is no per-prompt matching. `install` writes the session-start summary into `.goosehints` between `<!-- topology:begin -->` and `<!-- topology:end -->` markers, leaving the rest of the file alone. It's a snapshot: re-run `topology hooks install --target goose` after `sync`, `discover` or playbook changes. The block also tells goose to run `topology hook session-start` for current state, and gives the CLI commands directly, since goose can't load the skill.

### Testing the hooks against the real harnesses

Unit tests (`tests/test_hooks*.py`) cover the matching and the installers, but not whether each harness actually runs the hook and sends its output to the model. For that, `tests/harness/run_harness.py` runs the real `claude`, `pi` and `goose` binaries against `tests/harness/stub_model.py`, a fake model server that speaks the Anthropic Messages API and OpenAI Chat Completions, returns a fixed reply, and records every request body.

```bash
python3 tests/harness/run_harness.py [claude] [pi] [goose] [--keep]
```

Each harness gets a throwaway world (fixture topology, config dir pointing at the stub, hooks installed with the same installer code), so your real config isn't touched and no real model is called. For a matching prompt and an unrelated one, it checks that the summary and hint are in the request (or absent), and that the hint isn't in the system prompt. `--keep` keeps the recorded requests. It needs the harness binaries installed, so it isn't part of the pytest suite or CI.

What it found (Claude Code 2.1.319, pi 1.3.0, goose 1.1.1):

| Harness | Summary lands in | Per-prompt hint lands in |
|---|---|---|
| Claude Code | A `role: "system"` message appended after the prompt | Same message |
| pi | A `user` message after the prompt (the extension's custom message) | Same message |
| goose | The system prompt (`.goosehints`) | n/a |

All three keep the start of the conversation stable, so the prompt cache is preserved. goose's summary is in the system prompt, but it only changes when you re-run the install.
