"""Automation handler: import_lastfm_listening (M01).

The handler itself no longer resolves usernames or touches config beyond
the enabled flag - it just decides WHICH profile(s) the worker should run
for and delegates. Tested as a pure function against a duck-typed deps
object (only the two attributes the handler actually reads), matching
the style of the direct SimpleNamespace mocks used for the Flask routes.
"""

from __future__ import annotations

from types import SimpleNamespace

from core.automation.handlers.lastfm_import import auto_import_lastfm_listening


def _deps(worker, *, sync_enabled=True):
    calls = {}
    return SimpleNamespace(
        lastfm_import_worker=worker,
        config_manager=SimpleNamespace(
            get=lambda key, default=None: {"lastfm.listening_sync_enabled": sync_enabled}.get(key, default),
            set=lambda key, value: calls.__setitem__(key, value),
        ),
    ), calls


def test_worker_unavailable_reports_error():
    result = auto_import_lastfm_listening({}, SimpleNamespace(lastfm_import_worker=None))
    assert result["status"] == "error"


def test_disabled_and_not_manual_is_skipped():
    worker = SimpleNamespace(run_all_profiles=lambda **k: {"never": "called"})
    deps, _ = _deps(worker, sync_enabled=False)
    result = auto_import_lastfm_listening({}, deps)
    assert result["status"] == "skipped"


def test_manual_run_while_disabled_turns_sync_on_and_runs(monkeypatch):
    seen = {}
    worker = SimpleNamespace(run_all_profiles=lambda full=False: seen.setdefault("ran", full) or {1: {"status": "complete"}})
    deps, saved = _deps(worker, sync_enabled=False)
    result = auto_import_lastfm_listening({"_manual_run": True}, deps)
    assert saved["lastfm.listening_sync_enabled"] is True
    assert "ran" in seen
    assert result == {1: {"status": "complete"}}


def test_scheduled_run_syncs_every_profile():
    """The default, no explicit profile_id: this is the whole-household
    scheduled sync, not scoped to one person."""
    calls = {}

    def fake_run_all_profiles(full=False):
        calls["full"] = full
        return {1: {"status": "complete"}, 2: {"status": "complete"}}

    worker = SimpleNamespace(run_all_profiles=fake_run_all_profiles)
    deps, _ = _deps(worker)
    result = auto_import_lastfm_listening({}, deps)
    assert result == {1: {"status": "complete"}, 2: {"status": "complete"}}
    assert calls["full"] is False


def test_explicit_profile_id_targets_just_that_profile():
    """A 'sync my Last.fm now' button for one person must not touch
    anyone else's import."""
    calls = {}

    def fake_run_once(profile_id, username=None, full=False):
        calls["args"] = (profile_id, username, full)
        return {"status": "started", "profile_id": profile_id}

    worker = SimpleNamespace(run_once=fake_run_once)
    deps, _ = _deps(worker)
    result = auto_import_lastfm_listening({"profile_id": 2, "username": "kid-fm"}, deps)
    assert result == {"status": "started", "profile_id": 2}
    assert calls["args"] == (2, "kid-fm", False)


def test_full_flag_passes_through_to_run_all_profiles():
    calls = {}
    worker = SimpleNamespace(run_all_profiles=lambda full=False: calls.setdefault("full", full) or {})
    deps, _ = _deps(worker)
    auto_import_lastfm_listening({"full": True}, deps)
    assert calls["full"] is True
