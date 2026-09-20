"""Genre Tag Writer — the missing last step from clean DB genres to a clean
file tag.

Genre Enrichment and Genre Tag Cleanup only ever touch the database; the only
thing that ever pushed genre into a file was the manual "Write Tags" bulk
action. This job automates exactly that, reusing the same whitelist-filtered
payload builder (core.library.tag_sync) and the same diff logic
(core.tag_writer.build_tag_diff) so it can never disagree with the manual tool
about what "clean" means, and never downgrades a file that already carries a
richer genre list than the database.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from core.repair_jobs.base import JobContext
from core.repair_jobs.genre_tag_writer import GenreTagWriterJob, _parse_genres
from core.repair_worker import RepairWorker
from database.music_database import MusicDatabase


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_flac(path, tags=None):
    """Minimal but real FLAC with synthetic frames — same recipe used by the
    comma-artist-splitter tests (survives the atomic save's frame compare)."""
    from mutagen.flac import FLAC
    path = Path(path)
    si = bytearray(34)
    si[0:2] = struct.pack(">H", 4096)
    si[2:4] = struct.pack(">H", 4096)
    si[10] = 0x0A
    si[12] = 0x70
    block_header = bytes([0x80, 0x00, 0x00, 0x22])
    path.write_bytes(b"fLaC" + block_header + bytes(si) + bytes(range(256)) * 8)
    audio = FLAC(str(path))
    for k, v in (tags or {}).items():
        audio[k] = v if isinstance(v, list) else [v]
    audio.save()
    return audio


def _read_genre(path):
    from mutagen.flac import FLAC
    audio = FLAC(str(path))
    val = audio.get('genre')
    return val[0] if val else None


class _Cfg:
    """Fake JobContext.config_manager — only the job-settings lookup."""

    def __init__(self, dry_run=True):
        self._d = {'repair.jobs.genre_tag_writer.settings': {'dry_run': dry_run}}

    def get(self, key, default=None):
        return self._d.get(key, default)


@pytest.fixture()
def db(tmp_path):
    d = MusicDatabase(str(tmp_path / 't.db'))
    conn = d._get_connection()
    cur = conn.cursor()
    cur.execute("INSERT INTO artists (id, name) VALUES (1, 'Test Artist')")
    cur.execute("INSERT INTO albums (id, artist_id, title, genres) VALUES "
                "(1, 1, 'Test Album', ?)", (json.dumps(['Rock', 'Jazz']),))
    conn.commit()
    conn.close()
    return d


def _insert_track(db, file_path, track_id=1):
    conn = db._get_connection()
    conn.execute(
        "INSERT INTO tracks (id, album_id, artist_id, title, file_path) VALUES (?, 1, 1, 'Song', ?)",
        (track_id, str(file_path)))
    conn.commit()
    conn.close()


def _ctx(db, findings, dry_run=True, tmp_path=None):
    return JobContext(
        db=db, transfer_folder=str(tmp_path) if tmp_path else '/tmp',
        config_manager=_Cfg(dry_run=dry_run),
        create_finding=lambda **kw: findings.append(kw) or True,
    )


def _worker_with(db):
    w = RepairWorker.__new__(RepairWorker)
    w.db = db
    w.transfer_folder = '/tmp'
    w._config_manager = None
    return w


# ── _parse_genres ────────────────────────────────────────────────────────────

@pytest.mark.parametrize('raw,expected', [
    (None, []), ('', []), ('[]', []),
    ('["Rock", "Jazz"]', ['Rock', 'Jazz']),
    ('Rock, Jazz , ', ['Rock', 'Jazz']),
])
def test_parse_genres(raw, expected):
    assert _parse_genres(raw) == expected


# ── scan ─────────────────────────────────────────────────────────────────────

def test_scan_creates_finding_when_file_genre_differs(db, tmp_path):
    fp = tmp_path / 'song.flac'
    _make_flac(fp, {'genre': 'Pop'})
    _insert_track(db, fp)

    findings = []
    res = GenreTagWriterJob().scan(_ctx(db, findings, tmp_path=tmp_path))

    assert res.findings_created == 1
    assert res.scanned == 1
    f = findings[0]
    assert f['entity_type'] == 'track' and f['entity_id'] == '1'
    assert f['details']['current_genre'] == 'Pop'
    assert f['details']['new_genre'] == ['Rock', 'Jazz']
    # The file itself is untouched in dry-run mode.
    assert _read_genre(fp) == 'Pop'


def test_scan_skips_when_file_already_matches(db, tmp_path):
    fp = tmp_path / 'song.flac'
    _make_flac(fp, {'genre': 'Rock, Jazz'})
    _insert_track(db, fp)

    findings = []
    res = GenreTagWriterJob().scan(_ctx(db, findings, tmp_path=tmp_path))

    assert res.findings_created == 0
    assert res.skipped == 1


def test_scan_skips_when_album_has_no_genres(db, tmp_path):
    conn = db._get_connection()
    conn.execute("UPDATE albums SET genres = NULL WHERE id = 1")
    conn.commit(); conn.close()

    fp = tmp_path / 'song.flac'
    _make_flac(fp, {'genre': 'Pop'})
    _insert_track(db, fp)

    findings = []
    res = GenreTagWriterJob().scan(_ctx(db, findings, tmp_path=tmp_path))

    assert res.findings_created == 0
    assert res.scanned == 0  # excluded at the SQL level — nothing to check


def test_scan_does_not_downgrade_a_richer_file_genre_list(db, tmp_path):
    """A file with 'Pop, Synth Pop' against a DB of just ['Pop'] must NOT be
    flagged — the same subset protection build_tag_diff gives Write Tags."""
    conn = db._get_connection()
    conn.execute("UPDATE albums SET genres = ? WHERE id = 1", (json.dumps(['Pop']),))
    conn.commit(); conn.close()

    fp = tmp_path / 'song.flac'
    _make_flac(fp, {'genre': 'Pop, Synth Pop'})
    _insert_track(db, fp)

    findings = []
    res = GenreTagWriterJob().scan(_ctx(db, findings, tmp_path=tmp_path))

    assert res.findings_created == 0
    assert _read_genre(fp) == 'Pop, Synth Pop'


def test_scan_auto_writes_when_dry_run_off(db, tmp_path):
    fp = tmp_path / 'song.flac'
    _make_flac(fp, {'genre': 'Pop'})
    _insert_track(db, fp)

    findings = []
    res = GenreTagWriterJob().scan(_ctx(db, findings, dry_run=False, tmp_path=tmp_path))

    assert res.auto_fixed == 1
    assert findings == []  # no findings created in live mode
    assert _read_genre(fp) == 'Rock, Jazz'


def test_scan_respects_whitelist(db, tmp_path, monkeypatch):
    import core.library.tag_sync as tag_sync

    class _StrictCfg:
        def get(self, key, default=None):
            return {'genre_whitelist.enabled': True, 'genre_whitelist.genres': ['Rock']}.get(key, default)

    monkeypatch.setattr(tag_sync, 'config_manager', _StrictCfg())

    fp = tmp_path / 'song.flac'
    _make_flac(fp, {'genre': 'Pop'})
    _insert_track(db, fp)

    findings = []
    res = GenreTagWriterJob().scan(_ctx(db, findings, tmp_path=tmp_path))

    assert res.findings_created == 1
    assert findings[0]['details']['new_genre'] == ['Rock']  # Jazz filtered out


# ── fix ──────────────────────────────────────────────────────────────────────

def test_fix_writes_genre(db, tmp_path):
    fp = tmp_path / 'song.flac'
    _make_flac(fp, {'genre': 'Pop'})
    w = _worker_with(db)

    out = w._fix_genre_tag_writer('track', '1', str(fp), {
        'current_genre': 'Pop', 'new_genre': ['Rock', 'Jazz'], 'resolved_path': str(fp),
    })

    assert out['success'] and out['action'] == 'genre_written'
    assert _read_genre(fp) == 'Rock, Jazz'


def test_fix_refuses_stale_finding(db, tmp_path):
    """The file's genre no longer matches what the scan saw — something else
    already changed it. Must not clobber the current value."""
    fp = tmp_path / 'song.flac'
    _make_flac(fp, {'genre': 'Something Else'})
    w = _worker_with(db)

    out = w._fix_genre_tag_writer('track', '1', str(fp), {
        'current_genre': 'Pop', 'new_genre': ['Rock', 'Jazz'], 'resolved_path': str(fp),
    })

    assert out['success'] and out['action'] == 'already_changed'
    assert _read_genre(fp) == 'Something Else'


def test_fix_refuses_bad_inputs(db):
    w = _worker_with(db)
    assert not w._fix_genre_tag_writer('track', '1', None, {})['success']
    assert not w._fix_genre_tag_writer('track', '1', None, {'new_genre': []})['success']
