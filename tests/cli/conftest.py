"""CLI test fixtures."""
import pytest
from click.testing import CliRunner


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Point TSH_CONFIG_DIR at a temp dir for the test."""
    monkeypatch.setenv("TSH_CONFIG_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def fake_keyring(monkeypatch):
    """Replace keyring functions with an in-memory fake."""
    from tsh.config import credentials

    store: dict[tuple[str, str], str] = {}

    def set_pw(service, username, password):
        store[(service, username)] = password

    def get_pw(service, username):
        return store.get((service, username))

    def del_pw(service, username):
        if (service, username) in store:
            del store[(service, username)]
        else:
            raise credentials.keyring.errors.PasswordDeleteError("not found")

    monkeypatch.setattr(credentials.keyring, "set_password", set_pw)
    monkeypatch.setattr(credentials.keyring, "get_password", get_pw)
    monkeypatch.setattr(credentials.keyring, "delete_password", del_pw)
    return store
