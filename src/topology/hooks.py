#!/usr/bin/env python3
"""
hooks.py — context that agent harnesses inject so the model uses the topology
skill without being asked.

Invoked via:
  topology hook session-start [--format text|claude]
  topology hook prompt        [--format text|claude]   (prompt on stdin)

session-start prints a short summary of the topology: machines, running
models, playbooks and how to call the CLI. It is meant to be added once, at
the start of a session.

prompt reads the user's prompt and prints a targeted hint only when it
mentions a topology host, a known model or a playbook alias. Otherwise it
prints nothing, so unrelated prompts add
nothing to the context window. If the prompt contains skill notations
(`^top:pond`, see resolve_notation.py), it prints their expanded forms instead.

--format claude reads Claude Code's hook JSON on stdin and writes
{"hookSpecificOutput": {...additionalContext}}; --format text reads/writes
plain text (pi's extension, goose hints, manual testing).

Hooks run on every prompt, so they only read local TOML (no network, no SSH)
and never fail loudly: any error is reported on stderr and the hook exits 0
with no output, leaving the prompt untouched.
"""

import json
import os
import re
import sys

from .playbook import PlaybookError, get_topologies_home, load_playbooks, normalize_phrase
from .sync import get_topology_path
from .toml_io import read_toml

CLI = 'PYTHONPATH="${SKILLS_HOME:-$HOME/.agents/skills}/topology-skill/src" python3 -m topology.cli'
GENERIC_MODEL_PARTS = {'latest', 'gguf'}

CLAUDE_EVENT_NAMES = {'session-start': 'SessionStart', 'prompt': 'UserPromptSubmit'}


# ── Loading ──────────────────────────────────────────────────────────────────

def load_topology() -> dict:
    path = get_topology_path()
    if not os.path.exists(path):
        return {}
    return read_toml(path)


def load_playbooks_quietly() -> dict[str, dict]:
    try:
        return load_playbooks(get_topologies_home())
    except (PlaybookError, OSError):
        return {}


def playbook_hosts(playbook: dict) -> set[str]:
    """Hosts targeted directly by a playbook's tasks (refs are not followed)."""
    return {t['hosts'] for t in playbook['tasks'] if 'hosts' in t}


def up_models(topology: dict) -> list[dict]:
    """model_state rows for running backends that report at least one model."""
    return [
        row for row in topology.get('model_state', [])
        if row.get('status') == 'up' and row.get('models')
    ]


# ── Session-start summary ────────────────────────────────────────────────────

def build_summary(topology: dict, playbooks: dict[str, dict]) -> str:
    if not topology:
        return ''

    lines = [
        f'[topology] The user keeps a home-lab topology at {get_topology_path()}.',
        'For anything about these machines, their models, or services on them, use the '
        'topology skill (or the CLI below) rather than guessing.',
    ]

    machines = topology.get('machines', [])
    if machines:
        names = [m['name'] + (' (ssh)' if m.get('ssh') else '') for m in machines if m.get('name')]
        lines.append('Machines: ' + ', '.join(names))

    running = up_models(topology)
    if running:
        lines.append('Models running at last discover:')
        for row in running:
            lines.append(f"  {row.get('hostname')} {row.get('backend')}: {', '.join(row['models'])}")

    if playbooks:
        lines.append('Playbooks (run with: topology run "<alias>"):')
        for pb in playbooks.values():
            aliases = ', '.join(f'"{a}"' for a in pb['aliases'])
            lines.append(f"  {pb['name']}: {aliases}" if aliases else f"  {pb['name']}")

    lines.append(f'CLI: {CLI} show | discover | sync | run "<alias>" [--var KEY=VALUE] | playbook list')

    from .resolve_notation import read_notation_key  # imported here: resolve_notation imports this module
    key = read_notation_key()
    lines.append(
        f'Notation: {key} or {key}:<machine|model|playbook> in the user\'s message is followed by '
        '<skill-notation> blocks, each pairing the notation with its expanded form.'
    )
    return '\n'.join(lines)


# ── Prompt matching ──────────────────────────────────────────────────────────

def contains_term(text: str, term: str) -> bool:
    """Case-insensitive whole-term match; '-' counts as part of a word."""
    if not term:
        return False
    pattern = r'(?<![\w-])' + re.escape(term.lower()) + r'(?![\w-])'
    return re.search(pattern, text.lower()) is not None


def model_terms(model: str) -> set[str]:
    """The full model name plus its base name (before ':' or '.gguf') when distinctive."""
    terms = {model}
    base = model.split(':', 1)[0]
    if base.endswith('.gguf'):
        base = base[:-len('.gguf')]
    if len(base) >= 4 and base not in GENERIC_MODEL_PARTS:
        terms.add(base)
    return terms


def match_prompt(prompt: str, topology: dict, playbooks: dict[str, dict]) -> list[str]:
    """Return hint lines for everything in the prompt that refers to the topology."""
    if not topology or prompt.lstrip().startswith('/'):
        return []

    hints: list[str] = []
    normalized = normalize_phrase(prompt)

    for pb in playbooks.values():
        for alias in pb['aliases']:
            if contains_term(normalized, normalize_phrase(alias)):
                hints.append(
                    f'Playbook {pb["name"]} matches alias "{alias}". '
                    f'Run it with: {CLI} run "{alias}" (add --var KEY=VALUE for any variables it needs).'
                )
                break

    for machine in topology.get('machines', []):
        name = machine.get('name', '')
        if not any(contains_term(prompt, t) for t in {name, machine.get('hostname', '')} if t):
            continue
        detail = ', ssh' if machine.get('ssh') else ', no ssh'
        line = f'Host {name} is in the topology ({machine.get("os", "unknown os")}{detail}).'
        related = [pb['name'] for pb in playbooks.values() if name in playbook_hosts(pb)]
        if related:
            line += ' Playbooks targeting it: ' + ', '.join(related) + '.'
        hints.append(line)

    for row in topology.get('model_state', []):
        for model in row.get('models', []):
            if any(contains_term(prompt, t) for t in model_terms(model)):
                hints.append(
                    f'Model {model} is on {row.get("hostname")} '
                    f'({row.get("backend")}, {row.get("status")} at last discover).'
                )

    if not hints:
        return []
    return ['[topology] This prompt refers to the user\'s home-lab topology; use the topology skill.', *hints]


def build_prompt_hint(prompt: str, topology: dict, playbooks: dict[str, dict]) -> str:
    return '\n'.join(match_prompt(prompt, topology, playbooks))


# ── Entry point ──────────────────────────────────────────────────────────────

def read_prompt(stdin_text: str, fmt: str) -> str:
    if fmt == 'claude':
        return json.loads(stdin_text or '{}').get('prompt', '')
    return stdin_text


def format_output(text: str, event: str, fmt: str) -> str:
    if not text:
        return ''
    if fmt == 'claude':
        return json.dumps({
            'hookSpecificOutput': {
                'hookEventName': CLAUDE_EVENT_NAMES[event],
                'additionalContext': text,
            }
        })
    return text


def parse_argv(argv: list[str]) -> tuple[str, str]:
    if not argv or argv[0] not in CLAUDE_EVENT_NAMES:
        raise ValueError('usage: topology hook session-start|prompt [--format text|claude]')
    event, fmt = argv[0], 'text'
    rest = argv[1:]
    if rest[:1] == ['--format'] and len(rest) == 2 and rest[1] in ('text', 'claude'):
        fmt = rest[1]
    elif rest:
        raise ValueError(f'unexpected arguments: {" ".join(rest)}')
    return event, fmt


def run(event: str, fmt: str, stdin_text: str) -> str:
    topology = load_topology()
    playbooks = load_playbooks_quietly()
    if event == 'session-start':
        text = build_summary(topology, playbooks)
    else:
        from .resolve_notation import expand_prompt  # imported here: resolve_notation imports this module
        prompt = read_prompt(stdin_text, fmt)
        # Explicit notations take precedence over guessing from plain language.
        text = expand_prompt(prompt, topology, playbooks) or build_prompt_hint(prompt, topology, playbooks)
    return format_output(text, event, fmt)


def main(argv: list[str]) -> None:
    try:
        event, fmt = parse_argv(argv)
    except ValueError as e:
        print(e, file=sys.stderr)
        sys.exit(1)

    try:
        stdin_text = sys.stdin.read() if event == 'prompt' else ''
        output = run(event, fmt, stdin_text)
    except Exception as e:  # a hook must never block the user's prompt
        print(f'topology hook {event}: {e}', file=sys.stderr)
        return
    if output:
        print(output)
