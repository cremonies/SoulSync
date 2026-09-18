"""Seasonal Mix generator (variant = season key).

Variant = season key from ``SEASONAL_CONFIG`` (``'halloween'`` /
``'christmas'`` / ``'valentines'`` / ``'summer'`` / ``'spring'`` /
``'autumn'``). One playlist per season — user picks which seasons
to enable; idle seasons can stay un-refreshed until their active
period.

Reads curated track IDs from ``curated_seasonal_playlists`` (via
``SeasonalDiscoveryService.get_curated_seasonal_playlist``) and
hydrates them against ``seasonal_tracks`` (which carries full
metadata including ``track_data_json`` for sync-ready downstream
use)."""

from __future__ import annotations

import json
from typing import Any, List

from core.personalized.specs import PlaylistKindSpec, get_registry
from core.personalized.types import PlaylistConfig, Track


KIND = 'seasonal_mix'


def _resolve_seasonal_service(deps: Any):
    """Pull the SeasonalDiscoveryService instance from deps."""
    svc = getattr(deps, 'seasonal_service', None) or (
        deps.get('seasonal_service') if isinstance(deps, dict) else None
    )
    if svc is None:
        raise RuntimeError(
            "Seasonal mix generator deps missing `seasonal_service` "
            "(SeasonalDiscoveryService instance)."
        )
    return svc


def _resolve_database(deps: Any):
    db = getattr(deps, 'database', None) or (
        deps.get('database') if isinstance(deps, dict) else None
    )
    if db is None:
        raise RuntimeError("Seasonal mix generator deps missing `database`")
    return db


def _resolve_active_source(deps: Any) -> str:
    fn = getattr(deps, 'get_active_discovery_source', None) or (
        deps.get('get_active_discovery_source') if isinstance(deps, dict) else None
    )
    return fn() if callable(fn) else 'spotify'


def _hydrate_seasonal_tracks(db, season_key: str, source: str, track_ids: List[str]) -> List[Track]:
    """Look up the seasonal_tracks rows for the given IDs."""
    if not track_ids:
        return []
    placeholders = ','.join('?' * len(track_ids))
    with db._get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"""
            SELECT spotify_track_id, track_name, artist_name, album_name,
                   album_cover_url, duration_ms, popularity, track_data_json
            FROM seasonal_tracks
            WHERE season_key = ? AND source = ?
              AND spotify_track_id IN ({placeholders})
            """,
            (season_key, source, *track_ids),
        )
        rows = cursor.fetchall()

    by_id = {}
    for r in rows:
        if hasattr(r, 'keys'):
            r = dict(r)
        else:
            r = dict(zip(
                ('spotify_track_id', 'track_name', 'artist_name', 'album_name',
                 'album_cover_url', 'duration_ms', 'popularity', 'track_data_json'),
                r,
                strict=False,
            ))
        td = r.get('track_data_json')
        if isinstance(td, str):
            try:
                td = json.loads(td)
            except (ValueError, TypeError):
                td = None
        r['track_data_json'] = td
        r['source'] = source
        by_id[r['spotify_track_id']] = r

    # Preserve curated order.
    return [
        Track.from_dict(by_id[tid])
        for tid in track_ids
        if tid in by_id
    ]


def _profile_rewind_tracks(deps: Any, seasonal_service: Any, db: Any, variant: str,
                           limit: int) -> List[Track]:
    """Live, per-profile rewind leg (M01) - the ONE part of the seasonal mix
    that is personal taste rather than a library/discovery fact, so unlike
    the rest of the pool it is computed fresh per generation instead of
    pre-baked into the shared ``seasonal_tracks`` pool by the background
    sweep. Returns [] whenever there is nothing personal to add: a
    keyword-only season (halloween/christmas never had a rewind leg),
    listening history that isn't profile-attributed on this install, or a
    profile that hasn't linked a Jellyfin user - all of which fall back to
    exactly the pre-existing shared behaviour rather than an empty playlist.
    """
    try:
        from core.seasonal_vibes import VIBE_SEASONS, real_months_for, rewind_tracks
    except Exception:
        return []
    if variant not in VIBE_SEASONS:
        return []

    from core.profile_context import get_current_profile_id
    profile_id = get_current_profile_id()
    try:
        if db.listening_history_scope() != 'profile':
            return []
        linked = db.get_profiles_with_jellyfin_user()
    except Exception:
        return []
    if not any(link.get('profile_id') == profile_id for link in linked):
        return []

    try:
        from core.seasonal_discovery import SEASONAL_CONFIG
        season_config = SEASONAL_CONFIG.get(variant)
        if not season_config:
            return []
        holiday = variant == 'valentines'
        months = real_months_for(
            season_config['active_months'], seasonal_service._get_hemisphere(), holiday)
        rewind = rewind_tracks(db, months, limit=limit, profile_id=profile_id)
        return [Track.from_dict(t) for t in rewind]
    except Exception:
        return []


def _dedupe_tracks(tracks: List[Track]) -> List[Track]:
    """First occurrence wins - used to let personal rewind tracks lead
    without duplicating anything the shared pool also surfaced."""
    seen = set()
    out = []
    for t in tracks:
        key = t.primary_id() or (t.track_name.lower(), t.artist_name.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out


def generate(deps: Any, variant: str, config: PlaylistConfig) -> List[Track]:
    if not variant:
        raise ValueError('Seasonal Mix requires a season variant')
    seasonal_service = _resolve_seasonal_service(deps)
    db = _resolve_database(deps)
    source = _resolve_active_source(deps)
    track_ids = seasonal_service.get_curated_seasonal_playlist(variant, source=source) or []
    pool_tracks = _hydrate_seasonal_tracks(db, variant, source, track_ids)

    personal = _profile_rewind_tracks(deps, seasonal_service, db, variant, config.limit)
    tracks = _dedupe_tracks(personal + pool_tracks) if personal else pool_tracks
    return tracks[:config.limit]


def variant_resolver(deps: Any) -> List[str]:
    """Return every season key from SEASONAL_CONFIG."""
    try:
        from core.seasonal_discovery import SEASONAL_CONFIG
    except Exception:
        return []
    return list(SEASONAL_CONFIG.keys())


SPEC = PlaylistKindSpec(
    kind=KIND,
    name_template='Seasonal — {variant}',
    description='Holiday / season-themed picks. One playlist per season; user enables which to track.',
    default_config=PlaylistConfig(limit=50, max_per_album=2, max_per_artist=3),
    generator=generate,
    variant_resolver=variant_resolver,
    requires_variant=True,
    tags=['curated', 'seasonal'],
)


if get_registry().get(KIND) is None:
    get_registry().register(SPEC)
