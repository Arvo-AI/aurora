"""conversationUpdate handling: install, uninstall and channel changes."""

import pytest

from routes.teams import teams_lifecycle as lc

TEAM = "11111111-2222-3333-4444-555555555555"
BOT = "28:app-id"


@pytest.fixture
def calls(monkeypatch):
    log = []
    monkeypatch.setenv("TEAMS_APP_ID", "app-id")
    monkeypatch.setattr(lc.teams_installs, "mark_team_installed", lambda u, t: log.append(("installed", t)))
    monkeypatch.setattr(lc.teams_installs, "mark_team_uninstalled", lambda u, t: log.append(("uninstalled", t)))
    monkeypatch.setattr(lc.teams_channels, "remove_team_channels", lambda u, t: log.append(("purge_team", t)))
    monkeypatch.setattr(lc.teams_channels, "remove_channel", lambda u, c: log.append(("remove_channel", c)))
    monkeypatch.setattr(lc.teams_channels, "rename_channel", lambda u, c, n: log.append(("rename", c, n)))
    monkeypatch.setattr(lc, "_enqueue", lambda u, kind, *a: log.append(("enqueue", kind, *a)))
    return log


def _update(event=None, team=TEAM, **extra):
    channel_data = {"team": {"id": "19:team@thread.tacv2", "aadGroupId": team} if team else {}}
    if event:
        channel_data["eventType"] = event
    channel_data.update(extra.pop("channel_data", {}))
    return {"type": "conversationUpdate", "channelData": channel_data, **extra}


def test_bot_added_marks_installed_and_syncs_channels(calls):
    lc.handle_conversation_update("u", _update("teamMemberAdded", membersAdded=[{"id": BOT}]))
    assert calls == [("installed", TEAM), ("enqueue", "sync")]


def test_another_member_joining_is_not_a_bot_install_but_still_proves_it(calls):
    lc.handle_conversation_update("u", _update("teamMemberAdded", membersAdded=[{"id": "29:someone"}]))
    # Bot is evidently in the team, but no resync is needed
    assert calls == [("installed", TEAM)]


def test_bot_removed_clears_state_and_channels(calls):
    lc.handle_conversation_update("u", _update("teamMemberRemoved", membersRemoved=[{"id": BOT}]))
    assert calls == [("uninstalled", TEAM), ("purge_team", TEAM)]


def test_another_member_leaving_does_not_uninstall(calls):
    lc.handle_conversation_update("u", _update("teamMemberRemoved", membersRemoved=[{"id": "29:someone"}]))
    assert ("uninstalled", TEAM) not in calls


def test_team_deleted_clears_state_and_channels(calls):
    lc.handle_conversation_update("u", _update("teamDeleted"))
    assert calls == [("uninstalled", TEAM), ("purge_team", TEAM)]


@pytest.mark.parametrize("event", ["channelCreated", "channelRestored"])
def test_new_channel_is_registered(calls, event):
    lc.handle_conversation_update("u", _update(event, channel_data={"channel": {"id": "19:c@thread.tacv2"}}))
    assert ("enqueue", "channel", "19:c@thread.tacv2", TEAM) in calls


def test_deleted_channel_is_removed(calls):
    lc.handle_conversation_update("u", _update("channelDeleted", channel_data={"channel": {"id": "c1"}}))
    assert ("remove_channel", "c1") in calls


def test_renamed_channel_updates_the_stored_name(calls):
    lc.handle_conversation_update(
        "u", _update("channelRenamed", channel_data={"channel": {"id": "c1", "name": "ops"}}),
    )
    assert ("rename", "c1", "ops") in calls


def test_updates_without_a_graph_team_are_ignored(calls):
    # Personal chats and payloads lacking aadGroupId can't be keyed
    lc.handle_conversation_update("u", _update("teamMemberAdded", team=None, membersAdded=[{"id": BOT}]))
    assert calls == []


def test_unknown_event_only_confirms_the_install(calls):
    lc.handle_conversation_update("u", _update("teamRenamed"))
    assert calls == [("installed", TEAM)]


def test_bare_app_id_form_is_recognised_as_the_bot(calls):
    lc.handle_conversation_update("u", _update("teamMemberAdded", membersAdded=[{"id": "app-id"}]))
    assert ("enqueue", "sync") in calls
