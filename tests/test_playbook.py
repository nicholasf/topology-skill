import datetime
import os
import tomllib
from unittest.mock import patch

import pytest

from topology.playbook import (
    AGENT_SSH_USER,
    PlaybookError,
    apply_variables,
    classify_oversight,
    execute,
    flatten,
    list_playbook_files,
    load_playbooks,
    near_miss_candidates,
    parse_argv,
    parse_playbook_file,
    playbook_main,
    print_list,
    resolve_alias,
    resolve_remote_host,
    run_captured,
    safe_file_name,
    write_failure_report,
)


# ── parsing ───────────────────────────────────────────────────────────────────

def write(tmp_path, name, content):
    path = tmp_path / name
    path.write_text(content)
    return str(path)


POND_TOML = """
[[playbook]]
name = "start-pond-qwen"
aliases = ["start pond's qwen model", "wake pond up"]
description = "Starts llama-server on pond with qwen3.8 loaded."

  [[playbook.tasks]]
  name = "start llama-server"
  hosts = "pond"
  command = "llama-server --port 9337 &"

  [[playbook.tasks]]
  name = "health check"
  hosts = "pond"
  command = "curl -s http://pond:9337/health"
  oversight = false
"""

LOCALHOST_TOML = """
[[playbook]]
name = "start-pi-agent-pond-qwen"
aliases = ["start pi against pond"]
description = "Starts the Pi coding agent locally, pointed at pond's qwen3.8."

  [[playbook.tasks]]
  name = "start pi agent"
  hosts = "localhost"
  command = "pi start --target pond"
"""

SHARED_TOML = """
[[playbook]]
name = "verify-pond-qwen-via-pi"
aliases = ["verify pi against pond", "wake pond up"]
description = "Ensures pond's qwen3.8 is running, then starts Pi locally against it."

  [[playbook.tasks]]
  name = "ensure qwen is running on pond"
  ref = "start-pond-qwen"

  [[playbook.tasks]]
  name = "start pi agent"
  ref = "start-pi-agent-pond-qwen"
"""


def test_parse_single_node_file(tmp_path):
    path = write(tmp_path, 'topology-playbook-pond.toml', POND_TOML)
    playbooks = parse_playbook_file(path)
    assert len(playbooks) == 1
    assert playbooks[0]['name'] == 'start-pond-qwen'
    assert playbooks[0]['aliases'] == ["start pond's qwen model", 'wake pond up']
    assert len(playbooks[0]['tasks']) == 2


def test_parse_multiple_playbooks_in_one_file(tmp_path):
    content = POND_TOML + LOCALHOST_TOML
    path = write(tmp_path, 'topology-playbook-mixed.toml', content)
    playbooks = parse_playbook_file(path)
    assert {p['name'] for p in playbooks} == {'start-pond-qwen', 'start-pi-agent-pond-qwen'}


def test_load_playbooks_merges_across_files(tmp_path):
    write(tmp_path, 'topology-playbook-pond.toml', POND_TOML)
    write(tmp_path, 'topology-playbook-localhost.toml', LOCALHOST_TOML)
    write(tmp_path, 'topology-playbooks.toml', SHARED_TOML.replace('wake pond up', 'verify all'))
    playbooks = load_playbooks(str(tmp_path))
    assert set(playbooks) == {'start-pond-qwen', 'start-pi-agent-pond-qwen', 'verify-pond-qwen-via-pi'}


def test_load_playbooks_ignores_unrelated_sidecars(tmp_path):
    write(tmp_path, 'topology-playbook-pond.toml', POND_TOML)
    write(tmp_path, 'topology-ask-agent.md', '# not a playbook file')
    assert list(load_playbooks(str(tmp_path))) == ['start-pond-qwen']


def test_list_playbook_files_sorted(tmp_path):
    write(tmp_path, 'topology-playbook-pond.toml', POND_TOML)
    write(tmp_path, 'topology-playbook-localhost.toml', LOCALHOST_TOML)
    files = list_playbook_files(str(tmp_path))
    assert files == sorted(files)
    assert len(files) == 2


def test_duplicate_alias_across_files_raises(tmp_path):
    write(tmp_path, 'topology-playbook-pond.toml', POND_TOML)
    write(tmp_path, 'topology-playbooks.toml', SHARED_TOML)  # shares "wake pond up" with POND_TOML
    with pytest.raises(PlaybookError) as exc:
        load_playbooks(str(tmp_path))
    assert 'start-pond-qwen' in str(exc.value)
    assert 'verify-pond-qwen-via-pi' in str(exc.value)


def test_duplicate_playbook_name_raises(tmp_path):
    write(tmp_path, 'topology-playbook-a.toml', POND_TOML)
    write(tmp_path, 'topology-playbook-b.toml', POND_TOML.replace('wake pond up', 'something else'))
    with pytest.raises(PlaybookError) as exc:
        load_playbooks(str(tmp_path))
    assert 'start-pond-qwen' in str(exc.value)


@pytest.mark.parametrize('bad_task', [
    '\n  [[playbook.tasks]]\n  ref = "x"\n  hosts = "pond"\n  command = "echo hi"\n',
    '\n  [[playbook.tasks]]\n  name = "nothing"\n',
])
def test_task_must_be_ref_xor_command(tmp_path, bad_task):
    content = '[[playbook]]\nname = "bad"\n' + bad_task
    path = write(tmp_path, 'topology-playbook-bad.toml', content)
    with pytest.raises(PlaybookError):
        parse_playbook_file(path)


# ── alias resolution ──────────────────────────────────────────────────────────

def test_resolve_alias_exact_and_normalized_match(tmp_path):
    playbooks = {p['name']: p for p in parse_playbook_file(write(tmp_path, 'topology-playbook-pond.toml', POND_TOML))}
    assert resolve_alias('wake pond up', playbooks) == 'start-pond-qwen'
    assert resolve_alias('  WAKE   pond UP  ', playbooks) == 'start-pond-qwen'


def test_resolve_alias_no_match_returns_none(tmp_path):
    playbooks = {p['name']: p for p in parse_playbook_file(write(tmp_path, 'topology-playbook-pond.toml', POND_TOML))}
    assert resolve_alias('do something unrelated', playbooks) is None


def test_near_miss_candidates_surfaces_close_alias(tmp_path):
    playbooks = {p['name']: p for p in parse_playbook_file(write(tmp_path, 'topology-playbook-pond.toml', POND_TOML))}
    candidates = near_miss_candidates('wake pnod up', playbooks)
    assert 'wake pond up' in candidates


# ── oversight classification ──────────────────────────────────────────────────

def test_oversight_explicit_true():
    assert classify_oversight({'hosts': 'pond', 'command': 'echo hi', 'oversight': True}) is True


def test_oversight_explicit_false_overrides_heuristic():
    assert classify_oversight({'hosts': 'pond', 'command': 'restart llama-server', 'oversight': False}) is False


def test_oversight_heuristic_match():
    assert classify_oversight({'hosts': 'pond', 'command': 'kill -9 1234'}) is True


def test_oversight_default_false():
    assert classify_oversight({'hosts': 'pond', 'command': 'echo hello'}) is False


# ── host resolution ────────────────────────────────────────────────────────────

def test_resolve_remote_host_uses_hostname_and_ssh_user():
    machines = [{'name': 'pond', 'hostname': 'pond.tailnet', 'ssh_user': 'someone'}]
    assert resolve_remote_host('pond', machines) == ('pond.tailnet', 'someone')


def test_resolve_remote_host_falls_back_to_agent_ssh_user():
    machines = [{'name': 'pond', 'hostname': 'pond.tailnet', 'ssh_user': ''}]
    assert resolve_remote_host('pond', machines) == ('pond.tailnet', AGENT_SSH_USER)


def test_resolve_remote_host_unknown_raises():
    with pytest.raises(PlaybookError):
        resolve_remote_host('nowhere', [{'name': 'pond', 'hostname': 'pond.tailnet'}])


# ── flatten (composition, cycle detection, cross-node) ────────────────────────

def test_flatten_cross_node_composition(tmp_path):
    write(tmp_path, 'topology-playbook-pond.toml', POND_TOML)
    write(tmp_path, 'topology-playbook-localhost.toml', LOCALHOST_TOML)
    write(tmp_path, 'topology-playbooks.toml', SHARED_TOML.replace('wake pond up', 'verify all'))
    playbooks = load_playbooks(str(tmp_path))
    machines = [{'name': 'pond', 'hostname': 'pond.tailnet', 'ssh_user': 'nicholasf'}]

    flat = flatten('verify-pond-qwen-via-pi', playbooks, machines)

    assert [t['hosts'] for t in flat] == ['pond', 'pond', 'localhost']
    assert flat[0]['ssh_host'] == 'pond.tailnet'
    assert flat[-1]['ssh_host'] is None


def test_flatten_unknown_playbook_raises():
    with pytest.raises(PlaybookError):
        flatten('does-not-exist', {}, [])


def test_flatten_direct_cycle_raises():
    playbooks = {'a': {'name': 'a', 'tasks': [{'ref': 'a'}]}}
    with pytest.raises(PlaybookError, match='cycle detected'):
        flatten('a', playbooks, [])


def test_flatten_transitive_cycle_raises():
    playbooks = {
        'a': {'name': 'a', 'tasks': [{'ref': 'b'}]},
        'b': {'name': 'b', 'tasks': [{'ref': 'a'}]},
    }
    with pytest.raises(PlaybookError, match='cycle detected'):
        flatten('a', playbooks, [])


# ── execute (review/oversight gating/fail-fast) ────────────────────────────────

def _task(hosts='localhost', oversight=False, ssh_host=None, ssh_user=None, command='echo hi'):
    return {'name': '', 'hosts': hosts, 'command': command, 'oversight': oversight,
             'ssh_host': ssh_host, 'ssh_user': ssh_user}


class _Result:
    def __init__(self, returncode, stdout='', stderr=''):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture(autouse=True)
def isolated_topologies_home(tmp_path, monkeypatch):
    """Failure reports go under $TOPOLOGIES_HOME; never let tests write to the real one."""
    home = tmp_path / 'topologies_home'
    home.mkdir()
    monkeypatch.setenv('TOPOLOGIES_HOME', str(home))
    return home


def test_execute_runs_local_when_ssh_host_none():
    with patch('topology.playbook.run_local', return_value=_Result(0)) as run_local, \
         patch('topology.playbook.run_remote', return_value=_Result(0)) as run_remote:
        assert execute([_task()]) == 0
        run_local.assert_called_once()
        run_remote.assert_not_called()


def test_execute_runs_remote_when_ssh_host_set():
    task = _task(hosts='pond', ssh_host='pond.tailnet', ssh_user='nicholasf')
    with patch('topology.playbook.run_local', return_value=_Result(0)) as run_local, \
         patch('topology.playbook.run_remote', return_value=_Result(0)) as run_remote:
        assert execute([task]) == 0
        run_remote.assert_called_once_with('pond.tailnet', 'nicholasf', task['command'])
        run_local.assert_not_called()


def test_execute_oversight_task_prompts_and_declining_stops():
    task = _task(oversight=True)
    with patch('topology.playbook.confirm', return_value=False) as confirm, \
         patch('topology.playbook.run_local') as run_local:
        assert execute([task]) == 1
        confirm.assert_called_once()
        run_local.assert_not_called()


def test_execute_oversight_task_confirmed_runs():
    task = _task(oversight=True)
    with patch('topology.playbook.confirm', return_value=True), \
         patch('topology.playbook.run_local', return_value=_Result(0)) as run_local:
        assert execute([task]) == 0
        run_local.assert_called_once()


def test_execute_non_oversight_task_never_prompts():
    task = _task(oversight=False)
    with patch('topology.playbook.confirm') as confirm, \
         patch('topology.playbook.run_local', return_value=_Result(0)):
        assert execute([task]) == 0
        confirm.assert_not_called()


def test_execute_skip_oversight_bypasses_prompt_but_still_runs():
    task = _task(oversight=True)
    with patch('topology.playbook.confirm') as confirm, \
         patch('topology.playbook.run_local', return_value=_Result(0)) as run_local:
        assert execute([task], skip_oversight=True) == 0
        confirm.assert_not_called()
        run_local.assert_called_once()


def test_execute_fails_fast_on_nonzero_exit():
    failing = _task(command='false')
    unreached = _task(command='echo should not run')
    with patch('topology.playbook.run_local', side_effect=[_Result(1), _Result(0)]) as run_local:
        assert execute([failing, unreached]) == 1
        run_local.assert_called_once()


# ── failure reports ────────────────────────────────────────────────────────────

NOW = datetime.datetime(2026, 10, 17, 9, 30, 15)


def _reports(home):
    directory = home / 'error-reports'
    return sorted(p for p in directory.iterdir()) if directory.exists() else []


def test_failed_task_writes_report_with_all_fields(isolated_topologies_home):
    task = {**_task(command='false'), 'name': 'restore database', 'hosts': 'seagull'}
    result = _Result(3, stdout='partial\nout', stderr='boom "quoted"\n')
    task = {**task, 'ssh_host': 'seagull', 'ssh_user': 'nicholasf'}
    with patch('topology.playbook.run_remote', return_value=result):
        assert execute([task], playbook_name='update-seagull-database') == 3

    [report_path] = _reports(isolated_topologies_home)
    assert report_path.name.startswith('restore-database-')
    report = tomllib.loads(report_path.read_text())
    assert report['playbook'] == 'update-seagull-database'
    assert report['task'] == 'restore database'
    assert report['hosts'] == 'seagull'
    assert report['command'] == 'false'
    assert report['exit_code'] == 3
    assert report['stdout'] == 'partial\nout'
    assert report['stderr'] == 'boom "quoted"\n'
    assert report_path.name == f'restore-database-{report["timestamp"]}.toml'


def test_successful_task_writes_no_report(isolated_topologies_home):
    with patch('topology.playbook.run_local', return_value=_Result(0)):
        assert execute([_task()]) == 0
    assert _reports(isolated_topologies_home) == []


def test_report_file_name_uses_timestamp_format(isolated_topologies_home):
    path = write_failure_report(_task(command='echo hi'), _Result(1), 'pb', now=NOW)
    assert os.path.basename(path) == 'echo-hi-2026-10-17T09-30-15.toml'


def test_report_creates_directory_when_missing(isolated_topologies_home):
    assert not (isolated_topologies_home / 'error-reports').exists()
    assert write_failure_report(_task(), _Result(1), 'pb', now=NOW) is not None
    assert (isolated_topologies_home / 'error-reports').is_dir()


def test_report_name_collision_adds_counter_and_never_overwrites(isolated_topologies_home):
    first = write_failure_report(_task(), _Result(1, stdout='first'), 'pb', now=NOW)
    second = write_failure_report(_task(), _Result(2, stdout='second'), 'pb', now=NOW)
    third = write_failure_report(_task(), _Result(3, stdout='third'), 'pb', now=NOW)
    assert len({first, second, third}) == 3
    assert tomllib.loads(open(first).read())['stdout'] == 'first'
    assert os.path.basename(second).endswith('-1.toml')
    assert os.path.basename(third).endswith('-2.toml')


def test_report_leaves_no_temporary_files(isolated_topologies_home):
    write_failure_report(_task(), _Result(1), 'pb', now=NOW)
    names = [p.name for p in _reports(isolated_topologies_home)]
    assert not any(n.endswith('.tmp') for n in names)


def test_report_write_failure_keeps_exit_code_and_reports_to_stderr(isolated_topologies_home, capsys):
    (isolated_topologies_home / 'error-reports').write_text('a file where the directory should be')
    with patch('topology.playbook.run_local', return_value=_Result(7)):
        assert execute([_task(command='false')]) == 7
    assert 'Could not write failure report' in capsys.readouterr().err


def test_unnamed_task_report_uses_first_command_line(isolated_topologies_home):
    task = _task(command='\n  systemctl restart thing\nsecond line')
    path = write_failure_report(task, _Result(1), 'pb', now=NOW)
    assert os.path.basename(path).startswith('systemctl-restart-thing-')
    assert tomllib.loads(open(path).read())['task'] == 'systemctl restart thing'


def test_safe_file_name_falls_back_when_nothing_usable():
    assert safe_file_name('///') == 'task'
    assert '/' not in safe_file_name('a/b c')


def test_run_captured_records_and_echoes_both_streams(capsys):
    result = run_captured(['bash', '-c', 'echo out; echo err >&2; exit 4'])
    captured = capsys.readouterr()
    assert (result.returncode, result.stdout, result.stderr) == (4, 'out\n', 'err\n')
    assert captured.out == 'out\n'
    assert captured.err == 'err\n'


# ── playbook list ──────────────────────────────────────────────────────────────

def test_print_list_empty(capsys):
    print_list({})
    assert 'No playbooks found.' in capsys.readouterr().out


def test_print_list_shows_name_description_aliases_source(tmp_path, capsys):
    path = write(tmp_path, 'topology-playbook-pond.toml', POND_TOML)
    playbooks = {p['name']: p for p in parse_playbook_file(path)}

    print_list(playbooks)
    out = capsys.readouterr().out

    assert 'start-pond-qwen' in out
    assert "Starts llama-server on pond with qwen3.8 loaded." in out
    assert "start pond's qwen model" in out and 'wake pond up' in out
    assert 'topology-playbook-pond.toml' in out


def test_print_list_sorted_by_name(tmp_path, capsys):
    content = POND_TOML + LOCALHOST_TOML
    path = write(tmp_path, 'topology-playbook-mixed.toml', content)
    playbooks = {p['name']: p for p in parse_playbook_file(path)}

    print_list(playbooks)
    out = capsys.readouterr().out

    assert out.index('start-pi-agent-pond-qwen') < out.index('start-pond-qwen')


def test_playbook_main_list_dispatches(tmp_path, capsys, monkeypatch):
    write(tmp_path, 'topology-playbook-pond.toml', POND_TOML)
    monkeypatch.setattr('topology.playbook.get_topologies_home', lambda: str(tmp_path))

    playbook_main(['list'])
    assert 'start-pond-qwen' in capsys.readouterr().out


# ── argv parsing ─────────────────────────────────────────────────────────────

def test_parse_argv_plain_phrase():
    assert parse_argv(['wake', 'pond', 'up']) == ('wake pond up', False, {})


def test_parse_argv_skip_oversight_flag():
    assert parse_argv(['wake', 'pond', 'up', '--skip-oversight']) == ('wake pond up', True, {})


def test_parse_argv_single_var():
    assert parse_argv(['start', 'pi', '--var', 'session=work']) == ('start pi', False, {'session': 'work'})


def test_parse_argv_multiple_vars_and_skip_oversight():
    phrase, skip, variables = parse_argv(
        ['start', 'pi', '--var', 'session=work', '--skip-oversight', '--var', 'window=main']
    )
    assert phrase == 'start pi'
    assert skip is True
    assert variables == {'session': 'work', 'window': 'main'}


def test_parse_argv_var_missing_value_raises():
    with pytest.raises(PlaybookError):
        parse_argv(['start', 'pi', '--var'])


def test_parse_argv_var_missing_equals_raises():
    with pytest.raises(PlaybookError):
        parse_argv(['start', 'pi', '--var', 'session'])


# ── variable substitution ──────────────────────────────────────────────────────

def test_apply_variables_uses_provided_value():
    tasks = [_task(command='tmux new-window -t "${session}" -n pi \'pi\'')]
    resolved, bindings = apply_variables(tasks, {'session': 'work'})
    assert resolved[0]['command'] == 'tmux new-window -t "work" -n pi \'pi\''
    assert bindings == {'session': ('work', 'provided')}


def test_apply_variables_uses_default_when_not_provided():
    tasks = [_task(command='tmux new-window -t "${session:-local}" -n pi \'pi\'')]
    resolved, bindings = apply_variables(tasks, {})
    assert resolved[0]['command'] == 'tmux new-window -t "local" -n pi \'pi\''
    assert bindings == {'session': ('local', 'default')}


def test_apply_variables_provided_overrides_default():
    tasks = [_task(command='echo ${session:-local}')]
    resolved, bindings = apply_variables(tasks, {'session': 'work'})
    assert resolved[0]['command'] == 'echo work'
    assert bindings == {'session': ('work', 'provided')}


def test_apply_variables_missing_no_default_raises():
    tasks = [_task(command='echo ${session}')]
    with pytest.raises(PlaybookError, match='session'):
        apply_variables(tasks, {})


def test_apply_variables_no_placeholders_is_noop():
    tasks = [_task(command='echo hi')]
    resolved, bindings = apply_variables(tasks, {})
    assert resolved[0]['command'] == 'echo hi'
    assert bindings == {}


def test_playbook_main_unknown_subcommand_exits_nonzero(capsys):
    with pytest.raises(SystemExit) as exc:
        playbook_main(['bogus'])
    assert exc.value.code != 0
    assert 'Usage' in capsys.readouterr().err


def test_playbook_main_no_args_exits_nonzero(capsys):
    with pytest.raises(SystemExit):
        playbook_main([])
