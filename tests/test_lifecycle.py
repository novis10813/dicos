import json
import subprocess

import pytest
import yaml
from typer.testing import CliRunner

from dicos import cli, config, conn, jobs, slurm, sweep, update
from dicos.store import JobStore

runner = CliRunner()
WORKDIR = '/work/alice/dicos'


def done(stdout='', stderr='', code=0):
    return subprocess.CompletedProcess([], code, stdout, stderr)


@pytest.fixture
def remote(tmp_path, monkeypatch):
    config.save(config.Config('alice', workdir=WORKDIR, defaults={'partition': 'cpu'}))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, '_remote_config', lambda: config.load())
    monkeypatch.setattr(cli.credentials, 'load', lambda: None)
    monkeypatch.setattr(conn, 'ensure_master', lambda cfg, creds: None)
    calls = []

    def run(cfg, cmd, input=None, **kw):
        calls.append((cmd, input))
        if cmd.startswith('sbatch'):
            return done('555\n')
        return done()

    monkeypatch.setattr(conn, 'run', run)
    monkeypatch.setattr(conn, 'exec_interactive', lambda cfg, cmd: calls.append((cmd, None)))
    original = subprocess.run
    monkeypatch.setattr(jobs.subprocess, 'run', lambda argv, **kw: done() if argv[0] == 'rsync'
                        else original(argv, **kw))
    return calls


def add(status='queued', slurm_id='123', mode='sync', remote_dir=WORKDIR + '/jobs/demo-1'):
    return JobStore().add('demo', 'demo', slurm_id=slurm_id, status=status, mode=mode,
                          remote_dir=remote_dir, command='true')


# wait

@pytest.mark.parametrize('status,code', [('succeeded', 0), ('failed', 1), ('cancelled', 2), ('lost', 3)])
def test_wait_terminal(remote, monkeypatch, status, code):
    id = add()
    states = iter(['queued', 'running', status])
    sleeps = []
    monkeypatch.setattr(jobs, 'refresh', lambda store, cfg: store.update(id, status=next(states)))
    monkeypatch.setattr('time.sleep', sleeps.append)
    result = runner.invoke(cli.app, ['wait', id, '--interval', '2'])
    assert result.exit_code == code, result.output
    assert f'{id} {status}' in result.output
    assert sleeps == [2, 2]


def test_wait_already_finished(remote, monkeypatch):
    id = add('succeeded')
    monkeypatch.setattr(jobs, 'refresh', lambda *a: pytest.fail('no refresh for terminal jobs'))
    result = runner.invoke(cli.app, ['wait', id])
    assert result.exit_code == 0 and not remote


def test_wait_timeout(remote, monkeypatch):
    id = add()
    now = [0.0]
    monkeypatch.setattr(jobs, 'refresh', lambda store, cfg: None)
    monkeypatch.setattr('time.monotonic', lambda: now[0])
    monkeypatch.setattr('time.sleep', lambda s: now.__setitem__(0, now[0] + s))
    result = runner.invoke(cli.app, ['wait', id, '--timeout', '40', '--interval', '15'])
    assert result.exit_code == 124 and 'still queued after 40s' in result.output
    assert now[0] == 40


def test_wait_errors(remote, monkeypatch):
    result = runner.invoke(cli.app, ['wait', 'nope'])
    assert result.exit_code == 1 and 'Unknown job' in result.output
    id = add()
    monkeypatch.setattr(jobs, 'refresh', lambda store, cfg: (_ for _ in ()).throw(ValueError('Slurm down')))
    result = runner.invoke(cli.app, ['wait', id])
    assert result.exit_code == 1 and 'Slurm down' in result.output


# clean

@pytest.mark.parametrize('mode', ['sync', 'git'])
def test_clean(remote, mode):
    id = add('succeeded', mode=mode)
    result = runner.invoke(cli.app, ['clean', id, '-y'])
    assert result.exit_code == 0, result.output
    command = f'rm -rf {WORKDIR}/jobs/demo-1'
    if mode == 'git':
        command += f' && if [ -d {WORKDIR}/repos/demo ]; then git -C {WORKDIR}/repos/demo worktree prune; fi'
    assert remote == [(command, None)]


def test_clean_confirm(remote):
    id = add('failed')
    result = runner.invoke(cli.app, ['clean', id], input='n\n')
    assert result.exit_code == 1 and 'Cancelled' in result.output and not remote
    result = runner.invoke(cli.app, ['clean', id], input='y\n')
    assert result.exit_code == 0 and len(remote) == 1


@pytest.mark.parametrize('status', ['queued', 'running'])
def test_clean_active(remote, status):
    id = add(status)
    result = runner.invoke(cli.app, ['clean', id, '-y'])
    assert result.exit_code == 1 and status in result.output and not remote


@pytest.mark.parametrize('remote_dir', ['/etc', WORKDIR + '/jobs/', WORKDIR + '/jobs/../repos', '/other/jobs/demo-1'])
def test_clean_refuses_outside_jobs(remote, remote_dir):
    id = add('succeeded', remote_dir=remote_dir)
    result = runner.invoke(cli.app, ['clean', id, '-y'])
    assert result.exit_code == 1 and 'Refusing' in result.output and not remote


# sweep expansion

def test_sweep_expansion_order():
    commands, resources = sweep.load_sweep(yaml.safe_load('''
command: python train.py --lr {lr} --seed {seed}
grid:
  lr: [0.1, 0.01]
  seed: [1, 2, 3]
partition: a100-al9
gpus: 1
time: 6h
'''))
    assert commands == [f'python train.py --lr {lr} --seed {seed}' for lr in (0.1, 0.01) for seed in (1, 2, 3)]
    assert resources == {'partition': 'a100-al9', 'gpus': 1, 'time': '6h'}


@pytest.mark.parametrize('data,message', [
    ([], 'mapping'), ({'grid': {}}, 'command is required'),
    ({'command': 'x {a}', 'grid': {}}, 'missing from grid'),
    ({'command': 'x', 'grid': {'a': [1]}}, 'not used'),
    ({'command': 'x {a}', 'grid': {'a': []}}, 'non-empty list'),
    ({'command': 'x {0}', 'grid': {}}, 'positional'),
    ({'command': 'x {', 'grid': {}}, 'invalid command template'),
    ({'command': 'x', 'server': 'a'}, 'unknown key'),
    ({'command': 'x', 'gpus': -1}, 'gpus'), ({'command': 'x', 'cpus': 0}, 'cpus'),
    ({'command': 'x', 'gpus': True}, 'gpus'), ({'command': 'x', 'partition': ''}, 'partition'),
])
def test_sweep_errors(data, message):
    with pytest.raises(sweep.SweepError, match=message):
        sweep.load_sweep(data)


# array script

def test_array_script_runs(tmp_path):
    commands = ["printf '%s\\n' \"it's $DICOS_JOB_ID\"", 'echo "a;b" | tr ";" " "']
    script = slurm.render_array_script(ids=['demo-4', 'demo-5'], partition='cpu', time='1h', cpus=1, mem=None,
                                       gpus=0, jobdir=str(tmp_path), setup='echo setup', commands=commands,
                                       src_dir=str(tmp_path))
    assert '#SBATCH --array=0-1' in script
    assert f'#SBATCH --output={tmp_path}/output_%a.log' in script
    assert f'#SBATCH --error={tmp_path}/output_%a.log' in script
    assert '#SBATCH --job-name=demo-4' in script and 'flock 9' in script
    outputs = [subprocess.run(['bash', '-c', script], env={'SLURM_ARRAY_TASK_ID': str(i), 'PATH': '/usr/bin:/bin'},
                              capture_output=True, text=True, check=True).stdout for i in range(2)]
    assert outputs == ["setup\nit's demo-4\n", 'setup\na b\n']


def test_array_script_without_setup(tmp_path):
    script = slurm.render_array_script(ids=['d-1'], partition='cpu', time='1h', cpus=1, mem=None, gpus=0,
                                       jobdir='/j', setup='', commands=['true'], src_dir='/j/src')
    assert 'flock' not in script and 'uv sync' not in script
    with pytest.raises(ValueError):
        slurm.render_array_script(ids=['d-1'], partition='cpu', time='1h', cpus=1, mem=None, gpus=0,
                                  jobdir='/j', setup='', commands=['a', 'b'], src_dir='/j/src')


@pytest.mark.parametrize('raw,expected', [('12_3', ['12_3']), ('12', ['12']),
    ('12_[0-2]', ['12_0', '12_1', '12_2']), ('12_[1,4-5%2]', ['12_1', '12_4', '12_5'])])
def test_expand_array_ids(raw, expected):
    assert slurm.expand_array_ids(raw) == expected


def test_parse_compact_arrays():
    assert slurm.parse_squeue('12_[1-2]|PENDING\n12_0|RUNNING\n') == {
        '12_1': 'PENDING', '12_2': 'PENDING', '12_0': 'RUNNING'}
    assert slurm.parse_sacct('12_[1-2]|PENDING|0:0|Unknown|Unknown\n12_0.batch|RUNNING|0:0|x|y\n') == {
        '12_1': ('PENDING', 0, 'Unknown', 'Unknown'), '12_2': ('PENDING', 0, 'Unknown', 'Unknown')}


# sweep CLI and array elements

def write_sweep(tmp_path, extra=''):
    path = tmp_path / 'sweep.yaml'
    path.write_text("command: python t.py --lr {lr}\ngrid:\n  lr: [0.1, 0.2, 0.3]\npartition: gpu\ngpus: 2\n" + extra)
    return path


def test_sweep_submit(remote, tmp_path):
    JobStore().add(tmp_path.name, tmp_path.name, status='succeeded')
    result = runner.invoke(cli.app, ['sweep', str(write_sweep(tmp_path)), '--sync', '--gpus', '1', '-t', '30m'])
    assert result.exit_code == 0, result.output
    store = JobStore()
    ids = [f'{tmp_path.name}-{n}' for n in (2, 3, 4)]
    jobdir = f'{WORKDIR}/jobs/{ids[0]}'
    for i, id in enumerate(ids):
        job = store.get(id)
        assert f'{id} (slurm 555_{i}) queued on gpu' in result.output
        assert job['slurm_id'] == f'555_{i}' and job['remote_dir'] == jobdir
        assert job['command'] == f'python t.py --lr 0.{i + 1}'
        assert job['gpus'] == 1 and job['time_limit'] == '00:30:00' and job['partition'] == 'gpu'
    upload = [call for call in remote if call[0].startswith('cat >')]
    assert len(upload) == 1 and upload[0][0] == f'cat > {jobdir}/job.sh'
    assert '#SBATCH --array=0-2' in upload[0][1] and '#SBATCH --gres=gpu:1' in upload[0][1]
    assert sum(call[0].startswith('sbatch') for call in remote) == 1


def test_sweep_invalid_file(remote, tmp_path):
    path = tmp_path / 'bad.yaml'
    path.write_text('command: x {a}\n')
    result = runner.invoke(cli.app, ['sweep', str(path), '--sync'])
    assert result.exit_code == 1 and 'missing from grid' in result.output and not remote


def test_sweep_sbatch_failure(remote, tmp_path, monkeypatch):
    monkeypatch.setattr(conn, 'run', lambda cfg, cmd, input=None, **kw:
                        done(stderr='bad partition', code=1) if cmd.startswith('sbatch') else done())
    result = runner.invoke(cli.app, ['sweep', str(write_sweep(tmp_path)), '--sync'])
    assert result.exit_code == 1 and 'bad partition' in result.output
    assert [job['status'] for job in JobStore().list()] == ['failed'] * 3


def test_array_refresh(remote, monkeypatch):
    store = JobStore()
    ids = store.add_many('demo', 'demo', [{'slurm_id': f'9_{i}'} for i in range(3)])
    output = ('9_0|RUNNING\n9_[1-2]|PENDING\n' + jobs.REFRESH_MARKER + '\n'
              '9_0|RUNNING|0:0|2026-10-13T00:00:00|Unknown\n9_[1-2]|PENDING|0:0|Unknown|Unknown\n')
    commands = []
    monkeypatch.setattr(conn, 'run', lambda cfg, cmd, **kw: commands.append(cmd) or done(output))
    jobs.refresh(store, config.load())
    assert [store.get(id)['status'] for id in ids] == ['running', 'queued', 'queued']
    assert '-r' in commands[0].split()


def test_array_logs_stop_clean(remote):
    store = JobStore()
    jobdir = WORKDIR + '/jobs/demo-1'
    ids = store.add_many('demo', 'demo', [{'slurm_id': f'9_{i}', 'remote_dir': jobdir, 'mode': 'git'}
                                          for i in range(2)])
    result = runner.invoke(cli.app, ['logs', ids[1], '-n', '5'])
    assert result.exit_code == 0 and remote[-1] == (f'tail -n 5 {jobdir}/output_1.log', None)
    result = runner.invoke(cli.app, ['stop', ids[0]])
    assert result.exit_code == 0 and remote[-1] == ('scancel 9_0', None)
    remote.clear()
    result = runner.invoke(cli.app, ['clean', ids[0], '-y'])
    assert result.exit_code == 1 and ids[1] in result.output and not remote
    store.update(ids[1], status='succeeded')
    result = runner.invoke(cli.app, ['clean', ids[0], '-y'])
    assert result.exit_code == 0 and remote[0][0].startswith(f'rm -rf {jobdir} && ')


# update

class Dist:
    def __init__(self, text):
        self.text = text

    def read_text(self, name):
        return self.text


@pytest.mark.parametrize('data,expected', [
    ({'url': 'https://github.com/o/dicos.git', 'vcs_info': {'vcs': 'git', 'commit_id': 'a' * 40}},
     ('https://github.com/o/dicos.git', 'a' * 40)),
    ({'url': 'file:///src/dicos', 'dir_info': {'editable': True}}, None),
    (None, None),
])
def test_installed_source(monkeypatch, data, expected):
    monkeypatch.setattr(update, 'distribution', lambda name: Dist(None if data is None else json.dumps(data)))
    assert update.installed_source() == expected


def test_installed_source_pinned(monkeypatch):
    data = {'url': 'https://x/dicos.git', 'vcs_info': {'vcs': 'git', 'requested_revision': 'v1'}}
    monkeypatch.setattr(update, 'distribution', lambda name: Dist(json.dumps(data)))
    with pytest.raises(update.UpdateError, match='pinned'):
        update.installed_source()


@pytest.mark.parametrize('origin,requirement', [
    ('git@github.com:o/dicos.git', 'git+ssh://git@github.com/o/dicos.git'),
    ('https://github.com/o/dicos.git', 'git+https://github.com/o/dicos.git'),
    ('git+https://x/y', 'git+https://x/y')])
def test_requirement(origin, requirement):
    assert update.uv_git_requirement(origin) == requirement


@pytest.mark.parametrize('check,remote_sha,installs,output', [
    (True, 'b' * 40, [], 'update available: aaaaaaa -> bbbbbbb'),
    (False, 'a' * 40, [], 'up to date'),
    (False, 'b' * 40, [['uv', 'tool', 'install', '--force', '--refresh', 'git+https://x/dicos.git']], 'updated'),
])
def test_update_command(monkeypatch, check, remote_sha, installs, output):
    calls = []
    monkeypatch.setattr(update, 'installed_source', lambda: ('https://x/dicos.git', 'a' * 40))
    monkeypatch.setattr(update, 'is_uv_tool_install', lambda: True)
    monkeypatch.setattr(update, 'remote_head', lambda url: remote_sha)
    monkeypatch.setattr(update, 'run_install', lambda argv: calls.append(argv) or 0)
    monkeypatch.setattr('shutil.which', lambda name: 'uv')
    result = runner.invoke(cli.app, ['update'] + (['--check'] if check else []))
    assert result.exit_code == 0, result.output
    assert output in result.output and calls == installs


def test_update_without_git_source(monkeypatch):
    monkeypatch.setattr(update, 'installed_source', lambda: None)
    result = runner.invoke(cli.app, ['update', '--check'])
    assert result.exit_code == 1 and 'no installed git source' in result.output
