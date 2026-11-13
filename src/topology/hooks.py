#!/usr/bin/env python3
"""
hooks.py — context that agent harnesses inject so the model uses the topology
skill without being asked.

Invoked via:
  topology hook session-start [--format text|claude]
  topology hook prompt        [--format text|claude]   (prompt on stdin)

session-start prints a short summary of the topology: machines, running
LLMs, playbooks and how to call the CLI. It is meant to be added once, at
the start of a session.

prompt reads the user's prompt and prints a targeted hint only when it
mentions a topology host, a known LLM or GGUF file, or a playbook alias.
Otherwise it prints nothing, so unrelated prompts add nothing to the
context window. If the prompt contains skill notations
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

from .models import LLM, LLMProvider, Topology, read_topology
from .playbook import PlaybookError, get_topologies_home, load_playbooks, normalize_phrase
from .sync import get_topology_path

CLI = 'PYTHONPATH="${SKILLS_HOME:-$HOME/.agents/skills}/topology-skill/src" python3 -m topology.cli'
GENERIC_LLM_PARTS = {'latest', 'gguf'}

CLAUDE_EVENT_NAMES = {'session-start': 'SessionStart', 'prompt': 'UserPromptSubmit'}


# ── Loading ──────────────────────────────────────────────────────────────────

def load_topology() -> Topology | None:
    path = get_topology_path()
    if not os.path.exists(path):
        return None
    return read_topology(path)


def load_playbooks_quietly() -> dict[str, dict]:
    try:
        return load_playbooks(get_topologies_home())
    except (PlaybookError, OSError):
        return {}


def playbook_hosts(playbook: dict) -> set[str]:
    """Hosts targeted directly by a playbook's tasks (refs are not followed)."""
    return {t['hosts'] for t in playbook['tasks'] if 'hosts' in t}


def running_providers(topology: Topology) -> list[tuple[LLMProvider, list[LLM]]]:
    """Each LLM provider that is up and serves at least one LLM, with those LLMs."""
    running = []
    for provider in topology.llm_providers:
        llms = topology.llms_on(provider.hostname, provider.name)
        if provider.status == 'up' and llms:
            running.append((provider, llms))
    return running


def llm_status(topology: Topology, llm: LLM) -> str:
    """The status of the provider serving an LLM, or 'unknown' if it has no provider row."""
    provider = topology.provider_of(llm)
    return provider.status if provider else 'unknown'


# ── Session-start summary ────────────────────────────────────────────────────

def build_summary(topology: Topology | None, playbooks: dict[str, dict]) -> str:
    if topology is None:
        return ''

    lines = [
        f'[topology] The user keeps a home-lab topology at {get_topology_path()}.',
        'For anything about these machines, their LLMs, or services on them, use the '
        'topology skill (or the CLI below) rather than guessing.',
    ]

    if topology.machines:
        names = [m.name + (' (ssh)' if m.ssh else '') for m in topology.machines if m.name]
        lines.append('Machines: ' + ', '.join(names))

    running = running_providers(topology)
    if running:
        lines.append('LLMs running at last discover:')
        for provider, llms in running:
            lines.append(f"  {provider.hostname} {provider.name}: {', '.join(l.name for l in llms)}")

    if playbooks:
        lines.append('Playbooks (run with: topology run "<alias>"):')
        for pb in playbooks.values():
            aliases = ', '.join(f'"{a}"' for a in pb['aliases'])
            lines.append(f"  {pb['name']}: {aliases}" if aliases else f"  {pb['name']}")

    lines.append(f'CLI: {CLI} show | discover | sync | run "<alias>" [--var KEY=VALUE] | playbook list')

    from .resolve_notation import read_notation_key  # imported here: resolve_notation imports this module
    key = read_notation_key()
    lines.append(
        f'Notation: {key} or {key}:<machine|llm|gguf|playbook> in the user\'s message is followed by '
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


def llm_terms(name: str) -> set[str]:
    """The full LLM name plus its base name (before ':' or '.gguf') when distinctive."""
    terms = {name}
    base = name.split(':', 1)[0]
    if base.endswith('.gguf'):
        base = base[:-len('.gguf')]
    if len(base) >= 4 and base not in GENERIC_LLM_PARTS:
        terms.add(base)
    return terms


def match_prompt(prompt: str, topology: Topology | None, playbooks: dict[str, dict]) -> list[str]:
    """Return hint lines for everything in the prompt that refers to the topology."""
    if topology is None or prompt.lstrip().startswith('/'):
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

    for machine in topology.machines:
        name = machine.name
        if not any(contains_term(prompt, t) for t in {name, machine.hostname} if t):
            continue
        detail = ', ssh' if machine.ssh else ', no ssh'
        line = f'Host {name} is in the topology ({machine.os or "unknown os"}{detail}).'
        related = [pb['name'] for pb in playbooks.values() if name in playbook_hosts(pb)]
        if related:
            line += ' Playbooks targeting it: ' + ', '.join(related) + '.'
        hints.append(line)

    for llm in topology.llms:
        if any(contains_term(prompt, t) for t in llm_terms(llm.name)):
            hints.append(
                f'LLM {llm.name} is on {llm.hostname} '
                f'({llm.provider}, {llm_status(topology, llm)} at last discover).'
            )

    for gguf in topology.ggufs:
        if any(contains_term(prompt, t) for t in llm_terms(gguf.name)):
            hints.append(f'GGUF {gguf.name} is on {gguf.hostname} (on disk at {gguf.path}).')

    if not hints:
        return []
    return ['[topology] This prompt refers to the user\'s home-lab topology; use the topology skill.', *hints]


def build_prompt_hint(prompt: str, topology: Topology | None, playbooks: dict[str, dict]) -> str:
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
