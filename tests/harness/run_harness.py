#!/usr/bin/env python3
"""
run_harness.py — end-to-end check that each agent harness really runs the
topology hooks, using the real harness binary and a fake model.

For each harness this:
  1. builds a throwaway world in a temp dir: a fixture topology
     ($TOPOLOGIES_HOME), a $SKILLS_HOME whose topology-skill points at this
     repo, and the harness's own config dir,
  2. installs the hooks with `topology hooks install` logic into that world,
  3. starts stub_model.py and points the harness at it,
  4. sends one prompt that should match ("start qwen on pond") and one that
     should not ("say hello"), headless, one process each,
  5. inspects the recorded request bodies: is the session summary there, is
     the per-prompt hint there, and where in the request did each land
     (system prompt vs messages)?

Your real ~/.claude, ~/.pi and goose config are never written to. No real
model is called, so this is free and deterministic, but it does need the
harness binaries installed. It is deliberately not part of the pytest suite.

Usage:
  python3 tests/harness/run_harness.py [claude] [pi] [goose] [--keep]

--keep leaves the temp dir (with requests-*.jsonl recordings) for inspection.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
sys.path.insert(0, str(REPO_ROOT / 'src'))
sys.path.insert(0, str(HERE))

from stub_model import MODEL, StubModel  # noqa: E402
from topology.hooks_install import install  # noqa: E402
from topology.toml_io import write_toml  # noqa: E402

MATCHING_PROMPT = 'start qwen on pond'
UNRELATED_PROMPT = 'say hello'
SUMMARY_MARKER = '[topology] The user keeps a home-lab topology'
HINT_MARKER = 'Playbook start-pond-qwen matches alias'
TIMEOUT = 180

PLAYBOOK_TOML = '''[[playbook]]
name = "start-pond-qwen"
aliases = ["start qwen on pond"]
description = "Fixture playbook for the harness test."

  [[playbook.tasks]]
  hosts = "pond"
  command = "echo would start qwen"
'''


# ── Fixture world ────────────────────────────────────────────────────────────

def build_world(root: Path) -> dict[str, str]:
    """Create fixture topology + skills home; return env vars pointing at them."""
    topologies = root / 'topologies'
    topologies.mkdir()
    write_toml({
        'schema_version': 2,
        'network': 'manual',
        'machines': [{'name': 'pond', 'hostname': 'pond', 'os': 'linux', 'ssh': True}],
        'llm_providers': [{'hostname': 'pond', 'name': 'ollama', 'port': 11434, 'status': 'up'}],
        'llms': [{'hostname': 'pond', 'provider': 'ollama', 'name': 'qwen3-coder:30b'}],
    }, str(topologies / 'topology.toml'))
    (topologies / 'topology-playbook-pond.toml').write_text(PLAYBOOK_TOML)

    skills = root / 'skills'
    skills.mkdir()
    (skills / 'topology-skill').symlink_to(REPO_ROOT)

    (root / 'work').mkdir()
    return {'TOPOLOGIES_HOME': str(topologies), 'SKILLS_HOME': str(skills)}


def base_env(world_env: dict[str, str]) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(('ANTHROPIC_', 'CLAUDE_', 'OPENAI_', 'GOOSE_', 'PI_'))}
    env.update(world_env)
    return env


# ── Harness setups ───────────────────────────────────────────────────────────
# Each returns (command-builder, env) for one prompt; hooks are installed into
# the temp world using the same install() the CLI uses.

def setup_claude(root: Path, url: str, env: dict[str, str]):
    config = root / 'claude-config'
    install('claude', config / 'settings.json')
    env = {**env,
           'CLAUDE_CONFIG_DIR': str(config),
           'ANTHROPIC_BASE_URL': url,
           'ANTHROPIC_API_KEY': 'stub-key',
           'DISABLE_TELEMETRY': '1',
           'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1'}
    return (lambda prompt: ['claude', '-p', prompt]), env


def setup_pi(root: Path, url: str, env: dict[str, str]):
    agent_dir = root / 'pi-agent'
    install('pi', agent_dir / 'extensions' / 'topology.ts')
    (agent_dir / 'models.json').write_text(json.dumps({'providers': {'stub': {
        'baseUrl': url + '/v1',
        'api': 'openai-completions',
        'apiKey': 'stub-key',
        'compat': {'supportsDeveloperRole': False},
        'models': [{'id': MODEL}],
    }}}))
    env = {**env,
           'PI_CODING_AGENT_DIR': str(agent_dir),
           'PI_OFFLINE': '1',
           'PI_SKIP_VERSION_CHECK': '1',
           'PI_TELEMETRY': '0'}
    return (lambda prompt: ['pi', '-p', '--no-session', '--no-skills', '--no-context-files',
                            '--provider', 'stub', '--model', MODEL, prompt]), env


def setup_goose(root: Path, url: str, env: dict[str, str]):
    install('goose', root / 'work' / '.goosehints')
    env = {**env,
           'GOOSE_PROVIDER': 'openai',
           'GOOSE_MODEL': MODEL,
           'OPENAI_HOST': url,
           'OPENAI_API_KEY': 'stub-key',
           'GOOSE_DISABLE_KEYRING': '1'}
    return (lambda prompt: ['goose', 'run', '--no-session', '--max-turns', '1', '-t', prompt]), env


SETUPS = {'claude': setup_claude, 'pi': setup_pi, 'goose': setup_goose}

# What each harness is expected to do: (summary in context, hint for matching prompt).
EXPECT = {'claude': (True, True), 'pi': (True, True), 'goose': (True, False)}


# ── Inspecting recorded requests ─────────────────────────────────────────────

def locate(body: dict, marker: str) -> list[str]:
    """Where in a request body a marker appears.

    'system' means the system prompt at the start of the request: Anthropic's
    top-level `system` field, or an OpenAI system/developer message *before*
    the first user message. Anything later is 'messages[i]:<role>', even with
    role "system" (Claude Code appends hook context as a trailing
    system-role message), because only the start of the request affects the
    cached prefix of the conversation that follows.
    """
    places = []
    if marker in json.dumps(body.get('system', '')):
        places.append('system')
    seen_user = False
    for i, message in enumerate(body.get('messages', [])):
        role = message.get('role', '?')
        if marker in json.dumps(message):
            leading = role in ('system', 'developer') and not seen_user
            places.append('system' if leading else f'messages[{i}]:{role}')
        seen_user = seen_user or role == 'user'
    return places


def main_request(requests: list[dict], prompt: str) -> dict | None:
    """The last model request whose messages include the user's prompt."""
    candidates = [r['body'] for r in requests
                  if isinstance(r['body'], dict) and r['body'].get('messages')
                  and prompt in json.dumps(r['body']['messages'])]
    return candidates[-1] if candidates else None


def check(harness: str, prompt: str, requests: list[dict]) -> tuple[bool, str]:
    body = main_request(requests, prompt)
    if body is None:
        paths = sorted({r['path'] for r in requests}) or ['none']
        return False, f'no model request contained the prompt (requests seen: {", ".join(paths)})'

    expect_summary, expect_hint = EXPECT[harness]
    expect_hint = expect_hint and prompt == MATCHING_PROMPT
    summary_at, hint_at = locate(body, SUMMARY_MARKER), locate(body, HINT_MARKER)

    problems = []
    if bool(summary_at) != expect_summary:
        problems.append('summary ' + ('missing' if expect_summary else 'unexpected'))
    if bool(hint_at) != expect_hint:
        problems.append('hint ' + ('missing' if expect_hint else 'unexpected'))
    if hint_at and 'system' in hint_at:
        problems.append('hint landed in the system prompt (breaks the prefix cache)')

    detail = f'summary at {summary_at or "-"}; hint at {hint_at or "-"}'
    return not problems, '; '.join(problems + [detail]) if problems else detail


# ── Runner ───────────────────────────────────────────────────────────────────

def run_harness(harness: str, root: Path) -> list[tuple[str, bool, str]]:
    if shutil.which(harness) is None:
        return [('-', False, f'{harness} not found on PATH')]

    world_env = build_world(root)
    os.environ.update(world_env)  # goose's hints snapshot is built in this process
    results = []
    for prompt in (MATCHING_PROMPT, UNRELATED_PROMPT):
        record = root / f'requests-{harness}-{"match" if prompt == MATCHING_PROMPT else "unrelated"}.jsonl'
        with StubModel(str(record)) as stub:
            command, env = SETUPS[harness](root, stub.url, base_env(world_env))
            try:
                proc = subprocess.run(command(prompt), cwd=root / 'work', env=env,
                                      capture_output=True, text=True, timeout=TIMEOUT)
            except subprocess.TimeoutExpired:
                results.append((prompt, False, f'timed out after {TIMEOUT}s'))
                continue
            ok, detail = check(harness, prompt, stub.requests())
            if not ok and proc.returncode != 0:
                detail += f' | exit {proc.returncode}: {(proc.stderr or proc.stdout).strip()[-300:]}'
            results.append((prompt, ok, detail))
    return results


def main(argv: list[str]) -> int:
    keep = '--keep' in argv
    harnesses = [a for a in argv if a in SETUPS] or list(SETUPS)

    tmp = Path(tempfile.mkdtemp(prefix='topology-harness-'))
    failures = 0
    try:
        for harness in harnesses:
            root = tmp / harness
            root.mkdir()
            for prompt, ok, detail in run_harness(harness, root):
                failures += not ok
                print(f'{"PASS" if ok else "FAIL"}  {harness:6}  {prompt!r:22}  {detail}')
    finally:
        if keep:
            print(f'recordings kept in {tmp}')
        else:
            shutil.rmtree(tmp, ignore_errors=True)
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
