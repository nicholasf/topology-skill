import json
from unittest.mock import patch

import pytest

from topology.hooks_install import (
    GOOSE_BEGIN,
    GOOSE_END,
    HOOK_MARKER,
    PI_EXTENSION_SOURCE,
    add_claude_hooks,
    add_goose_block,
    build_goose_block,
    install,
    is_installed,
    parse_argv,
    remove_claude_hooks,
    strip_goose_block,
    target_path,
    uninstall,
)

OTHER_HOOK = {'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': 'echo other'}]}


def topology_commands(settings: dict) -> list[str]:
    return [
        h['command']
        for groups in settings.get('hooks', {}).values()
        for group in groups
        for h in group['hooks']
        if HOOK_MARKER in h['command']
    ]


# ── claude ───────────────────────────────────────────────────────────────────

def test_add_claude_hooks_adds_both_events_and_keeps_other_hooks():
    settings = add_claude_hooks({'model': 'opus', 'hooks': {'SessionStart': [OTHER_HOOK]}})
    assert settings['model'] == 'opus'
    assert OTHER_HOOK in settings['hooks']['SessionStart']
    assert len(settings['hooks']['SessionStart']) == 2
    assert 'hook prompt --format claude' in settings['hooks']['UserPromptSubmit'][0]['hooks'][0]['command']


def test_add_claude_hooks_is_idempotent():
    once = add_claude_hooks({})
    twice = add_claude_hooks(json.loads(json.dumps(once)))
    assert once == twice
    assert len(topology_commands(twice)) == 2


def test_remove_claude_hooks_removes_only_ours_and_prunes_empty_events():
    settings = add_claude_hooks({'hooks': {'SessionStart': [OTHER_HOOK]}})
    settings = remove_claude_hooks(settings)
    assert settings == {'hooks': {'SessionStart': [OTHER_HOOK]}}
    assert remove_claude_hooks(add_claude_hooks({})) == {}


def test_claude_install_writes_backup_and_uninstall_restores(tmp_path):
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps({'model': 'opus'}))
    install('claude', path)
    assert is_installed('claude', path)
    assert json.loads((tmp_path / 'settings.json.topology-backup').read_text()) == {'model': 'opus'}
    uninstall('claude', path)
    assert json.loads(path.read_text()) == {'model': 'opus'}
    assert not is_installed('claude', path)


# ── pi ───────────────────────────────────────────────────────────────────────

def test_pi_install_symlinks_extension_and_uninstall_removes_it(tmp_path):
    path = tmp_path / 'extensions' / 'topology.ts'
    install('pi', path)
    assert path.is_symlink() and path.resolve() == PI_EXTENSION_SOURCE
    install('pi', path)  # re-install replaces the symlink
    uninstall('pi', path)
    assert not path.exists()


def test_pi_install_refuses_to_replace_a_regular_file(tmp_path):
    path = tmp_path / 'topology.ts'
    path.write_text('mine')
    with pytest.raises(FileExistsError):
        install('pi', path)


# ── goose ────────────────────────────────────────────────────────────────────

def test_goose_block_round_trips_and_preserves_other_hints():
    block = build_goose_block('summary text')
    text = add_goose_block('my own hint\n', block)
    assert text.startswith('my own hint\n\n' + GOOSE_BEGIN)
    assert 'summary text' in text and text.rstrip().endswith(GOOSE_END)
    assert add_goose_block(text, block) == text
    assert strip_goose_block(text) == 'my own hint'


def test_goose_install_and_uninstall(tmp_path):
    path = tmp_path / '.goosehints'
    with patch('topology.hooks_install.load_topology', return_value={'machines': [{'name': 'pond'}]}), \
         patch('topology.hooks_install.load_playbooks_quietly', return_value={}):
        install('goose', path)
    assert is_installed('goose', path)
    assert 'Machines: pond' in path.read_text()
    uninstall('goose', path)
    assert not is_installed('goose', path)


# ── arguments & paths ────────────────────────────────────────────────────────

def test_parse_argv():
    assert parse_argv(['install', '--target', 'pi']) == ('install', 'pi', 'user')
    assert parse_argv(['status', '--scope', 'project']) == ('status', None, 'project')
    for bad in (['install'], ['install', '--target', 'vim'], ['frob']):
        with pytest.raises(ValueError):
            parse_argv(bad)


def test_target_path_by_scope(tmp_path):
    home, cwd = tmp_path / 'home', tmp_path / 'repo'
    assert target_path('claude', 'user', cwd, home) == home / '.claude' / 'settings.json'
    assert target_path('pi', 'project', cwd, home) == cwd / '.pi' / 'extensions' / 'topology.ts'
    assert target_path('goose', 'user', cwd, home) == home / '.config' / 'goose' / '.goosehints'
