import shlex
import subprocess

import pytest
from typer.testing import CliRunner

from dicos import cli, config, conn, jobs
from dicos.store import JobStore

runner = CliRunner()


def done(stdout='', stderr='', code=0):
    return subprocess.CompletedProcess([], code, stdout, stderr)


@pytest.fixture
def remote(tmp_path, monkeypatch):
    cfg = config.Config('alice', workdir='/work/alice/dicos', defaults={'partition': 'cpu'})
    config.save(cfg)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, '_remote_config', lambda: config.load())
    calls = []

    def run(cfg, cmd, input=None):
        calls.append((cmd, input))
        if cmd.startswith('sbatch'):
            return done('123;cluster\n')
        if cmd.startswith('squeue'):
            return done('123|PENDING\n' + jobs.REFRESH_MARKER + '\n')
        if cmd.startswith('scontrol'):
            return done('JobId=123 Reason=Resources Other=field')
        return done()

    monkeypatch.setattr(conn, 'run', run)
    monkeypatch.setattr(conn, 'exec_interactive', lambda cfg, cmd: calls.append((cmd, None)))
    original = subprocess.run

    def local(argv, **kwargs):
        if argv[0] == 'rsync':
            calls.append((argv, None))
            return done()
        return original(argv, **kwargs)

    monkeypatch.setattr(jobs.subprocess, 'run', local)
    return cfg, calls


def git(root, *args):
    subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True)


def test_project_resolution(tmp_path, monkeypatch):
    root = tmp_path / 'tree'
    root.mkdir()
    git(root, 'init')
    git(root, 'remote', 'add', 'origin', 'git@example.org:org/a.b.git')
    child = root / 'child'
    child.mkdir()
    cfg = config.Config('alice')
    resolved, name, project = jobs.resolve_project(cfg, child)
    assert resolved == root and name == 'a.b'
    assert project == {'repo': 'git@example.org:org/a.b.git', 'prefix': 'a_b', 'setup': None}
    assert config.load().projects == cfg.projects
    cfg.projects = {'custom': {'repo': project['repo'], 'prefix': 'custom', 'setup': 'module load cuda'}}
    assert jobs.resolve_project(cfg, child) == (root, 'custom', cfg.projects['custom'])
    cfg.projects['custom']['prefix'] = 'unsafe prefix'
    with pytest.raises(ValueError, match='prefix'):
        jobs.resolve_project(cfg, child)


def test_project_outside_git(tmp_path):
    cfg = config.Config('alice')
    root, name, project = jobs.resolve_project(cfg, tmp_path)
    assert root == tmp_path and name == tmp_path.name
    assert project['repo'] is None and project['setup'] is None


def test_git_without_origin(tmp_path):
    git(tmp_path, 'init')
    cfg = config.Config('alice')
    assert jobs.resolve_project(cfg, tmp_path)[2]['repo'] is None


def test_project_collision(tmp_path):
    git(tmp_path, 'init')
    git(tmp_path, 'remote', 'add', 'origin', 'https://example.org/demo.git')
    cfg = config.Config('alice', projects={'demo': {'repo': 'other', 'prefix': 'demo', 'setup': None}})
    with pytest.raises(ValueError, match='already registered'):
        jobs.resolve_project(cfg, tmp_path)


def test_resources():
    cfg = config.Config('alice', defaults={'partition': 'gpu', 'gpus': 2, 'cpus': 8, 'mem': '32G', 'time': '6h'})
    assert jobs.resources(cfg) == {'partition': 'gpu', 'gpus': 2, 'cpus': 8, 'mem': '32G', 'time': '06:00:00'}
    assert jobs.resources(cfg, 'cpu', 0, 1, '4G', '2m') == {
        'partition': 'cpu', 'gpus': 0, 'cpus': 1, 'mem': '4G', 'time': '00:02:00'}
    cfg.defaults = {'partition': 'cpu'}
    assert jobs.resources(cfg) == {'partition': 'cpu', 'gpus': 0, 'cpus': 1, 'mem': None, 'time': '01:00:00'}
    cfg.defaults = {}
    with pytest.raises(ValueError, match='Partition required'):
        jobs.resources(cfg)


def test_rsync(tmp_path):
    cfg = config.Config('alice', jump='lab')
    argv = jobs.rsync_argv(cfg, tmp_path, '/work/job')
    assert argv[:3] == ['rsync', '-az', '--delete']
    ssh = shlex.split(argv[argv.index('-e') + 1])
    assert ssh == conn.ssh_argv(cfg, None, tty=False)[:-1]
    assert 'alice@slurm-ui.twgrid.org' not in ssh
    for flag in ['ControlMaster=no', 'BatchMode=yes', 'ProxyCommand=false']:
        assert flag in ssh
    assert any(flag.startswith('ControlPath=') for flag in ssh)
    for flag in ['--exclude=.git/', '--exclude=.venv/', '--exclude=__pycache__/', '--filter=:- .gitignore']:
        assert flag in argv
    assert argv[-2:] == [str(tmp_path) + '/', 'alice@slurm-ui.twgrid.org:/work/job/src/']


@pytest.mark.parametrize('argv,command', [(['echo hello; true'], 'echo hello; true'),
                                         (['printf', '%s', 'two words'], "printf %s 'two words'")])
def test_submit(remote, argv, command):
    cfg, calls = remote
    result = runner.invoke(cli.app, ['run', '--sync', '-p', 'gpu', '--gpus', '2', '--cpus', '4',
                                     '--mem', '8G', '-t', '2m', '--', *argv])
    assert result.exit_code == 0, result.output
    job = JobStore().list()[0]
    assert f"{job['id']} (slurm 123) queued on gpu" in result.output
    assert job['mode'] == 'sync' and job['status'] == 'queued' and job['slurm_id'] == '123'
    assert job['command'] == command and job['time_limit'] == '00:02:00'
    jobdir = cfg.workdir + '/jobs/' + job['id']
    assert job['remote_dir'] == jobdir
    assert calls[0] == ('mkdir -p ' + jobdir, None)
    assert isinstance(calls[1][0], list) and calls[1][0][0] == 'rsync'
    assert calls[2][0] == 'cat > ' + jobdir + '/job.sh'
    script = calls[2][1]
    for expected in ['#SBATCH --partition=gpu', '#SBATCH --gres=gpu:2', '#SBATCH --cpus-per-task=4',
                     '#SBATCH --mem=8G', '#SBATCH --time=00:02:00', 'uv sync', command]:
        assert expected in script
    assert calls[3] == ('sbatch --parsable ' + jobdir + '/job.sh', None)


@pytest.mark.parametrize('failure', ['sbatch', 'cat', 'mkdir'])
def test_submit_failure(remote, monkeypatch, failure):
    _, calls = remote
    original = conn.run

    def fail(cfg, cmd, input=None):
        return done(stderr='submission denied', code=1) if cmd.startswith(failure) else original(cfg, cmd, input)

    monkeypatch.setattr(conn, 'run', fail)
    result = runner.invoke(cli.app, ['run', '--sync', '--', 'true'])
    assert result.exit_code == 1 and 'submission denied' in result.output
    job = JobStore().list()[0]
    assert job['status'] == 'failed' and job['error'] == 'submission denied'
    assert job['finished_at']


@pytest.mark.parametrize('args,message', [(['run', '--', 'true'], 'git mode not implemented yet, use --sync'),
                                         (['run', '--sync'], 'Provide a command')])
def test_run_rejected(remote, args, message):
    result = runner.invoke(cli.app, args)
    assert result.exit_code != 0 and message in result.output
    assert not remote[1]


@pytest.mark.parametrize('missing,message', [('workdir', 'run dicos init'), ('partition', 'Partition required')])
def test_run_missing_config(remote, missing, message):
    cfg, calls = remote
    if missing == 'workdir':
        cfg.workdir = None
    else:
        cfg.defaults = {}
    config.save(cfg)
    result = runner.invoke(cli.app, ['run', '--sync', '--', 'true'])
    assert result.exit_code == 1 and message in result.output and not calls


@pytest.mark.parametrize('state,status,code', [('RUNNING', 'running', 0), ('COMPLETED', 'succeeded', 0),
    ('FAILED', 'failed', 1), ('TIMEOUT', 'failed', 0), ('CANCELLED by 10', 'cancelled', 0),
    ('PENDING', 'queued', 0), ('REQUEUED', 'queued', 0), (None, 'lost', None)])
def test_refresh(remote, monkeypatch, state, status, code):
    cfg, calls = remote
    store = JobStore()
    id = store.add('demo', 'demo', slurm_id='123')
    if state == 'REQUEUED':
        store.update(id, status='running')
    terminal = store.add('demo', 'demo', status='succeeded', slurm_id='999')
    before = store.get(terminal)
    queue = '123|RUNNING\n' if state == 'RUNNING' else ''
    acct = f'123|{state}|{code}:0|2026-10-13T00:00:00|Unknown\n123.batch|FAILED|1:0|Unknown|Unknown\n' if state else ''
    monkeypatch.setattr(conn, 'run', lambda cfg, cmd, input=None:
                        calls.append((cmd, input)) or done(queue + jobs.REFRESH_MARKER + '\n' + acct))
    jobs.refresh(store, cfg)
    job = store.get(id)
    assert job['status'] == status and job['slurm_state'] == state
    assert job['exit_code'] == code
    assert job['error'] == (state if status == 'failed' else None)
    assert job['started_at'] == ('2026-10-13T00:00:00' if state else None)
    assert bool(job['finished_at']) == (status in {'failed', 'succeeded', 'cancelled', 'lost'})
    assert store.get(terminal) == before
    assert len(calls) == 1
    assert 'squeue -h -j 123' in calls[0][0]
    assert 'sacct -j 123 -P -n -o JobID,State,ExitCode,Start,End' in calls[0][0]
    assert '999' not in calls[0][0]


def test_refresh_batched(remote, monkeypatch):
    cfg, calls = remote
    store = JobStore()
    for i in range(2):
        store.add('demo', 'demo', slurm_id=str(123 + i))
    monkeypatch.setattr(conn, 'run', lambda cfg, cmd, input=None:
                        calls.append((cmd, input)) or done('123|RUNNING\n124|PENDING\n' + jobs.REFRESH_MARKER))
    jobs.refresh(store, cfg)
    assert len(calls) == 1 and '124,123' in calls[0][0]
    assert store.get('demo-1')['started_at']
    assert store.get('demo-2')['status'] == 'queued'


def add_job(status='queued'):
    return JobStore().add('demo', 'demo', slurm_id='123', status=status, partition='cpu', gpus=0,
                          command='echo hello', remote_dir='/work/jobs/demo-1')


def test_list_show(remote):
    add_job()
    result = runner.invoke(cli.app, ['list'], terminal_width=180)
    assert result.exit_code == 0, result.output
    for text in ['ID', 'STATUS', 'PARTITION', 'GPUS', 'SLURM', 'CREATED', 'COMMAND', 'demo-1', 'queued']:
        assert text in result.output
    result = runner.invoke(cli.app, ['show', 'demo-1'], terminal_width=180)
    assert result.exit_code == 0, result.output
    assert 'Reason' in result.output and 'Resources' in result.output
    for key in JobStore().get('demo-1'):
        assert key in result.output


def test_list_limits_filter(remote):
    for i in range(25):
        add_job('failed' if i % 2 else 'succeeded')
    result = runner.invoke(cli.app, ['list'], terminal_width=240)
    assert result.exit_code == 0
    assert 'demo-25' in result.output and 'demo-5 ' not in result.output
    result = runner.invoke(cli.app, ['list', '--all', '-s', 'succeeded'], terminal_width=240)
    assert 'demo-1 ' in result.output and 'demo-2 ' not in result.output
    assert not remote[1]


@pytest.mark.parametrize('follow', [False, True])
def test_logs(remote, follow):
    add_job()
    result = runner.invoke(cli.app, ['logs', 'demo-1', '-n', '20'] + (['-f'] if follow else []))
    assert result.exit_code == 0, result.output
    assert remote[1] == [('tail -n 20 ' + ('-F ' if follow else '') + '/work/jobs/demo-1/output.log', None)]


@pytest.mark.parametrize('status', ['queued', 'running', 'succeeded', 'failed', 'cancelled', 'lost'])
def test_stop(remote, status):
    add_job(status)
    result = runner.invoke(cli.app, ['stop', 'demo-1'])
    assert result.exit_code == 0, result.output
    if status in {'queued', 'running'}:
        assert remote[1] == [('scancel 123', None)]
        assert JobStore().get('demo-1')['status'] == 'cancelled'
    else:
        assert 'already finished' in result.output and not remote[1]


@pytest.mark.parametrize('command', ['show', 'logs', 'stop'])
def test_unknown_job(remote, command):
    result = runner.invoke(cli.app, [command, 'unknown'])
    assert result.exit_code == 1 and 'Unknown job' in result.output
    assert not remote[1]


def test_stop_failure(remote, monkeypatch):
    add_job()
    monkeypatch.setattr(conn, 'run', lambda *args, **kwargs: done(stderr='cancel failed', code=1))
    result = runner.invoke(cli.app, ['stop', 'demo-1'])
    assert result.exit_code == 1 and 'cancel failed' in result.output
    assert JobStore().get('demo-1')['status'] == 'queued'


def test_refresh_failure_no_changes(remote, monkeypatch):
    cfg, _ = remote
    id = add_job()
    store = JobStore()
    before = store.get(id)
    monkeypatch.setattr(conn, 'run', lambda *args, **kwargs: done(stderr='Slurm unavailable', code=1))
    with pytest.raises(ValueError, match='Slurm unavailable'):
        jobs.refresh(store, cfg)
    assert store.get(id) == before


def test_refresh_empty(remote):
    cfg, calls = remote
    jobs.refresh(JobStore(), cfg)
    assert not calls


def test_list_preserves_full_ids_at_default_width(remote):
    id = JobStore().add('tmp_R6x08ChS3H', 'tmp_R6x08ChS3H', status='succeeded',
                        slurm_id='123', partition='edr1-al9_short_serial', gpus=0,
                        command='python3 hi.py')
    result = runner.invoke(cli.app, ['list'], terminal_width=80)
    assert result.exit_code == 0 and id in result.output
