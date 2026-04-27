"""Tests for tsh.jira.push (plan_push + execute_push)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import httpx
import pytest
import respx

from tsh.core.timezones import to_jira_started
from tsh.core.models import TimeEntry
from tsh.jira.client import JiraClient, JiraClientError
from tsh.jira.push import PlannedPush, PushResult, execute_push, plan_push

BASE_URL = "https://example.atlassian.net"
EMAIL = "user@example.com"
TOKEN = "myapitoken"

# A fixed UTC instant used across tests for reproducibility
_BASE_DT = datetime(2026, 4, 27, 3, 0, 0, tzinfo=timezone.utc)


def _entry(
    entry_id,
    ticket_key,
    start,
    duration_seconds,
    *,
    note="",
    kind="work",
    pushed_at=None,
):
    """Build a TimeEntry for tests."""
    return TimeEntry(
        id=entry_id,
        ticket_key=ticket_key,
        start_at=start,
        end_at=start + timedelta(seconds=duration_seconds) if duration_seconds else None,
        kind=kind,  # type: ignore[arg-type]
        note=note,
        jira_worklog_id=None,
        pushed_at=pushed_at,
        created_at=start,
        updated_at=start + timedelta(seconds=duration_seconds or 0),
    )


def _make_client(sleep_fn=None) -> JiraClient:
    """Create a JiraClient backed by a real httpx.Client for respx to intercept."""
    kwargs: dict = {}
    if sleep_fn is not None:
        kwargs["_sleep"] = sleep_fn
    return JiraClient(base_url=BASE_URL, email=EMAIL, token=TOKEN, **kwargs)


# ===========================================================================
# plan_push tests
# ===========================================================================


class TestPlanPush:
    def test_empty_input_returns_empty_list(self):
        """Empty entry list produces no plans."""
        assert plan_push([]) == []

    def test_single_entry_rounds_up_to_15m(self):
        """Single 600s (10m) entry → one plan rounded to 900s (15m)."""
        start = _BASE_DT
        entry = _entry(1, "SFXS-1", start, 600, note="Coded auth")
        plans = plan_push([entry], tz="Australia/Sydney")

        assert len(plans) == 1
        p = plans[0]
        assert p.entry_ids == (1,)
        assert p.ticket_key == "SFXS-1"
        assert p.time_spent_seconds == 900
        assert p.comment == "Coded auth"
        assert p.started_iso == to_jira_started(start, tz="Australia/Sydney")

    def test_two_entries_same_ticket_sum_then_round(self):
        """Two 420s entries on same ticket: sum=840s → round to 900s."""
        start1 = _BASE_DT
        start2 = _BASE_DT + timedelta(minutes=30)
        e1 = _entry(1, "SFXS-1", start1, 420, note="A")
        e2 = _entry(2, "SFXS-1", start2, 420, note="B")
        plans = plan_push([e1, e2], tz="Australia/Sydney")

        assert len(plans) == 1
        p = plans[0]
        assert p.time_spent_seconds == 900
        assert p.entry_ids == (1, 2)
        assert p.comment == "A; B"

    def test_two_different_tickets_produce_two_plans_ordered_by_started_iso(self):
        """Two entries on different tickets → two plans, ordered ascending by started_iso."""
        start_a = _BASE_DT + timedelta(hours=1)
        start_b = _BASE_DT  # earlier
        e_a = _entry(1, "SFXS-2", start_a, 900)
        e_b = _entry(2, "SFXS-1", start_b, 900)
        plans = plan_push([e_a, e_b], tz="Australia/Sydney")

        assert len(plans) == 2
        assert plans[0].ticket_key == "SFXS-1"
        assert plans[1].ticket_key == "SFXS-2"
        assert plans[0].started_iso < plans[1].started_iso

    def test_started_iso_uses_earliest_start_at(self):
        """When two entries share a ticket, started_iso reflects the earliest start_at."""
        start_later = _BASE_DT + timedelta(hours=2)
        start_earlier = _BASE_DT + timedelta(hours=1)
        e1 = _entry(1, "SFXS-1", start_later, 900)
        e2 = _entry(2, "SFXS-1", start_earlier, 900)
        plans = plan_push([e1, e2], tz="Australia/Sydney")

        assert len(plans) == 1
        assert plans[0].started_iso == to_jira_started(start_earlier, tz="Australia/Sydney")

    def test_skips_active_entry(self):
        """Entry with end_at=None (active) is excluded."""
        active = _entry(1, "SFXS-1", _BASE_DT, 0)  # duration_seconds=0 → end_at=None
        plans = plan_push([active])
        assert plans == []

    def test_skips_not_work_entry(self):
        """Entry with kind='not_work' is excluded."""
        e = _entry(1, "SFXS-1", _BASE_DT, 900, kind="not_work")
        plans = plan_push([e])
        assert plans == []

    def test_skips_entry_with_no_ticket_key(self):
        """Entry with ticket_key=None is excluded."""
        e = _entry(1, None, _BASE_DT, 900)
        plans = plan_push([e])
        assert plans == []

    def test_skips_already_pushed_entry(self):
        """Entry with pushed_at != None is excluded."""
        pushed_time = _BASE_DT + timedelta(hours=1)
        e = _entry(1, "SFXS-1", _BASE_DT, 900, pushed_at=pushed_time)
        plans = plan_push([e])
        assert plans == []

    def test_mixed_valid_and_skipped_entries(self):
        """3 valid + 2 skipped → only 3 valid entries influence the plans."""
        start = _BASE_DT
        valid1 = _entry(1, "SFXS-1", start, 900, note="Valid 1")
        valid2 = _entry(2, "SFXS-1", start + timedelta(hours=1), 900, note="Valid 2")
        valid3 = _entry(3, "SFXS-2", start, 900, note="Valid 3")
        skipped_active = _entry(4, "SFXS-1", start + timedelta(hours=2), 0)  # active
        skipped_not_work = _entry(5, "SFXS-3", start, 900, kind="not_work")

        plans = plan_push([valid1, valid2, valid3, skipped_active, skipped_not_work], tz="Australia/Sydney")

        ticket_keys = {p.ticket_key for p in plans}
        assert ticket_keys == {"SFXS-1", "SFXS-2"}
        sfxs1_plan = next(p for p in plans if p.ticket_key == "SFXS-1")
        assert set(sfxs1_plan.entry_ids) == {1, 2}

    def test_zero_rounded_total_is_dropped(self):
        """30s entry with mode='nearest' rounds to 0 → no plan emitted."""
        e = _entry(1, "SFXS-1", _BASE_DT, 30)
        plans = plan_push([e], mode="nearest", minutes=15)
        assert plans == []

    def test_mode_up_rounds_any_nonzero_to_first_bucket(self):
        """30s entry with mode='up' → plan with time_spent_seconds=900."""
        e = _entry(1, "SFXS-1", _BASE_DT, 30)
        plans = plan_push([e], mode="up", minutes=15)
        assert len(plans) == 1
        assert plans[0].time_spent_seconds == 900

    def test_comment_dedup_preserves_first_occurrence_order(self):
        """Notes ["A", "A", "B", "A"] → comment = "A; B"."""
        start = _BASE_DT
        entries = [
            _entry(1, "SFXS-1", start, 300, note="A"),
            _entry(2, "SFXS-1", start + timedelta(minutes=5), 300, note="A"),
            _entry(3, "SFXS-1", start + timedelta(minutes=10), 300, note="B"),
            _entry(4, "SFXS-1", start + timedelta(minutes=15), 300, note="A"),
        ]
        plans = plan_push(entries, tz="Australia/Sydney")
        assert len(plans) == 1
        assert plans[0].comment == "A; B"

    def test_all_empty_notes_produces_empty_comment(self):
        """All empty notes → comment = ''."""
        e1 = _entry(1, "SFXS-1", _BASE_DT, 900, note="")
        e2 = _entry(2, "SFXS-1", _BASE_DT + timedelta(hours=1), 900, note="")
        plans = plan_push([e1, e2])
        assert len(plans) == 1
        assert plans[0].comment == ""

    def test_notes_with_surrounding_whitespace_are_trimmed_before_dedup(self):
        """Notes ["A ", " A", "B"] → comment = "A; B" (trimmed, deduped)."""
        start = _BASE_DT
        entries = [
            _entry(1, "SFXS-1", start, 300, note="A "),
            _entry(2, "SFXS-1", start + timedelta(minutes=5), 300, note=" A"),
            _entry(3, "SFXS-1", start + timedelta(minutes=10), 300, note="B"),
        ]
        plans = plan_push(entries, tz="Australia/Sydney")
        assert len(plans) == 1
        assert plans[0].comment == "A; B"


# ===========================================================================
# execute_push tests
# ===========================================================================


def _make_planned(
    entry_ids: tuple,
    ticket_key: str,
    started_iso: str | None = None,
    time_spent_seconds: int = 900,
    comment: str = "test comment",
) -> PlannedPush:
    if started_iso is None:
        started_iso = to_jira_started(_BASE_DT, tz="Australia/Sydney")
    return PlannedPush(
        entry_ids=entry_ids,
        ticket_key=ticket_key,
        started_iso=started_iso,
        time_spent_seconds=time_spent_seconds,
        comment=comment,
    )


class TestExecutePush:
    def test_empty_planned_returns_empty_list(self):
        """Empty planned list → [] and Jira is never called."""
        sleep_mock = Mock()
        client = _make_client(sleep_fn=sleep_mock)
        results = execute_push([], client, _sleep=sleep_mock)
        assert results == []
        sleep_mock.assert_not_called()

    @respx.mock
    def test_single_planned_push_succeeds(self):
        """Single planned push → 1 POST, 1 PushResult(ok=True, worklog_id='wl-123')."""
        route = respx.post(f"{BASE_URL}/rest/api/3/issue/SFXS-1/worklog").mock(
            return_value=httpx.Response(201, json={"id": "wl-123"})
        )
        sleep_mock = Mock()
        client = _make_client(sleep_fn=sleep_mock)
        planned = [_make_planned((1,), "SFXS-1")]
        results = execute_push(planned, client, _sleep=sleep_mock)

        assert len(results) == 1
        r = results[0]
        assert r.ok is True
        assert r.jira_worklog_id == "wl-123"
        assert r.dry_run is False
        assert r.entry_ids == (1,)
        assert route.call_count == 1
        sleep_mock.assert_not_called()

    @respx.mock
    def test_two_planned_pushes_sleep_called_once_between(self):
        """Two successful POSTs → sleep called exactly once (between them)."""
        respx.post(f"{BASE_URL}/rest/api/3/issue/SFXS-1/worklog").mock(
            return_value=httpx.Response(201, json={"id": "wl-1"})
        )
        respx.post(f"{BASE_URL}/rest/api/3/issue/SFXS-2/worklog").mock(
            return_value=httpx.Response(201, json={"id": "wl-2"})
        )
        sleep_mock = Mock()
        client = _make_client(sleep_fn=sleep_mock)
        planned = [
            _make_planned((1,), "SFXS-1"),
            _make_planned((2,), "SFXS-2"),
        ]
        results = execute_push(planned, client, sleep_ms=100, _sleep=sleep_mock)

        assert len(results) == 2
        assert all(r.ok for r in results)
        sleep_mock.assert_called_once_with(0.1)

    @respx.mock
    def test_three_pushes_middle_fails_isolation(self):
        """Middle push failing → ok=False for middle, loop continues; sleep called twice."""
        respx.post(f"{BASE_URL}/rest/api/3/issue/SFXS-1/worklog").mock(
            return_value=httpx.Response(201, json={"id": "wl-1"})
        )
        respx.post(f"{BASE_URL}/rest/api/3/issue/SFXS-2/worklog").mock(
            return_value=httpx.Response(400, text="bad payload")
        )
        respx.post(f"{BASE_URL}/rest/api/3/issue/SFXS-3/worklog").mock(
            return_value=httpx.Response(201, json={"id": "wl-3"})
        )
        sleep_mock = Mock()
        client = _make_client(sleep_fn=sleep_mock)
        planned = [
            _make_planned((1,), "SFXS-1"),
            _make_planned((2,), "SFXS-2"),
            _make_planned((3,), "SFXS-3"),
        ]
        results = execute_push(planned, client, sleep_ms=100, _sleep=sleep_mock)

        assert len(results) == 3
        assert results[0].ok is True
        assert results[1].ok is False
        assert results[1].error is not None
        assert results[2].ok is True
        assert results[2].jira_worklog_id == "wl-3"
        assert sleep_mock.call_count == 2
        sleep_mock.assert_called_with(0.1)

    @respx.mock
    def test_dry_run_no_requests_no_sleep(self):
        """Dry run: 2 planned → 2 PushResults(ok=True, dry_run=True); no HTTP, no sleep."""
        # Route will assert it's never called via respx assertion mode
        sleep_mock = Mock()
        client = _make_client(sleep_fn=sleep_mock)
        planned = [
            _make_planned((1,), "SFXS-1"),
            _make_planned((2,), "SFXS-2"),
        ]
        results = execute_push(planned, client, dry_run=True, _sleep=sleep_mock)

        assert len(results) == 2
        for r in results:
            assert r.ok is True
            assert r.dry_run is True
            assert r.jira_worklog_id is None
        sleep_mock.assert_not_called()
        # respx has no routes registered; if any request were made it would raise
