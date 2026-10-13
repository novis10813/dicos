import json
import re
import stat

import pytest
from typer.testing import CliRunner

from dicos import config, credentials, init_net, otp, paths
from dicos.cli import app, _choose_account

SECRET = "JBSWY3DPEHPK3PXP"  # Synthetic test fixture, not a live credential.
runner = CliRunner()


def test_home_modes(private_home):
    assert paths.ensure_home() == private_home
    assert stat.S_IMODE(private_home.stat().st_mode) == 0o700
    private_home.chmod(0o755)
    paths.ensure_home()
    assert stat.S_IMODE(private_home.stat().st_mode) == 0o700


def test_private_atomic_write(private_home, monkeypatch):
    path = paths.ensure_home() / "file"
    path.write_text("old")
    path.chmod(0o644)
    replace = paths.os.replace
    seen = []

    def check_replace(src, dst):
        assert dst.read_text() == "old"
        assert src.read_text() == "new"
        assert stat.S_IMODE(src.stat().st_mode) == 0o600
        seen.append(True)
        replace(src, dst)

    monkeypatch.setattr(paths.os, "replace", check_replace)
    paths.write_private(path, "new")
    assert seen and path.read_text() == "new"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert list(private_home.iterdir()) == [path]


def test_failed_atomic_write(private_home, monkeypatch):
    path = paths.ensure_home() / "file"
    path.write_text("old")

    def fail(*args):
        raise OSError("replace failed")

    monkeypatch.setattr(paths.os, "replace", fail)
    with pytest.raises(OSError):
        paths.write_private(path, "new")
    assert path.read_text() == "old"
    assert list(private_home.iterdir()) == [path]


def test_config_roundtrip(private_home):
    cfg = config.Config("alice", jump="lab", workdir="/work", defaults={"gpus": 1}, projects={"p": {"repo": "url"}})
    config.save(cfg)
    assert config.load() == cfg
    assert stat.S_IMODE((private_home / "config.yaml").stat().st_mode) == 0o600


def test_missing_state():
    for load in (config.load, credentials.load):
        with pytest.raises(ValueError, match="dicos init"):
            load()


def test_credentials(private_home):
    creds = credentials.Credentials("synthetic-password", SECRET)
    credentials.save(creds)
    assert credentials.load() == creds
    assert creds.password not in repr(creds) and SECRET not in repr(creds)
    path = private_home / "credentials.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.chmod(0o644)
    with pytest.raises(ValueError, match="chmod 600"):
        credentials.load()


def test_bad_credentials_sanitized(private_home):
    path = paths.ensure_home() / "credentials.json"
    paths.write_private(path, '{"synthetic-password": bad}')
    with pytest.raises(ValueError) as error:
        credentials.load()
    assert "synthetic-password" not in str(error.value)


@pytest.mark.parametrize("flags,input_text", [
    (["--password-stdin", "--totp-secret-stdin"], "synthetic-password\n" + SECRET + "\n"),
    (["--totp-secret-stdin"], SECRET + "\nsynthetic-password\n"),
    (["--password-stdin"], "synthetic-password\n" + SECRET + "\n"),
])
def test_init_stdin(private_home, monkeypatch, flags, input_text):
    calls = []

    def connect(cfg, creds):
        assert config.load() == cfg
        assert credentials.load() == creds
        calls.append(True)
        return "/detected"

    monkeypatch.setattr(init_net, "connect_and_detect", connect)
    result = runner.invoke(app, ["init", "--user", "alice", "--no-jump", *flags], input=input_text)
    assert result.exit_code == 0, result.output
    assert calls and config.load().workdir == "/detected"
    assert credentials.load().password == "synthetic-password"
    assert SECRET not in result.output and "synthetic-password" not in result.output
    assert re.search(r"Current TOTP code: \d{6}", result.output)


def test_init_overwrite(monkeypatch):
    config.save(config.Config("original"))
    args = ["init", "--user", "alice", "--jump", "lab", "--password-stdin", "--totp-secret-stdin"]
    result = runner.invoke(app, args)
    assert result.exit_code == 1 and "--force" in result.output
    assert config.load().user == "original"
    monkeypatch.setattr(init_net, "connect_and_detect", lambda cfg, creds: None)
    result = runner.invoke(app, [*args, "--force"], input="synthetic-password\n" + SECRET + "\n")
    assert result.exit_code == 0, result.output
    assert config.load().jump == "lab"


def test_code():
    credentials.save(credentials.Credentials("synthetic-password", SECRET))
    result = runner.invoke(app, ["code"])
    assert result.exit_code == 0
    assert re.fullmatch(r"\d{6}\n", result.output)


def test_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0 and result.output.strip() == "0.1.0"


def test_jump_aliases(tmp_path, monkeypatch):
    monkeypatch.setattr(init_net.Path, "home", lambda: tmp_path)
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".ssh/config").write_text("Host lab lab2 *.bad !excluded\nHost *\n  HostName ignored\n")
    monkeypatch.setattr(init_net.typer, "prompt", lambda *a, **kw: "lab2")
    assert init_net.select_jump() == "lab2"
    monkeypatch.setattr(init_net.typer, "prompt", lambda *a, **kw: "none")
    assert init_net.select_jump() is None


def test_account_selection(monkeypatch):
    accounts = [otp.OtpAccount("other", "", SECRET), otp.OtpAccount("DiCoS:user", "", SECRET)]
    assert _choose_account(accounts) is accounts[1]
    accounts[1].name = "second"
    monkeypatch.setattr("dicos.cli.typer.prompt", lambda *a, **kw: 2)
    assert _choose_account(accounts) is accounts[1]


def test_init_qr(monkeypatch):
    monkeypatch.setattr(otp, "decode_qr", lambda path: SECRET)
    monkeypatch.setattr(init_net, "connect_and_detect", lambda cfg, creds: None)
    result = runner.invoke(app, ["init", "--user", "alice", "--no-jump", "--qr", "fake.png", "--password-stdin"], input="synthetic-password\n")
    assert result.exit_code == 0, result.output


def test_init_interactive_qr(monkeypatch):
    seen = []
    monkeypatch.setattr(otp, "decode_qr", lambda path: seen.append(path) or SECRET)
    monkeypatch.setattr(init_net, "select_jump", lambda: "lab")
    result = runner.invoke(app, ["init"], input="alice\n\nfake.png\nsynthetic-password\n")
    assert result.exit_code == 0, result.output
    assert seen == ["fake.png"] and config.load().jump == "lab"
    assert "Remote verification is not yet implemented" in result.output
    assert SECRET not in result.output and "synthetic-password" not in result.output


@pytest.mark.parametrize("flags", [["--jump", "lab", "--no-jump"], ["--qr", "fake.png", "--totp-secret-stdin"]])
def test_conflicting_flags(flags):
    result = runner.invoke(app, ["init", *flags])
    assert result.exit_code != 0


def test_missing_stdin():
    result = runner.invoke(app, ["init", "--user", "alice", "--no-jump", "--password-stdin", "--totp-secret-stdin"], input="synthetic-password\n")
    assert result.exit_code == 1 and "Missing TOTP line" in result.output
    assert not paths.home().exists()
