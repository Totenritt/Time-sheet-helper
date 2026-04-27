"""Thin wrapper over the OS credential manager via the ``keyring`` library.

The Jira API token is stored under service ``tsh-jira`` keyed by the user's
Jira email. Never stored in plaintext on disk.
"""

from __future__ import annotations

import keyring
import keyring.errors

SERVICE = "tsh-jira"


def set_token(email: str, token: str) -> None:
    """Store ``token`` for ``email`` in the OS credential manager."""
    keyring.set_password(SERVICE, email, token)


def get_token(email: str) -> str | None:
    """Return the stored token for ``email``, or None if not present."""
    return keyring.get_password(SERVICE, email)


def delete_token(email: str) -> None:
    """Remove the stored token for ``email``. No-op if absent."""
    try:
        keyring.delete_password(SERVICE, email)
    except keyring.errors.PasswordDeleteError:
        pass
