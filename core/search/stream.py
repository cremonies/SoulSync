"""Single-track stream search — finds the best Soulseek result for a track
play preview.

Builds a small ordered list of search query variants (artist+title,
artist+cleaned title; or title-only when the stream source is Soulseek
itself) and walks them until one returns a usable match through the
matching engine.

Stream source resolution:
- If `download_source.stream_source` is "youtube" (default), use the
  YouTube downloader for previews — instant, no auth pressure on the
  download stack.
- If it's "deezer_preview", fetch Deezer's own public 30-second preview
  clip directly (no auth, no queueing, no full download) — the actual
  "preview" feature Deezer's public catalog API exposes for every track.
  Independent of download_source.mode: it never touches the download
  stack at all, so it stays "deezer_preview" even when the active/hybrid
  download source is something else entirely.
- If it's "active", mirror the user's download mode (tidal / qobuz /
  hifi / deezer_dl / lidarr) — but coerce Soulseek to YouTube because
  Soulseek is too slow for streaming previews.
"""

from __future__ import annotations

import logging
import re
from typing import Callable, Optional

logger = logging.getLogger(__name__)


def _resolve_effective_stream_mode(config_manager) -> str:
    """Pick the streaming source based on settings."""
    stream_source = config_manager.get('download_source.stream_source', 'youtube')
    download_mode = config_manager.get('download_source.mode', 'hybrid')

    if stream_source == 'youtube':
        return 'youtube'
    if stream_source == 'deezer_preview':
        return 'deezer_preview'

    hybrid_order = config_manager.get('download_source.hybrid_order', ['hifi', 'youtube', 'soulseek'])
    hybrid_first = hybrid_order[0] if hybrid_order else config_manager.get('download_source.hybrid_primary', 'hifi')

    if download_mode == 'soulseek' or (download_mode == 'hybrid' and hybrid_first == 'soulseek'):
        logger.info("Stream source is 'active' but primary is Soulseek — falling back to YouTube")
        return 'youtube'
    if download_mode == 'hybrid':
        return hybrid_first
    return download_mode


def _strip_version_markers(track_name: str) -> str:
    """Drop parenthesized/bracketed suffixes ("(Radio Edit)", "[Live]").

    Deezer's own catalog title rarely carries these — a search for the
    exact "(Radio Edit)"-suffixed title a source track came from routinely
    comes back empty even though the song itself is on Deezer under its
    plain title.
    """
    cleaned = re.sub(r'\s*\([^)]*\)', '', track_name)
    cleaned = re.sub(r'\s*\[[^\]]*\]', '', cleaned)
    return cleaned.strip()


def _deezer_preview_result(artist_name: str, track_name: str, deezer_client) -> Optional[dict]:
    """Look up a track on Deezer's public catalog and return its 30-second
    preview clip as a stream result, or None when there's no match or no
    preview URL (some regions/tracks omit it).

    Tries the exact title first, then a version-marker-stripped variant —
    same "(Radio Edit)"/"[Live]" cleaning the Soulseek/YouTube queries use,
    since Deezer's own title field almost never carries those suffixes.

    Shaped differently from ``_result_to_dict``'s Soulseek-style dict —
    ``result_type: "preview_url"`` is the signal ``prepare_stream_task``
    uses to skip the download-orchestrator flow entirely and just fetch
    this one small, already-public file.
    """
    if deezer_client is None:
        return None

    candidates = [track_name]
    cleaned = _strip_version_markers(track_name)
    if cleaned and cleaned.lower() != track_name.lower():
        candidates.append(cleaned)

    for candidate_title in candidates:
        try:
            track = deezer_client.search_track(artist_name, candidate_title)
        except Exception as e:
            logger.warning(f"Deezer preview lookup failed for '{artist_name} - {candidate_title}': {e}")
            continue
        if not track:
            continue
        preview_url = track.get('preview')
        if not preview_url:
            logger.info(f"Deezer match for '{artist_name} - {candidate_title}' has no preview clip")
            continue
        return {
            "result_type": "preview_url",
            "preview_url": preview_url,
            "filename": f"{artist_name} - {track_name} (preview).mp3",
            "size": 0,
            "bitrate": 128,
            "duration": 30,
            "quality": "preview",
            "username": "deezer_preview",
        }
    return None


def _build_stream_queries(track_name: str, artist_name: str, effective_mode: str) -> list[str]:
    """Build an ordered, deduped list of search queries to try."""
    queries: list[str] = []

    is_streaming_source = effective_mode in ('youtube', 'tidal', 'qobuz', 'hifi', 'deezer_dl', 'lidarr')

    cleaned_name = _strip_version_markers(track_name)

    if is_streaming_source:
        if artist_name and track_name:
            queries.append(f"{artist_name} {track_name}".strip())
        if cleaned_name and cleaned_name.lower() != track_name.lower():
            queries.append(f"{artist_name} {cleaned_name}".strip())
    else:
        if track_name.strip():
            queries.append(track_name.strip())
        if cleaned_name and cleaned_name.lower() != track_name.lower():
            queries.append(cleaned_name.strip())

    seen: set[str] = set()
    deduped: list[str] = []
    for q in queries:
        if q and q.lower() not in seen:
            deduped.append(q)
            seen.add(q.lower())
    return deduped


def _result_to_dict(best_result) -> dict:
    return {
        "username": best_result.username,
        "filename": best_result.filename,
        "size": best_result.size,
        "bitrate": best_result.bitrate,
        "duration": best_result.duration,
        "quality": best_result.quality,
        "free_upload_slots": best_result.free_upload_slots,
        "upload_speed": best_result.upload_speed,
        "queue_length": best_result.queue_length,
        "result_type": "track",
    }


def stream_search_track(
    *,
    track_name: str,
    artist_name: str,
    album_name: Optional[str],
    duration_ms: int,
    config_manager,
    download_orchestrator,
    matching_engine,
    run_async: Callable,
    deezer_client_getter: Optional[Callable] = None,
) -> Optional[dict]:
    """Find the best Soulseek/stream-source result for a single track.

    Returns the matched result dict on success, or `None` if no query
    variant produced a usable match. The route layer turns `None` into a
    404 response.

    ``deezer_client_getter`` is only consulted when the effective mode is
    "deezer_preview" — it's optional so existing callers/tests that never
    exercise that mode don't need to supply one.
    """
    effective_mode = _resolve_effective_stream_mode(config_manager)
    logger.info(f"Stream source effective mode: {effective_mode}")

    if effective_mode == 'deezer_preview':
        # Deliberately does not fall through to a full download on a miss —
        # the user picked "preview only", and silently substituting a full
        # download from a different source on failure would defeat that
        # choice. A miss here just means no stream, same as an exhausted
        # query loop below.
        client = deezer_client_getter() if deezer_client_getter else None
        result = _deezer_preview_result(artist_name, track_name, client)
        if result:
            logger.info(f"Deezer preview found for '{artist_name} - {track_name}'")
        else:
            logger.warning(f"No Deezer preview available for '{artist_name} - {track_name}'")
        return result

    temp_track = type('TempTrack', (), {
        'name': track_name,
        'artists': [artist_name],
        'album': album_name if album_name else None,
        'duration_ms': duration_ms,
    })()

    queries = _build_stream_queries(track_name, artist_name, effective_mode)

    # Map mode name to canonical registry name (legacy 'deezer_dl'
    # alias resolves to 'deezer' via the orchestrator's registry).
    stream_client = download_orchestrator.client(effective_mode)
    use_direct_client = stream_client is not None

    max_peer_queue = config_manager.get('soulseek.max_peer_queue', 0) or 0

    # #1056: user override from Settings → Downloads; unset keeps the
    # historical 15s. This explicit value has always governed the stream
    # path regardless of which source serves it (including soulseek's own
    # windowed search_timeout, which this path never used).
    # getattr-guarded: config_manager is injected, and test fakes may not
    # implement the new accessor.
    search_timeout = getattr(config_manager, 'get_source_search_timeout', lambda: None)() or 15

    for query_index, query in enumerate(queries):
        logger.info(f"Stream query {query_index + 1}/{len(queries)}: '{query}'")
        try:
            if use_direct_client:
                tracks_result, _ = run_async(stream_client.search(query, timeout=search_timeout))
            else:
                tracks_result, _ = run_async(download_orchestrator.search(query, timeout=search_timeout))

            if not tracks_result:
                logger.info(f"No results for query '{query}', trying next...")
                continue

            best_matches = matching_engine.find_best_slskd_matches_enhanced(
                temp_track, tracks_result, max_peer_queue=max_peer_queue
            )
            if best_matches:
                best = best_matches[0]
                logger.info(f"Stream match for '{query}': {best.filename} ({best.quality})")
                return _result_to_dict(best)

            logger.info(f"No suitable matches for query '{query}', trying next...")
        except Exception as e:
            logger.warning(f"Stream search failed for query '{query}': {e}")
            continue

    logger.warning(f"No stream match found after {len(queries)} queries")
    return None
