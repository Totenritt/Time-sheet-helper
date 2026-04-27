"""Tests for tsh.config.credentials.

The real OS keyring is never touched — we monkeypatch the keyring module
with an in-memory fake.
"""

from __future__ import annotations

import pytest

from tsh.config import credentials


class FakeKeyring:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def delete_password(self, service: str, username: str) -> None:
        if (service, username) in self.store:
            del self.store[(service, username)]
        else:
            raise credentials.keyring.errors.PasswordDeleteError("not found")


@pytest.fixture
def fake_keyring(monkeypatch: pytest.MonkeyPatch) -> FakeKeyring:
    fake = FakeKeyring()
    monkeypatch.setattr(credentials.keyring, "set_password", fake.set_password)
    monkeypatch.setattr(credentials.keyring, "get_password", fake.get_password)
    monkeypatch.setattr(credentials.keyring, "delete_password", fake.delete_password)
    return fake


def test_set_then_get_token(fake_keyring: FakeKeyring) -> None:
    credentials.set_token("casey@example.com", "abc123")
    assert credentials.get_token("casey@example.com") == "abc123"


def test_get_token_missing_returns_none(fake_keyring: FakeKeyring) -> None:
    assert credentials.get_token("nobody@example.com") is None


def test_delete_token(fake_keyring: FakeKeyring) -> None:
    credentials.set_token("casey@example.com", "abc123")
    credentials.delete_token("casey@example.com")
    assert credentials.get_token("casey@example.com") is None


def test_delete_missing_token_is_silent(fake_keyring: FakeKeyring) -> None:
    # No exception should escape — the desired postcondition (no token) holds.
    credentials.delete_token("nobody@example.com")
    assert credentials.get_token("nobody@example.com") is None


def test_uses_correct_service_name(fake_keyring: FakeKeyring) -> None:
    credentials.set_token("casey@example.com", "abc123")
    assert ("tsh-jira", "casey@example.com") in fake_keyring.store
