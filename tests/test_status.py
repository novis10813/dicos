import subprocess

import pytest
from typer.testing import CliRunner

from dicos import config, conn, credentials
from dicos.cli import app

runner = CliRunner()
SEPARATOR = '__DICOS_STATUS_SECTION__'
OUTPUT = f'''a100-al9*|gpu:a100:8|2/3/1/6|2-00:00:00
restricted|gpu:v100:8|0/2/0/2|06:00:00
cpu|(null)|0/4/0/4|infinite
{SEPARATOR}
PartitionName=a100-al9 AllowAccounts=IDLAB,other MaxTime=2-00:00:00 State=UP
PartitionName=restricted AllowAccounts=other MaxTime=06:00:00 State=UP
PartitionName=cpu AllowAccounts=ALL MaxTime=UNLIMITED State=UP
{SEPARATOR}
 idlab|
'''


@pytest.fixture
def remote(monkeypatch):
    config.save(config.Config('alice'))
    credentials.save(credentials.Credentials('synthetic-password', 'JBSWY3DPEHPK3PXP'))
    calls = []
    monkeypatch.setattr(conn, 'ensure_master', lambda *args: calls.append('master'))
    monkeypatch.setattr(conn, 'run', lambda cfg, cmd: calls.append(cmd) or subprocess.CompletedProcess([], 0, OUTPUT, ''))
    return calls


@pytest.mark.parametrize('all_partitions', [False, True])
def test_status(remote, all_partitions):
    result = runner.invoke(app, ['status'] + (['--all'] if all_partitions else []), terminal_width=140)
    assert result.exit_code == 0, result.output
    assert 'a100-al9' in result.output and 'cpu' in result.output
    assert ('restricted' in result.output) == all_partitions
    assert ('ALLOWED' in result.output) == all_partitions
    assert '3/6' in result.output and 'gpu:a100:8' in result.output
    assert 'PARTITION' in result.output and 'TIMELIMIT' in result.output
    assert len(remote) == 2 and remote[0] == 'master'
    for command in ["sinfo -h -o '%P|%G|%F|%l'", 'scontrol show partition -o',
                    'sacctmgr -n show assoc user=$USER format=account -P | head -1']:
        assert command in remote[1]
    if all_partitions:
        restricted_row = next(line for line in result.output.splitlines() if 'restricted' in line)
        assert 'no' in restricted_row


def test_login_error(remote, monkeypatch):
    def fail(*args):
        raise conn.LoginError('previous answer rejected')
    monkeypatch.setattr(conn, 'ensure_master', fail)
    result = runner.invoke(app, ['status'])
    assert result.exit_code == 1 and 'previous answer rejected' in result.output
    assert not remote


@pytest.mark.parametrize('stdout,stderr,code,message', [
    ('', 'sinfo failed', 1, 'sinfo failed'),
    ('broken', '', 0, 'Incomplete remote status response'),
    (OUTPUT.rsplit(SEPARATOR, 1)[0] + SEPARATOR + '\n', '', 0, 'No Slurm account'),
    (OUTPUT.replace('PartitionName=restricted', 'PartitionName=other'), '', 0, 'Missing account permissions')])
def test_remote_errors(remote, monkeypatch, stdout, stderr, code, message):
    monkeypatch.setattr(conn, 'run', lambda *args: subprocess.CompletedProcess([], code, stdout, stderr))
    result = runner.invoke(app, ['status', '--all'])
    assert result.exit_code == 1 and message in result.output


def test_run_login_error(remote, monkeypatch):
    def fail(*args):
        raise conn.LoginError('master unavailable')
    monkeypatch.setattr(conn, 'run', fail)
    result = runner.invoke(app, ['status'])
    assert result.exit_code == 1 and 'master unavailable' in result.output
