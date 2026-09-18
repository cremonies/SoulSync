"""Per-profile Jellyfin listening history (M01).

Before this, ListeningStatsWorker polled one shared connection and every
play landed in listening_history with no owner - a household on one
Jellyfin server had one blended taste signal for everyone. Profiles that
link their own jellyfin_user_id now get polled and attributed individually;
everything else (Plex, Navidrome, unlinked Jellyfin accounts) keeps the
exact pre-existing shared/unattributed behaviour.
"""

from __future__ import annotations

import pytest

from database.music_database import MusicDatabase
from core.listening_stats_worker import ListeningStatsWorker


class _FakeConfigManager:
    def __init__(self, active_server='jellyfin'):
        self._active_server = active_server

    def get(self, key, default=None):
        return default

    def get_active_media_server(self):
        return self._active_server


class _FakeJellyfinClient:
    """Records which user_id each get_play_history call asked for, and
    returns a canned history for that user."""

    def __init__(self, history_by_user):
        self.history_by_user = history_by_user
        self.calls = []
        self.user_id = 'admin-account'  # the client's own connected account

    def get_play_history(self, limit=500, user_id=None):
        target = user_id or self.user_id
        self.calls.append(target)
        return self.history_by_user.get(target, [])

    def get_track_play_counts(self):
        return {}


class _FakeEngine:
    def __init__(self, client):
        self._client = client

    def client(self, name):
        return self._client


@pytest.fixture
def db(tmp_path):
    return MusicDatabase(str(tmp_path / "music.db"))


def _event(track_id, title, artist, played_at):
    return {
        'track_id': track_id,
        'track_title': title,
        'artist': artist,
        'album': 'Album',
        'played_at': played_at,
        'duration_ms': 180000,
    }


class TestGetProfilesWithJellyfinUser:
    def test_only_linked_profiles_come_back(self, db):
        p1 = db.create_profile('Parent')
        p2 = db.create_profile('Kid')
        db.create_profile('Unlinked')
        db.set_profile_server_library(p1, 'jellyfin', library_id='lib1', user_id='parent-jf')
        db.set_profile_server_library(p2, 'jellyfin', library_id='lib1', user_id='kid-jf')

        linked = db.get_profiles_with_jellyfin_user()
        by_profile = {row['profile_id']: row['jellyfin_user_id'] for row in linked}

        assert by_profile == {p1: 'parent-jf', p2: 'kid-jf'}

    def test_empty_when_nobody_linked(self, db):
        db.create_profile('Solo')
        assert db.get_profiles_with_jellyfin_user() == []


class TestPerProfileJellyfinPoll:
    def test_linked_profiles_polled_individually_and_attributed(self, db):
        parent_id = db.create_profile('Parent')
        kid_id = db.create_profile('Kid')
        db.set_profile_server_library(parent_id, 'jellyfin', library_id='lib1', user_id='parent-jf')
        db.set_profile_server_library(kid_id, 'jellyfin', library_id='lib1', user_id='kid-jf')

        client = _FakeJellyfinClient({
            'parent-jf': [_event('t1', 'Song A', 'Band A', '2026-01-01T10:00:00')],
            'kid-jf': [_event('t2', 'Wheels on the Bus', 'Kids Band', '2026-01-01T11:00:00')],
            # the shared/default poll uses the client's own account
            'admin-account': [_event('t3', 'Admin Play', 'Some Band', '2026-01-01T12:00:00')],
        })
        worker = ListeningStatsWorker(db, _FakeConfigManager('jellyfin'), _FakeEngine(client))
        worker._poll()

        # each linked profile was polled with ITS OWN user_id, not the
        # client's default account, and the default poll also still ran
        assert set(client.calls) == {'parent-jf', 'kid-jf', 'admin-account'}

        with db._get_connection() as conn:
            rows = conn.execute(
                "SELECT title, profile_id FROM listening_history ORDER BY title"
            ).fetchall()
        by_title = {r[0]: r[1] for r in rows}

        assert by_title['Song A'] == parent_id
        assert by_title['Wheels on the Bus'] == kid_id
        # the shared default-account play stays unattributed
        assert by_title['Admin Play'] is None

    def test_unlinked_install_keeps_old_shared_behaviour(self, db):
        # No profile has a jellyfin_user_id at all - must behave exactly
        # like before per-profile polling existed: one shared poll, NULL
        # profile_id, no per-profile calls attempted.
        client = _FakeJellyfinClient({
            'admin-account': [_event('t1', 'Only Play', 'Band', '2026-01-01T10:00:00')],
        })
        worker = ListeningStatsWorker(db, _FakeConfigManager('jellyfin'), _FakeEngine(client))
        worker._poll()

        assert client.calls == ['admin-account']
        with db._get_connection() as conn:
            row = conn.execute(
                "SELECT title, profile_id FROM listening_history"
            ).fetchone()
        assert row[0] == 'Only Play'
        assert row[1] is None

    def test_non_jellyfin_server_never_does_per_profile_polling(self, db):
        # Plex/Navidrome stay exactly as before - no per-profile branch at
        # all, even if a profile happens to have a stray jellyfin_user_id.
        profile_id = db.create_profile('Someone')
        db.set_profile_server_library(profile_id, 'jellyfin', library_id='lib1', user_id='some-jf')

        client = _FakeJellyfinClient({
            'admin-account': [_event('t1', 'Plex Play', 'Band', '2026-01-01T10:00:00')],
        })
        worker = ListeningStatsWorker(db, _FakeConfigManager('plex'), _FakeEngine(client))
        worker._poll()

        assert client.calls == ['admin-account']
