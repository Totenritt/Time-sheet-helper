"""Tests for tsh.jira.client using respx to mock HTTP."""

from __future__ import annotations

import base64
from unittest.mock import Mock

import httpx
import pytest
import respx

from tsh.jira.client import (
    JiraAuthError,
    JiraClient,
    JiraClientError,
    JiraNotFoundError,
    JiraServerError,
)

BASE_URL = "https://example.atlassian.net"
EMAIL = "user@example.com"
TOKEN = "myapitoken"


def _make_client(sleep_fn=None) -> JiraClient:
    """Create a JiraClient with a real httpx.Client for respx to intercept."""
    kwargs: dict = {}
    if sleep_fn is not None:
        kwargs["_sleep"] = sleep_fn
    return JiraClient(base_url=BASE_URL, email=EMAIL, token=TOKEN, **kwargs)


def _expected_auth_header() -> str:
    encoded = base64.b64encode(f"{EMAIL}:{TOKEN}".encode()).decode()
    return f"Basic {encoded}"


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


class TestAuthTest:
    @respx.mock
    def test_calls_correct_url_with_basic_auth(self):
        """auth_test() calls GET /rest/api/3/myself with basic auth header."""
        route = respx.get(f"{BASE_URL}/rest/api/3/myself").mock(
            return_value=httpx.Response(
                200,
                json={
                    "displayName": "Joe",
                    "emailAddress": EMAIL,
                    "accountId": "abc123",
                },
            )
        )
        client = _make_client()
        result = client.auth_test()
        client.close()

        assert route.called
        request = route.calls[0].request
        assert request.method == "GET"
        assert request.headers["Authorization"] == _expected_auth_header()
        assert result["displayName"] == "Joe"
        assert result["emailAddress"] == EMAIL

    @respx.mock
    def test_401_raises_jira_auth_error(self):
        """auth_test() on 401 raises JiraAuthError without retry."""
        route = respx.get(f"{BASE_URL}/rest/api/3/myself").mock(
            return_value=httpx.Response(401, json={"message": "Unauthorized"})
        )
        client = _make_client()
        with pytest.raises(JiraAuthError):
            client.auth_test()
        client.close()
        # No retry on 4xx
        assert route.call_count == 1

    @respx.mock
    def test_403_raises_jira_auth_error(self):
        """auth_test() on 403 raises JiraAuthError."""
        respx.get(f"{BASE_URL}/rest/api/3/myself").mock(
            return_value=httpx.Response(403, json={"message": "Forbidden"})
        )
        client = _make_client()
        with pytest.raises(JiraAuthError):
            client.auth_test()
        client.close()


# ---------------------------------------------------------------------------
# search_in_progress
# ---------------------------------------------------------------------------


class TestSearchInProgress:
    @respx.mock
    def test_default_jql_is_sent_as_query_param(self):
        """Default JQL is sent url-encoded as the jql query param."""
        route = respx.get(f"{BASE_URL}/rest/api/3/search").mock(
            return_value=httpx.Response(200, json={"issues": []})
        )
        client = _make_client()
        client.search_in_progress()
        client.close()

        assert route.called
        request = route.calls[0].request
        url = httpx.URL(str(request.url))
        params = dict(url.params)
        assert params["jql"] == 'assignee = currentUser() AND status = "In Progress"'

    @respx.mock
    def test_jql_override_is_sent_instead(self):
        """When jql_override is supplied, it replaces the default JQL."""
        route = respx.get(f"{BASE_URL}/rest/api/3/search").mock(
            return_value=httpx.Response(200, json={"issues": []})
        )
        client = _make_client()
        client.search_in_progress(jql_override="my custom jql")
        client.close()

        request = route.calls[0].request
        url = httpx.URL(str(request.url))
        params = dict(url.params)
        assert params["jql"] == "my custom jql"

    @respx.mock
    def test_empty_issues_returns_empty_list(self):
        """Empty issues array in response returns []."""
        respx.get(f"{BASE_URL}/rest/api/3/search").mock(
            return_value=httpx.Response(200, json={"issues": []})
        )
        client = _make_client()
        result = client.search_in_progress()
        client.close()
        assert result == []

    @respx.mock
    def test_two_issues_returns_list_of_two(self):
        """Response with two issues returns a list of two dicts."""
        issues = [
            {"key": "SFXS-1", "fields": {"summary": "First ticket"}},
            {"key": "SFXS-2", "fields": {"summary": "Second ticket"}},
        ]
        respx.get(f"{BASE_URL}/rest/api/3/search").mock(
            return_value=httpx.Response(200, json={"issues": issues})
        )
        client = _make_client()
        result = client.search_in_progress()
        client.close()
        assert len(result) == 2
        assert result[0]["key"] == "SFXS-1"
        assert result[1]["key"] == "SFXS-2"


# ---------------------------------------------------------------------------
# get_issue
# ---------------------------------------------------------------------------


class TestGetIssue:
    @respx.mock
    def test_calls_correct_url(self):
        """get_issue('SFXS-1') calls GET /rest/api/3/issue/SFXS-1."""
        route = respx.get(f"{BASE_URL}/rest/api/3/issue/SFXS-1").mock(
            return_value=httpx.Response(
                200, json={"key": "SFXS-1", "fields": {"summary": "Test"}}
            )
        )
        client = _make_client()
        result = client.get_issue("SFXS-1")
        client.close()

        assert route.called
        assert result["key"] == "SFXS-1"

    @respx.mock
    def test_404_raises_jira_not_found_error(self):
        """get_issue on 404 raises JiraNotFoundError."""
        respx.get(f"{BASE_URL}/rest/api/3/issue/SFXS-999").mock(
            return_value=httpx.Response(404, json={"errorMessages": ["Not found"]})
        )
        client = _make_client()
        with pytest.raises(JiraNotFoundError):
            client.get_issue("SFXS-999")
        client.close()

    @respx.mock
    def test_401_raises_jira_auth_error(self):
        """get_issue on 401 raises JiraAuthError."""
        respx.get(f"{BASE_URL}/rest/api/3/issue/SFXS-1").mock(
            return_value=httpx.Response(401, json={"message": "Unauthorized"})
        )
        client = _make_client()
        with pytest.raises(JiraAuthError):
            client.get_issue("SFXS-1")
        client.close()


# ---------------------------------------------------------------------------
# post_worklog
# ---------------------------------------------------------------------------


class TestPostWorklog:
    STARTED = "2026-04-27T14:30:00.000+1100"
    KEY = "SFXS-1073"
    TIME_SPENT = 900
    COMMENT = "Did some work"

    @respx.mock
    def test_successful_post_body_and_url(self):
        """Verify URL, started verbatim, timeSpentSeconds, ADF comment structure."""
        route = respx.post(f"{BASE_URL}/rest/api/3/issue/{self.KEY}/worklog").mock(
            return_value=httpx.Response(201, json={"id": "98765"})
        )
        client = _make_client()
        result = client.post_worklog(
            key=self.KEY,
            started=self.STARTED,
            time_spent_seconds=self.TIME_SPENT,
            comment=self.COMMENT,
        )
        client.close()

        assert route.called
        request = route.calls[0].request
        import json

        body = json.loads(request.content)

        # started must be verbatim — the key timezone discipline check
        assert body["started"] == "2026-04-27T14:30:00.000+1100"
        assert body["timeSpentSeconds"] == self.TIME_SPENT
        assert body["comment"] == {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": self.COMMENT}],
                }
            ],
        }
        assert result["id"] == "98765"

    @respx.mock
    def test_empty_comment_omits_comment_key(self):
        """Empty comment='' produces a body WITHOUT the comment key."""
        route = respx.post(f"{BASE_URL}/rest/api/3/issue/{self.KEY}/worklog").mock(
            return_value=httpx.Response(201, json={"id": "11111"})
        )
        client = _make_client()
        client.post_worklog(
            key=self.KEY,
            started=self.STARTED,
            time_spent_seconds=self.TIME_SPENT,
            comment="",
        )
        client.close()

        import json

        body = json.loads(route.calls[0].request.content)
        assert "comment" not in body

    @respx.mock
    def test_returns_response_json_with_id(self):
        """post_worklog returns the full response JSON including id."""
        respx.post(f"{BASE_URL}/rest/api/3/issue/{self.KEY}/worklog").mock(
            return_value=httpx.Response(
                201, json={"id": "55555", "timeSpent": "15m", "started": self.STARTED}
            )
        )
        client = _make_client()
        result = client.post_worklog(
            key=self.KEY,
            started=self.STARTED,
            time_spent_seconds=self.TIME_SPENT,
            comment=self.COMMENT,
        )
        client.close()
        assert result["id"] == "55555"
        assert result["timeSpent"] == "15m"

    @respx.mock
    def test_404_raises_jira_not_found_error(self):
        """post_worklog on 404 raises JiraNotFoundError."""
        respx.post(f"{BASE_URL}/rest/api/3/issue/{self.KEY}/worklog").mock(
            return_value=httpx.Response(404, json={"errorMessages": ["Not found"]})
        )
        client = _make_client()
        with pytest.raises(JiraNotFoundError):
            client.post_worklog(
                key=self.KEY,
                started=self.STARTED,
                time_spent_seconds=self.TIME_SPENT,
                comment=self.COMMENT,
            )
        client.close()

    @respx.mock
    def test_400_raises_jira_client_error(self):
        """post_worklog on 400 raises JiraClientError."""
        respx.post(f"{BASE_URL}/rest/api/3/issue/{self.KEY}/worklog").mock(
            return_value=httpx.Response(
                400, json={"errorMessages": ["Bad request payload"]}
            )
        )
        client = _make_client()
        with pytest.raises(JiraClientError):
            client.post_worklog(
                key=self.KEY,
                started=self.STARTED,
                time_spent_seconds=self.TIME_SPENT,
                comment=self.COMMENT,
            )
        client.close()


# ---------------------------------------------------------------------------
# Retry logic
# ---------------------------------------------------------------------------


class TestRetryLogic:
    @respx.mock
    def test_5xx_then_200_retries_once_and_succeeds(self):
        """5xx followed by 200: retries once and returns success."""
        sleep_mock = Mock()
        route = respx.get(f"{BASE_URL}/rest/api/3/myself").mock(
            side_effect=[
                httpx.Response(503, json={"message": "Service Unavailable"}),
                httpx.Response(
                    200,
                    json={
                        "displayName": "Joe",
                        "emailAddress": EMAIL,
                        "accountId": "abc",
                    },
                ),
            ]
        )
        client = _make_client(sleep_fn=sleep_mock)
        result = client.auth_test()
        client.close()

        assert result["displayName"] == "Joe"
        assert route.call_count == 2
        sleep_mock.assert_called_once_with(1.0)

    @respx.mock
    def test_5xx_twice_raises_jira_server_error(self):
        """Two consecutive 5xx responses raise JiraServerError after 2 total attempts."""
        sleep_mock = Mock()
        route = respx.get(f"{BASE_URL}/rest/api/3/myself").mock(
            return_value=httpx.Response(503, json={"message": "Service Unavailable"})
        )
        client = _make_client(sleep_fn=sleep_mock)
        with pytest.raises(JiraServerError):
            client.auth_test()
        client.close()

        assert route.call_count == 2
        sleep_mock.assert_called_once_with(1.0)

    @respx.mock
    def test_connection_error_then_success_retries(self):
        """ConnectError followed by 200: retries and succeeds."""
        sleep_mock = Mock()
        route = respx.get(f"{BASE_URL}/rest/api/3/myself").mock(
            side_effect=[
                httpx.ConnectError("Connection refused"),
                httpx.Response(
                    200,
                    json={
                        "displayName": "Joe",
                        "emailAddress": EMAIL,
                        "accountId": "abc",
                    },
                ),
            ]
        )
        client = _make_client(sleep_fn=sleep_mock)
        result = client.auth_test()
        client.close()

        assert result["displayName"] == "Joe"
        assert route.call_count == 2
        sleep_mock.assert_called_once_with(1.0)

    @respx.mock
    def test_no_retry_on_4xx(self):
        """4xx response raises immediately after exactly 1 request — no retry."""
        sleep_mock = Mock()
        route = respx.get(f"{BASE_URL}/rest/api/3/myself").mock(
            return_value=httpx.Response(401, json={"message": "Unauthorized"})
        )
        client = _make_client(sleep_fn=sleep_mock)
        with pytest.raises(JiraAuthError):
            client.auth_test()
        client.close()

        assert route.call_count == 1
        sleep_mock.assert_not_called()

    @respx.mock
    def test_timeout_then_success_retries(self):
        """A TimeoutException on first attempt triggers retry."""
        sleep_mock = Mock()
        client = _make_client(sleep_fn=sleep_mock)
        route = respx.get(f"{BASE_URL}/rest/api/3/myself").mock(
            side_effect=[
                httpx.TimeoutException("timed out"),
                httpx.Response(200, json={"displayName": "Casey"}),
            ]
        )
        result = client.auth_test()
        client.close()

        assert result == {"displayName": "Casey"}
        assert route.call_count == 2
        sleep_mock.assert_called_once_with(1.0)

    @respx.mock
    def test_5xx_then_4xx_surfaces_4xx(self):
        """A 5xx followed by a 4xx surfaces the 4xx after one retry."""
        sleep_mock = Mock()
        client = _make_client(sleep_fn=sleep_mock)
        route = respx.post(
            f"{BASE_URL}/rest/api/3/issue/SFXS-1/worklog"
        ).mock(
            side_effect=[
                httpx.Response(503, text="server hiccup"),
                httpx.Response(400, text="bad payload"),
            ]
        )
        with pytest.raises(JiraClientError):
            client.post_worklog("SFXS-1", "2026-04-27T14:30:00.000+1000", 900, "test")
        client.close()

        assert route.call_count == 2
        sleep_mock.assert_called_once_with(1.0)
