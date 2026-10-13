import shlex
import subprocess

import pytest
from typer.testing import CliRunner

from dicos import cli, config, conn, gitutil, jobs
from dicos.store import JobStore

runner = CliRunner()


def git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """Local clone of a bare 'origin' with one pushed commit on main."""
    origin = tmp_path / 'origin.git'
    subprocess.run(['git', 'init', '--bare', '-b', 'main', str(origin)], check=True, capture_output=True)
    work = tmp_path / 'work'
    subprocess.run(['git', 'clone', '-q', str(origin), str(work)], check=True, capture_output=True)
    git(work, 'config', 'user.email', 't@example.org')
    git(work, 'config', 'user.name', 't')
    git(work, 'checkout', '-q', '-b', 'main')
    (work / 'a.txt').write_text('one\n')
    git(work, 'add', 'a.txt')
    git(work, 'commit', '-qm', 'one')
    git(work, 'push', '-q', 'origin', 'main')
    return work


def commit(work, name):
    (work / name).write_text(name)
    git(work, 'add', name)
    git(work, 'commit', '-qm', name)
    return git(work, 'rev-parse', 'HEAD')


def test_head_clean_and_pushed(repo):
    assert gitutil.resolve_commit(repo) == git(repo, 'rev-parse', 'HEAD')


@pytest.mark.parametrize('change', ['modified', 'untracked'])
def test_head_dirty(repo, change):
    (repo / ('a.txt' if change == 'modified' else 'new.txt')).write_text('x')
    with pytest.raises(ValueError, match='dirty'):
        gitutil.resolve_commit(repo)


def test_head_unpushed(repo):
    commit(repo, 'b.txt')
    with pytest.raises(ValueError, match='not on any remote branch'):
        gitutil.resolve_commit(repo)


def test_branch_target(repo):
    git(repo, 'checkout', '-q', '-b', 'feature')
    sha = commit(repo, 'b.txt')
    with pytest.raises(ValueError, match='not published'):
        gitutil.resolve_commit(repo, branch='feature')
    git(repo, 'push', '-q', 'origin', 'feature')
    (repo / 'a.txt').write_text('dirty')  # explicit targets allow a dirty tree
    assert gitutil.resolve_commit(repo, branch='feature') == sha
    assert not git(repo, 'for-each-ref', 'refs/dicos-submit')


@pytest.mark.parametrize('annotated', [False, True])
def test_tag_target(repo, annotated):
    sha = commit(repo, 'b.txt')
    git(repo, 'tag', *(['-a', '-m', 'v1'] if annotated else []), 'v1')
    with pytest.raises(ValueError, match='not published'):
        gitutil.resolve_commit(repo, tag='v1')
    git(repo, 'push', '-q', 'origin', 'v1')
    assert gitutil.resolve_commit(repo, tag='v1') == sha
    assert not git(repo, 'for-each-ref', 'refs/dicos-submit')


def test_commit_target(repo):
    sha = git(repo, 'rev-parse', 'HEAD')
    assert gitutil.resolve_commit(repo, commit=sha[:8]) == sha
    unpushed = commit(repo, 'b.txt')
    with pytest.raises(ValueError, match='not published'):
        gitutil.resolve_commit(repo, commit=unpushed)
    with pytest.raises(ValueError, match='hex'):
        gitutil.resolve_commit(repo, commit='HEAD')
    tree = git(repo, 'rev-parse', 'HEAD^{tree}')
    with pytest.raises(ValueError, match='commit object'):
        gitutil.resolve_commit(repo, commit=tree)


def test_target_errors(repo, tmp_path):
    with pytest.raises(ValueError, match='mutually exclusive'):
        gitutil.resolve_commit(repo, branch='main', tag='v1')
    with pytest.raises(ValueError, match='invalid branch'):
        gitutil.resolve_commit(repo, branch='bad..name')
    git(repo, 'remote', 'remove', 'origin')
    with pytest.raises(ValueError, match='origin remote'):
        gitutil.resolve_commit(repo)
    plain = tmp_path / 'plain'
    plain.mkdir()
    with pytest.raises(ValueError, match='not a git repository'):
        gitutil.resolve_commit(plain)


def test_ship_script_runs(repo, tmp_path):
    """Run the login-node script locally: first clone, then fetch for a newer commit."""
    workdir = tmp_path / 'remote'
    origin = git(repo, 'remote', 'get-url', 'origin')
    first = git(repo, 'rev-parse', 'HEAD')
    for n, sha in enumerate([first, None]):
        if sha is None:
            sha = commit(repo, 'b.txt')
            git(repo, 'push', '-q', 'origin', 'main')
        jobdir = f'{workdir}/jobs/demo-{n + 1}'
        script = jobs.git_ship_script(str(workdir), 'demo', origin, jobdir, sha)
        subprocess.run(['bash', '-s'], input=script, text=True, check=True, capture_output=True)
        assert git(jobdir + '/src', 'rev-parse', 'HEAD') == sha
        assert (workdir / 'jobs' / f'demo-{n + 1}' / 'src' / 'a.txt').exists()
    assert (workdir / 'repos' / 'demo' / '.git').is_dir()
    assert (workdir / 'jobs' / 'demo-2' / 'src' / 'b.txt').exists()
    assert not (workdir / 'jobs' / 'demo-1' / 'src' / 'b.txt').exists()


@pytest.fixture
def remote(repo, monkeypatch):
    config.save(config.Config('alice', workdir='/work/alice/dicos', defaults={'partition': 'cpu'}))
    monkeypatch.chdir(repo)
    monkeypatch.setattr(cli, '_remote_config', lambda: config.load())
    calls = []

    def run(cfg, cmd, input=None, agent=False, **kw):
        calls.append((cmd, input, agent))
        if cmd.startswith('sbatch'):
            return subprocess.CompletedProcess([], 0, '77\n', '')
        return subprocess.CompletedProcess([], 0, '', '')

    monkeypatch.setattr(conn, 'run', run)
    return calls


def test_run_git_mode(remote, repo):
    sha = git(repo, 'rev-parse', 'HEAD')
    result = runner.invoke(cli.app, ['run', '--', 'python', 'train.py'])
    assert result.exit_code == 0, result.output
    job = JobStore().list()[0]
    assert f"{job['id']} (slurm 77) queued on cpu" in result.output
    assert job['mode'] == 'git' and job['commit_sha'] == sha and job['id'] == 'origin-1'
    jobdir = '/work/alice/dicos/jobs/origin-1'
    ship, _, agent = remote[0]
    assert ship == 'bash -s' and agent
    script = remote[0][1]
    assert f'git clone --quiet --no-checkout {shlex.quote(str(repo.parent / "origin.git"))} /work/alice/dicos/repos/origin' in script
    assert f'worktree add --quiet --detach {jobdir}/src {sha}' in script
    assert remote[1][0] == f'cat > {jobdir}/job.sh' and f'cd {jobdir}/src' in remote[1][1]
    assert remote[2][0] == f'sbatch --parsable {jobdir}/job.sh'


def test_run_git_rejects_dirty_without_login(remote, repo, monkeypatch):
    monkeypatch.setattr(cli, '_remote_config', lambda: pytest.fail('must not log in'))
    (repo / 'a.txt').write_text('dirty')
    result = runner.invoke(cli.app, ['run', '--', 'true'])
    assert result.exit_code == 1 and 'dirty' in result.output
    assert not remote and not JobStore().list()


def test_run_git_checkout_failure(remote, monkeypatch):
    monkeypatch.setattr(conn, 'run', lambda cfg, cmd, **kw: subprocess.CompletedProcess(
        [], 128, '', 'fatal: repository not found'))
    result = runner.invoke(cli.app, ['run', '--', 'true'])
    assert result.exit_code == 1 and 'repository not found' in result.output
    job = JobStore().list()[0]
    assert job['status'] == 'failed' and 'git checkout' in job['error']
