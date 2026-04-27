"""Synchronous Jira REST API client.

Wraps four endpoints used by the timesheet helper:
  - GET  /rest/api/3/myself             (auth test)
  - GET  /rest/api/3/search             (pick in-progress tickets)
  - GET  /rest/api/3/issue/{key}        (resolve a typed ticket key)
  - POST /rest/api/3/issue/{key}/worklog (submit a worklog entry)

Uses HTTP Basic auth (email + API token). Retries once on 5xx and connection
errors with 1 s backoff; never retries on 4xx.

The ``started`` field on worklog POSTs must be a pre-formatted string produced
by ``tsh.core.timezones.to_jira_started`` — this client does NOT format
datetimes, keeping timezone discipline in a single place.
"""

from __future__ import annotations

import time
from typing import Any, Callable

import httpx

# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------


class JiraError(Exception):
    """Base for all Jira-related errors raised by JiraClient."""


class JiraAuthError(JiraError):
    """Raised on 401/403 — credentials are wrong or expired."""


class JiraNotFoundError(JiraError):
    """Raised on 404 — ticket key doesn't exist."""


class JiraServerError(JiraError):
    """Raised on 5xx after retries are exhausted, or on connection errors."""


class JiraClientError(JiraError):
    """Raised on other 4xx — usually a bad request payload."""


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

_DEFAULT_JQL = 'assignee = currentUser() AND status = "In Progress"'


class JiraClient:
    """Thin synchronous wrapper around the Jira REST API.

    Constructed with credentials; uses HTTP basic auth (email + API token).
    Times out after 10 seconds. Retries once on 5xx and connection errors
    with 1s backoff; never retries on 4xx.

    The ``started`` field on worklogs must be a pre-formatted string built by
    ``tsh.core.timezones.to_jira_started`` — this client does not format
    datetimes.
    """

    DEFAULT_TIMEOUT = 10.0
    RETRY_BACKOFF_SECONDS = 1.0

    def __init__(
        self,
        base_url: str,
        email: str,
        token: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        client: httpx.Client | None = None,
        _sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Initialise the client.

        Args:
            base_url: Jira instance root URL, e.g. ``https://foo.atlassian.net``.
            email: Atlassian account email address.
            token: Atlassian API token.
            timeout: Request timeout in seconds (default 10).
            client: Pre-built ``httpx.Client`` to use (for tests). If provided,
                the email/token/timeout args are ignored (the caller's client
                should already be configured with auth and timeout).
            _sleep: Callable used between retries. Defaults to ``time.sleep``;
                inject a ``Mock`` in tests to avoid real delays.
        """
        self._sleep = _sleep

        if client is not None:
            self._client = client
        else:
            self._client = httpx.Client(
                auth=(email, token),
                base_url=base_url,
                timeout=timeout,
                headers={"Accept": "application/json"},
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def auth_test(self) -> dict[str, Any]:
        """GET /rest/api/3/myself — returns the parsed JSON dict.

        Used by ``tsh auth test`` to confirm credentials work. The dict
        includes ``displayName``, ``emailAddress``, and ``accountId``.

        Raises:
            JiraAuthError: on 401 or 403.
        """
        return self._request("GET", "/rest/api/3/myself")

    def search_in_progress(
        self, jql_override: str | None = None
    ) -> list[dict[str, Any]]:
        """GET /rest/api/3/search with the picker JQL.

        Default JQL: ``assignee = currentUser() AND status = "In Progress"``.
        Pass ``jql_override`` to use a different query (e.g., from config's
        optional ``[jira] picker_jql``).

        Returns the ``issues`` array from the response (may be empty). Each
        item is a dict with at least ``key``, ``fields.summary``,
        ``fields.status.name``, ``fields.assignee.emailAddress``.

        Raises:
            JiraAuthError: on 401 or 403.
            JiraServerError: on 5xx after retry.
        """
        jql = jql_override if jql_override is not None else _DEFAULT_JQL
        data = self._request("GET", "/rest/api/3/search", params={"jql": jql})
        return data["issues"]

    def get_issue(self, key: str) -> dict[str, Any]:
        """GET /rest/api/3/issue/{key} — returns the parsed JSON dict.

        Used to resolve a manually-typed ticket key.

        Raises:
            JiraNotFoundError: on 404.
            JiraAuthError: on 401 or 403.
        """
        return self._request("GET", f"/rest/api/3/issue/{key}")

    def post_worklog(
        self,
        key: str,
        started: str,
        time_spent_seconds: int,
        comment: str,
    ) -> dict[str, Any]:
        """POST /rest/api/3/issue/{key}/worklog — returns the created worklog dict.

        Args:
            key: Jira issue key (e.g., ``SFXS-1073``).
            started: ISO 8601 string with explicit offset. **Must** be built by
                ``tsh.core.timezones.to_jira_started``; this client sends it
                verbatim without any transformation.
            time_spent_seconds: Integer seconds (caller is responsible for
                rounding to the desired bucket, e.g. 15-minute granularity).
            comment: Worklog comment text. Wrapped in Atlassian Document Format
                (ADF). If empty the ``comment`` field is omitted entirely.

        Returns:
            The worklog dict Jira created, including ``id`` (which becomes the
            entry's ``jira_worklog_id``).

        Raises:
            JiraNotFoundError: on 404.
            JiraAuthError: on 401 or 403.
            JiraClientError: on other 4xx.
            JiraServerError: on 5xx after retry.
        """
        payload: dict[str, Any] = {
            "started": started,
            "timeSpentSeconds": time_spent_seconds,
        }
        if comment:
            payload["comment"] = {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": comment}],
                    }
                ],
            }
        return self._request(
            "POST",
            f"/rest/api/3/issue/{key}/worklog",
            json=payload,
        )

    def close(self) -> None:
        """Close the underlying httpx client."""
        self._client.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Send a request and handle errors, with one retry on 5xx / connect errors.

        Flow:
        1. Send the request.
        2. On success (2xx), return the parsed JSON.
        3. On 401/403, raise ``JiraAuthError``.
        4. On 404, raise ``JiraNotFoundError``.
        5. On other 4xx, raise ``JiraClientError`` with response body (truncated
           to ~500 chars).
        6. On 5xx or ``httpx.ConnectError``/``httpx.TimeoutException``: sleep
           ``RETRY_BACKOFF_SECONDS`` then retry once. On second failure raise
           ``JiraServerError``.
        """
        for attempt in range(2):
            try:
                response = self._client.request(method, path, **kwargs)
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                if attempt == 0:
                    self._sleep(self.RETRY_BACKOFF_SECONDS)
                    continue
                raise JiraServerError(
                    f"Connection/timeout error after retry: {exc}"
                ) from exc

            status = response.status_code

            # --- Success ---
            if 200 <= status < 300:
                return response.json()

            # --- 4xx — never retry ---
            if 400 <= status < 500:
                if status in (401, 403):
                    raise JiraAuthError(
                        f"HTTP {status}: authentication failed"
                    )
                if status == 404:
                    raise JiraNotFoundError(
                        f"HTTP 404: resource not found ({path!r})"
                    )
                body_snippet = response.text[:500]
                raise JiraClientError(
                    f"HTTP {status}: bad request — {body_snippet}"
                )

            # --- 5xx — retry once ---
            if attempt == 0:
                self._sleep(self.RETRY_BACKOFF_SECONDS)
                continue
            # Second attempt also failed
            raise JiraServerError(
                f"HTTP {status} after retry: {response.text[:500]}"
            )

        # Unreachable — both loop iterations always either return or raise.
        raise JiraServerError("Unexpected retry exhaustion")  # pragma: no cover
