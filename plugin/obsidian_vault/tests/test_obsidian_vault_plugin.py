from __future__ import annotations

import importlib.util
import fcntl
import json
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]


def load_plugin():
    spec = importlib.util.spec_from_file_location('obsidian_vault_plugin_under_test', PLUGIN_DIR / '__init__.py')
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_tool_schemas_and_provider_name():
    mod = load_plugin()
    provider = mod.ObsidianVaultMemoryProvider()
    assert provider.name == 'obsidian_vault'
    names = {schema['name'] for schema in provider.get_tool_schemas()}
    assert names == {'obsidian_vault_search', 'obsidian_vault_read', 'obsidian_vault_capture'}


def test_is_available_does_not_touch_source_vault(tmp_path, monkeypatch):
    mod = load_plugin()
    home = tmp_path / 'home'
    vault = tmp_path / 'vault'
    home.mkdir()
    vault.mkdir()
    (vault / 'AGENTS.md').write_text('policy')
    (home / 'obsidian_vault.json').write_text(json.dumps({'vault_path': str(vault)}))
    monkeypatch.setenv('HERMES_HOME', str(home))
    original_exists = Path.exists

    def guarded_exists(path):
        if str(path).startswith(str(vault)):
            raise AssertionError('source-backed exists reached agent initialization')
        return original_exists(path)

    monkeypatch.setattr(Path, 'exists', guarded_exists)
    assert mod.ObsidianVaultMemoryProvider().is_available() is True


@pytest.mark.parametrize('term', ['AR', 'AP', 'PO', 'AI', 'HR', 'QA', 'UI', 'ML'])
def test_fts_query_preserves_approved_two_character_terms(term):
    mod = load_plugin()
    assert mod.ObsidianVaultMemoryProvider()._fts_query(term) == f'{term.lower()}*'


def test_fts_query_still_drops_unapproved_two_character_noise():
    mod = load_plugin()
    assert mod.ObsidianVaultMemoryProvider()._fts_query('to be') == '""'


def test_capture_path_resolution_runs_only_inside_worker(tmp_path, monkeypatch):
    mod = load_plugin()
    provider = mod.ObsidianVaultMemoryProvider()
    provider._vault = tmp_path
    provider._profile = 'default'
    provider._hermes_home = tmp_path / 'home'
    provider._config = {'capture_timeout_seconds': 2}
    provider._index_path = provider._hermes_home / 'state' / 'obsidian_vault' / 'index.sqlite3'
    (tmp_path / 'AGENTS.md').write_text('policy')

    def parent_resolution_forbidden(*_args, **_kwargs):
        raise AssertionError('source path resolution ran on request thread')

    monkeypatch.setattr(provider, '_resolve_vault_path', parent_resolution_forbidden)
    payload = json.loads(provider.handle_tool_call('obsidian_vault_capture', {
        'category': 'inbox',
        'content': 'Bounded worker capture.',
        'title': 'Bounded Worker Capture',
    }))
    assert payload['status'] == 'created'
    assert (tmp_path / payload['path']).exists()


def test_capture_timeout_is_bounded_and_cleans_payload(tmp_path, monkeypatch):
    mod = load_plugin()
    provider = mod.ObsidianVaultMemoryProvider()
    provider._vault = tmp_path
    provider._profile = 'default'
    provider._hermes_home = tmp_path / 'home'
    provider._config = {'capture_timeout_seconds': 0.1}
    provider._index_path = provider._hermes_home / 'state' / 'obsidian_vault' / 'index.sqlite3'
    (tmp_path / 'AGENTS.md').write_text('policy')

    def timed_out(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0] if args else 'worker', 0.1)

    monkeypatch.setattr(mod.subprocess, 'run', timed_out)
    before = time.monotonic()
    payload = json.loads(provider.handle_tool_call('obsidian_vault_capture', {
        'category': 'inbox',
        'content': 'Bounded timeout capture.',
        'title': 'Bounded Timeout Capture',
    }))
    elapsed = time.monotonic() - before
    assert 'timed out' in payload['error'].lower()
    assert elapsed < 0.5
    state_dir = provider._index_path.parent
    assert not list(state_dir.glob('.capture-*.json'))


def test_capture_log_failure_is_committed_and_exact_retry_is_idempotent(tmp_path, monkeypatch):
    mod = load_plugin()
    provider = mod.ObsidianVaultMemoryProvider()
    provider._vault = tmp_path
    provider._profile = 'default'
    provider._hermes_home = tmp_path / 'home'
    provider._config = {}
    provider._index_path = provider._hermes_home / 'state' / 'obsidian_vault' / 'index.sqlite3'
    (tmp_path / 'AGENTS.md').write_text('policy')
    args = {
        'category': 'inbox',
        'content': 'Committed capture must survive audit-log failure.',
        'title': 'Capture Commit Receipt',
        'slug': 'capture-commit-receipt',
    }
    original_append_log = provider._append_log

    def log_failed(*_args, **_kwargs):
        raise RuntimeError('synthetic log failure')

    monkeypatch.setattr(provider, '_append_log', log_failed)
    first = provider._capture_unbounded(args)
    assert first['status'] == 'created'
    assert first['warnings']
    assert provider._index_dirty is True
    target = tmp_path / first['path']
    original = target.read_text()
    receipts = list((provider._capture_state_dir() / 'capture_receipts').glob('*.json'))
    assert len(receipts) == 1
    receipts[0].unlink()  # Simulate worker death after source commit but before receipt durability.

    monkeypatch.setattr(provider, '_append_log', original_append_log)
    second = provider._capture_unbounded(args)
    assert second['status'] == 'created'
    assert second['deduplicated'] is True
    assert target.read_text() == original
    assert not list((target.parent / '_drafts').glob('capture-commit-receipt*.md'))


def test_partially_initialized_capture_keeps_worker_state_outside_vault(tmp_path):
    mod = load_plugin()
    provider = mod.ObsidianVaultMemoryProvider()
    provider._vault = tmp_path / 'vault'
    provider._vault.mkdir()
    provider._profile = 'default'
    provider._hermes_home = tmp_path / 'home'
    provider._config = {'capture_timeout_seconds': 2}
    provider._index_path = None
    (provider._vault / 'AGENTS.md').write_text('policy')

    result = json.loads(provider.handle_tool_call('obsidian_vault_capture', {
        'category': 'inbox',
        'content': 'Even partial initialization must keep state local.',
        'title': 'Partial Provider State Boundary',
    }))

    assert result['status'] == 'created'
    assert list((provider._hermes_home / 'state/obsidian_vault/capture_receipts').glob('*.json'))
    assert not (provider._vault / '.hermes-capture-state').exists()


def test_capture_worker_exact_retry_returns_same_receipt(tmp_path):
    mod = load_plugin()
    provider = mod.ObsidianVaultMemoryProvider()
    provider._vault = tmp_path
    provider._profile = 'default'
    provider._config = {'capture_timeout_seconds': 2}
    provider._index_path = tmp_path / 'state' / 'index.sqlite3'
    (tmp_path / 'AGENTS.md').write_text('policy')
    args = {
        'category': 'inbox',
        'content': 'Exact worker retries must not duplicate source bytes.',
        'title': 'Worker Retry Receipt',
        'slug': 'worker-retry-receipt',
    }

    first = json.loads(provider.handle_tool_call('obsidian_vault_capture', args))
    target = tmp_path / first['path']
    original = target.read_text()
    second = json.loads(provider.handle_tool_call('obsidian_vault_capture', args))

    assert first['operation_id'] == second['operation_id']
    assert second['deduplicated'] is True
    assert target.read_text() == original
    assert len(list((tmp_path / 'state' / 'capture_receipts').glob('*.json'))) == 1


def test_stale_capture_receipt_does_not_hide_missing_source_note(tmp_path):
    mod = load_plugin()
    provider = mod.ObsidianVaultMemoryProvider()
    provider._vault = tmp_path
    provider._profile = 'default'
    provider._config = {}
    provider._index_path = tmp_path / 'state' / 'index.sqlite3'
    (tmp_path / 'AGENTS.md').write_text('policy')
    args = {
        'category': 'inbox',
        'content': 'A stale receipt must be checked against durable source.',
        'title': 'Stale Receipt Check',
        'slug': 'stale-receipt-check',
    }

    first = provider._capture_unbounded(args)
    target = tmp_path / first['path']
    target.unlink()
    second = provider._capture_unbounded(args)

    assert second['status'] == 'created'
    assert 'deduplicated' not in second
    assert target.exists()


def test_capture_worker_requires_vault_policy_before_writing(tmp_path):
    mod = load_plugin()
    provider = mod.ObsidianVaultMemoryProvider()
    provider._vault = tmp_path
    provider._profile = 'default'
    provider._config = {'capture_timeout_seconds': 2}
    provider._index_path = tmp_path / 'state' / 'index.sqlite3'

    payload = json.loads(provider.handle_tool_call('obsidian_vault_capture', {
        'category': 'inbox',
        'content': 'This must not create an unmanaged vault.',
        'title': 'Missing Policy',
    }))
    assert 'AGENTS.md is missing' in payload['error']
    if (tmp_path / '10-RAW').exists():
        assert not list((tmp_path / '10-RAW').rglob('*.md'))


def test_capture_worker_respects_cross_process_lock(tmp_path):
    mod = load_plugin()
    provider = mod.ObsidianVaultMemoryProvider()
    provider._vault = tmp_path
    provider._profile = 'default'
    provider._hermes_home = tmp_path / 'home'
    provider._config = {'capture_timeout_seconds': 2}
    provider._index_path = tmp_path / 'state' / 'index.sqlite3'
    (tmp_path / 'AGENTS.md').write_text('policy')
    lock_root = provider._installation_home() / 'state' / 'obsidian_vault' / 'capture-locks'
    lock_path = lock_root / (mod.hashlib.sha256(str(tmp_path.resolve()).encode()).hexdigest() + '.lock')
    lock_path.parent.mkdir(parents=True)
    lock_file = lock_path.open('a+')
    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        payload = json.loads(provider.handle_tool_call('obsidian_vault_capture', {
            'category': 'inbox',
            'content': 'The held lock must reject this concurrent capture.',
            'title': 'Capture Lock Busy',
        }))
    finally:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()

    assert 'already running' in payload['error']
    assert not (tmp_path / '10-RAW/inbox/capture-lock-busy.md').exists()


def test_cross_profile_captures_share_one_vault_lock_and_preserve_updates(tmp_path):
    mod = load_plugin()
    vault = tmp_path / 'vault'
    root_home = tmp_path / 'hermes'
    (vault / '10-RAW/inbox').mkdir(parents=True)
    (vault / 'AGENTS.md').write_text('policy')
    target = vault / '10-RAW/inbox/shared-cross-profile.md'
    target.write_text('---\nstatus: draft\n---\n# Shared Cross Profile\n')

    providers = []
    for profile, home in (
        ('default', root_home),
        ('assistant', root_home / 'profiles/assistant'),
    ):
        provider = mod.ObsidianVaultMemoryProvider()
        provider._vault = vault
        provider._profile = profile
        provider._hermes_home = home
        provider._config = {'capture_timeout_seconds': 2}
        provider._index_path = home / 'state/obsidian_vault/index.sqlite3'
        providers.append(provider)

    lock_roots = {
        provider._installation_home() / 'state/obsidian_vault/capture-locks'
        for provider in providers
    }
    assert len(lock_roots) == 1

    args = [
        {
            'category': 'inbox',
            'content': 'DEFAULT_PROFILE_UPDATE_MUST_SURVIVE',
            'target_path': '10-RAW/inbox/shared-cross-profile.md',
        },
        {
            'category': 'inbox',
            'content': 'ASSISTANT_PROFILE_UPDATE_MUST_SURVIVE',
            'target_path': '10-RAW/inbox/shared-cross-profile.md',
        },
    ]
    barrier = threading.Barrier(2)
    outputs = [None, None]

    def capture(index):
        barrier.wait()
        outputs[index] = json.loads(
            providers[index].handle_tool_call('obsidian_vault_capture', args[index])
        )

    threads = [threading.Thread(target=capture, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    for index, output in enumerate(outputs):
        assert output is not None
        current = output
        if current.get('error'):
            assert 'already running' in current['error']
            current = json.loads(
                providers[index].handle_tool_call('obsidian_vault_capture', args[index])
            )
            outputs[index] = current
        assert current['status'] == 'drafted'

    final_text = target.read_text()
    assert 'DEFAULT_PROFILE_UPDATE_MUST_SURVIVE' not in final_text
    assert 'ASSISTANT_PROFILE_UPDATE_MUST_SURVIVE' not in final_text
    staged_notes = []
    for output in outputs:
        assert isinstance(output, dict)
        staged_notes.append((vault / output['draft_path']).read_text())
    staged_text = "\n".join(staged_notes)
    assert 'DEFAULT_PROFILE_UPDATE_MUST_SURVIVE' in staged_text
    assert 'ASSISTANT_PROFILE_UPDATE_MUST_SURVIVE' in staged_text


def test_capture_receipt_state_is_bounded_and_keeps_current_receipt(tmp_path):
    mod = load_plugin()
    provider = mod.ObsidianVaultMemoryProvider()
    receipt_dir = tmp_path / 'capture_receipts'
    receipt_dir.mkdir()
    for index in range(mod._MAX_CAPTURE_RECEIPTS + 8):
        path = receipt_dir / f'{index:04d}.json'
        path.write_text('{}')
        path.chmod(0o600)
    keep = receipt_dir / f'{mod._MAX_CAPTURE_RECEIPTS + 7:04d}.json'

    provider._prune_capture_receipts(receipt_dir, keep=keep)

    receipts = list(receipt_dir.glob('*.json'))
    assert len(receipts) == mod._MAX_CAPTURE_RECEIPTS
    assert keep in receipts


def test_capture_receipt_is_private_before_atomic_publish(tmp_path, monkeypatch):
    mod = load_plugin()
    provider = mod.ObsidianVaultMemoryProvider()
    provider._index_path = tmp_path / 'state/obsidian_vault/index.sqlite3'
    observed = {}
    original_replace = Path.replace

    def inspect_replace(self, target):
        observed['mode'] = self.stat().st_mode & 0o777
        return original_replace(self, target)

    monkeypatch.setattr(Path, 'replace', inspect_replace)
    provider._write_capture_receipt('a' * 32, {
        'status': 'created',
        'path': '10-RAW/inbox/private-receipt.md',
        '_capture_proofs': {'10-RAW/inbox/private-receipt.md': 'b' * 64},
    })
    assert observed['mode'] == 0o600


def test_capture_rejects_policy_files_and_model_claimed_approval(tmp_path):
    mod = load_plugin()
    provider = mod.ObsidianVaultMemoryProvider()
    provider._vault = tmp_path
    provider._profile = 'default'
    provider._hermes_home = tmp_path / 'home'
    provider._config = {'capture_timeout_seconds': 2}
    provider._index_path = provider._hermes_home / 'state/obsidian_vault/index.sqlite3'
    (tmp_path / 'AGENTS.md').write_text('policy')

    policy_attempt = json.loads(provider.handle_tool_call('obsidian_vault_capture', {
        'category': 'system',
        'content': 'This must never modify policy.',
        'target_path': 'AGENTS.md',
    }))
    assert 'write lane' in policy_attempt['error']
    assert (tmp_path / 'AGENTS.md').read_text() == 'policy'

    published = tmp_path / '20-WIKI/concepts/published-lock.md'
    published.parent.mkdir(parents=True)
    published.write_text('---\nstatus: published\n---\n# Published Lock\n')
    claimed = json.loads(provider.handle_tool_call('obsidian_vault_capture', {
        'category': 'concept',
        'content': 'Model-controlled approval must not edit the published source.',
        'target_path': '20-WIKI/concepts/published-lock.md',
        'explicit_user_approval': True,
    }))
    assert claimed['status'] == 'drafted'
    assert 'Model-controlled approval' not in published.read_text()


def test_capture_treats_commented_published_status_as_locked(tmp_path):
    mod = load_plugin()
    provider = mod.ObsidianVaultMemoryProvider()
    provider._vault = tmp_path
    provider._profile = 'default'
    provider._hermes_home = tmp_path / 'home'
    provider._config = {}
    provider._index_path = provider._hermes_home / 'state/obsidian_vault/index.sqlite3'
    (tmp_path / 'AGENTS.md').write_text('policy')
    target = tmp_path / '20-WIKI/concepts/commented-published.md'
    target.parent.mkdir(parents=True)
    before = '---\nstatus: published # locked canonical note\n---\n# Commented Published\n\nORIGINAL\n'
    target.write_text(before)

    result = provider._capture_unbounded({
        'category': 'concept',
        'content': 'COMMENTED_PUBLISHED_MUST_DRAFT',
        'target_path': '20-WIKI/concepts/commented-published.md',
    })

    assert result['status'] == 'drafted'
    assert target.read_text() == before
    assert 'COMMENTED_PUBLISHED_MUST_DRAFT' in (tmp_path / result['draft_path']).read_text()
    parsed = mod._frontmatter('---\ntitle: "Quoted # Value" # note\nstatus: published # locked\n---\n')
    assert parsed == {'title': 'Quoted # Value', 'status': 'published'}


def test_capture_rejects_google_api_key_shape(tmp_path):
    mod = load_plugin()
    provider = mod.ObsidianVaultMemoryProvider()
    provider._vault = tmp_path
    provider._profile = 'default'
    provider._hermes_home = tmp_path / 'home'
    provider._config = {}
    provider._index_path = provider._hermes_home / 'state/obsidian_vault/index.sqlite3'
    (tmp_path / 'AGENTS.md').write_text('policy')
    synthetic = 'AIza' + ('SYNTHETICONLY' * 3)[:35]

    with pytest.raises(ValueError, match='secret'):
        provider._capture_unbounded({
            'category': 'inbox',
            'content': f'Synthetic Google credential {synthetic}',
            'title': 'Google Secret Gate',
        })
    assert not (tmp_path / '10-RAW/inbox/google-secret-gate.md').exists()


@pytest.mark.parametrize('value', [
    '-----BEGIN ' + 'ENCRYPTED PRIVATE KEY-----',
    'gl' + 'pat-' + 'A' * 24,
    'SG.' + 'A' * 22 + '.' + 'B' * 43,
])
def test_capture_rejects_common_private_key_and_provider_token_shapes(tmp_path, value):
    mod = load_plugin()
    provider = mod.ObsidianVaultMemoryProvider()
    provider._vault = tmp_path
    provider._profile = 'default'
    provider._hermes_home = tmp_path / 'home'
    provider._config = {}
    provider._index_path = provider._hermes_home / 'state/obsidian_vault/index.sqlite3'
    (tmp_path / 'AGENTS.md').write_text('policy')

    with pytest.raises(ValueError, match='secret'):
        provider._capture_unbounded({
            'category': 'inbox',
            'content': f'Synthetic credential shape {value}',
            'title': 'Common Secret Gate',
        })
    assert not (tmp_path / '10-RAW/inbox/common-secret-gate.md').exists()


def test_existing_target_update_never_overwrites_concurrent_human_edit(tmp_path, monkeypatch):
    mod = load_plugin()
    provider = mod.ObsidianVaultMemoryProvider()
    provider._vault = tmp_path
    provider._profile = 'default'
    provider._hermes_home = tmp_path / 'home'
    provider._config = {}
    provider._index_path = provider._hermes_home / 'state/obsidian_vault/index.sqlite3'
    (tmp_path / 'AGENTS.md').write_text('policy')
    target = tmp_path / '10-RAW/inbox/human-race.md'
    target.parent.mkdir(parents=True)
    target.write_text('---\nstatus: draft\n---\n# Human Race\n\nBASE\n')
    original_read = provider._secure_read_rel
    injected = {'done': False}

    def raced_read(rel):
        text = original_read(rel)
        if rel == '10-RAW/inbox/human-race.md' and not injected['done']:
            injected['done'] = True
            with target.open('a') as destination:
                destination.write('\nHUMAN_CONCURRENT_EDIT_MUST_SURVIVE\n')
        return text

    monkeypatch.setattr(provider, '_secure_read_rel', raced_read)
    result = provider._capture_unbounded({
        'category': 'inbox',
        'content': 'HERMES_CAPTURE_MUST_STAGE',
        'target_path': '10-RAW/inbox/human-race.md',
    })

    assert injected['done'] is True
    assert result['status'] == 'drafted'
    assert 'HUMAN_CONCURRENT_EDIT_MUST_SURVIVE' in target.read_text()
    assert 'HERMES_CAPTURE_MUST_STAGE' not in target.read_text()
    assert 'HERMES_CAPTURE_MUST_STAGE' in (tmp_path / result['draft_path']).read_text()


def test_new_target_create_never_replaces_concurrent_human_note(tmp_path, monkeypatch):
    mod = load_plugin()
    provider = mod.ObsidianVaultMemoryProvider()
    provider._vault = tmp_path
    provider._profile = 'default'
    provider._hermes_home = tmp_path / 'home'
    provider._config = {}
    provider._index_path = provider._hermes_home / 'state/obsidian_vault/index.sqlite3'
    (tmp_path / 'AGENTS.md').write_text('policy')
    target = tmp_path / '10-RAW/inbox/create-race.md'
    target.parent.mkdir(parents=True)
    original_link = mod.os.link
    original_replace = mod.os.replace
    injected = {'done': False}

    def inject_human_target(dst):
        if dst == 'create-race.md' and not injected['done']:
            injected['done'] = True
            target.write_text('HUMAN_NEW_NOTE_MUST_SURVIVE\n')

    def raced_link(src, dst, *args, **kwargs):
        inject_human_target(dst)
        return original_link(src, dst, *args, **kwargs)

    def raced_replace(src, dst, *args, **kwargs):
        inject_human_target(dst)
        return original_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(mod.os, 'link', raced_link)
    monkeypatch.setattr(mod.os, 'replace', raced_replace)
    with pytest.raises(FileExistsError):
        provider._capture_unbounded({
            'category': 'inbox',
            'content': 'HERMES_NEW_CAPTURE_MUST_NOT_OVERWRITE',
            'slug': 'create-race',
        })

    assert injected['done'] is True
    assert target.read_text() == 'HUMAN_NEW_NOTE_MUST_SURVIVE\n'


@pytest.mark.parametrize('field,value', [
    ('content', 'Synthetic credential sk-proj-' + 'A' * 32),
    ('title', 'token=' + 'B' * 32),
    ('source', 'api_key=' + 'C' * 32),
])
def test_capture_secret_gate_covers_all_persisted_fields_and_project_keys(tmp_path, field, value):
    mod = load_plugin()
    provider = mod.ObsidianVaultMemoryProvider()
    provider._vault = tmp_path
    provider._profile = 'default'
    provider._hermes_home = tmp_path / 'home'
    provider._config = {'capture_timeout_seconds': 2}
    provider._index_path = provider._hermes_home / 'state/obsidian_vault/index.sqlite3'
    (tmp_path / 'AGENTS.md').write_text('policy')
    args = {
        'category': 'inbox',
        'content': 'Safe durable fact.',
        'title': 'Secret Gate Coverage',
        'source': 'hermes',
    }
    args[field] = value
    payload = json.loads(provider.handle_tool_call('obsidian_vault_capture', args))
    assert payload.get('error')
    assert 'secret' in payload['error'].lower() or field == 'source'
    assert not list((tmp_path / '10-RAW').rglob('*.md')) if (tmp_path / '10-RAW').exists() else True


def test_capture_payload_is_private_at_creation_and_drops_unbounded_unknown_fields(tmp_path, monkeypatch):
    mod = load_plugin()
    provider = mod.ObsidianVaultMemoryProvider()
    provider._vault = tmp_path / 'vault'
    provider._vault.mkdir()
    provider._profile = 'default'
    provider._hermes_home = tmp_path / 'home'
    provider._config = {'capture_timeout_seconds': 2}
    provider._index_path = provider._hermes_home / 'state/obsidian_vault/index.sqlite3'
    (provider._vault / 'AGENTS.md').write_text('policy')
    observed = {}

    def inspect_payload(command, **_kwargs):
        payload_path = Path(command[-1])
        observed['mode'] = payload_path.stat().st_mode & 0o777
        observed['size'] = payload_path.stat().st_size
        observed['payload'] = json.loads(payload_path.read_text())
        return subprocess.CompletedProcess(command, 0, json.dumps({'status': 'created', 'path': '10-RAW/inbox/test.md'}), '')

    monkeypatch.setattr(mod.subprocess, 'run', inspect_payload)
    result = json.loads(provider.handle_tool_call('obsidian_vault_capture', {
        'category': 'inbox',
        'content': 'Bounded private payload.',
        'title': 'Private Payload',
        'untrusted_extra': 'X' * 1_250_000,
    }))
    assert result['status'] == 'created'
    assert observed['mode'] == 0o600
    assert observed['size'] < 200_000
    assert 'untrusted_extra' not in observed['payload']['capture_args']


def test_secure_atomic_write_rejects_parent_symlink_swap(tmp_path):
    mod = load_plugin()
    provider = mod.ObsidianVaultMemoryProvider()
    provider._vault = tmp_path / 'vault'
    inbox = provider._vault / '10-RAW/inbox'
    outside = tmp_path / 'outside'
    inbox.mkdir(parents=True)
    outside.mkdir()
    inbox.rmdir()
    inbox.symlink_to(outside, target_is_directory=True)

    with pytest.raises((OSError, ValueError)):
        provider._atomic_write('10-RAW/inbox/escape.md', 'must stay confined')
    assert not (outside / 'escape.md').exists()


def test_capture_fails_closed_if_open_parent_is_renamed_before_publish(tmp_path, monkeypatch):
    mod = load_plugin()
    provider = mod.ObsidianVaultMemoryProvider()
    provider._vault = tmp_path / 'vault'
    provider._profile = 'default'
    provider._hermes_home = tmp_path / 'home'
    provider._config = {}
    provider._index_path = provider._hermes_home / 'state/obsidian_vault/index.sqlite3'
    inbox = provider._vault / '10-RAW/inbox'
    moved = tmp_path / 'outside/moved-inbox'
    inbox.mkdir(parents=True)
    moved.parent.mkdir()
    (provider._vault / 'AGENTS.md').write_text('policy')
    real_link = mod.os.link
    injected = {'done': False}

    def rename_parent_then_link(*args, **kwargs):
        if not injected['done']:
            injected['done'] = True
            inbox.rename(moved)
            inbox.mkdir()
        return real_link(*args, **kwargs)

    monkeypatch.setattr(mod.os, 'link', rename_parent_then_link)
    with pytest.raises(RuntimeError, match='capture parent changed during commit'):
        provider._capture_unbounded({
            'category': 'inbox',
            'content': 'RENAME_RACE_MUST_NOT_ESCAPE',
            'slug': 'rename-race',
        })

    assert injected['done'] is True
    assert not (inbox / 'rename-race.md').exists()
    assert not (moved / 'rename-race.md').exists()
    assert not list(moved.glob('.rename-race.md.tmp.*'))


def test_audit_log_fails_closed_if_vault_root_moves_during_append(tmp_path, monkeypatch):
    mod = load_plugin()
    provider = mod.ObsidianVaultMemoryProvider()
    vault = tmp_path / 'vault'
    moved = tmp_path / 'moved-vault'
    vault.mkdir()
    (vault / 'AGENTS.md').write_text('policy')
    provider._vault = vault
    provider._profile = 'default'
    provider._hermes_home = tmp_path / 'home'
    provider._config = {}
    provider._index_path = provider._hermes_home / 'state/obsidian_vault/index.sqlite3'
    real_open = mod.os.open
    injected = {'done': False}

    def rename_root_then_open(path, flags, mode=0o777, *, dir_fd=None):
        if path == 'log.md' and dir_fd is not None and not injected['done']:
            injected['done'] = True
            vault.rename(moved)
            vault.mkdir()
            (vault / 'AGENTS.md').write_text('replacement policy')
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(mod.os, 'open', rename_root_then_open)
    with pytest.raises(RuntimeError, match='capture parent changed during commit'):
        provider._append_log('10-RAW/inbox/x.md', 'created')

    assert injected['done'] is True
    assert not (vault / 'log.md').exists()
    assert not (moved / 'log.md').exists()


def test_audit_log_rolls_back_new_file_if_root_moves_after_open_before_write(tmp_path, monkeypatch):
    mod = load_plugin()
    provider = mod.ObsidianVaultMemoryProvider()
    vault = tmp_path / 'vault'
    moved = tmp_path / 'moved-vault'
    vault.mkdir()
    (vault / 'AGENTS.md').write_text('policy')
    provider._vault = vault
    provider._profile = 'default'
    provider._hermes_home = tmp_path / 'home'
    provider._config = {}
    provider._index_path = provider._hermes_home / 'state/obsidian_vault/index.sqlite3'
    real_write = mod.os.write
    injected = {'done': False}

    def rename_root_then_write(fd, data):
        if not injected['done']:
            injected['done'] = True
            vault.rename(moved)
            vault.mkdir()
            (vault / 'AGENTS.md').write_text('replacement policy')
        return real_write(fd, data)

    monkeypatch.setattr(mod.os, 'write', rename_root_then_write)
    with pytest.raises(RuntimeError, match='capture parent changed during commit'):
        provider._append_log('10-RAW/inbox/y.md', 'created')

    assert injected['done'] is True
    assert not (vault / 'log.md').exists()
    assert not (moved / 'log.md').exists()


def test_audit_log_restores_existing_bytes_if_root_moves_after_open_before_write(tmp_path, monkeypatch):
    mod = load_plugin()
    provider = mod.ObsidianVaultMemoryProvider()
    vault = tmp_path / 'vault'
    moved = tmp_path / 'moved-vault'
    vault.mkdir()
    (vault / 'AGENTS.md').write_text('policy')
    (vault / 'log.md').write_text('EXISTING AUDIT DATA\n')
    provider._vault = vault
    provider._profile = 'default'
    provider._hermes_home = tmp_path / 'home'
    provider._config = {}
    provider._index_path = provider._hermes_home / 'state/obsidian_vault/index.sqlite3'
    real_write = mod.os.write
    injected = {'done': False}

    def rename_root_then_write(fd, data):
        if not injected['done']:
            injected['done'] = True
            vault.rename(moved)
            vault.mkdir()
            (vault / 'AGENTS.md').write_text('replacement policy')
            (vault / 'log.md').write_text('CURRENT HUMAN LOG\n')
        return real_write(fd, data)

    monkeypatch.setattr(mod.os, 'write', rename_root_then_write)
    with pytest.raises(RuntimeError, match='capture parent changed during commit'):
        provider._append_log('10-RAW/inbox/z.md', 'created')

    assert injected['done'] is True
    assert (vault / 'log.md').read_text() == 'CURRENT HUMAN LOG\n'
    assert (moved / 'log.md').read_text() == 'EXISTING AUDIT DATA\n'


def test_audit_log_prewrite_cleanup_preserves_concurrent_foreign_append(tmp_path, monkeypatch):
    mod = load_plugin()
    provider = mod.ObsidianVaultMemoryProvider()
    vault = tmp_path / 'vault'
    moved = tmp_path / 'moved-vault'
    vault.mkdir()
    (vault / 'AGENTS.md').write_text('policy')
    provider._vault = vault
    provider._profile = 'default'
    provider._hermes_home = tmp_path / 'home'
    provider._config = {}
    provider._index_path = provider._hermes_home / 'state/obsidian_vault/index.sqlite3'
    original_require = provider._require_current_capture_parent
    calls = {'count': 0}

    def rename_root_and_foreign_append(rel, parent_fd):
        calls['count'] += 1
        if calls['count'] == 2:
            vault.rename(moved)
            vault.mkdir()
            (vault / 'AGENTS.md').write_text('replacement policy')
            with (moved / 'log.md').open('ab') as destination:
                destination.write(b'FOREIGN APPEND MUST SURVIVE\n')
        return original_require(rel, parent_fd)

    monkeypatch.setattr(provider, '_require_current_capture_parent', rename_root_and_foreign_append)
    with pytest.raises(RuntimeError, match='capture parent changed during commit'):
        provider._append_log('10-RAW/inbox/prewrite.md', 'created')

    assert calls['count'] == 2
    assert not (vault / 'log.md').exists()
    assert (moved / 'log.md').read_bytes() == b'FOREIGN APPEND MUST SURVIVE\n'


def test_capture_rollback_preserves_concurrent_human_edit(tmp_path):
    mod = load_plugin()
    provider = mod.ObsidianVaultMemoryProvider()
    provider._vault = tmp_path
    provider._profile = 'default'
    provider._hermes_home = tmp_path / 'home'
    provider._config = {}
    provider._index_path = provider._hermes_home / 'state/obsidian_vault/index.sqlite3'
    (tmp_path / 'AGENTS.md').write_text('policy')
    hub = tmp_path / '20-WIKI/concepts/README.md'
    hub.parent.mkdir(parents=True)
    hub.write_text('---\nstatus: published\n---\n# Concepts\n')
    target = tmp_path / '20-WIKI/concepts/concurrent-human-edit.md'

    def human_edit_then_fail(*_args):
        target.write_text(target.read_text() + '\nHUMAN EDIT MUST SURVIVE\n')
        raise OSError('synthetic queue failure after human edit')

    provider._emit_wiring_queue = human_edit_then_fail
    result = provider._capture_unbounded({
        'category': 'concept',
        'content': 'Promotion before a human edit.',
        'title': 'Concurrent Human Edit',
        'related_hub': '20-WIKI/concepts/README.md',
    })
    assert result['status'] == 'created_trusted_promote'
    assert result['wiring_queue_recorded'] is False
    assert any('wiring queue' in warning for warning in result['warnings'])
    assert target.exists()
    assert 'HUMAN EDIT MUST SURVIVE' in target.read_text()


def test_exact_retry_does_not_trust_marker_after_capture_content_removed(tmp_path):
    mod = load_plugin()
    provider = mod.ObsidianVaultMemoryProvider()
    provider._vault = tmp_path
    provider._profile = 'default'
    provider._hermes_home = tmp_path / 'home'
    provider._config = {}
    provider._index_path = provider._hermes_home / 'state/obsidian_vault/index.sqlite3'
    (tmp_path / 'AGENTS.md').write_text('policy')
    args = {
        'category': 'inbox',
        'content': 'PUBLIC_MUST_SURVIVE_RETRY',
        'title': 'Receipt Content Proof',
        'slug': 'receipt-content-proof',
    }
    first = provider._capture_unbounded(args)
    target = tmp_path / first['path']
    text = target.read_text()
    target.write_text(text.replace('PUBLIC_MUST_SURVIVE_RETRY', 'CONTENT_REMOVED'))

    second = provider._capture_unbounded(args)
    assert second.get('deduplicated') is not True
    assert second['status'] == 'drafted'
    assert 'PUBLIC_MUST_SURVIVE_RETRY' in (tmp_path / second['draft_path']).read_text()


def test_trusted_retry_stages_new_draft_if_published_payload_was_removed(tmp_path):
    mod = load_plugin()
    provider = mod.ObsidianVaultMemoryProvider()
    provider._vault = tmp_path
    provider._profile = 'default'
    provider._hermes_home = tmp_path / 'home'
    provider._config = {}
    provider._index_path = provider._hermes_home / 'state/obsidian_vault/index.sqlite3'
    (tmp_path / 'AGENTS.md').write_text('policy')
    hub = tmp_path / '20-WIKI/concepts/README.md'
    hub.parent.mkdir(parents=True)
    hub.write_text('---\nstatus: published\n---\n# Concepts\n')
    args = {
        'category': 'concept',
        'content': 'TRUSTED_PAYLOAD_MUST_SURVIVE_RETRY',
        'title': 'Trusted Payload Proof',
        'related_hub': '20-WIKI/concepts/README.md',
    }
    first = provider._capture_unbounded(args)
    target = tmp_path / first['path']
    target.write_text(
        target.read_text().replace('TRUSTED_PAYLOAD_MUST_SURVIVE_RETRY', 'CONTENT_REMOVED')
    )

    second = provider._capture_unbounded(args)
    assert second.get('deduplicated') is not True
    assert second['status'] == 'drafted'
    new_draft = tmp_path / second['draft_path']
    assert 'status: draft' in new_draft.read_text()
    assert 'TRUSTED_PAYLOAD_MUST_SURVIVE_RETRY' in new_draft.read_text()
    assert len(list((tmp_path / '20-WIKI/concepts/_drafts').glob('trusted-payload-proof-*.md'))) == 2


def test_capture_rejects_secret_like_content(tmp_path):
    mod = load_plugin()
    provider = mod.ObsidianVaultMemoryProvider()
    provider._vault = tmp_path
    provider._profile = 'default'
    provider._config = {}
    (tmp_path / 'AGENTS.md').write_text('policy')
    bad_value = ('to' + 'ken') + '=' + ('s' + 'k_') + ('test' * 6)
    out = provider.handle_tool_call('obsidian_vault_capture', {
        'category': 'concept',
        'content': bad_value + ' should not be stored',
        'title': 'bad secret',
    })
    assert 'secret' in out.lower()
    assert not list(tmp_path.rglob('bad-secret*.md'))


def test_capture_trusted_promote_creates_draft_and_published_note(tmp_path):
    mod = load_plugin()
    provider = mod.ObsidianVaultMemoryProvider()
    provider._vault = tmp_path
    provider._profile = 'default'
    provider._config = {}
    provider._index_path = tmp_path / 'state' / 'index.sqlite3'
    (tmp_path / 'AGENTS.md').write_text('policy')
    hub = tmp_path / '20-WIKI' / 'concepts' / 'README.md'
    hub.parent.mkdir(parents=True)
    hub.write_text('''---
status: published
---
# Concepts
''')
    payload = json.loads(provider.handle_tool_call('obsidian_vault_capture', {
        'category': 'concept',
        'content': '# Plugin Audit Test\n\nDurable plugin audit test fact.',
        'title': 'Plugin Audit Test',
        'slug': 'plugin-audit-test',
        'related_hub': '20-WIKI/concepts/README.md',
    }))
    assert payload['status'] == 'created_trusted_promote'
    published = (tmp_path / payload['path']).read_text()
    draft = (tmp_path / payload['draft_path']).read_text()
    assert '[[20-WIKI/concepts/README|Concepts]]' in published
    assert published.count('# Plugin Audit Test') == 1
    assert 'status: applied' in draft
    assert f"applied-to: {payload['path']}" in draft
    assert 'applied-by: hermes' in draft
    assert 'applied-at:' in draft
    assert payload['wiring_queue_recorded'] is True
    queue = (tmp_path / 'state' / 'wiring_queue.jsonl').read_text().splitlines()
    assert len(queue) == 1
    record = json.loads(queue[0])
    assert record['target_path'] == payload['path']
    assert record['related_hub'] == '20-WIKI/concepts/README.md'
    assert record['status'] == 'pending'
    assert (tmp_path / 'log.md').exists()
    assert ((tmp_path / payload['path']).stat().st_mode & 0o777) == 0o600
    assert ((tmp_path / payload['draft_path']).stat().st_mode & 0o777) == 0o600


def test_trusted_promotion_rejects_occupied_applied_draft_before_final_commit(tmp_path):
    mod = load_plugin()
    provider = mod.ObsidianVaultMemoryProvider()
    provider._vault = tmp_path
    provider._profile = 'default'
    provider._config = {}
    provider._index_path = tmp_path / 'state' / 'index.sqlite3'
    (tmp_path / 'AGENTS.md').write_text('policy')
    hub = tmp_path / '20-WIKI/concepts/README.md'
    hub.parent.mkdir(parents=True)
    hub.write_text('---\nstatus: published\n---\n# Concepts\n')
    args = {
        'category': 'concept',
        'content': 'Final must not commit before draft path proof.',
        'title': 'Draft Conflict',
        'slug': 'draft-conflict',
        'related_hub': '20-WIKI/concepts/README.md',
    }
    operation_id = provider._capture_operation_id(args)
    draft = tmp_path / '20-WIKI/concepts/_drafts' / f'draft-conflict-capture-{operation_id[:16]}.md'
    draft.parent.mkdir(parents=True)
    draft.write_text('---\nstatus: draft\n---\n# Human draft occupying deterministic path\n')

    with pytest.raises(ValueError, match='applied draft path is occupied'):
        provider._capture_unbounded(args, operation_id)

    assert not (tmp_path / '20-WIKI/concepts/draft-conflict.md').exists()
    assert 'Human draft occupying deterministic path' in draft.read_text()
    with pytest.raises(ValueError, match='applied draft path is occupied'):
        provider._capture_unbounded(args, operation_id)
    assert not (tmp_path / '20-WIKI/concepts/draft-conflict.md').exists()


def test_trusted_promotion_cleans_applied_draft_if_final_create_loses_race(tmp_path, monkeypatch):
    mod = load_plugin()
    provider = mod.ObsidianVaultMemoryProvider()
    provider._vault = tmp_path
    provider._profile = 'default'
    provider._config = {}
    provider._index_path = tmp_path / 'state' / 'index.sqlite3'
    (tmp_path / 'AGENTS.md').write_text('policy')
    hub = tmp_path / '20-WIKI/concepts/README.md'
    hub.parent.mkdir(parents=True)
    hub.write_text('---\nstatus: published\n---\n# Concepts\n')
    args = {
        'category': 'concept',
        'content': 'Concurrent final should not leave stale applied draft.',
        'title': 'Final Race',
        'slug': 'final-race',
        'related_hub': '20-WIKI/concepts/README.md',
    }
    target = tmp_path / '20-WIKI/concepts/final-race.md'
    original_atomic = provider._atomic_write

    def raced_final_write(rel, content, *, expect_absent=False):
        if rel == '20-WIKI/concepts/final-race.md':
            target.write_text('HUMAN_FINAL_MUST_SURVIVE\n')
        return original_atomic(rel, content, expect_absent=expect_absent)

    monkeypatch.setattr(provider, '_atomic_write', raced_final_write)
    with pytest.raises(FileExistsError):
        provider._capture_unbounded(args)

    assert target.read_text() == 'HUMAN_FINAL_MUST_SURVIVE\n'
    assert not list((tmp_path / '20-WIKI/concepts/_drafts').glob('final-race-capture-*.md'))


def test_capture_retry_recovers_missing_applied_snapshot_without_rewriting_source(tmp_path):
    mod = load_plugin()
    provider = mod.ObsidianVaultMemoryProvider()
    provider._vault = tmp_path
    provider._profile = 'default'
    provider._hermes_home = tmp_path / 'home'
    provider._config = {}
    provider._index_path = provider._hermes_home / 'state/obsidian_vault/index.sqlite3'
    (tmp_path / 'AGENTS.md').write_text('policy')
    hub = tmp_path / '20-WIKI/concepts/README.md'
    hub.parent.mkdir(parents=True)
    hub.write_text('---\nstatus: published\n---\n# Concepts\n')
    args = {
        'category': 'concept',
        'content': 'APPLIED_SNAPSHOT_RECOVERY_PAYLOAD',
        'title': 'Applied Snapshot Recovery',
        'related_hub': '20-WIKI/concepts/README.md',
    }

    first = provider._capture_unbounded(args)
    target = tmp_path / first['path']
    draft = tmp_path / first['draft_path']
    target_before = target.read_bytes()
    draft.unlink()

    second = provider._capture_unbounded(args)

    assert second['status'] == 'created_trusted_promote'
    assert second['deduplicated'] is True
    assert second['recovered'] is True
    assert target.read_bytes() == target_before
    assert 'status: applied' in draft.read_text()
    assert 'APPLIED_SNAPSHOT_RECOVERY_PAYLOAD' in draft.read_text()


def test_capture_requires_existing_active_related_hub_for_new_active_note(tmp_path):
    mod = load_plugin()
    provider = mod.ObsidianVaultMemoryProvider()
    provider._vault = tmp_path
    provider._profile = 'default'
    provider._config = {}
    provider._index_path = tmp_path / 'state' / 'index.sqlite3'
    (tmp_path / 'AGENTS.md').write_text('policy')

    missing = provider.handle_tool_call('obsidian_vault_capture', {
        'category': 'concept',
        'content': 'Missing hub must fail.',
        'title': 'Missing Hub',
    })
    assert 'related_hub is required' in missing

    draft_hub = tmp_path / '20-WIKI' / 'concepts' / '_drafts' / 'README.md'
    draft_hub.parent.mkdir(parents=True)
    draft_hub.write_text('---\nstatus: draft\n---\n# Draft Hub\n')
    rejected = provider.handle_tool_call('obsidian_vault_capture', {
        'category': 'concept',
        'content': 'Draft hub must fail.',
        'title': 'Draft Hub Target',
        'related_hub': '20-WIKI/concepts/_drafts/README.md',
    })
    assert '_drafts' in rejected
    assert not list(tmp_path.rglob('draft-hub-target*.md'))


def test_capture_rejects_target_path_inside_drafts(tmp_path):
    mod = load_plugin()
    provider = mod.ObsidianVaultMemoryProvider()
    provider._vault = tmp_path
    provider._profile = 'default'
    provider._config = {}
    provider._index_path = tmp_path / 'state' / 'index.sqlite3'
    (tmp_path / 'AGENTS.md').write_text('policy')
    hub = tmp_path / '20-WIKI' / 'concepts' / 'README.md'
    hub.parent.mkdir(parents=True)
    hub.write_text('---\nstatus: published\n---\n# Concepts\n')

    out = provider.handle_tool_call('obsidian_vault_capture', {
        'category': 'concept',
        'content': 'This must not be promoted into a draft lane.',
        'title': 'Nested Draft Regression',
        'target_path': '20-WIKI/concepts/_drafts/nested-draft-regression.md',
        'related_hub': '20-WIKI/concepts/README.md',
    })
    assert 'target_path cannot contain _drafts' in out
    assert not list(tmp_path.rglob('nested-draft-regression*.md'))


def test_capture_canonicalizes_target_before_trusted_lane_policy(tmp_path):
    mod = load_plugin()
    provider = mod.ObsidianVaultMemoryProvider()
    provider._vault = tmp_path
    provider._profile = 'default'
    provider._config = {'private_profiles': ['default']}  # granted profile: private stays unwritable
    provider._index_path = tmp_path / 'state' / 'index.sqlite3'
    (tmp_path / 'AGENTS.md').write_text('policy')
    hub = tmp_path / '20-WIKI' / 'concepts' / 'README.md'
    hub.parent.mkdir(parents=True)
    hub.write_text('---\nstatus: published\n---\n# Concepts\n')

    for target_path in (
        '20-WIKI/concepts/../beta/traversal-beta-escape.md',
        '20-WIKI/concepts/../../private/traversal-personal-escape.md',
    ):
        out = provider.handle_tool_call('obsidian_vault_capture', {
            'category': 'concept',
            'content': 'Traversal must not bypass the trusted lane policy.',
            'title': 'Traversal Escape',
            'target_path': target_path,
            'related_hub': '20-WIKI/concepts/README.md',
        })
        assert 'outside the approved trusted-promotion lanes' in out

    assert not (tmp_path / '20-WIKI/beta/traversal-beta-escape.md').exists()
    assert not (tmp_path / 'private/traversal-personal-escape.md').exists()


def test_capture_other_category_cannot_direct_write_explicit_non_inbox_target(tmp_path):
    mod = load_plugin()
    provider = mod.ObsidianVaultMemoryProvider()
    provider._vault = tmp_path
    provider._profile = 'default'
    provider._config = {'private_profiles': ['default']}  # granted profile: private stays unwritable
    provider._index_path = tmp_path / 'state' / 'index.sqlite3'
    (tmp_path / 'AGENTS.md').write_text('policy')

    out = provider.handle_tool_call('obsidian_vault_capture', {
        'category': 'other',
        'content': 'Explicit targets do not inherit direct-write authority from category.',
        'title': 'Other Category Escape',
        'target_path': 'private/other-category-escape.md',
    })
    assert 'outside the approved trusted-promotion lanes' in out
    assert not (tmp_path / 'private/other-category-escape.md').exists()


def test_capture_rejects_personal_related_hub_and_peer(tmp_path):
    mod = load_plugin()
    provider = mod.ObsidianVaultMemoryProvider()
    provider._vault = tmp_path
    provider._profile = 'default'
    provider._config = {'private_profiles': ['default']}  # granted profile: private stays unwritable
    provider._index_path = tmp_path / 'state' / 'index.sqlite3'
    (tmp_path / 'AGENTS.md').write_text('policy')
    public_hub = tmp_path / '20-WIKI' / 'concepts' / 'README.md'
    public_hub.parent.mkdir(parents=True)
    public_hub.write_text('---\nstatus: published\n---\n# Concepts\n')
    private_hub = tmp_path / 'private' / 'private-hub.md'
    private_hub.parent.mkdir(parents=True)
    private_hub.write_text('---\nstatus: published\n---\n# Private Hub\n')

    private_hub_out = provider.handle_tool_call('obsidian_vault_capture', {
        'category': 'concept',
        'content': 'Public notes must not expose personal hub paths.',
        'title': 'Private Hub Leak',
        'related_hub': 'private/private-hub.md',
    })
    assert 'must reference an active 20-WIKI note' in private_hub_out

    private_peer_out = provider.handle_tool_call('obsidian_vault_capture', {
        'category': 'concept',
        'content': 'Public notes must not expose personal peer paths.',
        'title': 'Private Peer Leak',
        'related_hub': '20-WIKI/concepts/README.md',
        'related': ['private/private-hub.md'],
    })
    assert 'must reference an active 20-WIKI note' in private_peer_out
    assert not list((tmp_path / '20-WIKI/concepts').glob('private-*-leak.md'))


def test_capture_inbox_still_works_without_related_hub(tmp_path):
    mod = load_plugin()
    provider = mod.ObsidianVaultMemoryProvider()
    provider._vault = tmp_path
    provider._profile = 'default'
    provider._config = {}
    provider._index_path = tmp_path / 'state' / 'index.sqlite3'
    (tmp_path / 'AGENTS.md').write_text('policy')

    payload = json.loads(provider.handle_tool_call('obsidian_vault_capture', {
        'category': 'inbox',
        'content': 'Needs later classification.',
        'title': 'Inbox Capture',
    }))
    assert payload['status'] == 'created'
    assert (tmp_path / payload['path']).exists()


def test_capture_rejects_new_note_outside_approved_trusted_promotion_lanes(tmp_path):
    mod = load_plugin()
    provider = mod.ObsidianVaultMemoryProvider()
    provider._vault = tmp_path
    provider._profile = 'default'
    provider._config = {}
    provider._index_path = tmp_path / 'state' / 'index.sqlite3'
    (tmp_path / 'AGENTS.md').write_text('policy')
    hub = tmp_path / '20-WIKI' / 'alpha' / 'README.md'
    hub.parent.mkdir(parents=True)
    hub.write_text('---\nstatus: published\n---\n# Alpha\n')

    out = provider.handle_tool_call('obsidian_vault_capture', {
        'category': 'concept',
        'content': 'The owning profile must publish this.',
        'title': 'Wrong Trusted Lane',
        'target_path': '20-WIKI/alpha/wrong-trusted-lane.md',
        'related_hub': '20-WIKI/alpha/README.md',
    })
    assert 'outside the approved trusted-promotion lanes' in out
    assert not list(tmp_path.rglob('wrong-trusted-lane*.md'))


def test_capture_retains_committed_target_for_exact_retry_when_wiring_queue_fails(tmp_path):
    mod = load_plugin()
    provider = mod.ObsidianVaultMemoryProvider()
    provider._vault = tmp_path
    provider._profile = 'default'
    provider._config = {}
    provider._index_path = tmp_path / 'state' / 'index.sqlite3'
    (tmp_path / 'AGENTS.md').write_text('policy')
    hub = tmp_path / '20-WIKI' / 'concepts' / 'README.md'
    hub.parent.mkdir(parents=True)
    hub.write_text('---\nstatus: published\n---\n# Concepts\n')

    def fail_queue(*_args):
        raise OSError('simulated queue failure')

    provider._emit_wiring_queue = fail_queue
    first = provider._capture_unbounded({
        'category': 'concept',
        'content': 'This promotion must roll back.',
        'title': 'Queue Failure Rollback',
        'related_hub': '20-WIKI/concepts/README.md',
    })
    assert first['status'] == 'created_trusted_promote'
    assert first['wiring_queue_recorded'] is False
    assert any('wiring queue' in warning for warning in first['warnings'])
    target = tmp_path / '20-WIKI/concepts/queue-failure-rollback.md'
    assert target.exists()
    drafts = list((tmp_path / '20-WIKI/concepts/_drafts').glob('queue-failure-rollback-*.md'))
    assert len(drafts) == 1
    assert 'status: applied' in drafts[0].read_text()

    provider._emit_wiring_queue = lambda *_args: True
    recovered = provider._capture_unbounded({
        'category': 'concept',
        'content': 'This promotion must roll back.',
        'title': 'Queue Failure Rollback',
        'related_hub': '20-WIKI/concepts/README.md',
    })
    assert recovered['recovered'] is True
    assert recovered['deduplicated'] is True
    assert len(list((tmp_path / '20-WIKI/concepts').glob('queue-failure-rollback.md'))) == 1
    assert len(list((tmp_path / '20-WIKI/concepts/_drafts').glob('queue-failure-rollback-*.md'))) == 1


def test_public_queue_failure_returns_committed_warning_and_exact_retry_resumes(tmp_path):
    mod = load_plugin()
    provider = mod.ObsidianVaultMemoryProvider()
    provider._vault = tmp_path
    provider._profile = 'default'
    provider._hermes_home = tmp_path / 'home'
    provider._config = {'capture_timeout_seconds': 2}
    provider._index_path = provider._hermes_home / 'state/obsidian_vault/index.sqlite3'
    (tmp_path / 'AGENTS.md').write_text('policy')
    hub = tmp_path / '20-WIKI/concepts/README.md'
    hub.parent.mkdir(parents=True)
    hub.write_text('---\nstatus: published\n---\n# Concepts\n')
    queue_path = provider._index_path.parent / 'wiring_queue.jsonl'
    queue_path.mkdir(parents=True)
    args = {
        'category': 'concept',
        'content': 'PUBLIC_QUEUE_FAILURE_MUST_BE_UNAMBIGUOUS',
        'title': 'Public Queue Failure Outcome',
        'related_hub': '20-WIKI/concepts/README.md',
    }

    first = json.loads(provider.handle_tool_call('obsidian_vault_capture', args))

    assert first['status'] == 'created_trusted_promote'
    assert first['wiring_queue_recorded'] is False
    assert any('exact retry' in warning for warning in first['warnings'])
    assert 'PUBLIC_QUEUE_FAILURE_MUST_BE_UNAMBIGUOUS' in (tmp_path / first['path']).read_text()
    assert 'status: applied' in (tmp_path / first['draft_path']).read_text()

    queue_path.rmdir()
    second = json.loads(provider.handle_tool_call('obsidian_vault_capture', args))

    assert second['status'] == 'created_trusted_promote'
    assert second['wiring_queue_recorded'] is True
    assert second['deduplicated'] is True
    assert second['recovered'] is True
    assert queue_path.is_file()
    assert len(queue_path.read_text().splitlines()) == 1


def test_search_indexes_frontmatter_aliases_tags_and_links(tmp_path):
    mod = load_plugin()
    provider = mod.ObsidianVaultMemoryProvider()
    provider._vault = tmp_path
    provider._profile = 'default'
    provider._config = {'refresh_seconds': 0}
    provider._index_path = tmp_path / 'state' / 'index.sqlite3'
    notes = tmp_path / '20-WIKI' / 'concepts'
    notes.mkdir(parents=True)
    (tmp_path / 'AGENTS.md').write_text('policy')
    (notes / 'ops-hub.md').write_text('''---
status: published
source: hermes
tags: [hub, automation]
aliases:
  - Ops Memory Hub
updated: 2026-07-09
---
# Ops Hub
Links to [[memory-target|target display]].
''')
    (notes / 'memory-target.md').write_text('''---
status: published
source: owner
tags:
  - memory-provider
aliases:
  - Queue Flow
updated: 2026-07-08
---
# Memory Target
This note describes durable retrieval behavior.
''')

    payload = json.loads(provider.handle_tool_call('obsidian_vault_search', {'query': 'queue flow', 'limit': 3}))
    assert payload['count'] >= 1
    target = next(r for r in payload['results'] if r['path'] == '20-WIKI/concepts/memory-target.md')
    assert 'Queue Flow' in target['metadata']['aliases']
    assert 'memory-provider' in target['metadata']['tags']
    assert target['metadata']['status'] == 'published'
    assert target['metadata']['source'] == 'owner'
    assert any(b['path'] == '20-WIKI/concepts/ops-hub.md' for b in target['backlinks'])

    hub_payload = json.loads(provider.handle_tool_call('obsidian_vault_search', {'query': 'ops memory hub', 'limit': 3}))
    hub = next(r for r in hub_payload['results'] if r['path'] == '20-WIKI/concepts/ops-hub.md')
    assert any(link['path'] == '20-WIKI/concepts/memory-target.md' for link in hub['links'])

    hyphen_payload = json.loads(provider.handle_tool_call('obsidian_vault_search', {'query': 'memory-provider', 'limit': 3}))
    assert hyphen_payload['count'] >= 1


def test_prefetch_includes_metadata_and_backlinks(tmp_path):
    mod = load_plugin()
    provider = mod.ObsidianVaultMemoryProvider()
    provider._vault = tmp_path
    provider._profile = 'default'
    provider._config = {'refresh_seconds': 0, 'max_prefetch_results': 2}
    provider._index_path = tmp_path / 'state' / 'index.sqlite3'
    notes = tmp_path / '20-WIKI' / 'concepts'
    notes.mkdir(parents=True)
    (tmp_path / 'AGENTS.md').write_text('policy')
    (notes / 'hub.md').write_text('# Hub\n[[retrieval-note]]\n')
    (notes / 'retrieval-note.md').write_text('''---
status: published
tags: [memory-provider]
updated: 2026-07-09
---
# Retrieval Note
Searchable retrieval anchor.
''')

    context = provider.prefetch('retrieval anchor')
    assert 'status=published' in context
    assert 'updated=2026-07-09' in context
    assert 'tags=memory-provider' in context
    assert 'backlinks=20-WIKI/concepts/hub.md' in context


def _indexed_provider(mod, tmp_path, *, content='stable retrieval anchor'):
    provider = mod.ObsidianVaultMemoryProvider()
    provider._vault = tmp_path
    provider._profile = 'default'
    provider._config = {'refresh_seconds': 600, 'initial_wait_seconds': 0.05}
    provider._index_path = tmp_path / 'state' / 'index.sqlite3'
    notes = tmp_path / '20-WIKI' / 'concepts'
    notes.mkdir(parents=True, exist_ok=True)
    (tmp_path / 'AGENTS.md').write_text('policy')
    (notes / 'stable.md').write_text(f'# Stable\n{content}\n')
    result = provider._build_index_database(provider._index_path)
    assert result['indexed_files'] >= 2
    return provider


def _retrieval_quality_provider(tmp_path, monkeypatch, notes):
    """Synthetic schema-v3 index only: no source vault is created or read."""
    mod = load_plugin()
    provider = mod.ObsidianVaultMemoryProvider()
    provider._profile = 'default'
    provider._config = {}
    provider._vault = tmp_path / 'nonexistent-source'
    provider._index_path = tmp_path / 'quality.sqlite3'
    with sqlite3.connect(provider._index_path) as conn:
        provider._create_index_tables(conn)
        for path, text in notes.items():
            meta = mod._note_metadata(path, text)
            conn.execute('INSERT INTO vault_fts VALUES (?, ?, ?, ?, ?)',
                         (path, meta['title'], '', '', text))
            conn.execute('INSERT INTO note_meta VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                         (path, meta['slug'], meta['title'], '[]', '[]', meta['status'],
                          meta['source'], meta['updated'], meta['created'], '[]', 0, 0))
            conn.executemany('INSERT INTO note_links VALUES (?, ?, ?, ?, ?)',
                             [(path, x['target'], x['target_slug'], x['display'], x['is_embed'])
                              for x in meta['links']])
    monkeypatch.setattr(provider, '_ensure_index', lambda: True)
    def forbidden(*args, **kwargs):
        raise AssertionError('query touched source filesystem')
    monkeypatch.setattr(provider, '_resolve_vault_path', forbidden)
    monkeypatch.setattr(provider, '_rel', forbidden)
    return provider


@pytest.mark.parametrize('setting, expected_count', [(None, 1), (0, 0), (4, 1)])
def test_retrieval_quality_related_limit_compatibility(tmp_path, monkeypatch, setting, expected_count):
    provider = _retrieval_quality_provider(tmp_path, monkeypatch, {
        '20-WIKI/source.md': '# Source\nneedle [[20-WIKI/target]]\n',
        '20-WIKI/target.md': '# Target\nneedle\n',
    })
    provider._config['max_related_notes'] = setting
    rows = provider._search('needle')
    source = next(row for row in rows if row['path'] == '20-WIKI/source.md')
    assert len(source['links']) == expected_count


@pytest.mark.parametrize('target, expected', [
    ('20-WIKI/foo/README', '20-WIKI/foo/README.md'),
    ('20-WIKI/foo/README.md#Details', '20-WIKI/foo/README.md'),
    ('20-WIKI/foo/README^block', '20-WIKI/foo/README.md'),
    ('./README', '20-WIKI/source/README.md'),
    ('../foo/README', '20-WIKI/foo/README.md'),
    ('README', '20-WIKI/source/README.md'),
    ('unique', '20-WIKI/else/unique.md'),
    ('20-WIKI/missing/README', None),
    ('./missing/README', None),
    ('ambiguous', None),
    ('../../../20-WIKI/foo/README', None),
    ('/20-WIKI/foo/README', None),
])
def test_retrieval_quality_resolves_real_edges(tmp_path, monkeypatch, target, expected):
    source = '20-WIKI/source/start.md'
    provider = _retrieval_quality_provider(tmp_path, monkeypatch, {
        source: f'# Start\n![[{target}|Label]]\n',
        '20-WIKI/foo/README.md': '# Intended',
        '20-WIKI/README.md': '# Wrong root',
        '20-WIKI/inventory/README.md': '# Wrong inventory',
        '20-WIKI/source/README.md': '# Sibling',
        '20-WIKI/else/unique.md': '# Unique',
        '20-WIKI/foo/ambiguous.md': '# Ambiguous one',
        '20-WIKI/else/ambiguous.md': '# Ambiguous two',
    })
    with provider._conn(readonly=True) as conn:
        links, _ = provider._related_for_path(conn, source)
        assert len(links) == 1
        assert links[0]['path'] == expected
        assert links[0]['display'] == 'Label' and links[0]['embed'] is True
        for candidate in ['20-WIKI/foo/README.md', '20-WIKI/README.md',
                          '20-WIKI/inventory/README.md', '20-WIKI/source/README.md',
                          '20-WIKI/else/unique.md', '20-WIKI/foo/ambiguous.md']:
            _, backlinks = provider._related_for_path(conn, candidate)
            assert bool(backlinks) == (candidate == expected)


def test_retrieval_quality_bare_ambiguous_readme_has_no_backlinks(tmp_path, monkeypatch):
    provider = _retrieval_quality_provider(tmp_path, monkeypatch, {
        '20-WIKI/source/start.md': '# Source\n[[README]]',
        '20-WIKI/foo/README.md': '# One', '20-WIKI/bar/README.md': '# Two',
    })
    with provider._conn(readonly=True) as conn:
        assert provider._related_for_path(conn, '20-WIKI/source/start.md')[0][0]['path'] is None
        assert provider._related_for_path(conn, '20-WIKI/foo/README.md')[1] == []


def test_retrieval_quality_genuine_limits_and_policy(tmp_path, monkeypatch):
    source = '20-WIKI/start.md'
    provider = _retrieval_quality_provider(tmp_path, monkeypatch, {
        source: '# Anchor\n[[20-WIKI/foo/README]]\n[[20-WIKI/foo/README]]\n[[20-WIKI/ok]]\n[[private/private]]',
        '20-WIKI/foo/README.md': '# Anchor', '20-WIKI/bar/README.md': '# Anchor',
        '20-WIKI/ok.md': '# Anchor',
        'private/private.md': '# Anchor\n[[20-WIKI/foo/README]]',
        '20-WIKI/denied.md': '# Anchor\n[[20-WIKI/foo/README]]',
    })
    provider._profile = 'work'
    provider._config = {'deny_paths': ['20-WIKI/denied.md']}
    results = provider._search('anchor', limit=20)
    assert {r['path'] for r in results} == {source, '20-WIKI/foo/README.md', '20-WIKI/bar/README.md', '20-WIKI/ok.md'}
    with provider._conn(readonly=True) as conn:
        links, _ = provider._related_for_path(conn, source, limit=2)
        assert {x['path'] for x in links} == {'20-WIKI/foo/README.md', '20-WIKI/ok.md'}
        assert provider._related_for_path(conn, source, limit=0) == ([], [])
        _, backlinks = provider._related_for_path(conn, '20-WIKI/foo/README.md', limit=2)
        assert [x['path'] for x in backlinks] == [source]
        links, _ = provider._related_for_path(conn, source, limit=10)
        assert all(x['path'] != 'private/private.md' for x in links)
    assert results == provider._search('anchor', limit=20)


def test_retrieval_quality_backlinks_ignore_wrong_readmes_before_limit(tmp_path, monkeypatch):
    target = '20-WIKI/z/README.md'
    provider = _retrieval_quality_provider(tmp_path, monkeypatch, {
        target: '# Target', '20-WIKI/a/README.md': '# Decoy',
        '20-WIKI/a-source.md': '# Wrong\n[[20-WIKI/a/README]]',
        '20-WIKI/b-source.md': '# Missing\n[[20-WIKI/missing/README]]',
        '20-WIKI/c-source.md': '# Ambiguous\n[[README]]',
        '20-WIKI/z-source.md': '# Real\n[[20-WIKI/z/README]]',
    })
    with provider._conn(readonly=True) as conn:
        _, backlinks = provider._related_for_path(conn, target, limit=1)
        assert [x['path'] for x in backlinks] == ['20-WIKI/z-source.md']


def test_retrieval_quality_bounded_cached_target_lookup_no_source_io(tmp_path, monkeypatch):
    canonical = '20-WIKI/quality/published.md'
    provider = _retrieval_quality_provider(tmp_path, monkeypatch, {
        **{f'20-WIKI/quality/_drafts/{i:03}.md': _quality_note(status='applied', applied_to=canonical)
           for i in range(60)},
        canonical: _quality_note(body='needle ' + 'ordinary ' * 1000),
    })
    provider._config = {'max_related_notes': 0}
    queries = []
    original_conn = provider._conn
    def traced_conn(*args, **kwargs):
        conn = original_conn(*args, **kwargs)
        conn.set_trace_callback(queries.append)
        return conn
    monkeypatch.setattr(provider, '_conn', traced_conn)
    for name in ('resolve', 'stat', 'exists', 'read_text'):
        original = getattr(Path, name)
        def guarded(path, *args, _original=original, **kwargs):
            if str(path).startswith(str(provider._vault)):
                raise AssertionError('source filesystem touched during search')
            return _original(path, *args, **kwargs)
        monkeypatch.setattr(Path, name, guarded)
    results = provider._search('needle', limit=2)
    assert [r['path'] for r in results] == [canonical]  # Cap is exhausted, not unbounded refill.
    # SQLite also traces FTS5's internal shadow-table reads; count public queries only.
    assert len([q for q in queries if q.startswith('SELECT f.path,')]) == 2
    assert results[0]['links'] == results[0]['backlinks'] == []


def _quality_note(title='Shared', status='published', body='needle', applied_to=''):
    return f'---\ntitle: {title}\nstatus: {status}\napplied-to: {applied_to}\nupdated: 2099-01-01\n---\n{body}\n'


def test_retrieval_quality_applied_snapshots_collapse_and_fill(tmp_path, monkeypatch):
    canonical = '20-WIKI/quality/published.md'
    drafts = {f'20-WIKI/quality/_drafts/snapshot-{i:02}.md':
              _quality_note(status='applied', applied_to=canonical) for i in range(12)}
    provider = _retrieval_quality_provider(tmp_path, monkeypatch, {
        **drafts, canonical: _quality_note(body='needle ' + 'ordinary ' * 100),
        '20-WIKI/quality/other.md': _quality_note(title='Other', body='needle ' + 'ordinary ' * 120),
    })
    results = provider._search('needle', limit=2)
    assert {r['path'] for r in results} == {canonical, '20-WIKI/quality/other.md'}
    assert len(provider._search('needle', limit=20, path_prefix='20-WIKI/quality/_drafts/')) == 12
    assert len(provider._search('needle', limit=20, path_prefix='20-WIKI/quality/_drafts')) == 12
    assert provider._search('needle', limit=2) == results
    assert next(r for r in results if r['path'] == canonical)['metadata']['status'] == 'published'


@pytest.mark.parametrize('case', ['orphan', 'unmatched', 'pending', 'different-title',
                                  'target-draft', 'target-unpublished', 'no-applied-to',
                                  'denied-target', 'personal-target', 'outside-prefix', 'traversal-target'])
def test_retrieval_quality_preserves_unproven_history(tmp_path, monkeypatch, case):
    target = '20-WIKI/quality/published.md'
    draft = '20-WIKI/quality/_drafts/snapshot.md'
    target_text = _quality_note()
    status, applied, prefix = 'applied', target, ''
    if case == 'orphan':
        applied = '20-WIKI/missing.md'
    elif case == 'unmatched':
        target_text = _quality_note(body='unrelated content')
    elif case == 'pending':
        status = 'draft'
    elif case == 'different-title':
        target_text = _quality_note(title='Different')
    elif case == 'target-draft':
        target = applied = '20-WIKI/quality/_drafts/other.md'
    elif case == 'target-unpublished':
        target_text = _quality_note(status='draft')
    elif case == 'no-applied-to':
        applied = ''
    elif case == 'personal-target':
        target = applied = 'private/target.md'
    elif case == 'outside-prefix':
        target = applied = '20-WIKI/else/target.md'
        prefix = '20-WIKI/quality/'
    elif case == 'traversal-target':
        applied = '20-WIKI/else/../quality/published.md'
    provider = _retrieval_quality_provider(tmp_path, monkeypatch, {
        draft: _quality_note(status=status, applied_to=applied), target: target_text,
    })
    if case == 'denied-target':
        provider._config = {'deny_paths': [target]}
    if case == 'personal-target':
        provider._profile = 'work'
    results = provider._search('needle', limit=20, path_prefix=prefix)
    assert draft in {r['path'] for r in results}
    if case in {'denied-target', 'personal-target', 'outside-prefix', 'unmatched'}:
        assert target not in {r['path'] for r in results}


@pytest.mark.parametrize('prefix, decoy', [
    ('20-WIKI/a_b/', '20-WIKI/axb/'), ('20-WIKI/a%b/', '20-WIKI/axxb/'),
    ('20-WIKI/quality/', '20-WIKI/quality-other/'),
    ('20-WIKI/Case/', '20-WIKI/case/'),
])
def test_retrieval_quality_literal_prefix(tmp_path, monkeypatch, prefix, decoy):
    provider = _retrieval_quality_provider(tmp_path, monkeypatch, {
        prefix + 'note.md': '# Anchor', decoy + 'note.md': '# Anchor',
    })
    assert [r['path'] for r in provider._search('anchor', path_prefix=prefix)] == [prefix + 'note.md']


def _wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


def test_stale_index_search_returns_immediately_while_refresh_is_blocked(tmp_path, monkeypatch):
    mod = load_plugin()
    provider = _indexed_provider(mod, tmp_path)
    with sqlite3.connect(provider._index_path) as conn:
        conn.execute("UPDATE meta SET value='1' WHERE key='indexed_at'")
    started = threading.Event()
    release = threading.Event()

    def blocked_run(*args, **kwargs):
        started.set()
        release.wait(timeout=2)
        raise subprocess.TimeoutExpired(args[0] if args else 'worker', 1)

    monkeypatch.setattr(mod.subprocess, 'run', blocked_run)
    before = time.monotonic()
    payload = json.loads(provider.handle_tool_call('obsidian_vault_search', {'query': 'stable retrieval'}))
    elapsed = time.monotonic() - before
    assert payload['count'] >= 1
    assert elapsed < 0.25
    assert started.wait(timeout=1)
    release.set()


def test_timed_out_refresh_preserves_last_good_index(tmp_path, monkeypatch):
    mod = load_plugin()
    provider = _indexed_provider(mod, tmp_path)
    before = provider._index_state().copy()
    provider._index_dirty = True

    def timed_out(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0] if args else 'worker', 1)

    monkeypatch.setattr(mod.subprocess, 'run', timed_out)
    payload = json.loads(provider.handle_tool_call('obsidian_vault_search', {'query': 'stable retrieval'}))
    assert payload['count'] >= 1
    status_path = provider._index_path.parent / 'refresh_status.json'
    assert _wait_for(status_path.exists)
    assert json.loads(status_path.read_text())['status'] == 'timeout'
    assert provider._index_state() == before


def test_missing_index_returns_bounded_warming_error(tmp_path, monkeypatch):
    mod = load_plugin()
    provider = mod.ObsidianVaultMemoryProvider()
    provider._vault = tmp_path
    provider._profile = 'default'
    provider._config = {'initial_wait_seconds': 0.05, 'refresh_retry_seconds': 1}
    provider._index_path = tmp_path / 'state' / 'index.sqlite3'
    (tmp_path / '20-WIKI').mkdir()
    (tmp_path / 'AGENTS.md').write_text('policy')
    release = threading.Event()

    def blocked_run(*args, **kwargs):
        release.wait(timeout=2)
        raise subprocess.TimeoutExpired(args[0] if args else 'worker', 1)

    monkeypatch.setattr(mod.subprocess, 'run', blocked_run)
    before = time.monotonic()
    payload = json.loads(provider.handle_tool_call('obsidian_vault_search', {'query': 'anything'}))
    elapsed = time.monotonic() - before
    assert 'bounded' in payload['error']
    assert elapsed < 0.5
    release.set()


def test_refresh_is_single_flight_across_provider_instances(tmp_path, monkeypatch):
    mod = load_plugin()
    first = _indexed_provider(mod, tmp_path)
    with sqlite3.connect(first._index_path) as conn:
        conn.execute("UPDATE meta SET value='1' WHERE key='indexed_at'")
    second = mod.ObsidianVaultMemoryProvider()
    second._vault = first._vault
    second._profile = first._profile
    second._config = dict(first._config)
    second._index_path = first._index_path
    started = threading.Event()
    release = threading.Event()
    calls = []

    def blocked_run(*args, **kwargs):
        calls.append(args)
        started.set()
        release.wait(timeout=2)
        raise subprocess.TimeoutExpired(args[0] if args else 'worker', 1)

    monkeypatch.setattr(mod.subprocess, 'run', blocked_run)
    assert json.loads(first.handle_tool_call('obsidian_vault_search', {'query': 'stable'}))['count'] >= 1
    assert started.wait(timeout=1)
    assert json.loads(second.handle_tool_call('obsidian_vault_search', {'query': 'stable'}))['count'] >= 1
    assert len(calls) == 1
    release.set()


def test_successful_refresh_atomically_replaces_index(tmp_path):
    mod = load_plugin()
    provider = _indexed_provider(mod, tmp_path)
    new_note = tmp_path / '20-WIKI' / 'concepts' / 'new.md'
    new_note.write_text('# New\nnewly indexed sentinel\n')
    provider._index_dirty = True

    # A dirty index is still searchable while the isolated refresh runs.
    assert json.loads(provider.handle_tool_call('obsidian_vault_search', {'query': 'stable'}))['count'] >= 1
    status_path = provider._index_path.parent / 'refresh_status.json'
    assert _wait_for(lambda: status_path.exists() and json.loads(status_path.read_text()).get('status') == 'ok', timeout=5)
    payload = json.loads(provider.handle_tool_call('obsidian_vault_search', {'query': 'newly indexed sentinel'}))
    assert any(row['path'].endswith('/new.md') for row in payload['results'])


def test_dataless_files_are_skipped_without_invalidating_candidate(tmp_path, monkeypatch):
    mod = load_plugin()
    provider = _indexed_provider(mod, tmp_path)
    cold = tmp_path / '20-WIKI' / 'concepts' / 'cold.md'
    cold.write_text('# Cold\nshould not be opened\n')
    original = provider._is_dataless
    monkeypatch.setattr(provider, '_is_dataless', lambda p: p == cold or original(p))
    candidate = tmp_path / 'state' / 'candidate.sqlite3'
    result = provider._build_index_database(candidate)
    assert result['skipped_dataless'] == 1
    assert provider._index_state(candidate)['valid'] is True
    with sqlite3.connect(candidate) as conn:
        paths = {row[0] for row in conn.execute('SELECT path FROM vault_fts')}
    assert '20-WIKI/concepts/cold.md' not in paths


def test_fresh_persisted_index_does_not_rebuild_after_new_provider_instance(tmp_path, monkeypatch):
    mod = load_plugin()
    first = _indexed_provider(mod, tmp_path)
    second = mod.ObsidianVaultMemoryProvider()
    second._vault = first._vault
    second._profile = first._profile
    second._config = {'refresh_seconds': 600}
    second._index_path = first._index_path
    monkeypatch.setattr(second, '_start_index_refresh', lambda **kwargs: (_ for _ in ()).throw(AssertionError('unexpected refresh')))
    payload = json.loads(second.handle_tool_call('obsidian_vault_search', {'query': 'stable retrieval'}))
    assert payload['count'] >= 1


def test_failed_dirty_refresh_respects_retry_cooldown(tmp_path, monkeypatch):
    mod = load_plugin()
    provider = _indexed_provider(mod, tmp_path)
    provider._config['refresh_retry_seconds'] = 60
    provider._index_dirty = True
    calls = []

    def timed_out(*args, **kwargs):
        calls.append(args)
        raise subprocess.TimeoutExpired(args[0] if args else 'worker', 1)

    monkeypatch.setattr(mod.subprocess, 'run', timed_out)
    assert json.loads(provider.handle_tool_call('obsidian_vault_search', {'query': 'stable'}))['count'] >= 1
    status_path = provider._index_path.parent / 'refresh_status.json'
    assert _wait_for(status_path.exists)
    assert json.loads(provider.handle_tool_call('obsidian_vault_search', {'query': 'stable'}))['count'] >= 1
    assert len(calls) == 1


def test_invalid_candidate_never_replaces_last_good_index(tmp_path, monkeypatch):
    mod = load_plugin()
    provider = _indexed_provider(mod, tmp_path)
    before = provider._index_path.read_bytes()
    provider._index_dirty = True

    def successful_without_candidate(*args, **kwargs):
        return subprocess.CompletedProcess(args[0] if args else 'worker', 0, stdout='{}\n', stderr='')

    monkeypatch.setattr(mod.subprocess, 'run', successful_without_candidate)
    assert json.loads(provider.handle_tool_call('obsidian_vault_search', {'query': 'stable'}))['count'] >= 1
    status_path = provider._index_path.parent / 'refresh_status.json'
    assert _wait_for(status_path.exists)
    assert json.loads(status_path.read_text())['status'] == 'error'
    assert provider._index_path.read_bytes() == before


def test_incremental_refresh_does_not_read_unchanged_notes(tmp_path, monkeypatch):
    mod = load_plugin()
    provider = _indexed_provider(mod, tmp_path)
    source = provider._index_path
    candidate = tmp_path / 'state' / 'incremental.sqlite3'

    def unexpected_read(_path):
        raise AssertionError('unchanged notes must not be opened during incremental refresh')

    monkeypatch.setattr(provider, '_read_index_text', unexpected_read, raising=False)
    result = provider._build_index_database(candidate, source_index=source)

    assert result['mode'] == 'incremental'
    assert result['changed_files'] == 0
    assert result['removed_files'] == 0
    assert result['unchanged_files'] >= 2
    assert provider._index_state(candidate)['valid'] is True


def test_incremental_refresh_updates_only_changed_note(tmp_path, monkeypatch):
    mod = load_plugin()
    provider = _indexed_provider(mod, tmp_path)
    source = provider._index_path
    changed = tmp_path / '20-WIKI' / 'concepts' / 'stable.md'
    time.sleep(0.002)
    changed.write_text('# Stable\nupdated incremental sentinel\n')
    reads = []
    original = provider._read_index_text

    def tracked_read(path):
        reads.append(provider._rel(path))
        return original(path)

    monkeypatch.setattr(provider, '_read_index_text', tracked_read)
    candidate = tmp_path / 'state' / 'incremental.sqlite3'
    result = provider._build_index_database(candidate, source_index=source)

    assert result['mode'] == 'incremental'
    assert result['changed_files'] == 1
    assert reads == ['20-WIKI/concepts/stable.md']
    provider._index_path = candidate
    payload = json.loads(provider.handle_tool_call('obsidian_vault_search', {'query': 'updated incremental sentinel'}))
    assert payload['count'] == 1


def test_incremental_refresh_removes_deleted_note(tmp_path):
    mod = load_plugin()
    provider = _indexed_provider(mod, tmp_path)
    source = provider._index_path
    deleted = tmp_path / '20-WIKI' / 'concepts' / 'stable.md'
    deleted.unlink()
    candidate = tmp_path / 'state' / 'incremental.sqlite3'

    result = provider._build_index_database(candidate, source_index=source)

    assert result['mode'] == 'incremental'
    assert result['removed_files'] == 1
    with sqlite3.connect(candidate) as conn:
        assert conn.execute("SELECT count(*) FROM vault_fts WHERE path='20-WIKI/concepts/stable.md'").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM note_links WHERE source_path='20-WIKI/concepts/stable.md'").fetchone()[0] == 0


def test_incremental_refresh_preserves_last_row_when_changed_note_read_fails(tmp_path, monkeypatch):
    mod = load_plugin()
    provider = _indexed_provider(mod, tmp_path, content='last known good sentinel')
    source = provider._index_path
    changed = tmp_path / '20-WIKI' / 'concepts' / 'stable.md'
    time.sleep(0.002)
    changed.write_text('# Stable\nnew bytes that cannot be read\n')
    original = provider._read_index_text

    def fail_changed(path):
        if path == changed:
            raise OSError('simulated File Provider read failure')
        return original(path)

    monkeypatch.setattr(provider, '_read_index_text', fail_changed)
    candidate = tmp_path / 'state' / 'incremental.sqlite3'
    result = provider._build_index_database(candidate, source_index=source)

    assert result['mode'] == 'incremental'
    assert result['skipped_errors'] == 1
    provider._index_path = candidate
    payload = json.loads(provider.handle_tool_call('obsidian_vault_search', {'query': 'last known good sentinel'}))
    assert payload['count'] == 1


def test_incremental_refresh_rejects_mass_removal_candidate(tmp_path):
    mod = load_plugin()
    provider = _indexed_provider(mod, tmp_path)
    notes = tmp_path / '20-WIKI' / 'concepts'
    for number in range(30):
        (notes / f'note-{number}.md').write_text(f'# Note {number}\nretained {number}\n')
    source = tmp_path / 'state' / 'source.sqlite3'
    provider._build_index_database(source)
    source_bytes = source.read_bytes()
    for path in notes.glob('*.md'):
        path.unlink()

    candidate = tmp_path / 'state' / 'incremental.sqlite3'
    try:
        provider._build_index_database(candidate, source_index=source)
    except RuntimeError as exc:
        assert 'mass-removal candidate' in str(exc)
    else:
        raise AssertionError('mass removal must be rejected')
    assert source.read_bytes() == source_bytes


def test_incremental_refresh_rejects_near_empty_candidate_for_small_index(tmp_path):
    mod = load_plugin()
    provider = _indexed_provider(mod, tmp_path)
    notes = tmp_path / '20-WIKI' / 'concepts'
    for number in range(19):
        (notes / f'small-{number}.md').write_text(f'# Small {number}\nsentinel {number}\n')
    source = tmp_path / 'state' / 'small-source.sqlite3'
    provider._build_index_database(source)
    with sqlite3.connect(source) as conn:
        assert conn.execute('SELECT count(*) FROM vault_fts').fetchone()[0] == 21
    for path in notes.glob('*.md'):
        path.unlink()

    candidate = tmp_path / 'state' / 'small-candidate.sqlite3'
    with pytest.raises(RuntimeError, match='mass-removal candidate'):
        provider._build_index_database(candidate, source_index=source)


def test_schema_v2_source_is_copied_then_upgraded(tmp_path):
    mod = load_plugin()
    provider = _indexed_provider(mod, tmp_path)
    source = provider._index_path
    with sqlite3.connect(source) as conn:
        conn.execute('ALTER TABLE note_meta DROP COLUMN source_mtime_ns')
        conn.execute('ALTER TABLE note_meta DROP COLUMN source_size')
        conn.execute("UPDATE meta SET value='2' WHERE key='schema_version'")
    candidate = tmp_path / 'state' / 'upgraded.sqlite3'

    result = provider._build_index_database(candidate, source_index=source)

    assert result['mode'] == 'upgrade'
    with sqlite3.connect(candidate) as conn:
        assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == '3'
        columns = {row[1] for row in conn.execute('PRAGMA table_info(note_meta)')}
    assert {'source_mtime_ns', 'source_size'}.issubset(columns)


def test_schema_v2_upgrade_preserves_rows_omitted_by_scan(tmp_path):
    mod = load_plugin()
    provider = _indexed_provider(mod, tmp_path, content='schema two sentinel')
    notes = tmp_path / '20-WIKI' / 'concepts'
    for number in range(29):
        (notes / f'legacy-{number}.md').write_text(f'# Legacy {number}\nlegacy sentinel {number}\n')
    source = tmp_path / 'state' / 'schema2.sqlite3'
    built = provider._build_index_database(source)
    source_count = built['indexed_files']
    with sqlite3.connect(source) as conn:
        conn.execute('ALTER TABLE note_meta DROP COLUMN source_mtime_ns')
        conn.execute('ALTER TABLE note_meta DROP COLUMN source_size')
        conn.execute("UPDATE meta SET value='2' WHERE key='schema_version'")
    for path in notes.glob('*.md'):
        path.unlink()

    candidate = tmp_path / 'state' / 'schema3.sqlite3'
    result = provider._build_index_database(candidate, source_index=source)

    assert result['mode'] == 'upgrade'
    assert result['removed_files'] == 0
    assert result['indexed_files'] == source_count
    provider._index_path = candidate
    payload = json.loads(provider.handle_tool_call('obsidian_vault_search', {'query': 'schema two sentinel'}))
    assert any(row['path'] == '20-WIKI/concepts/stable.md' for row in payload['results'])


def test_dirty_generation_is_not_cleared_by_older_refresh(tmp_path, monkeypatch):
    mod = load_plugin()
    provider = _indexed_provider(mod, tmp_path)
    provider._mark_index_dirty()
    started_generation = provider._index_dirty_generation

    def completed_refresh(args, **_kwargs):
        payload = json.loads(Path(args[-1]).read_text())
        provider._build_index_database(
            Path(payload['destination']),
            source_index=Path(payload['source_index']),
        )
        provider._mark_index_dirty()
        return subprocess.CompletedProcess(args, 0, stdout='{}\n', stderr='')

    monkeypatch.setattr(mod.subprocess, 'run', completed_refresh)
    provider._refresh_index_bounded(str(provider._index_path), started_generation)

    assert provider._index_dirty is True
    assert provider._index_dirty_generation == started_generation + 1


def test_cross_process_refresh_lock_skips_competing_rebuild(tmp_path, monkeypatch):
    mod = load_plugin()
    if mod._fcntl is None:
        return
    provider = _indexed_provider(mod, tmp_path)
    before = provider._index_path.read_bytes()
    lock_path = provider._index_path.parent / 'index-refresh.lock'
    lock_handle = lock_path.open('a+')
    mod._fcntl.flock(lock_handle.fileno(), mod._fcntl.LOCK_EX | mod._fcntl.LOCK_NB)

    def unexpected_worker(*_args, **_kwargs):
        raise AssertionError('competing refresh must not launch a worker')

    monkeypatch.setattr(mod.subprocess, 'run', unexpected_worker)
    try:
        provider._refresh_index_bounded(str(provider._index_path), provider._index_dirty_generation)
    finally:
        mod._fcntl.flock(lock_handle.fileno(), mod._fcntl.LOCK_UN)
        lock_handle.close()

    assert provider._index_path.read_bytes() == before


def test_vault_read_uses_bounded_worker_and_preserves_truncation(tmp_path):
    mod = load_plugin()
    provider = mod.ObsidianVaultMemoryProvider()
    provider._vault = tmp_path
    provider._profile = 'default'
    provider._config = {'read_timeout_seconds': 1}
    provider._index_path = tmp_path / 'state' / 'index.sqlite3'
    note = tmp_path / '20-WIKI' / 'concepts' / 'read-me.md'
    note.parent.mkdir(parents=True)
    note.write_text('# Read Me\n' + ('bounded worker content ' * 100))
    payload = json.loads(provider.handle_tool_call('obsidian_vault_read', {
        'path': '20-WIKI/concepts/read-me.md',
        'max_chars': 1000,
    }))
    assert payload['path'] == '20-WIKI/concepts/read-me.md'
    assert payload['content'].startswith('# Read Me')
    assert len(payload['content']) == 1000
    assert payload['truncated'] is True


def test_vault_read_does_not_resolve_source_path_on_request_thread(tmp_path, monkeypatch):
    mod = load_plugin()
    provider = mod.ObsidianVaultMemoryProvider()
    provider._vault = tmp_path
    provider._profile = 'default'
    provider._config = {'read_timeout_seconds': 1}
    provider._index_path = tmp_path / 'state' / 'index.sqlite3'
    note = tmp_path / '20-WIKI' / 'concepts' / 'worker-only.md'
    note.parent.mkdir(parents=True)
    note.write_text('# Worker Only\nsource-backed resolution stays bounded\n')

    def unexpected_parent_resolution(*_args, **_kwargs):
        raise AssertionError('request thread must not resolve or relativize a vault path')

    monkeypatch.setattr(provider, '_resolve_vault_path', unexpected_parent_resolution)
    monkeypatch.setattr(provider, '_rel', unexpected_parent_resolution)
    payload = json.loads(provider.handle_tool_call('obsidian_vault_read', {
        'path': '20-WIKI/concepts/worker-only.md',
    }))

    assert payload['path'] == '20-WIKI/concepts/worker-only.md'
    assert 'source-backed resolution stays bounded' in payload['content']


def test_vault_read_worker_enforces_allow_and_deny_boundaries(tmp_path):
    mod = load_plugin()
    provider = mod.ObsidianVaultMemoryProvider()
    provider._vault = tmp_path
    provider._profile = 'default'
    provider._config = {
        'read_timeout_seconds': 1,
        'allow_paths': ['20-WIKI/'],
        'deny_paths': ['20-WIKI/private/'],
    }
    provider._index_path = tmp_path / 'state' / 'index.sqlite3'
    note = tmp_path / '20-WIKI' / 'private' / 'secret.md'
    note.parent.mkdir(parents=True)
    note.write_text('must not escape worker authorization')

    payload = json.loads(provider.handle_tool_call('obsidian_vault_read', {
        'path': '20-WIKI/private/secret.md',
    }))

    assert 'error' in payload
    assert 'allowed vault lanes' in payload['error']
    assert 'must not escape' not in json.dumps(payload)


def test_vault_read_timeout_returns_bounded_error(tmp_path, monkeypatch):
    mod = load_plugin()
    provider = mod.ObsidianVaultMemoryProvider()
    provider._vault = tmp_path
    provider._profile = 'default'
    provider._config = {'read_timeout_seconds': 0.1}
    provider._index_path = tmp_path / 'state' / 'index.sqlite3'
    note = tmp_path / '20-WIKI' / 'concepts' / 'cold.md'
    note.parent.mkdir(parents=True)
    note.write_text('# Cold')

    def timed_out(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0] if args else 'worker', 0.1)

    monkeypatch.setattr(mod.subprocess, 'run', timed_out)
    before = time.monotonic()
    payload = json.loads(provider.handle_tool_call('obsidian_vault_read', {'path': '20-WIKI/concepts/cold.md'}))
    assert time.monotonic() - before < 0.25
    assert 'remained bounded' in payload['error']
    assert not list(provider._index_path.parent.glob('.read-*.json'))


def _inbox_provider(tmp_path):
    mod = load_plugin()
    provider = mod.ObsidianVaultMemoryProvider()
    provider._vault = tmp_path
    provider._profile = 'default'
    provider._config = {}
    provider._index_path = tmp_path / 'state' / 'index.sqlite3'
    (tmp_path / 'AGENTS.md').write_text('policy')
    return provider


def test_capture_system_id_adds_system_pointer(tmp_path):
    provider = _inbox_provider(tmp_path)
    payload = json.loads(provider.handle_tool_call('obsidian_vault_capture', {
        'category': 'inbox', 'content': 'Backup fact.', 'title': 'Sys Capture', 'system_id': 'backup-runner',
    }))
    assert payload['status'] == 'created'
    assert 'system: backup-runner\n' in (tmp_path / payload['path']).read_text()


def test_capture_system_id_fails_open_on_malformed_value(tmp_path):
    provider = _inbox_provider(tmp_path)
    payload = json.loads(provider.handle_tool_call('obsidian_vault_capture', {
        'category': 'inbox', 'content': 'Fact.', 'title': 'Bad Sys', 'system_id': '../Bad Id',
    }))
    assert payload['status'] == 'created'
    assert 'system:' not in (tmp_path / payload['path']).read_text()


def test_capture_without_system_id_unchanged(tmp_path):
    provider = _inbox_provider(tmp_path)
    payload = json.loads(provider.handle_tool_call('obsidian_vault_capture', {
        'category': 'inbox', 'content': 'Fact.', 'title': 'No Sys',
    }))
    assert payload['status'] == 'created'
    assert 'system:' not in (tmp_path / payload['path']).read_text()


def _hide_applied_provider(tmp_path, monkeypatch, target_status='published', applied_to='20-WIKI/quality/published.md'):
    tmp_path.mkdir(parents=True, exist_ok=True)
    provider = _retrieval_quality_provider(tmp_path, monkeypatch, {
        '20-WIKI/quality/_drafts/snap.md': _quality_note(status='applied', applied_to=applied_to, title='Snap'),
        '20-WIKI/quality/other.md': _quality_note(title='Other', body='needle ' + 'ordinary ' * 50),
    })
    vault = tmp_path / 'vault'
    (vault / '20-WIKI/quality').mkdir(parents=True)
    (vault / 'AGENTS.md').write_text('policy')
    (vault / '20-WIKI/quality/published.md').write_text(f'---\nstatus: {target_status}\n---\n# Pub\n')
    provider._vault = vault
    return provider


def test_search_hides_merged_draft_when_published_target_exists(tmp_path, monkeypatch):
    provider = _hide_applied_provider(tmp_path, monkeypatch)
    paths = {r['path'] for r in provider._search('needle', limit=20)}
    assert '20-WIKI/quality/_drafts/snap.md' not in paths
    assert '20-WIKI/quality/other.md' in paths
    # explicit _drafts prefix still shows history
    assert provider._search('needle', limit=20, path_prefix='20-WIKI/quality/_drafts/')


def test_search_keeps_draft_when_target_unpublished_or_missing(tmp_path, monkeypatch):
    p1 = _hide_applied_provider(tmp_path / 'a', monkeypatch, target_status='draft')
    assert '20-WIKI/quality/_drafts/snap.md' in {r['path'] for r in p1._search('needle', limit=20)}
    p2 = _hide_applied_provider(tmp_path / 'b', monkeypatch, applied_to='20-WIKI/quality/missing.md')
    assert '20-WIKI/quality/_drafts/snap.md' in {r['path'] for r in p2._search('needle', limit=20)}


def _dated_note(title, updated, body, tags='', extra=''):
    tag_line = f'tags: [{tags}]\n' if tags else ''
    return f'---\ntitle: {title}\nstatus: published\nupdated: {updated}\n{tag_line}{extra}---\n{body}\n'


def test_recency_ranks_newer_completion_note_above_older_remaining_work(tmp_path, monkeypatch):
    from datetime import date, timedelta
    recent = (date.today() - timedelta(days=1)).isoformat()
    provider = _retrieval_quality_provider(tmp_path, monkeypatch, {
        '20-WIKI/q/old-remaining.md': _dated_note('Old', '2025-01-01', 'website build remaining ' + 'filler ' * 30),
        '20-WIKI/q/new-done.md': _dated_note('New', recent, 'website build complete ' + 'filler ' * 70),
    })
    assert provider._search('website build', limit=2)[0]['path'] == '20-WIKI/q/new-done.md'
    provider._config = {'recency_weight': 0}
    assert provider._search('website build', limit=2)[0]['path'] == '20-WIKI/q/old-remaining.md'


def test_current_state_hub_boosted_and_superseded_demoted(tmp_path, monkeypatch):
    provider = _retrieval_quality_provider(tmp_path, monkeypatch, {
        '20-WIKI/q/a.md': _dated_note('A', '2026-01-01', 'catalog status ' * 4, extra='superseded-by: 20-WIKI/q/hub.md\n'),
        '20-WIKI/q/hub.md': _dated_note('Hub', '2026-01-01', 'catalog status ' + 'filler ' * 40, tags='current-state'),
    })
    assert provider._search('catalog status', limit=2)[0]['path'] == '20-WIKI/q/hub.md'


def test_rank_score_fails_safe_on_bad_metadata(tmp_path, monkeypatch):
    provider = _retrieval_quality_provider(tmp_path, monkeypatch, {})
    row = ('p', 't', '', -3.0, '[]', 'not-json', None, 'garbage', None, None, None)
    assert provider._rank_score(row) == -3.0


def test_current_state_hub_boosted_by_slug_without_tag(tmp_path, monkeypatch):
    provider = _retrieval_quality_provider(tmp_path, monkeypatch, {
        '20-WIKI/q/older-plan.md': _dated_note('Plan', '2026-01-01', 'backup runner status ' * 3 + 'filler ' * 20),
        '20-WIKI/q/backup-runner-current-state.md': _dated_note('Hub', '2026-01-01', 'backup runner status ' + 'filler ' * 40),
    })
    assert provider._search('backup runner status', limit=2)[0]['path'] == '20-WIKI/q/backup-runner-current-state.md'
