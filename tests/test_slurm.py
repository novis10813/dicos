import pytest

from dicos import slurm


@pytest.mark.parametrize('value,expected', [
    ('30m', '00:30:00'), ('6h', '06:00:00'), ('2d', '2-00:00:00'),
    ('1d12h', '1-12:00:00'), ('90s', '00:01:30'), ('25h', '1-01:00:00'),
    ('1h30m15s', '01:30:15'), ('06:00:00', '06:00:00'),
    ('1-00:00:00', '1-00:00:00'), ('30', '30'), ('20:30', '20:30'),
    ('2-12', '2-12'), ('2-12:30', '2-12:30'), ('0s', '00:00:00')])
def test_time(value, expected):
    assert slurm.parse_time(value) == expected


@pytest.mark.parametrize('value', ['', 'abc', '-1h', '1.5h', '1m2h', '6H', '1h garbage', '1:2:3:4'])
def test_time_invalid(value):
    with pytest.raises(ValueError):
        slurm.parse_time(value)


def test_job_script():
    assert slurm.render_job_script('demo-1', 'a100-al9', '6h', 4, '32G', 2,
                                   '/work/jobs/demo-1', 'module load cuda/12.0',
                                   'python main.py --flag', '/work/jobs/demo-1/src') == '''#!/bin/bash
#SBATCH --job-name=demo-1
#SBATCH --partition=a100-al9
#SBATCH --time=06:00:00
#SBATCH --cpus-per-task=4
#SBATCH --mem=32G
#SBATCH --gres=gpu:2
#SBATCH --output=/work/jobs/demo-1/output.log
#SBATCH --error=/work/jobs/demo-1/output.log
set -eo pipefail
export DICOS_JOB_ID=demo-1 PYTHONUNBUFFERED=1
export PATH="$HOME/.local/bin:$PATH"
cd /work/jobs/demo-1/src
module load cuda/12.0
python main.py --flag
'''


@pytest.mark.parametrize('setup', [None, ''])
def test_optional_setup(setup):
    script = slurm.render_job_script('x-1', 'cpu', '30m', 1, None, 0,
                                    '/work/job', setup, 'echo "$HOME"', '/work/job/src')
    assert '--mem=' not in script and '--gres=' not in script
    assert script.endswith('echo "$HOME"\n')
    assert ('if [ -f pyproject.toml ]; then' in script) == (setup is None)
    if setup is None:
        assert 'command -v uv' in script
        assert 'curl -LsSf https://astral.sh/uv/install.sh | sh' in script
        assert '    uv sync\nfi' in script


def test_script_quotes_paths():
    script = slurm.render_job_script('x-1', 'cpu', '30', 1, None, 0,
                                    '/work/a b', '', 'pwd', '/work/a b/src')
    assert "#SBATCH --output='/work/a b/output.log'" in script
    assert "cd '/work/a b/src'" in script


@pytest.mark.parametrize('raw,expected', [
    *[(state, 'queued') for state in ['PENDING', 'REQUEUED', 'CONFIGURING']],
    *[(state, 'running') for state in ['RUNNING', 'COMPLETING']],
    ('COMPLETED', 'succeeded'), ('COMPLETED+', 'succeeded'),
    *[(state, 'failed') for state in ['FAILED', 'TIMEOUT', 'OUT_OF_MEMORY', 'NODE_FAIL',
                                    'BOOT_FAIL', 'DEADLINE', 'PREEMPTED']],
    ('CANCELLED', 'cancelled'), ('CANCELLED+', 'cancelled'), ('CANCELLED by 1001', 'cancelled'),
    ('CANCELLED+ by 1001', 'cancelled'), ('', 'lost'), ('UNKNOWN', 'lost')])
def test_state(raw, expected):
    assert slurm.map_state(raw) == expected


@pytest.mark.parametrize('text', ['12345', '12345;cluster', '12345;cluster\n'])
def test_sbatch(text):
    assert slurm.parse_sbatch_parsable(text) == '12345'


@pytest.mark.parametrize('text', ['', 'Submitted batch job 12345', 'abc'])
def test_sbatch_invalid(text):
    with pytest.raises(ValueError):
        slurm.parse_sbatch_parsable(text)


def test_squeue():
    assert slurm.parse_squeue('\n 12345|PENDING\n12346_0|RUNNING\n') == {
        '12345': 'PENDING', '12346_0': 'RUNNING'}
    assert slurm.parse_squeue('') == {}


def test_sacct():
    text = '''12345|COMPLETED|0:0|2026-10-13T01:00:00|2026-10-13T02:00:00|
12345.batch|COMPLETED|0:0|2026-10-13T01:00:00|2026-10-13T02:00:00|
12345.extern|COMPLETED|0:0|2026-10-13T01:00:00|2026-10-13T02:00:00|
12346_0|FAILED|7:9|Unknown|Unknown|
12346_0.0|FAILED|7:9|Unknown|Unknown|
12347|CANCELLED by 1001|0:15|Unknown|Unknown|
'''
    assert slurm.parse_sacct(text) == {
        '12345': ('COMPLETED', 0, '2026-10-13T01:00:00', '2026-10-13T02:00:00'),
        '12346_0': ('FAILED', 7, 'Unknown', 'Unknown'),
        '12347': ('CANCELLED by 1001', 0, 'Unknown', 'Unknown')}
    assert slurm.parse_sacct('') == {}


def test_partitions():
    text = '''PartitionName=a100-al9 AllowGroups=ALL AllowAccounts=idlab,other AllowQos=ALL Default=NO MaxTime=2-00:00:00 State=UP TRES=cpu=256,mem=1T,gres/gpu=16
PartitionName=cpu AllowAccounts=ALL MaxTime=UNLIMITED State=DOWN Gres=(null)
PartitionName=default MaxTime=01:00:00 State=UP
'''
    assert slurm.parse_partitions(text) == [
        {'name': 'a100-al9', 'allow_accounts': ['idlab', 'other'], 'max_time': '2-00:00:00',
         'state': 'UP', 'tres': 'cpu=256,mem=1T,gres/gpu=16'},
        {'name': 'cpu', 'allow_accounts': ['ALL'], 'max_time': 'UNLIMITED', 'state': 'DOWN', 'gres': '(null)'},
        {'name': 'default', 'allow_accounts': ['ALL'], 'max_time': '01:00:00', 'state': 'UP'}]
    assert slurm.parse_partitions('') == []


def test_sinfo():
    assert slurm.parse_sinfo('a100-al9*|gpu:a100:8(S:0-1)|2/3/1/6|2-00:00:00\ncpu|(null)|0/4/0/4|infinite\n') == {
        'a100-al9': {'gres': 'gpu:a100:8(S:0-1)', 'nodes': {'allocated': 2, 'idle': 3, 'other': 1, 'total': 6}, 'timelimit': '2-00:00:00'},
        'cpu': {'gres': '(null)', 'nodes': {'allocated': 0, 'idle': 4, 'other': 0, 'total': 4}, 'timelimit': 'infinite'}}
    assert slurm.parse_sinfo('') == {}
