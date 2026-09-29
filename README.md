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
