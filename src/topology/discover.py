#!/usr/bin/env python3
"""
discover.py — probe all reachable nodes and write live state into topology.toml.

Invoked via: /topology discover

Probes every machine in the machines table:
  - HTTP:  llama-server (:9337) and Ollama (:11434) for running models
  - SSH:   gpu, vram, local_ip, GGUF inventory
  - HTTP:  configured agent endpoints (hermes_gateway, goose_acp_url keys)
  - SSH:   pgrep scan for known agent processes

Writes/replaces four arrays in topology.toml (schema_version 2, see models.py):
  llm_providers  — one row per LLM server per node: status, port, context window
  llms           — one row per LLM, naming the node and provider that serve it
  agents         — one row per agent runtime per node: liveness and reasoning_buffer
  ggufs          — one row per GGUF file found on a node over SSH

A schema_version 1 file (model_state / agent_state) is upgraded on write.
"""

import json
import os
import posixpath
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime

from .models import GGUF, GGUF_DIR, LLM, Agent, LLMProvider, read_topology_data
from .sync import get_topology_path
from .toml_io import write_toml

AGENT_SSH_USER = os.environ.get('AGENT_SSH_USER', 'nicholasf')

KNOWN_AGENT_PROCS = ['hermes', 'goose', 'aider', 'open-webui']


def ts() -> str:
    return datetime.now().strftime('%Y-%m-%dT%H-%M-%S')


# ── SSH ───────────────────────────────────────────────────────────────────────

def ssh_run(host: str, user: str, cmd: str, timeout: int = 10) -> str | None:
    try:
        r = subprocess.run(
            ['ssh',
             '-o', 'ConnectTimeout=5',
             '-o', 'BatchMode=yes',
             '-o', 'StrictHostKeyChecking=no',
             f'{user}@{host}', cmd],
            capture_output=True, text=True, timeout=timeout,
        )
        return r.stdout.strip() if r.returncode == 0 else None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


# ── HTTP ──────────────────────────────────────────────────────────────────────

def http_json(url: str, timeout: int = 3) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


def http_up(url: str, timeout: int = 3) -> bool:
    try:
        urllib.request.urlopen(url, timeout=timeout)
        return True
    except urllib.error.HTTPError:
        return True  # got a response — server is reachable
    except Exception:
        return False


# ── Context window probing ────────────────────────────────────────────────────

def probe_llama_context_window(host: str) -> int | None:
    """GET /props from llama-server; returns n_ctx or None."""
    data = http_json(f'http://{host}:9337/props')
    if data and 'n_ctx' in data:
        return data['n_ctx']
    return None


def probe_ollama_context_window(host: str, model: str) -> int | None:
    """POST /api/show to Ollama for a specific model; returns context length or None."""
    try:
        body = json.dumps({'name': model}).encode()
        req = urllib.request.Request(
            f'http://{host}:11434/api/show',
            data=body,
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read())
        mi = data.get('model_info', {})
        ctx = next((v for k, v in mi.items() if k.endswith('.context_length')), None)
        return int(ctx) if ctx else None
    except Exception:
        return None


# ── Preserved reasoning_buffer values ────────────────────────────────────────

def read_existing_agent_reasoning_buffers(agents: list[dict]) -> dict[tuple[str, str], int]:
    """Read existing reasoning_buffer values from the agents table before overwriting."""
    return {
        (row['hostname'], row['name']): row['reasoning_buffer']
        for row in agents
        if row.get('hostname') and row.get('name') and row.get('reasoning_buffer') is not None
    }


# ── Discovery ─────────────────────────────────────────────────────────────────

def discover_hardware(host: str, user: str, os_name: str) -> dict:
    out: dict[str, str] = {}

    ip = ssh_run(host, user, "ip route get 1 2>/dev/null | awk '{print $7; exit}'")
    if ip:
        out['local_ip'] = ip

    if 'macos' in os_name.lower():
        gpu = ssh_run(host, user,
            "system_profiler SPDisplaysDataType 2>/dev/null"
            " | awk -F': ' '/Chipset Model/{print $2; exit}'")
    else:
        nvidia_smi = "$(which nvidia-smi 2>/dev/null || echo /usr/lib/wsl/lib/nvidia-smi)"
        gpu = ssh_run(host, user,
            f"{nvidia_smi} --query-gpu=name --format=csv,noheader 2>/dev/null | head -1")
        if not gpu:
            gpu = ssh_run(host, user,
                "lspci 2>/dev/null | grep -i 'vga\\|3d controller\\|display'"
                " | sed 's/.*: //' | head -1")
    if gpu:
        out['gpu'] = gpu[:60]

    nvidia_smi = "$(which nvidia-smi 2>/dev/null || echo /usr/lib/wsl/lib/nvidia-smi)"
    vram = ssh_run(host, user,
        f"{nvidia_smi} --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1")
    if vram and vram.strip().isdigit():
        out['vram'] = f'{int(vram.strip()) // 1024}GB (CUDA)'
    else:
        vram = ssh_run(host, user,
            "rocm-smi --showmeminfo vram 2>/dev/null"
            " | grep -i 'total memory' | awk '{print $NF}' | head -1")
        if vram and vram.strip().isdigit():
            gb = int(vram.strip()) // (1024 ** 3)
            out['vram'] = f'{gb}GB UMA (ROCm)'

    out['last_verified'] = ts()[:10]
    return out


def list_ggufs(host: str, user: str) -> list[str]:
    """Full paths of the GGUF files in GGUF_DIR on a host."""
    out = ssh_run(host, user, f"ls -1 {GGUF_DIR}/*.gguf 2>/dev/null")
    if not out:
        return []
    return [f.strip() for f in out.splitlines() if f.strip()]


def probe_models(host: str) -> dict:
    ls_data = http_json(f'http://{host}:9337/v1/models')
    ol_data = http_json(f'http://{host}:11434/v1/models')

    ls_up = bool(ls_data and 'data' in ls_data)
    ls_models = [m['id'] for m in ls_data.get('data', [])] if ls_data else []
    ls_ctx = probe_llama_context_window(host) if ls_up else None

    ol_up = bool(ol_data and 'data' in ol_data)
    ol_models = [m['id'] for m in ol_data.get('data', [])] if ol_data else []
    ol_ctx = probe_ollama_context_window(host, ol_models[0]) if (ol_up and ol_models) else None

    return {
        'llama_server': {'up': ls_up, 'models': ls_models, 'context_window': ls_ctx},
        'ollama': {'up': ol_up, 'models': ol_models, 'context_window': ol_ctx},
    }


def probe_agents(host: str, user: str | None, row: dict) -> list[dict]:
    now = ts()
    results = []

    for agent_name, key in [('hermes', 'hermes_gateway'), ('goose', 'goose_acp_url')]:
        endpoint = row.get(key)
        if not endpoint:
            continue
        probe_url = endpoint.replace('ws://', 'http://').replace('wss://', 'https://')
        up = http_up(probe_url)
        proc = ssh_run(host, user, f"pgrep -x {agent_name} 2>/dev/null | head -1") if user else None
        entry = {
            'hostname': host,
            'name': agent_name,
            'endpoint': endpoint,
            'status': 'up' if up else 'down',
            'process': ('running' if proc else 'not found') if user else '(no SSH)',
        }
        if up:
            entry['last_seen'] = now
        results.append(entry)

    if user:
        covered = {r['name'] for r in results}
        for proc_name in KNOWN_AGENT_PROCS:
            if proc_name in covered:
                continue
            pid = ssh_run(host, user, f"pgrep -x {proc_name} 2>/dev/null | head -1")
            if pid:
                results.append({
                    'hostname': host,
                    'name': proc_name,
                    'endpoint': '(not configured)',
                    'status': 'process up',
                    'process': 'running',
                    'last_seen': now,
                })

    return results


# ── Section builders ──────────────────────────────────────────────────────────

# Each LLM provider: (name in topology.toml, key in probe_models output, port).
LLM_PROVIDERS = [('llama-server', 'llama_server', 9337), ('ollama', 'ollama', 11434)]


def build_llm_providers(discoveries: dict) -> list[LLMProvider]:
    """One LLMProvider per server per host, up or down."""
    providers = []
    now = ts()
    for host in sorted(discoveries):
        probed = discoveries[host].get('models', {})
        for name, key, port in LLM_PROVIDERS:
            p = probed.get(key, {})
            provider = LLMProvider(hostname=host, name=name, port=port, status='up' if p.get('up') else 'down')
            if p.get('up'):
                provider.last_seen = now
                provider.context_window = p.get('context_window')
            providers.append(provider)
    return providers


def build_llms(discoveries: dict) -> list[LLM]:
    """One LLM per model a running provider reports."""
    llms = []
    for host in sorted(discoveries):
        probed = discoveries[host].get('models', {})
        for name, key, _port in LLM_PROVIDERS:
            p = probed.get(key, {})
            if p.get('up'):
                llms += [LLM(hostname=host, provider=name, name=m) for m in p.get('models', [])]
    return llms


def build_ggufs(discoveries: dict) -> list[GGUF]:
    """One GGUF per file SSH listed; hosts not reached over SSH have none."""
    now = ts()
    return [
        GGUF(hostname=host, name=posixpath.basename(path), path=path, last_seen=now)
        for host in sorted(discoveries)
        for path in discoveries[host].get('ggufs') or []
    ]


def build_agents(all_rows: list[dict], preserved_buffers: dict[tuple[str, str], int] | None = None) -> list[Agent]:
    preserved_buffers = preserved_buffers or {}
    agents = []
    for row in all_rows:
        agent = Agent.from_dict(row)
        buf = preserved_buffers.get((agent.hostname, agent.name))
        if buf is not None:
            agent.reasoning_buffer = buf
        agents.append(agent)
    return agents


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    path = get_topology_path()
    if not os.path.exists(path):
        print(f'Topology not found: {path}', file=sys.stderr)
        sys.exit(1)

    data = read_topology_data(path)
    rows = data.get('machines', [])
    if not rows:
        print('No machines table found in topology file.', file=sys.stderr)
        sys.exit(1)

    preserved_buffers = read_existing_agent_reasoning_buffers(data.get('agents', []))

    discoveries: dict[str, dict] = {}
    all_agent_rows: list[dict] = []
    hw_updates: dict[str, dict] = {}

    for row in rows:
        host = row.get('hostname', '').strip()
        if not host:
            continue

        can_ssh = row.get('ssh') is True
        user = (row.get('ssh_user') or AGENT_SSH_USER) if can_ssh else None
        os_name = row.get('os', 'linux')

        print(f'{host}:', end=' ', flush=True)

        if can_ssh:
            hw = discover_hardware(host, user, os_name)
            hw_updates[host] = hw

        ggufs = list_ggufs(host, user) if can_ssh else None
        models = probe_models(host)
        agents = probe_agents(host, user, row)

        discoveries[host] = {'models': models, 'ggufs': ggufs}
        all_agent_rows.extend(agents)

        parts = []
        if models['llama_server']['up']:
            m = models['llama_server']['models']
            parts.append(f"llama-server:{m[0] if m else 'up'}")
        if models['ollama']['up']:
            parts.append(f"ollama:{len(models['ollama']['models'])} models")
        agent_summary = [f"{a['name']}:{a['status']}" for a in agents]
        print(
            (', '.join(parts) if parts else 'no inference running') +
            (f'  agents={agent_summary}' if agent_summary else '')
        )

    if hw_updates:
        for row in rows:
            host = row.get('hostname', '')
            if host in hw_updates:
                row.update(hw_updates[host])

    data['machines'] = rows
    data['llm_providers'] = [p.to_dict() for p in build_llm_providers(discoveries)]
    data['llms'] = [l.to_dict() for l in build_llms(discoveries)]
    data['agents'] = [a.to_dict() for a in build_agents(all_agent_rows, preserved_buffers)]
    data['ggufs'] = [g.to_dict() for g in build_ggufs(discoveries)]

    write_toml(data, path)

    print(f'\nTopology updated: {path}')


if __name__ == '__main__':
    main()
