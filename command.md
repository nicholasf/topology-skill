# Topology

Reads `$TOPOLOGIES_HOME/topology.toml` (default `~/.agents/skills/topology.toml` if unset) to enumerate available machines and models, then helps the user start a chosen model or refresh the topology.

## Step 1 — Load the skills env

Before reading the topology, check for `$SKILLS_HOME/.env` and load it if present:

```bash
[ -f "${SKILLS_HOME:-$HOME/.agents/skills}/.env" ] && source "${SKILLS_HOME:-$HOME/.agents/skills}/.env"
```

This file holds secrets and per-node configuration (API keys, URLs) used by skills that depend on this one. It is gitignored and machine-local. See `.env.example` in this repo for the expected format.

## Step 2 — Read the topology file

Resolve the path: `$TOPOLOGIES_HOME/topology.toml` → `~/.agents/skills/topology.toml`.

If the file does not exist, run the init script to guide first-time setup:

```bash
PYTHONPATH="${SKILLS_HOME:-$HOME/.agents/skills}/topology-skill/src" python3 -m topology.cli init
```

The script asks the user to choose a provider (Tailscale or manual), collects any required input, writes `topology.toml`, and runs sync automatically. Once it exits successfully, continue to Step 3 with the freshly written file.

Do not proceed with cached or assumed knowledge if init fails.

Read the full file.

## Step 3 — Present the machines table

Display the machines table clearly. Note which machines have `ssh: yes` — these are the ones agents can act on remotely. Note the `last_refreshed` date if present.

## Step 4 — Present available LLMs and live state

If the topology file contains `llms` and `backends` arrays, use them as the primary source —
they reflect the last discover run and show what is actually installed and running per node.
Each `llms` row names the `hostname` and `backend` serving it; the matching `backends` row
(same `hostname`, `name` equal to the LLM's `backend`) gives its status, port and context window.
A backend with no LLMs (down, or serving nothing) still has a `backends` row.
Present the LLMs as a numbered list:

```
Available LLMs:

  1. qwen3-coder-30b.gguf  on pond    (llama-server / CUDA, port 9337, ctx 65536) — up
  2. qwen3-coder:30b        on gollum  (Ollama / ROCm,       port 11434, ctx 131072) — up
  3. qwen2.5-coder:14b      on gollum  (Ollama / ROCm,       port 11434, ctx 32768) — up
```

If the file has a `model_state` array instead (schema_version 1), it is from before this
format; suggest the user run `/topology discover`, which upgrades it. If there are no LLM
arrays at all, fall back to any narrative notes in the file, or suggest `/topology discover`.

## Step 4b — Present agent state

If the topology file contains an `agents` array, present it alongside the LLMs:

```
Agents:

  pond    hermes  http://pond:8642  — up   (process: running)
  pond    goose   ws://pond:3284    — down (process: not found)
```

Also check for any `topology-*.toml` sidecar files in `$TOPOLOGIES_HOME` — these are written by
dependent skills (e.g. ask-agent). Read and summarise any that are present.

## Step 5 — Verify live state if needed

If the user wants a fresh live check beyond what `backends` and `llms` show, run:

```bash
curl -s http://<hostname>:9337/v1/models
```

or:

```bash
ssh $AGENT_SSH_USER@<hostname> "pgrep -a llama-server"
```

Report the result.

## Step 6 — Start a model

Ask the user which model they want to load (or confirm the current one is fine).

Find the matching playbook for their choice (`/topology playbook list`) and display its command.

If swapping models, remind the user to kill the existing process first:

```bash
ssh $AGENT_SSH_USER@<hostname> "pkill -f llama-server"
```

Offer to execute the startup command directly if the user confirms.

## Step 7 — Confirm the model is live

After starting, verify:

```bash
curl -s http://<hostname>:9337/v1/models
```

Report the model name returned. The model is ready when this returns a valid JSON response.

## Step 8 — Hand off

Inform the user which machine and model are active, the API endpoint (e.g. `http://gollum:9337`), and that tasks can now be delegated using the **track-tasks-skill**.

---

## Discover subcommand

When the user says `/topology discover` or "discover topology" or "probe nodes":

1. Run:
   ```bash
   PYTHONPATH="${SKILLS_HOME:-$HOME/.agents/skills}/topology-skill/src" python3 -m topology.cli discover
   ```

2. The script probes every machine in the machines table:
   - HTTP: llama-server (:9337) and Ollama (:11434) for running models
   - SSH: gpu, vram, local_ip, GGUF inventory at `~/.local/share/gguf/`
   - HTTP: configured agent endpoints (`hermes_gateway`, `goose_acp_url` fields)
   - SSH: pgrep scan for known agent processes (hermes, goose, aider)

3. It writes three arrays into `topology.toml` (schema_version 2), upgrading a version 1 file:
   - `backends` — one row per inference server per node (ollama, llama-server, and `ggufs` for
     GGUF files on disk): status, port, context window
   - `llms` — one row per LLM, naming the node and backend that serve it
   - `agents` — one row per agent runtime per node: liveness and `reasoning_buffer`
     (preserved from prior runs)

4. Re-read the topology file and present the updated `backends`, `llms` and `agents`
   arrays to the user.

Run discover at the start of a session whenever you need an accurate picture of what is
actually installed and running, rather than relying on a stale topology.

---

## Sync subcommand

When the user says `/topology sync` or "sync topology" or "refresh topology":

1. Run:
   ```bash
   PYTHONPATH="${SKILLS_HOME:-$HOME/.agents/skills}/topology-skill/src" python3 -m topology.cli sync
   ```
2. Report a summary of changes (new machines added, IPs updated, machines marked offline).
   A single `topology-backup.toml` is written before any changes.
3. Re-read the updated topology file and present the refreshed machines table.

---

## Benchmark subcommand

When the user says `/topology benchmark <hostname> <model>` or "benchmark llm":

1. Confirm the model is reachable:
   ```bash
   curl -s http://<hostname>:9337/v1/models
   ```
   Stop and report if the server is not responding.

2. Run the benchmark script:
   ```bash
   PYTHONPATH="${SKILLS_HOME:-$HOME/.agents/skills}/topology-skill/src" python3 -m topology.cli benchmark \
     <hostname> <model> [--port 9337] [--runs 3]
   ```
   Default is 3 runs. The script streams a fixed prompt, measures TTFT (time to first token) and
   generation throughput (tok/s), averages across runs, then writes to the `benchmarks`
   array in the topology file.

3. Report the results:
   ```
   gollum / qwen3-coder-30b:  ttft=312ms  tok/s=46.8  (avg of 3 runs)
   ```

4. Re-read the topology file and show the updated `benchmarks` array so the user can
   see the new entry alongside any prior results.

---

## Show subcommand

When the user says `/topology show` or "show topology" or "show all topology files":

1. Run:
   ```bash
   PYTHONPATH="${SKILLS_HOME:-$HOME/.agents/skills}/topology-skill/src" python3 -m topology.cli show
   ```

2. The script reads `topology.toml` followed by every `topology-*.toml` sidecar file in
   `$TOPOLOGIES_HOME`, printing them in sequence separated by a divider.

3. Present the combined output to the user. Highlight any sidecar files found so it is
   clear which skill owns which data.

Use this when the user wants a full picture of all topology data in one view — machines
table, model state, agent state, and any skill-specific sidecars — without running a
fresh probe.

---

## Help subcommand

When the user says `/topology help` or "show help" or "list subcommands":

1. Run:
   ```bash
   PYTHONPATH="${SKILLS_HOME:-$HOME/.agents/skills}/topology-skill/src" python3 -m topology.cli help
   ```

2. Present the output as-is to the user — it lists every subcommand with a one-line
   description.

---

## Notes

- The topology file is the source of truth. Always read it fresh — do not rely on cached knowledge.
- Model state is ephemeral. The topology records installed capacity; always check what is actually running before assuming.
- If the user asks about mesh-llm or multi-node tensor-split, refer them to the mesh-llm sections of the topology file.

---

## Topology extension convention

Skills that depend on `topology-skill` may add fields to the machines table
to record their own per-node configuration. The base table covers
hostnames, IPs, SSH access, and model availability. Dependent skills extend it
as needed — for example:

| skill | array | fields added |
|---|---|---|
| `ask-agent-skill` | `machines`, `agents` | `hermes_gateway`, `hermes_key_env`, `reasoning_buffer` |

`hermes_gateway` is the HTTP URL of the Hermes agent server on that node (e.g.
`http://pond:8642`). `hermes_key_env` is the name of the env var in
`$SKILLS_HOME/.env` that holds the Bearer token for that gateway (e.g.
`POND_HERMES_KEY`).

`reasoning_buffer` is the estimated token overhead for the model's chain-of-thought
reasoning before it writes output (e.g. `12000` for Qwen3 with thinking enabled,
`0` for models without extended thinking). It is set via the `topology` subcommand
of `ask-agent-skill` and preserved across `discover` runs.

Any skill can follow this pattern: add columns to `topology.toml` for structural
config, put secrets in `$SKILLS_HOME/.env` under a predictable name, and
reference the env var name in the table so the skill knows where to look.
