"""Genre Tag Writer Job — the missing last step between clean genre data in
the database and a clean genre tag in the actual file.

Genre Enrichment (merges genres from every matched source into the DB) and
Genre Tag Cleanup (strips off-whitelist genres already stored) both stop at
the database — neither one ever opens a file. The only thing that has ever
pushed a track's genre into its file is the manual "Write Tags" bulk action.
This job automates exactly that one step, using the same whitelist-filtered
payload builder (core.library.tag_sync) so it can never disagree with the
manual tool about what "clean" means, and the same diff logic (core.tag_writer
.build_tag_diff) so a file that already carries a richer genre list than the
database is left alone rather than downgraded.

It never invents genre data — it only ever writes what a track's ALBUM
already has stored, same as Write Tags does. If the whitelist strips every
genre an album had, there is nothing to write and the track is skipped.
"""

import json
import os

from core.library.path_resolver import resolve_library_file_path
from core.library.tag_sync import build_library_tag_db_data
from core.repair_jobs import register_job
from core.repair_jobs.base import JobContext, JobResult, RepairJob
from core.tag_writer import build_tag_diff, read_file_tags
from utils.logging_config import get_logger

logger = get_logger("repair_job.genre_tag_writer")

_COLUMNS = (
    'id', 'file_path', 'title', 'track_number', 'disc_number', 'bpm',
    'spotify_track_id', 'itunes_track_id', 'musicbrainz_recording_id',
    'artist_name', 'album_title', 'album_genres', 'track_count',
    'album_thumb_url', 'artist_thumb_url',
)


def _parse_genres(raw) -> list:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [str(g).strip() for g in parsed if str(g).strip()]
    except (TypeError, ValueError):
        pass
    return [g.strip() for g in str(raw).split(',') if g.strip()]


@register_job
class GenreTagWriterJob(RepairJob):
    job_id = 'genre_tag_writer'
    display_name = 'Genre Tag Writer'
    description = "Writes each album's whitelist-clean genre into files whose tag has drifted from the database"
    help_text = (
        'Genre Enrichment and Genre Tag Cleanup only ever touch the database — neither one writes '
        'to your actual files. This job is the missing last step: for every track, it compares the '
        "file's current genre tag against its album's genre list in the database (the same "
        'whitelist-filtered value "Write Tags" uses) and, where they differ, writes the clean value '
        'into the file.\n\n'
        'It never invents genres — it only ever writes what is already stored on the album. If the '
        'file already carries a richer genre list that includes everything in the database (e.g. the '
        'file has "Pop, Dance Pop" and the database just has "Pop"), it is left alone rather than '
        'replaced with something less specific — the same protection the manual Write Tags preview '
        'uses.\n\n'
        'Dry run is enabled by default: each finding shows the exact old -> new genre change before '
        'anything is written. Disable dry run to write every found change automatically.'
    )
    icon = 'repair-icon-genre'
    default_enabled = False
    default_interval_hours = 168  # Weekly
    default_settings = {'dry_run': True}
    auto_fix = True
    writes_library_files = True

    def _dry_run(self, context: JobContext) -> bool:
        if not context.config_manager:
            return True
        settings = context.config_manager.get(f'repair.jobs.{self.job_id}.settings', {}) or {}
        return bool(settings.get('dry_run', True))

    def estimate_scope(self, context: JobContext) -> int:
        try:
            conn = context.db._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT COUNT(*) FROM tracks t
                    JOIN albums al ON al.id = t.album_id
                    WHERE t.file_path IS NOT NULL AND t.file_path != ''
                      AND al.genres IS NOT NULL AND al.genres != '' AND al.genres != '[]'
                """)
                return cursor.fetchone()[0]
            finally:
                conn.close()
        except Exception:
            return 0

    def scan(self, context: JobContext) -> JobResult:
        result = JobResult()
        dry_run = self._dry_run(context)

        conn = None
        try:
            conn = context.db._get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT t.id, t.file_path, t.title, t.track_number, t.disc_number, t.bpm,
                       t.spotify_track_id, t.itunes_track_id, t.musicbrainz_recording_id,
                       a.name AS artist_name,
                       al.title AS album_title, al.genres AS album_genres, al.track_count,
                       al.thumb_url AS album_thumb_url, a.thumb_url AS artist_thumb_url
                FROM tracks t
                JOIN artists a ON a.id = t.artist_id
                JOIN albums al ON al.id = t.album_id
                WHERE t.file_path IS NOT NULL AND t.file_path != ''
                  AND al.genres IS NOT NULL AND al.genres != '' AND al.genres != '[]'
            """)
            tracks = cursor.fetchall()
        except Exception as e:
            logger.error("Error fetching tracks: %s", e, exc_info=True)
            result.errors += 1
            return result
        finally:
            if conn:
                conn.close()

        if not tracks:
            if context.report_progress:
                context.report_progress(phase='No tracks with album genres to check',
                                        log_line='Nothing to check', log_type='success')
            return result

        if context.update_progress:
            context.update_progress(0, len(tracks))
        if context.report_progress:
            context.report_progress(phase=f'Checking {len(tracks)} track(s) for genre drift...',
                                    total=len(tracks))

        fixer = None
        if not dry_run:
            from core.repair_worker import RepairWorker
            fixer = RepairWorker(context.db, transfer_folder=context.transfer_folder)
            fixer._config_manager = context.config_manager

        for i, raw_row in enumerate(tracks):
            if context.check_stop():
                return result
            if i % 20 == 0 and context.wait_if_paused():
                return result
            if context.update_progress and (i + 1) % 20 == 0:
                context.update_progress(i + 1, len(tracks))

            row = dict(zip(_COLUMNS, raw_row, strict=True))
            result.scanned += 1

            try:
                resolved = resolve_library_file_path(
                    row['file_path'], transfer_folder=context.transfer_folder,
                    config_manager=context.config_manager)
                if not resolved or not os.path.exists(resolved):
                    result.skipped += 1
                    continue

                album_genres = _parse_genres(row.get('album_genres'))
                if not album_genres:
                    result.skipped += 1
                    continue

                db_data = build_library_tag_db_data(row, album_genres)
                filtered_genres = db_data.get('genres') or []
                if not filtered_genres:
                    # Every genre this album had was off-whitelist — nothing to write.
                    result.skipped += 1
                    continue

                file_tags = read_file_tags(resolved)
                if file_tags.get('error'):
                    result.errors += 1
                    continue

                diff = build_tag_diff(file_tags, db_data)
                genre_diff = next((d for d in diff if d['field'] == 'Genre'), None)
                if not genre_diff or not genre_diff['changed']:
                    result.skipped += 1
                    continue

                details = {
                    'track_id': row['id'],
                    'file_path': row['file_path'],
                    'resolved_path': resolved,
                    'current_genre': genre_diff['file_value'],
                    'new_genre': filtered_genres,
                    'artist_name': row['artist_name'],
                    'album_title': row['album_title'],
                    'title': row['title'],
                }

                if dry_run:
                    if context.create_finding:
                        inserted = context.create_finding(
                            job_id=self.job_id, finding_type=self.job_id, severity='info',
                            entity_type='track', entity_id=str(row['id']), file_path=row['file_path'],
                            title=f"Genre tag drift: {row['artist_name']} - {row['title']}",
                            description=(
                                f"File genre \"{genre_diff['file_value']}\" -> "
                                f"\"{', '.join(filtered_genres)}\""
                            ),
                            details=details)
                        if inserted:
                            result.findings_created += 1
                        else:
                            result.findings_skipped_dedup += 1
                else:
                    applied = fixer._fix_genre_tag_writer('track', str(row['id']), row['file_path'], details)
                    if applied.get('success'):
                        result.auto_fixed += 1
                    else:
                        result.errors += 1
                        logger.warning("Could not auto-write genre for track %s: %s",
                                       row['id'], applied.get('error'))

            except Exception as e:
                logger.debug("genre tag writer: track %s failed: %s", row.get('id'), e)
                result.errors += 1

            if context.sleep_or_stop(0.02):
                return result

        if context.update_progress:
            context.update_progress(len(tracks), len(tracks))
        if context.report_progress:
            context.report_progress(
                phase=(f'Done — {result.findings_created} track(s) need a genre write'
                       if dry_run else f'Done — {result.auto_fixed} track(s) re-tagged'),
                log_line=f'{result.scanned} checked, {result.skipped} skipped, {result.errors} error(s)',
                log_type='success')
        return result
