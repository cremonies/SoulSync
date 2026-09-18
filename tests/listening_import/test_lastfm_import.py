from __future__ import annotations

from database.music_database import MusicDatabase
from core.listening_import.lastfm import LastFMListeningImportWorker, normalize_lastfm_scrobble


class _Config:
    def get(self, key, default=None):
        values = {
            "lastfm.api_key": "key",
            "lastfm.username": "tester",
        }
        return values.get(key, default)

    def set(self, key, value):
        pass


def test_normalizes_lastfm_recent_track_payload():
    event = normalize_lastfm_scrobble({
        "name": "Ceremony",
        "artist": {"#text": "New Order"},
        "album": {"#text": "Substance"},
        "mbid": "mbid-1",
        "date": {"uts": "1700000000"},
    })

    assert event == {
        "track_id": "mbid-1",
        "title": "Ceremony",
        "artist": "New Order",
        "album": "Substance",
        "played_at": "2023-11-14 22:13:20",
        "duration_ms": 0,
        "server_source": "lastfm",
        "db_track_id": None,
    }


def test_lastfm_import_skips_probable_server_duplicates(tmp_path):
    db = MusicDatabase(str(tmp_path / "music.db"))
    conn = db._get_connection()
    conn.execute(
        """
        INSERT INTO listening_history
            (track_id, title, artist, album, played_at, duration_ms, server_source)
        VALUES ('plex-1', 'Ceremony', 'New Order', 'Substance', '2023-11-14 22:13:25', 180000, 'plex')
        """
    )
    conn.commit()
    conn.close()

    worker = LastFMListeningImportWorker(db, _Config())
    inserted = worker._insert_events_deduped([{
        "track_id": "lastfm-1",
        "title": "Ceremony",
        "artist": "New Order",
        "album": "Substance",
        "played_at": "2023-11-14 22:13:20",
        "duration_ms": 180000,
        "db_track_id": None,
    }])

    assert inserted == 0
    assert db.get_listening_stats("all")["total_plays"] == 1


def test_lastfm_import_stamps_the_target_profile(tmp_path):
    # M01: each profile has its own Last.fm account, so the profile a run
    # was started FOR is the one every row it inserts belongs to.
    db = MusicDatabase(str(tmp_path / "music.db"))
    worker = LastFMListeningImportWorker(db, _Config())
    inserted = worker._insert_events_deduped([{
        "track_id": "lastfm-1",
        "title": "Ceremony",
        "artist": "New Order",
        "album": "Substance",
        "played_at": "2023-11-14 22:13:20",
        "duration_ms": 180000,
        "db_track_id": None,
    }], profile_id=2)

    assert inserted == 1
    conn = db._get_connection()
    row = conn.execute("SELECT profile_id FROM listening_history").fetchone()
    conn.close()
    assert row[0] == 2


def test_lastfm_import_stays_unattributed_when_no_profile_given(tmp_path):
    # Direct low-level call with no profile_id (e.g. a caller that hasn't
    # been made profile-aware): unchanged behaviour, rows stay NULL exactly
    # like before per-profile attribution existed.
    db = MusicDatabase(str(tmp_path / "music.db"))
    worker = LastFMListeningImportWorker(db, _Config())
    inserted = worker._insert_events_deduped([{
        "track_id": "lastfm-1",
        "title": "Ceremony",
        "artist": "New Order",
        "album": "Substance",
        "played_at": "2023-11-14 22:13:20",
        "duration_ms": 180000,
        "db_track_id": None,
    }])

    assert inserted == 1
    conn = db._get_connection()
    row = conn.execute("SELECT profile_id FROM listening_history").fetchone()
    conn.close()
    assert row[0] is None


def test_lastfm_backfill_error_does_not_advance_incremental_cursor(tmp_path, monkeypatch):
    import core.listening_import.lastfm as lastfm_module

    db = MusicDatabase(str(tmp_path / "music.db"))
    calls = []

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def get_user_recent_tracks(self, username, page=1, limit=200, from_ts=None, to_ts=None, extended=False):
            calls.append({"username": username, "page": page, "from_ts": from_ts})
            if page == 1:
                return _recent_tracks_payload(page=1, total_pages=3, uts_values=[300, 299])
            raise RuntimeError("network fell over")

    monkeypatch.setattr(lastfm_module, "LastFMClient", FakeClient)
    monkeypatch.setattr(lastfm_module, "TRANSIENT_PAGE_RETRIES", 1)

    worker = LastFMListeningImportWorker(db, _Config())
    state = worker.run_once()

    assert state["status"] == "error"
    assert state["progress"] < 100
    assert state["backfill_complete"] is False
    assert state["backfill_next_page"] == 2
    assert state.get("last_imported_ts", 0) == 0
    assert state["pending_last_imported_ts"] == 300
    assert calls == [{"username": "tester", "page": 1, "from_ts": None}, {"username": "tester", "page": 2, "from_ts": None}]


def test_lastfm_retries_transient_page_failure_before_failing_backfill(tmp_path, monkeypatch):
    import core.listening_import.lastfm as lastfm_module

    db = MusicDatabase(str(tmp_path / "music.db"))
    calls = []

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def get_user_recent_tracks(self, username, page=1, limit=200, from_ts=None, to_ts=None, extended=False):
            calls.append(page)
            if page == 2 and calls.count(2) == 1:
                raise RuntimeError("500 Server Error for url: https://ws.audioscrobbler.com/2.0/?api_key=secret-token&page=2")
            return _recent_tracks_payload(page=page, total_pages=2, uts_values=[300 - page])

    monkeypatch.setattr(lastfm_module, "LastFMClient", FakeClient)
    monkeypatch.setattr(lastfm_module, "TRANSIENT_PAGE_RETRY_BASE_SECONDS", 0)

    worker = LastFMListeningImportWorker(db, _Config())
    state = worker.run_once()

    assert calls == [1, 2, 2]
    assert state["status"] == "complete"
    assert state["backfill_complete"] is True
    assert state.get("error") is None


def test_lastfm_failed_page_keeps_resume_point_and_redacts_api_key(tmp_path, monkeypatch):
    import core.listening_import.lastfm as lastfm_module

    db = MusicDatabase(str(tmp_path / "music.db"))
    calls = []

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def get_user_recent_tracks(self, username, page=1, limit=200, from_ts=None, to_ts=None, extended=False):
            calls.append(page)
            if page == 1:
                return _recent_tracks_payload(page=1, total_pages=3, uts_values=[300, 299])
            raise RuntimeError("500 Server Error for url: https://ws.audioscrobbler.com/2.0/?method=user.getRecentTracks&api_key=secret-token&page=2")

    monkeypatch.setattr(lastfm_module, "LastFMClient", FakeClient)
    monkeypatch.setattr(lastfm_module, "TRANSIENT_PAGE_RETRIES", 2)
    monkeypatch.setattr(lastfm_module, "TRANSIENT_PAGE_RETRY_BASE_SECONDS", 0)

    worker = LastFMListeningImportWorker(db, _Config())
    state = worker.run_once()

    assert calls == [1, 2, 2]
    assert state["status"] == "error"
    assert state["progress"] < 100
    assert state["backfill_complete"] is False
    assert state["backfill_next_page"] == 2
    assert "secret-token" not in state["error"]
    assert "api_key=REDACTED" in state["error"]


def test_lastfm_backfill_resumes_interrupted_page_instead_of_incremental(tmp_path, monkeypatch):
    import core.listening_import.lastfm as lastfm_module

    db = MusicDatabase(str(tmp_path / "music.db"))
    db.set_metadata("lastfm_listening_import_state", """{
        "status": "error",
        "page": 1,
        "total_pages": 2,
        "backfill_complete": false,
        "backfill_next_page": 2,
        "pending_last_imported_ts": 300,
        "pending_last_imported_at": "1970-01-01 00:05:00"
    }""")
    calls = []

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def get_user_recent_tracks(self, username, page=1, limit=200, from_ts=None, to_ts=None, extended=False):
            calls.append({"username": username, "page": page, "from_ts": from_ts})
            assert page == 2
            assert from_ts is None
            return _recent_tracks_payload(page=2, total_pages=2, uts_values=[200, 199])

    monkeypatch.setattr(lastfm_module, "LastFMClient", FakeClient)

    worker = LastFMListeningImportWorker(db, _Config())
    state = worker.run_once()

    assert state["status"] == "complete"
    assert state["backfill_complete"] is True
    assert state["last_imported_ts"] == 300
    assert state["backfill_next_page"] is None
    assert calls == [{"username": "tester", "page": 2, "from_ts": None}]


def _recent_tracks_payload(*, page: int, total_pages: int, uts_values: list[int]):
    return {
        "recenttracks": {
            "@attr": {"page": str(page), "totalPages": str(total_pages), "total": str(total_pages * 200)},
            "track": [
                {
                    "name": f"Track {uts}",
                    "artist": {"#text": "Artist"},
                    "album": {"#text": "Album"},
                    "date": {"uts": str(uts)},
                }
                for uts in uts_values
            ],
        }
    }


def test_lastfm_status_normalizes_legacy_false_complete_backfill(tmp_path):
    db = MusicDatabase(str(tmp_path / "music.db"))
    db.set_metadata("lastfm_listening_import_state", """{
        "status": "complete",
        "phase": "Last.fm is up to date",
        "page": 5,
        "total_pages": 500,
        "progress": 100,
        "last_success_at": "2026-08-23 10:00:00"
    }""")

    worker = LastFMListeningImportWorker(db, _Config())
    state = worker.status()

    assert state["status"] == "partial"
    assert state["phase"] == "Last.fm import needs to resume"
    assert state["progress"] == 1
    assert state["last_success_at"] is None


def test_lastfm_backfill_does_not_stop_after_duplicate_pages(tmp_path, monkeypatch):
    import core.listening_import.lastfm as lastfm_module

    db = MusicDatabase(str(tmp_path / "music.db"))
    for page in range(1, 4):
        worker = LastFMListeningImportWorker(db, _Config())
        events = [
            {
                "track_id": f"existing-{page}-{i}",
                "title": f"Track {page}-{i}",
                "artist": "Artist",
                "album": "Album",
                "played_at": f"2023-11-14 22:{page:02d}:{i:02d}",
                "duration_ms": 180000,
                "db_track_id": None,
            }
            for i in range(2)
        ]
        worker._insert_events_deduped(events)

    calls = []

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def get_user_recent_tracks(self, username, page=1, limit=200, from_ts=None, to_ts=None, extended=False):
            calls.append(page)
            if page <= 3:
                return {
                    "recenttracks": {
                        "@attr": {"page": str(page), "totalPages": "5", "total": "62200"},
                        "track": [
                            {
                                "name": f"Track {page}-{i}",
                                "artist": {"#text": "Artist"},
                                "album": {"#text": "Album"},
                                "date": {"uts": str(1700000000 + page * 60 + i)},
                            }
                            for i in range(2)
                        ],
                    }
                }
            return _recent_tracks_payload(page=page, total_pages=5, uts_values=[1000 - page])

    monkeypatch.setattr(lastfm_module, "LastFMClient", FakeClient)

    worker = LastFMListeningImportWorker(db, _Config())
    state = worker.run_once()

    assert calls == [1, 2, 3, 4, 5]
    assert state["status"] == "complete"
    assert state["backfill_complete"] is True
    assert state["page"] == 5


def test_lastfm_status_normalizes_false_complete_even_when_backfill_flag_was_set(tmp_path):
    db = MusicDatabase(str(tmp_path / "music.db"))
    db.set_metadata("lastfm_listening_import_state", """{
        "status": "complete",
        "phase": "Last.fm is up to date",
        "page": 3,
        "total_pages": 311,
        "total_scrobbles": 62200,
        "imported": 600,
        "progress": 100,
        "backfill_complete": true,
        "last_success_at": "2026-08-23 10:00:00"
    }""")

    worker = LastFMListeningImportWorker(db, _Config())
    state = worker.status()

    assert state["status"] == "partial"
    assert state["progress"] == 1
    assert state["last_success_at"] is None



def test_corrected_username_does_not_reuse_old_accounts_cursor(tmp_path, monkeypatch):
    import json
    import core.listening_import.lastfm as module
    db = MusicDatabase(str(tmp_path / "music.db"))
    db.set_metadata("lastfm_listening_import_state", json.dumps({
        "username": "k", "status": "complete", "backfill_complete": True,
        "last_imported_ts": 999999, "page": 3, "total_pages": 3,
    }))
    calls = []
    class Client:
        def __init__(self, **kwargs):
            pass
        def get_user_recent_tracks(self, username, **kwargs):
            calls.append((username, kwargs["page"], kwargs["from_ts"]))
            return _recent_tracks_payload(page=1, total_pages=1, uts_values=[100])
    monkeypatch.setattr(module, "LastFMClient", Client)
    state = LastFMListeningImportWorker(db, _Config()).run_once(username="corrected")
    assert state["status"] == "complete"
    assert calls == [("corrected", 1, None)]
    assert state["last_imported_ts"] == 100


def test_failed_corrected_account_does_not_keep_old_pending_cursor(tmp_path, monkeypatch):
    import json
    import core.listening_import.lastfm as module
    db = MusicDatabase(str(tmp_path / "music.db"))
    db.set_metadata("lastfm_listening_import_state", json.dumps({
        "username": "k", "status": "error", "pending_last_imported_ts": 999999,
        "last_imported_ts": 999999, "page": 2, "total_pages": 3,
    }))
    class Client:
        def __init__(self, **kwargs):
            pass
        def get_user_recent_tracks(self, username, **kwargs):
            raise RuntimeError("Account unavailable")
    monkeypatch.setattr(module, "LastFMClient", Client)
    monkeypatch.setattr(module, "TRANSIENT_PAGE_RETRIES", 1)
    state = LastFMListeningImportWorker(db, _Config()).run_once(username="corrected")
    assert state["status"] == "error"
    assert state["username"] == "corrected"
    assert not state.get("last_imported_ts")
    assert not state.get("pending_last_imported_ts")


# ── multi-profile (M01) ──────────────────────────────────────────────────

def test_two_profiles_import_independently_with_their_own_usernames(tmp_path, monkeypatch):
    """Each profile's OWN username is used for its own run, and the rows it
    inserts carry its own profile_id - no shared state, no cross-talk."""
    import core.listening_import.lastfm as lastfm_module

    db = MusicDatabase(str(tmp_path / "music.db"))
    db.create_profile("Parent")
    db.create_profile("Kid")
    db.set_profile_lastfm_username(2, "kid-fm")

    seen_usernames = []

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def get_user_recent_tracks(self, username, page=1, limit=200, from_ts=None, to_ts=None, extended=False):
            seen_usernames.append(username)
            return _recent_tracks_payload(page=1, total_pages=1, uts_values=[100 + len(seen_usernames)])

    monkeypatch.setattr(lastfm_module, "LastFMClient", FakeClient)

    worker = LastFMListeningImportWorker(db, _Config())
    state1 = worker.run_once(1)  # profile 1: no DB username -> legacy config "tester"
    state2 = worker.run_once(2)  # profile 2: its own "kid-fm"

    assert state1["status"] == "complete"
    assert state2["status"] == "complete"
    assert seen_usernames == ["tester", "kid-fm"]

    conn = db._get_connection()
    rows = conn.execute("SELECT profile_id FROM listening_history ORDER BY profile_id").fetchall()
    conn.close()
    assert [r[0] for r in rows] == [1, 2]


def test_run_all_profiles_imports_everyone_with_a_username(tmp_path, monkeypatch):
    import core.listening_import.lastfm as lastfm_module

    db = MusicDatabase(str(tmp_path / "music.db"))
    db.create_profile("Parent")
    db.create_profile("Kid")
    db.create_profile("Guest")
    db.set_profile_lastfm_username(2, "kid-fm")
    db.set_profile_lastfm_username(3, "guest-fm")
    # profile 1 has no DB username, but _Config's legacy "lastfm.username"
    # covers it - it must still be included.

    calls = []

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def get_user_recent_tracks(self, username, page=1, limit=200, from_ts=None, to_ts=None, extended=False):
            calls.append(username)
            return _recent_tracks_payload(page=1, total_pages=1, uts_values=[100 + len(calls)])

    monkeypatch.setattr(lastfm_module, "LastFMClient", FakeClient)

    worker = LastFMListeningImportWorker(db, _Config())
    results = worker.run_all_profiles()

    assert set(results.keys()) == {1, 2, 3}
    assert all(r["status"] == "complete" for r in results.values())
    assert sorted(calls) == ["guest-fm", "kid-fm", "tester"]


def test_a_profile_with_no_username_anywhere_is_skipped_by_run_all_profiles(tmp_path, monkeypatch):
    import core.listening_import.lastfm as lastfm_module

    db = MusicDatabase(str(tmp_path / "music.db"))
    db.create_profile("Kid")  # no lastfm_username set

    class _NoLegacyConfig:
        def get(self, key, default=None):
            return default

        def set(self, key, value):
            pass

    monkeypatch.setattr(lastfm_module, "LastFMClient", lambda **_k: None)

    worker = LastFMListeningImportWorker(db, _NoLegacyConfig())
    results = worker.run_all_profiles()

    assert results == {}


def test_is_running_is_isolated_per_profile(tmp_path, monkeypatch):
    """One profile's in-flight import must not report another profile (or
    the old no-arg caller) as busy, and vice versa."""
    import threading
    import core.listening_import.lastfm as lastfm_module

    db = MusicDatabase(str(tmp_path / "music.db"))
    db.create_profile("Parent")
    db.create_profile("Kid")
    db.set_profile_lastfm_username(2, "kid-fm")

    release = threading.Event()
    entered = threading.Event()

    class BlockingClient:
        def __init__(self, **_kwargs):
            pass

        def get_user_recent_tracks(self, username, page=1, limit=200, from_ts=None, to_ts=None, extended=False):
            entered.set()
            release.wait(timeout=5)
            return _recent_tracks_payload(page=1, total_pages=1, uts_values=[100])

    monkeypatch.setattr(lastfm_module, "LastFMClient", BlockingClient)

    worker = LastFMListeningImportWorker(db, _Config())
    worker.start_import(1)
    assert entered.wait(timeout=5)

    try:
        assert worker.is_running(1) is True
        assert worker.is_running(2) is False
        assert worker.is_running() is True  # "is anything running" for the automation-engine guard

        # NOTE: `status` here reads back as "running", not "skipped" - a
        # pre-existing dict-merge quirk in start_import (self.status()'s own
        # "status" key is spread in after the literal "skipped" and wins).
        # Unrelated to per-profile isolation, so asserted on the field that
        # actually distinguishes this response: `reason` is only ever set
        # on the duplicate-start path.
        skipped = worker.start_import(1)
        assert skipped.get("reason") == "Last.fm import already running"
    finally:
        release.set()
        thread = worker._threads.get(1)
        if thread:
            thread.join(timeout=5)
