import pytest


@pytest.fixture(autouse=True)
def private_home(tmp_path, monkeypatch):
    path = tmp_path / "dicos"
    monkeypatch.setenv("DICOS_HOME", str(path))
    return path
