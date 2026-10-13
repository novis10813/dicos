from concurrent.futures import ThreadPoolExecutor
import sqlite3
import stat
from threading import Barrier

import pytest

from dicos import paths
from dicos.store import JobStore


def test_schema_and_mode():
    store = JobStore()
    assert store.path == paths.home() / 'dicos.db'
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(paths.home().stat().st_mode) == 0o700
    with sqlite3.connect(store.path) as db:
        assert [row[1] for row in db.execute('PRAGMA table_info(jobs)')] == (
            'id project seq slurm_id mode commit_sha command partition gpus cpus mem time_limit '
            'status slurm_state exit_code error remote_dir created_at started_at finished_at').split()


def test_add_get_sequences():
    store = JobStore()
    assert store.add('project-a', 'a', command='echo hi', mode='git') == 'a-1'
    assert store.add('project-b', 'b') == 'b-1'
    assert store.add('project-a', 'a') == 'a-2'
    record = store.get('a-1')
    assert record['project'] == 'project-a' and record['seq'] == 1
    assert record['command'] == 'echo hi' and record['status'] == 'queued'
    assert record['created_at']
    assert store.get('missing') is None
    assert JobStore().add('project-a', 'a') == 'a-3'


@pytest.mark.parametrize('separate_stores', [False, True])
def test_concurrent_add(separate_stores):
    store = JobStore()
    barrier = Barrier(2)

    def add(_):
        local = JobStore(store.path) if separate_stores else store
        barrier.wait(timeout=5)
        return local.add('demo', 'demo')

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert set(pool.map(add, range(2))) == {'demo-1', 'demo-2'}


def test_list_and_active():
    store = JobStore()
    for i in range(25):
        store.add('demo', 'demo', created_at='2026-10-13T00:00:00Z')
    store.update('demo-1', status='succeeded', exit_code=0)
    store.update('demo-2', status='running')
    assert len(store.list()) == 20
    assert len(store.list(limit=None)) == 25
    assert store.list()[0]['id'] == 'demo-25'
    assert store.list(status='succeeded')[0]['id'] == 'demo-1'
    assert len(store.active()) == 24
    assert {row['status'] for row in store.active()} == {'queued', 'running'}
    assert store.list(limit=0) == []
    with pytest.raises(ValueError):
        store.list(limit=-1)


@pytest.mark.parametrize('initial', ['queued', 'running'])
@pytest.mark.parametrize('terminal', ['succeeded', 'failed', 'cancelled', 'lost'])
def test_transitions_terminal_protection(initial, terminal):
    store = JobStore()
    id = store.add('demo', 'demo')
    if initial == 'running':
        store.update(id, status='running')
    store.update(id, status=terminal, slurm_state='COMPLETED', exit_code=0)
    before = store.get(id)
    for fields in [{'status': 'running'}, {'status': terminal}, {'exit_code': 7}]:
        with pytest.raises(ValueError, match='Terminal'):
            store.update(id, **fields)
        assert store.get(id) == before
    assert store.active() == []


def test_invalid_transitions_and_fields():
    store = JobStore()
    id = store.add('demo', 'demo')
    store.update(id, status='running')
    store.update(id, status='running', started_at='now')
    with pytest.raises(ValueError, match='Invalid transition'):
        store.update(id, status='queued')
    with pytest.raises(ValueError, match='Invalid status'):
        store.update(id, status='unknown')
    with pytest.raises(KeyError):
        store.update('absent', status='running')
    for field in ['id', 'project', 'seq', 'bad; DROP TABLE jobs']:
        with pytest.raises((ValueError, TypeError)):
            store.update(id, **{field: 'value'})
        if field != 'project':
            with pytest.raises(ValueError):
                store.add('demo', 'demo', **{field: 'value'})
    assert store.get(id)['status'] == 'running'
    with pytest.raises(ValueError):
        store.add('demo', 'invalid prefix')
