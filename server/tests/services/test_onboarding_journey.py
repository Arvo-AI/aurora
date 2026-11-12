"""Setup-email rules: who gets which email when, and that the links in them
can't be turned against anyone else."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from services.onboarding import guides
from services.onboarding.emails import EmailContext, compose
from services.onboarding.schedule import Due, JourneyState, due_email

T0 = datetime(2026, 11, 12, 15, 0, 0)


def state(**kw) -> JourneyState:
    base = dict(enrolled_at=T0, email_verified=True)
    base.update(kw)
    return JourneyState(**base)


def at(**delta) -> datetime:
    return T0 + timedelta(**delta)


# ── schedule ──


def test_welcome_waits_a_few_minutes_then_goes_out():
    assert due_email(state(), at(minutes=1)) is None
    assert due_email(state(), at(minutes=4)) == Due("welcome")


def test_unverified_or_unsubscribed_accounts_get_nothing():
    assert due_email(state(email_verified=False), at(days=1)) is None
    assert due_email(state(unsubscribed=True), at(days=1)) is None


def test_a_new_connection_is_announced_once_and_bursts_are_batched():
    # Welcome went out hours ago; the burst window still starts at the first connect.
    s = state(sent={"welcome": at(minutes=4)}, parts_done=["alerts", "cloud"],
              pending_parts=["alerts", "cloud"], pending_since=at(hours=5))
    assert due_email(s, at(hours=5, minutes=5)) is None
    assert due_email(s, at(hours=5, minutes=25)) == Due("connected", ("alerts", "cloud"))
    s.sent["connected:alerts"] = s.sent["connected:cloud"] = at(hours=5, minutes=25)
    assert due_email(s, at(hours=5, minutes=50)) is None


def test_a_tool_removed_before_the_email_is_not_announced():
    s = state(sent={"welcome": at(minutes=4)}, parts_done=[], parts_ever=["alerts"],
              pending_parts=["alerts"], pending_since=at(hours=1))
    assert due_email(s, at(hours=2)) is None


def test_a_failed_check_does_not_count_as_nothing_connected():
    # The live check said nothing, but alerts were seen connected before.
    sent = {"welcome": at(minutes=4), "checkin": at(days=1)}
    s = state(sent=sent, parts_done=[], parts_ever=["alerts"])
    assert due_email(s, at(days=3, hours=1)) is None


def test_checkin_a_day_in_unless_the_stack_is_done():
    s = state(sent={"welcome": at(minutes=4)}, parts_done=["code"])
    assert due_email(s, at(hours=23)) == Due("checkin")
    done = state(sent={"welcome": at(minutes=4)}, parts_done=["alerts", "cloud", "code", "chat"])
    assert due_email(done, at(hours=23)) is None


def test_checkin_never_lands_right_after_another_email():
    s = state(sent={"welcome": at(minutes=4), "connected:code": at(hours=20)}, parts_done=["code"])
    assert due_email(s, at(hours=23)) is None
    assert due_email(s, at(hours=33)) == Due("checkin")


def test_nudges_only_while_nothing_is_connected():
    sent = {"welcome": at(minutes=4), "checkin": at(days=1)}
    assert due_email(state(sent=dict(sent)), at(days=3, hours=1)) == Due("nudge_day3")
    assert due_email(state(sent=dict(sent), parts_done=["alerts"]), at(days=3, hours=1)) is None
    sent["nudge_day3"] = at(days=3, hours=1)
    assert due_email(state(sent=sent), at(days=7, hours=1)) == Due("nudge_day7")


def test_journey_ends_after_two_weeks():
    assert due_email(state(), at(days=15)) is None


# ── guides ──


def test_guide_photo_can_live_on_the_app_but_not_on_another_host():
    raw = json.dumps([
        {"key": "a", "name": "Ada Lane", "email": "ada@example.com", "avatar_url": "/guides/ada.jpg"},
        {"key": "b", "name": "Bo Kim", "email": "bo@example.com", "avatar_url": "//evil.example/bo.jpg"},
    ])
    a, b = guides.load_guides(raw)
    assert a.avatar_url == "/guides/ada.jpg"
    assert b.avatar_url == ""


def test_guide_links_must_be_https():
    raw = json.dumps([{
        "key": "a", "name": "Ada Lane", "email": "ada@example.com",
        "avatar_url": "javascript:alert(1)", "booking_url": "http://cal.example.com/ada",
    }])
    (g,) = guides.load_guides(raw)
    assert g.avatar_url == ""
    assert g.booking_url == ""


def test_guides_without_an_address_are_skipped_and_bad_json_means_none():
    raw = json.dumps([{"key": "a", "name": "Ada"}, {"key": "b", "name": "Bo Kim", "email": "bo@example.com"}])
    assert [g.key for g in guides.load_guides(raw)] == ["b"]
    assert guides.load_guides("{not json") == []


def test_new_accounts_go_to_the_least_busy_guide(monkeypatch):
    monkeypatch.setenv("ONBOARDING_GUIDES", json.dumps([
        {"key": "a", "name": "Ada Lane", "email": "ada@example.com"},
        {"key": "b", "name": "Bo Kim", "email": "bo@example.com"},
    ]))
    assert guides.pick({}).key == "a"
    assert guides.pick({"a": 3, "b": 1}).key == "b"


# ── unsubscribe links ──


def test_unsubscribe_token_only_works_for_its_own_user(monkeypatch):
    monkeypatch.setenv("ONBOARDING_UNSUBSCRIBE_SECRET", "s3cret")
    from services.onboarding.sender import unsubscribe_token, unsubscribe_token_ok
    tok = unsubscribe_token("user-1")
    assert unsubscribe_token_ok("user-1", tok)
    assert not unsubscribe_token_ok("user-2", tok)
    assert not unsubscribe_token_ok("user-1", "")


def test_no_emails_when_the_unsubscribe_link_would_point_at_localhost(monkeypatch):
    monkeypatch.setenv("ONBOARDING_EMAILS_ENABLED", "true")
    monkeypatch.setenv("ONBOARDING_UNSUBSCRIBE_SECRET", "s3cret")
    for v in ("ONBOARDING_GRAPH_TENANT_ID", "ONBOARDING_GRAPH_CLIENT_ID", "ONBOARDING_GRAPH_CLIENT_SECRET"):
        monkeypatch.setenv(v, "x")
    monkeypatch.delenv("NGROK_URL", raising=False)
    from services.onboarding import sender
    monkeypatch.setenv("NEXT_PUBLIC_BACKEND_URL", "http://localhost:5080")
    assert not sender.emails_enabled()
    monkeypatch.setenv("NEXT_PUBLIC_BACKEND_URL", "https://api.example.com")
    assert sender.emails_enabled()


def test_no_secret_means_no_valid_tokens(monkeypatch):
    monkeypatch.delenv("ONBOARDING_UNSUBSCRIBE_SECRET", raising=False)
    monkeypatch.delenv("FLASK_SECRET_KEY", raising=False)
    from services.onboarding.sender import unsubscribe_token, unsubscribe_token_ok
    assert not unsubscribe_token_ok("user-1", unsubscribe_token("user-1"))


# ── composed emails ──


def _ctx(**kw) -> EmailContext:
    base = dict(
        guide=guides.Guide(key="a", name="Ada Lane", email="ada@example.com", booking_url="https://cal.example.com/ada"),
        first_name="Dana", via="password", providers=frozenset(), parts_done=(),
        setup_url="https://app.example.com/onboarding",
        unsubscribe_url="https://api.example.com/onboarding/unsubscribe?u=1&t=x",
    )
    base.update(kw)
    return EmailContext(**base)


@pytest.mark.parametrize("kind", ["welcome", "checkin", "nudge_day3", "nudge_day7"])
def test_every_email_carries_a_working_unsubscribe_link(kind):
    msg = compose(kind, _ctx())
    assert "https://api.example.com/onboarding/unsubscribe?u=1&t=x" in msg.text
    assert 'href="https://api.example.com/onboarding/unsubscribe?u=1&amp;t=x"' in msg.html


def test_connected_email_names_the_tool_and_points_at_whats_left():
    ctx = _ctx(providers=frozenset({"datadog"}), parts_done=("alerts",))
    msg = compose("connected", ctx, ("alerts",))
    assert "Datadog" in msg.subject
    assert ctx.setup_url in msg.text


def test_user_supplied_names_are_escaped_in_html():
    msg = compose("welcome", _ctx(first_name="<script>x</script>"))
    assert "<script>" not in msg.html

