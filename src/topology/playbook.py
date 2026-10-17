#!/usr/bin/env python3
"""
playbook.py — resolve a trigger phrase to a named, alias-tagged sequence of
host commands ("playbook") and run it.

Invoked via: /topology run "<phrase>" [--skip-oversight] [--var KEY=VALUE ...]

Playbooks live in $TOPOLOGIES_HOME as topology-playbook*.toml:
  topology-playbook-<node>.toml  — a playbook whose tasks all target one host
  topology-playbooks.toml        — a playbook composed from more than one host

A task's `hosts` is either a `name` from the topology.toml machines table
(resolved to `hostname`/`ssh_user` and run over SSH) or the reserved
`localhost`, which runs as a local subprocess with no SSH and no table
lookup. A task may instead be a `ref` to another playbook, which is expanded
in place (composition) — cyclic references are rejected.

A task's `command` may reference `${VAR}` or `${VAR:-default}` placeholders.
Values come from `--var KEY=VALUE` on the command line; a placeholder with no
default and no `--var` value is a hard error before anything runs. The
resolved bindings (value + whether it came from `--var` or a default) are
always printed before the plan, so a wrong value is caught by inspection, not
by watching the wrong thing run.

No LLM is involved in resolving a phrase or executing a task: alias matching
is exact/normalized string comparison, and every task is reviewed (full
flattened plan printed) before anything runs. A task can require individual
Y/N sign-off ("oversight") — set explicitly by the author or detected by a
keyword heuristic; explicit authoring always wins over the heuristic.
"""

import datetime
import difflib
import glob
import os
import re
import subprocess
import sys
import tempfile
import threading
import tomllib

from .discover import AGENT_SSH_USER
from .sync import get_topology_path
from .toml_io import read_toml, write_toml

HEURISTIC_OVERSIGHT_PATTERNS = ['restart', 'kill', 'stop', 'rm ', 'rm\t', 'drop', 'wipe']
VAR_PATTERN = re.compile(r'\$\{(\w+)(?::-([^}]*))?\}')


class PlaybookError(Exception):
    """A playbook file, reference, or invocation could not be resolved."""


# ── Discovery & parsing ─────────────────────────────────────────────────────

def get_topologies_home() -> str:
    return os.path.dirname(get_topology_path())


def list_playbook_files(skills_home: str) -> list[str]:
    """Return sorted topology-playbook*.toml files (node-scoped and shared)."""
    return sorted(glob.glob(os.path.join(skills_home, 'topology-playbook*.toml')))


def parse_playbook_file(path: str) -> list[dict]:
    """Parse one topology-playbook*.toml file into a list of playbook records."""
    try:
        with open(path, 'rb') as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise PlaybookError(f'{path}: invalid TOML — {e}') from e

    playbooks = []
    for entry in data.get('playbook', []):
        if 'name' not in entry:
            raise PlaybookError(f'{path}: a [[playbook]] entry is missing "name"')
        tasks = entry.get('tasks', [])
        for i, task in enumerate(tasks):
            _validate_task(task, entry['name'], i, path)
        playbooks.append({
            'name': entry['name'],
            'aliases': entry.get('aliases', []),
            'description': entry.get('description', ''),
            'tasks': tasks,
            'source': path,
        })
    return playbooks


def _validate_task(task: dict, playbook_name: str, index: int, path: str) -> None:
    where = f'{path}: playbook "{playbook_name}" task {index + 1}'
    is_ref = 'ref' in task
    is_command = 'hosts' in task and 'command' in task
    if is_ref and is_command:
        raise PlaybookError(f'{where}: has both "ref" and "hosts"/"command" — a task is one or the other')
    if not is_ref and not is_command:
        raise PlaybookError(f'{where}: must have "ref", or both "hosts" and "command"')


def normalize_phrase(s: str) -> str:
    return ' '.join(s.strip().lower().split())


def load_playbooks(skills_home: str) -> dict[str, dict]:
    """Glob and parse every playbook file, enforcing global name/alias uniqueness."""
    by_name: dict[str, dict] = {}
    alias_owner: dict[str, str] = {}

    for path in list_playbook_files(skills_home):
        for pb in parse_playbook_file(path):
            if pb['name'] in by_name:
                raise PlaybookError(
                    f'duplicate playbook name "{pb["name"]}" in {path} and {by_name[pb["name"]]["source"]}'
                )
            for alias in pb['aliases']:
                key = normalize_phrase(alias)
                if key in alias_owner:
                    raise PlaybookError(
                        f'alias "{alias}" is used by both "{alias_owner[key]}" and "{pb["name"]}"'
                    )
                alias_owner[key] = pb['name']
            by_name[pb['name']] = pb

    return by_name


# ── Alias resolution ─────────────────────────────────────────────────────────

def resolve_alias(phrase: str, playbooks: dict[str, dict]) -> str | None:
    target = normalize_phrase(phrase)
    for pb in playbooks.values():
        if target in (normalize_phrase(a) for a in pb['aliases']):
            return pb['name']
    return None


def near_miss_candidates(phrase: str, playbooks: dict[str, dict], limit: int = 5) -> list[str]:
    normalized_to_alias = {
        normalize_phrase(alias): alias
        for pb in playbooks.values()
        for alias in pb['aliases']
    }
    matches = difflib.get_close_matches(normalize_phrase(phrase), list(normalized_to_alias), n=limit, cutoff=0.0)
    return [normalized_to_alias[m] for m in matches]


# ── Oversight classification ─────────────────────────────────────────────────

def classify_oversight(task: dict) -> bool:
    """explicit_true > explicit_false > heuristic_match > default_false."""
    if 'oversight' in task:
        return bool(task['oversight'])
    command = task.get('command', '')
    return any(p in command.lower() for p in HEURISTIC_OVERSIGHT_PATTERNS)


# ── Machines table / host resolution ─────────────────────────────────────────

def load_machines_table() -> list[dict]:
    path = get_topology_path()
    if not os.path.exists(path):
        return []
    return read_toml(path).get('machines', [])


def resolve_remote_host(hosts: str, machines: list[dict]) -> tuple[str, str]:
    """Resolve a machines-table `name` to (hostname, ssh_user). Raises if not found."""
    for row in machines:
        if row.get('name') == hosts:
            hostname = row.get('hostname', '').strip() or hosts
            user = row.get('ssh_user', '').strip() or AGENT_SSH_USER
            return hostname, user
    raise PlaybookError(f'unknown host "{hosts}" — not "localhost" and not a name in the machines table')


# ── Flattening (composition + cycle detection) ───────────────────────────────

def _resolve_task(task: dict, machines: list[dict]) -> dict:
    hosts = task['hosts']
    if hosts == 'localhost':
        ssh_host, ssh_user = None, None
    else:
        ssh_host, ssh_user = resolve_remote_host(hosts, machines)
    return {
        'name': task.get('name', ''),
        'hosts': hosts,
        'command': task['command'],
        'oversight': classify_oversight(task),
        'ssh_host': ssh_host,
        'ssh_user': ssh_user,
    }


def flatten(name: str, playbooks: dict[str, dict], machines: list[dict], _stack: tuple[str, ...] = ()) -> list[dict]:
    if name not in playbooks:
        raise PlaybookError(f'unknown playbook "{name}"')
    if name in _stack:
        chain = ' -> '.join((*_stack, name))
        raise PlaybookError(f'cycle detected: {chain}')

    flat: list[dict] = []
    for task in playbooks[name]['tasks']:
        if 'ref' in task:
            flat.extend(flatten(task['ref'], playbooks, machines, (*_stack, name)))
        else:
            flat.append(_resolve_task(task, machines))
    return flat


# ── Variable substitution ────────────────────────────────────────────────────

def apply_variables(
    flat_tasks: list[dict], variables: dict[str, str]
) -> tuple[list[dict], dict[str, tuple[str, str]]]:
    """Substitute ${VAR}/${VAR:-default} in each task's command.

    Returns the substituted tasks plus a {name: (value, source)} map of every
    variable actually referenced, source being 'provided' or 'default', for
    reporting back to the user before execution. Raises PlaybookError on a
    placeholder with no --var value and no default.
    """
    bindings: dict[str, tuple[str, str]] = {}
    resolved_tasks = []
    for t in flat_tasks:
        command = t['command']
        for match in VAR_PATTERN.finditer(command):
            name, default = match.group(1), match.group(2)
            if name in variables:
                bindings[name] = (variables[name], 'provided')
            elif default is not None:
                bindings.setdefault(name, (default, 'default'))
            elif name not in bindings:
                label = t['name'] or command.strip().splitlines()[0]
                raise PlaybookError(
                    f'missing variable "{name}" on {t["hosts"]} ({label}) — '
                    f'pass --var {name}=... or give the placeholder a default'
                )
        resolved_tasks.append({**t, 'command': VAR_PATTERN.sub(lambda m: bindings[m.group(1)][0], command)})
    return resolved_tasks, bindings


def print_variables(bindings: dict[str, tuple[str, str]]) -> None:
    if not bindings:
        return
    print('Variables:')
    for name, (value, source) in sorted(bindings.items()):
        print(f'  {name} = {value}  ({source})')
    print()
    sys.stdout.flush()


# ── Review, execution ─────────────────────────────────────────────────────────

def print_review(flat_tasks: list[dict]) -> None:
    print('Plan:')
    for i, t in enumerate(flat_tasks, 1):
        marker = '  [requires oversight]' if t['oversight'] else ''
        label = t['name'] or t['command'].strip().splitlines()[0]
        print(f'  {i}. [{t["hosts"]}] {label}{marker}')
    print()
    # Python buffers stdout when it isn't a tty (piped/redirected output); without
    # an explicit flush here, a subprocess's own writes to the shared fd can appear
    # before this review — silently defeating the "review before execution" guarantee.
    sys.stdout.flush()


def confirm(prompt: str) -> bool:
    sys.stdout.flush()
    return input(f'{prompt} [y/N] ').strip().lower() in ('y', 'yes')


def run_captured(args: list[str]) -> subprocess.CompletedProcess:
    """Run args, echoing stdout/stderr live while also recording both for a failure report."""
    sys.stdout.flush()
    process = subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors='replace'
    )
    recorded: dict[str, list[str]] = {'stdout': [], 'stderr': []}

    def pump(stream, echo, key: str) -> None:
        for line in stream:
            echo.write(line)
            echo.flush()
            recorded[key].append(line)

    pumps = [
        threading.Thread(target=pump, args=(process.stdout, sys.stdout, 'stdout')),
        threading.Thread(target=pump, args=(process.stderr, sys.stderr, 'stderr')),
    ]
    for thread in pumps:
        thread.start()
    returncode = process.wait()
    for thread in pumps:
        thread.join()
    return subprocess.CompletedProcess(args, returncode, ''.join(recorded['stdout']), ''.join(recorded['stderr']))


def run_local(command: str) -> subprocess.CompletedProcess:
    return run_captured(['bash', '-c', command])


def run_remote(host: str, user: str, command: str) -> subprocess.CompletedProcess:
    return run_captured([
        'ssh',
        '-o', 'ConnectTimeout=5',
        '-o', 'BatchMode=yes',
        '-o', 'StrictHostKeyChecking=no',
        f'{user}@{host}', command,
    ])


# ── Failure reports ──────────────────────────────────────────────────────────

ERROR_REPORTS_DIRECTORY = 'error-reports'
REPORT_TIMESTAMP_FORMAT = '%Y-%m-%dT%H-%M-%S'
REPORT_FALLBACK_NAME = 'task'
REPORT_NAME_MAXIMUM_LENGTH = 80


def task_label(task: dict) -> str:
    """The task's name, or the first line of its command when it has none."""
    return task['name'] or task['command'].strip().splitlines()[0]


def safe_file_name(label: str) -> str:
    """Reduce a task label to characters that are safe in a file name."""
    cleaned = re.sub(r'[^A-Za-z0-9._-]+', '-', label).strip('-.')
    return cleaned[:REPORT_NAME_MAXIMUM_LENGTH].strip('-.') or REPORT_FALLBACK_NAME


def write_failure_report(
    task: dict, result: subprocess.CompletedProcess, playbook_name: str, now: datetime.datetime | None = None
) -> str | None:
    """Write a TOML report for a failed task into $TOPOLOGIES_HOME/error-reports/.

    The file is written to a temporary name in the same directory and then
    linked into place, so a half-written report never appears under its final
    name and an existing report is never overwritten (a counter is appended on
    a name collision). Returns the report path, or None if it could not be
    written, in which case the reason goes to stderr: a failure to report must
    not change the task's exit code.
    """
    now = now or datetime.datetime.now()
    timestamp = now.strftime(REPORT_TIMESTAMP_FORMAT)
    base_name = f'{safe_file_name(task_label(task))}-{timestamp}'
    report = {
        'playbook': playbook_name,
        'task': task_label(task),
        'hosts': task['hosts'],
        'command': task['command'],
        'exit_code': result.returncode,
        'timestamp': timestamp,
        'stdout': result.stdout or '',
        'stderr': result.stderr or '',
    }
    temporary_path = None
    try:
        directory = os.path.join(get_topologies_home(), ERROR_REPORTS_DIRECTORY)
        os.makedirs(directory, exist_ok=True)
        descriptor, temporary_path = tempfile.mkstemp(dir=directory, prefix='.report-', suffix='.tmp')
        os.close(descriptor)
        write_toml(report, temporary_path)
        counter = 0
        while True:
            suffix = f'-{counter}' if counter else ''
            final_path = os.path.join(directory, f'{base_name}{suffix}.toml')
            try:
                os.link(temporary_path, final_path)
                return final_path
            except FileExistsError:
                counter += 1
    except OSError as e:
        print(f'Could not write failure report: {e}', file=sys.stderr)
        return None
    finally:
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass


def execute(flat_tasks: list[dict], skip_oversight: bool = False, playbook_name: str = '') -> int:
    for t in flat_tasks:
        if t['oversight'] and not skip_oversight:
            label = t['name'] or t['command'].strip().splitlines()[0]
            if not confirm(f'Run on {t["hosts"]}: {label}?'):
                print('Skipped by user; stopping.')
                return 1

        if t['ssh_host'] is None:
            result = run_local(t['command'])
        else:
            result = run_remote(t['ssh_host'], t['ssh_user'], t['command'])

        if result.returncode != 0:
            print(f'Step failed on {t["hosts"]} (exit {result.returncode}); stopping.', file=sys.stderr)
            report_path = write_failure_report(t, result, playbook_name)
            if report_path is not None:
                print(f'Failure report written to {report_path}', file=sys.stderr)
            return result.returncode

    return 0


# ── list ──────────────────────────────────────────────────────────────────────

def print_list(playbooks: dict[str, dict]) -> None:
    if not playbooks:
        print('No playbooks found.')
        return
    for pb in sorted(playbooks.values(), key=lambda p: p['name']):
        print(pb['name'])
        if pb['description']:
            print(f'  {pb["description"]}')
        if pb['aliases']:
            print(f'  aliases: {", ".join(pb["aliases"])}')
        print(f'  source: {os.path.basename(pb["source"])}')
        print()


def list_main(argv: list[str]) -> None:
    try:
        playbooks = load_playbooks(get_topologies_home())
    except PlaybookError as e:
        print(f'Error: {e}', file=sys.stderr)
        sys.exit(1)
    print_list(playbooks)


PLAYBOOK_SUBCOMMANDS = {'list': list_main}


def playbook_main(argv: list[str]) -> None:
    if not argv or argv[0] not in PLAYBOOK_SUBCOMMANDS:
        print(f'Usage: topology playbook <{"|".join(PLAYBOOK_SUBCOMMANDS)}>', file=sys.stderr)
        sys.exit(1)
    subcommand, rest = argv[0], argv[1:]
    PLAYBOOK_SUBCOMMANDS[subcommand](rest)


USAGE = 'Usage: topology run "<phrase>" [--skip-oversight] [--var KEY=VALUE ...]'


def parse_argv(argv: list[str]) -> tuple[str, bool, dict[str, str]]:
    """Split argv into (phrase, skip_oversight, variables), pulling out flags."""
    skip_oversight = False
    variables: dict[str, str] = {}
    phrase_parts = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == '--skip-oversight':
            skip_oversight = True
        elif arg == '--var':
            i += 1
            if i >= len(argv) or '=' not in argv[i]:
                raise PlaybookError('--var requires KEY=VALUE')
            key, _, value = argv[i].partition('=')
            variables[key] = value
        else:
            phrase_parts.append(arg)
        i += 1
    return ' '.join(phrase_parts).strip(), skip_oversight, variables


# ── Main ──────────────────────────────────────────────────────────────────────

def main(argv: list[str]) -> None:
    try:
        phrase, skip_oversight, variables = parse_argv(argv)
    except PlaybookError as e:
        print(f'Error: {e}', file=sys.stderr)
        sys.exit(1)

    if not phrase:
        print(USAGE, file=sys.stderr)
        sys.exit(1)

    try:
        playbooks = load_playbooks(get_topologies_home())
    except PlaybookError as e:
        print(f'Error: {e}', file=sys.stderr)
        sys.exit(1)

    name = resolve_alias(phrase, playbooks)
    if name is None:
        print(f'No playbook matches "{phrase}".', file=sys.stderr)
        candidates = near_miss_candidates(phrase, playbooks)
        if candidates:
            print('Did you mean:', file=sys.stderr)
            for c in candidates:
                print(f'  - {c}', file=sys.stderr)
        sys.exit(1)

    try:
        flat = flatten(name, playbooks, load_machines_table())
        flat, bindings = apply_variables(flat, variables)
    except PlaybookError as e:
        print(f'Error: {e}', file=sys.stderr)
        sys.exit(1)

    print_variables(bindings)
    print_review(flat)
    sys.exit(execute(flat, skip_oversight=skip_oversight, playbook_name=name))


if __name__ == '__main__':
    main(sys.argv[1:])
