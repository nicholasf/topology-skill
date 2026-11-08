#!/usr/bin/env python3
"""
notation.py — proof of concept for skill notations.

A notation is a short reference typed into a prompt, e.g. `^top:pond`:

  ^top    the notation key, declared in SKILL.md front matter (metadata.notation-key)
  pond    the payload, URL-encoded (`+` for a space), resolved by this skill

A notation's expanded form is what the notation leads to. The prompt hook
finds notations in the user's prompt, resolves each payload against the
topology, and builds the expanded forms. They are added to the context after
the prompt as XML, pairing each notation with its expanded form:

  <skill-notation skill="topology">
    <notation>^top:pond</notation>
    <expanded>Host pond: linux, ssh. ...</expanded>
  </skill-notation>

A notation that cannot be resolved gets <error> in place of <expanded>, so the
model never guesses:

  <skill-notation skill="topology">
    <notation>^top:dtv</notation>
    <error>Unknown handle 'dtv' in the topology. Did you mean: dtiv?</error>
  </skill-notation>

The skill attribute names the skill that produced the block (its SKILL.md
name), so anyone reading a recorded context can tell where each block came
from without knowing which notation key belongs to which skill.

The tag itself says whether resolution worked; there is no outcome attribute.
Everything in the block is context for the model anyway, so the only
distinction worth marking is success versus failure. If a skill later runs
something and returns its output, that is still an expanded form whose text
says so.

Payloads this skill understands:
  (none)            ^top                    the topology summary
  machine           ^top:pond               host details, models, playbooks targeting it
  model             ^top:qwen3-coder        where the model is running
  playbook          ^top:start-pond-qwen    playbook details and how to run it
                    ^top:wake+pond+up       (a playbook alias works too)
Anything else gives <error> with suggestions.

Every step is logged to $TOPOLOGY_NOTATION_LOG (default
~/.local/state/topology/notation.log); set TOPOLOGY_NOTATION_DEBUG=1 to also
log to stderr.
"""

import difflib
import json
import logging
import os
import re
import sys
from pathlib import Path
from urllib.parse import quote_plus, unquote_plus
from xml.sax.saxutils import escape, quoteattr

from .hooks import CLI, build_summary, load_playbooks_quietly, load_topology, model_terms, playbook_hosts
from .playbook import VAR_PATTERN, normalize_phrase, resolve_alias

SKILL_MD = Path(__file__).resolve().parents[2] / 'SKILL.md'
DEFAULT_KEY = '^top'
DEFAULT_SKILL_NAME = 'topology'
TRAILING_PUNCTUATION = '.,;:!?)\'"'

log = logging.getLogger('topology.notation')


# ── Logging ──────────────────────────────────────────────────────────────────

def default_log_path() -> str:
    if os.environ.get('TOPOLOGY_NOTATION_LOG'):
        return os.environ['TOPOLOGY_NOTATION_LOG']
    state = os.environ.get('XDG_STATE_HOME') or os.path.join(os.path.expanduser('~'), '.local', 'state')
    return os.path.join(state, 'topology', 'notation.log')


def setup_logging(path: str | None = None, debug: bool | None = None) -> None:
    """(Re)configure the notation logger; never raises."""
    log.handlers.clear()
    log.setLevel(logging.INFO)
    log.propagate = False
    formatter = logging.Formatter('%(asctime)s %(message)s', '%Y-%m-%d %H:%M:%S')
    try:
        path = path or default_log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        handler = logging.FileHandler(path)
        handler.setFormatter(formatter)
        log.addHandler(handler)
    except OSError as e:
        print(f'topology notation: cannot write log: {e}', file=sys.stderr)
    if debug if debug is not None else os.environ.get('TOPOLOGY_NOTATION_DEBUG') == '1':
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(formatter)
        log.addHandler(handler)


# ── Notation key ────────────────────────────────────────────────────────────────

def front_matter_value(pattern: str, default: str, skill_md: Path = SKILL_MD) -> str:
    """First capture of `pattern` in SKILL.md front matter (no YAML dependency)."""
    try:
        text = skill_md.read_text()
    except OSError:
        return default
    front = re.match(r'---\n(.*?)\n---', text, re.S)
    found = front and re.search(pattern, front.group(1), re.M)
    return found.group(1) if found else default


def read_notation_key(skill_md: Path = SKILL_MD) -> str:
    """metadata.notation-key from SKILL.md front matter."""
    return front_matter_value(r'^\s+notation-key:\s*["\']?([^\s"\']+)', DEFAULT_KEY, skill_md)


def read_skill_name(skill_md: Path = SKILL_MD) -> str:
    """name from SKILL.md front matter; shown as the skill attribute on each block."""
    return front_matter_value(r'^name:\s*["\']?([^\s"\']+)', DEFAULT_SKILL_NAME, skill_md)


# ── Finding notations ────────────────────────────────────────────────────────

def find_notations(prompt: str, key: str) -> list[tuple[str, str]]:
    """(notation as typed, decoded payload) for each notation, in order, without duplicates.

    A notation starts at the beginning of the prompt or after whitespace, so a
    regex anchor like `x^top` or `^topology` is not one. Trailing sentence
    punctuation (`is ^top:pond?`) is not part of the notation.
    """
    pattern = re.compile(r'(?<!\S)' + re.escape(key) + r'(?::(\S*))?(?![\w-])')
    found: list[tuple[str, str]] = []
    for match in pattern.finditer(prompt):
        raw = (match.group(1) or '').rstrip(TRAILING_PUNCTUATION)
        notation = f'{key}:{raw}' if raw else key
        if notation not in (n for n, _ in found):
            found.append((notation, unquote_plus(raw)))
    return found


# ── Resolving payloads ───────────────────────────────────────────────────────

def describe_machine(machine: dict, topology: dict, playbooks: dict[str, dict]) -> str:
    name = machine.get('name', '')
    names = {name, machine.get('hostname', '')} - {''}
    parts = [f'Host {name}: {machine.get("os", "unknown os")}, {"ssh" if machine.get("ssh") else "no ssh"}.']
    for row in topology.get('model_state', []):
        if row.get('hostname') in names:
            models = ', '.join(row.get('models', [])) or 'no models'
            parts.append(f'{row.get("backend")} ({row.get("status")} at last discover): {models}.')
    related = [pb['name'] for pb in playbooks.values() if names & playbook_hosts(pb)]
    if related:
        parts.append('Playbooks targeting it: ' + ', '.join(related) + '.')
    return ' '.join(parts)


def describe_playbook(pb: dict) -> str:
    parts = [f'Playbook {pb["name"]}.']
    if pb.get('description'):
        parts.append(pb['description'].rstrip('.') + '.')
    if pb['aliases']:
        parts.append('Aliases: ' + ', '.join(f'"{a}"' for a in pb['aliases']) + '.')
    hosts = sorted(playbook_hosts(pb))
    if hosts:
        parts.append('Hosts: ' + ', '.join(hosts) + '.')
    variables = sorted({m.group(1) for t in pb['tasks'] for m in VAR_PATTERN.finditer(t.get('command', ''))})
    run_as = pb['aliases'][0] if pb['aliases'] else pb['name']
    parts.append(f'Run with: {CLI} run "{run_as}"' + ''.join(f' --var {v}=...' for v in variables))
    return ' '.join(parts)


def all_handles(topology: dict, playbooks: dict[str, dict]) -> list[str]:
    handles = set()
    for m in topology.get('machines', []):
        handles |= {m.get('name', ''), m.get('hostname', '')}
    for row in topology.get('model_state', []):
        for model in row.get('models', []):
            handles |= model_terms(model)
    for pb in playbooks.values():
        handles |= {pb['name'], *pb['aliases']}
    return sorted(handles - {''})


def expand(payload: str, topology: dict, playbooks: dict[str, dict]) -> tuple[str, str, str]:
    """Resolve a payload. Returns (tag, kind, text); tag is 'expanded' or 'error'."""
    if not topology:
        return 'error', 'none', 'No topology found; run the topology skill to set one up.'
    if not payload:
        return 'expanded', 'summary', build_summary(topology, playbooks)

    wanted = payload.lower()
    descriptions: list[tuple[str, str]] = []

    for machine in topology.get('machines', []):
        if wanted in {machine.get('name', '').lower(), machine.get('hostname', '').lower()}:
            descriptions.append(('host', describe_machine(machine, topology, playbooks)))

    model_lines = []
    for row in topology.get('model_state', []):
        for model in row.get('models', []):
            if wanted in {t.lower() for t in model_terms(model)}:
                model_lines.append(f'Model {model} is on {row.get("hostname")} '
                                   f'({row.get("backend")}, {row.get("status")} at last discover).')
    if model_lines:
        descriptions.append(('model', ' '.join(model_lines)))

    name = payload if payload in playbooks else resolve_alias(payload, playbooks)
    if name:
        descriptions.append(('playbook', describe_playbook(playbooks[name])))

    if descriptions:
        return 'expanded', '+'.join(k for k, _ in descriptions), '\n'.join(t for _, t in descriptions)

    suggestions = difflib.get_close_matches(normalize_phrase(payload), all_handles(topology, playbooks), n=3, cutoff=0.5)
    text = f"Unknown handle '{payload}' in the topology."
    if suggestions:
        text += ' Did you mean: ' + ', '.join(suggestions) + '?'
    return 'error', 'unknown', text


# ── Expanded form ────────────────────────────────────────────────────────────

def build_expanded_form(notation: str, tag: str, text: str, skill: str) -> str:
    """tag is 'expanded' or 'error'; skill names the skill that produced the block."""
    return (
        f'<skill-notation skill={quoteattr(skill)}>\n'
        f'  <notation>{escape(notation)}</notation>\n'
        f'  <{tag}>\n'
        + '\n'.join(f'    {line}' for line in escape(text).splitlines())
        + f'\n  </{tag}>\n'
        '</skill-notation>'
    )


def expand_prompt(prompt: str, topology: dict, playbooks: dict[str, dict], key: str | None = None) -> str:
    """Expanded forms for every notation in the prompt, or '' when there are none."""
    key = key or read_notation_key()
    skill = read_skill_name()
    notations = find_notations(prompt, key)
    if not notations:
        return ''

    setup_logging()
    log.info('prompt received (%d chars): %d notation(s) for %s', len(prompt), len(notations), key)
    forms = []
    for notation, payload in notations:
        log.info('notation found: %s  payload=%r', notation, payload)
        tag, kind, text = expand(payload, topology, playbooks)
        log.info('resolved: %s -> %s (%s)', notation, tag, kind)
        form = build_expanded_form(notation, tag, text, skill)
        log.info('expanded form:\n%s', form)
        forms.append(form)
    return '\n'.join(forms)


# ── Completion (pi autocomplete) ─────────────────────────────────────────────

def encode_payload(handle: str) -> str:
    """URL-encode a handle for a notation; `+` for spaces, `:` left readable."""
    return quote_plus(handle, safe=':')


def completion_items(topology: dict, playbooks: dict[str, dict]) -> list[dict]:
    """Handles this skill can resolve, each as {value, description}, value URL-encoded."""
    items: list[dict] = []
    seen: set[str] = set()

    def add(handle: str, description: str) -> None:
        value = encode_payload(handle)
        if handle and value not in seen:
            seen.add(value)
            items.append({'value': value, 'description': description})

    for machine in topology.get('machines', []):
        add(machine.get('name', ''), f'host: {machine.get("os", "unknown os")}, {"ssh" if machine.get("ssh") else "no ssh"}')
    for row in topology.get('model_state', []):
        for model in row.get('models', []):
            add(model, f'model on {row.get("hostname")} ({row.get("backend")}, {row.get("status")})')
    for pb in playbooks.values():
        summary = pb.get('description', '') or 'playbook'
        add(pb['name'], f'playbook: {summary}')
        for alias in pb['aliases']:
            add(alias, f'alias of playbook {pb["name"]}')
    return items


def build_completion(topology: dict, playbooks: dict[str, dict]) -> dict:
    return {
        'key': read_notation_key(),
        'skill': read_skill_name(),
        'description': 'machines, models and playbooks in the home-lab topology',
        'handles': completion_items(topology, playbooks),
    }


def main(argv: list[str]) -> None:
    if argv != ['complete']:
        print('usage: topology notation complete', file=sys.stderr)
        sys.exit(1)
    print(json.dumps(build_completion(load_topology(), load_playbooks_quietly())))
