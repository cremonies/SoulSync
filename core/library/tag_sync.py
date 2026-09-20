"""Shared DB-side tag-write payload builder.

Lifted out of web_server.py so the automatic Genre Tag Writer repair job can
build the exact same whitelist-filtered payload the manual "Write Tags"
endpoints already use, without either side duplicating the other or the
repair job importing from web_server (which would risk a circular import at
startup — web_server builds the repair worker, which imports the job
modules).
"""

from core.settings import config_manager


def build_library_tag_db_data(track_data, album_genres=None):
    """Build the metadata payload consumed by core.tag_writer.

    #1057 — strict genre filtering applies at the tag-write seam too, so
    this cleans up genres written before the whitelist was enabled.
    Strict off -> filter_genres is a no-op.
    """
    album_genres = album_genres or []
    if album_genres:
        from core.genre_filter import filter_genres
        album_genres = filter_genres(album_genres, config_manager)
    db_data = {
        'title': track_data.get('title'),
        'artist_name': track_data.get('artist_name'),
        'track_artist': track_data.get('track_artist'),
        'album_title': track_data.get('album_title'),
        'year': track_data.get('year'),
        'release_date': track_data.get('release_date'),  # #824: full date wins over year when present
        'genres': album_genres,
        'track_number': track_data.get('track_number'),
        'disc_number': track_data.get('disc_number'),
        'bpm': track_data.get('bpm'),
        'track_count': track_data.get('track_count'),
        'thumb_url': track_data.get('album_thumb_url') or track_data.get('artist_thumb_url'),
    }

    track_artist = track_data.get('track_artist')
    if isinstance(track_artist, str) and ';' in track_artist:
        artists_list = [name.strip() for name in track_artist.split(';') if name.strip()]
        if artists_list:
            db_data['artists_list'] = artists_list

    # Carry the known source IDs through so they get embedded too (the writer
    # only acts on the ones present). These come from t.* on the track row.
    for _k in ('spotify_track_id', 'itunes_track_id', 'musicbrainz_recording_id'):
        _v = track_data.get(_k)
        if _v:
            db_data[_k] = _v

    return db_data
