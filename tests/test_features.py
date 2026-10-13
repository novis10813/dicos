import shlex
import subprocess

import pytest
from typer.testing import CliRunner

from dicos import cli, config, conn, jobs
from dicos.store import JobStore

runner = CliRunner()
WORKDIR = '/work/alice/dicos'


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    cfg = config.Config('alice', jump='lab', workdir=WORKDIR)
    config.save(cfg)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, '_remote_config', lambda: config.load())
    return cfg


# fetch

def add_job(remote_dir=WORKDIR + '/jobs/demo-1'):
    return JobStore().add('demo', 'demo', status='succeeded', remote_dir=remote_dir, slurm_id='1')


@pytest.mark.parametrize('path,source', [
    ('out/model.pt', WORKDIR + '/jobs/demo-1/src/out/model.pt'),
    ('results/', WORKDIR + '/jobs/demo-1/src/results'),
    ('../output.log', WORKDIR + '/jobs/demo-1/src/../output.log'),
    ('/ceph/work/IDLAB/alice/data dir', '/ceph/work/IDLAB/alice/data dir'),
])
def test_fetch_argv(cfg, path, source):
    job = JobStore().get(add_job())
    argv = jobs.fetch_argv(cfg, job, path, 'dest')
    assert argv[:3] == ['rsync', '-az', '--protect-args']
    assert shlex.split(argv[argv.index('-e') + 1]) == conn.ssh_argv(cfg, None, tty=False)[:-1]
    assert argv[-2:] == [f'alice@slurm-ui.twgrid.org:{source}', 'dest/']


def test_fetch_command(cfg, tmp_path, monkeypatch):
    id = add_job()
    calls = []
    monkeypatch.setattr('subprocess.run', lambda argv, **kw: calls.append(argv) or
                        subprocess.CompletedProcess(argv, 0, '', ''))
    result = runner.invoke(cli.app, ['fetch', id, 'out/', 'metrics.json'])
    assert result.exit_code == 0, result.output
    assert (tmp_path / id).is_dir()
    assert [argv[-2:] for argv in calls] == [
        [f'alice@slurm-ui.twgrid.org:{WORKDIR}/jobs/demo-1/src/out', f'{id}/'],
        [f'alice@slurm-ui.twgrid.org:{WORKDIR}/jobs/demo-1/src/metrics.json', f'{id}/']]
    assert f'out/ -> {id}/out' in result.output and f'metrics.json -> {id}/metrics.json' in result.output
    calls.clear()
    result = runner.invoke(cli.app, ['fetch', id, 'x', '-o', str(tmp_path / 'here')])
    assert result.exit_code == 0 and calls[0][-1] == str(tmp_path / 'here') + '/'


def test_fetch_errors(cfg, monkeypatch):
    result = runner.invoke(cli.app, ['fetch', 'nope', 'x'])
    assert result.exit_code == 1 and 'Unknown job' in result.output
    id = add_job()
    monkeypatch.setattr('subprocess.run', lambda argv, **kw: subprocess.CompletedProcess(
        argv, 23, '', 'rsync: link_stat "x" failed: No such file or directory'))
    result = runner.invoke(cli.app, ['fetch', id, 'x'])
    assert result.exit_code == 1 and 'fetch x failed' in result.output and 'No such file' in result.output
    no_dir = JobStore().add('demo', 'demo', status='failed')
    result = runner.invoke(cli.app, ['fetch', no_dir, 'x'])
    assert result.exit_code == 1 and 'no remote directory' in result.output


# init defaults

PARTITIONS = [('a100-al9', {}, True), ('a100_devel-al9', {}, True), ('secret-al9', {}, False)]


@pytest.fixture
def partitions(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, '_partitions', lambda cfg: calls.append(cfg) or PARTITIONS)
    return calls


def test_defaults_skipped_without_terminal(cfg, partitions, monkeypatch):
    monkeypatch.setattr(cli, '_interactive', lambda: False)
    assert cli._ask_defaults(cfg, None, None) == {}
    assert not partitions


def test_defaults_from_flags(cfg, partitions, monkeypatch):
    monkeypatch.setattr(cli, '_interactive', lambda: False)
    assert cli._ask_defaults(cfg, 'a100-al9', '6h') == {'partition': 'a100-al9', 'time': '6h'}
    with pytest.raises(ValueError, match='not available'):
        cli._ask_defaults(cfg, 'secret-al9', None)
    with pytest.raises(ValueError, match='Invalid time'):
        cli._ask_defaults(cfg, None, 'soon')


def test_defaults_prompts(cfg, partitions, monkeypatch, capsys):
    monkeypatch.setattr(cli, '_interactive', lambda: True)
    answers = iter(['secret-al9', 'a100_devel-al9', 'later', '15m'])
    monkeypatch.setattr(cli.typer, 'prompt', lambda label, **kw: next(answers))
    assert cli._ask_defaults(cfg, None, None) == {'partition': 'a100_devel-al9', 'time': '15m'}
    out = capsys.readouterr().out
    assert 'a100-al9, a100_devel-al9' in out and 'secret-al9' not in out.split('\n')[0]
    assert 'Choose a listed partition' in out and 'Use a duration' in out


def test_defaults_prompts_empty(cfg, partitions, monkeypatch):
    monkeypatch.setattr(cli, '_interactive', lambda: True)
    answers = iter(['', ''])
    monkeypatch.setattr(cli.typer, 'prompt', lambda label, **kw: next(answers))
    assert cli._ask_defaults(cfg, None, None) == {}


def test_init_saves_defaults(monkeypatch, partitions):
    monkeypatch.setattr(cli.init_net, 'probe_jumps', lambda candidates, **kw: dict.fromkeys(candidates, True))
    monkeypatch.setattr(cli.init_net, 'connect_and_detect', lambda cfg, creds: WORKDIR)
    result = runner.invoke(cli.app, ['init', '--user', 'alice', '--no-jump', '--password-stdin',
                                     '--totp-secret-stdin', '--default-partition', 'a100-al9',
                                     '--default-time', '2h'], input='synthetic-password\nJBSWY3DPEHPK3PXP\n')
    assert result.exit_code == 0, result.output
    saved = config.load()
    assert saved.workdir == WORKDIR and saved.defaults == {'partition': 'a100-al9', 'time': '2h'}


# feedback

@pytest.mark.parametrize('args,extra', [(['Bug', '--body', 'It broke'], ['--title', 'Bug']),
                                       (['Bug'], ['--title', 'Bug'])])
def test_feedback(monkeypatch, args, extra):
    calls = []
    monkeypatch.setattr('subprocess.run', lambda argv, **kw: calls.append(argv) or
                        subprocess.CompletedProcess(argv, 0))
    result = runner.invoke(cli.app, ['feedback', *args])
    assert result.exit_code == 0, result.output
    body = ('It broke\n\n' if '--body' in args else '') + f'dicos version: {cli.__version__}'
    assert calls == [['gh', 'issue', 'create', '--repo', 'novis10813/dicos', '--body', body, *extra]]


def test_feedback_editor(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, '_interactive', lambda: True)
    monkeypatch.setattr('subprocess.run', lambda argv, **kw: calls.append(argv) or
                        subprocess.CompletedProcess(argv, 0))
    assert runner.invoke(cli.app, ['feedback']).exit_code == 0
    assert calls[0][-1] == '--editor'


@pytest.mark.parametrize('args,message', [(['  '], 'must not be empty'), ([], 'interactive terminal')])
def test_feedback_rejected(monkeypatch, args, message):
    monkeypatch.setattr('subprocess.run', lambda *a, **kw: pytest.fail('gh must not run'))
    result = runner.invoke(cli.app, ['feedback', *args])
    assert result.exit_code == 1 and message in result.output


@pytest.mark.parametrize('error,message', [(FileNotFoundError, 'gh) is required'), (None, 'exit 4')])
def test_feedback_gh_failure(monkeypatch, error, message):
    def run(argv, **kw):
        if error:
            raise error()
        return subprocess.CompletedProcess(argv, 4)
    monkeypatch.setattr('subprocess.run', run)
    result = runner.invoke(cli.app, ['feedback', 'Bug'])
    assert result.exit_code == 1 and message in result.output
