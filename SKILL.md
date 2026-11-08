---
name: topology
description: The user's home-lab network, read from $TOPOLOGIES_HOME/topology.toml - machines, GPUs, local LLM models and playbooks. Use whenever the user mentions one of their machines by name, asks what models are available or running, wants to start, stop, swap or benchmark a model, run or list a playbook, or sync, discover or show the topology. Also triggers on "topology", "what models are available", "which machines are running", "start a model", "run playbook", "list playbooks".
argument-hint: "[sync | benchmark <hostname> <model> | show | run \"<phrase>\" [--skip-oversight] [--var KEY=VALUE ...] | playbook list | help]"
metadata:
  notation-key: "^top"
---

Reads `$TOPOLOGIES_HOME/topology.toml` (default `~/.agents/skills/topology.toml` if unset) to enumerate machines and models. On first run, if no topology file exists, guides the user through setup automatically.
