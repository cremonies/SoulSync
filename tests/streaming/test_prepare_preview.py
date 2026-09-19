"""Tests for the Deezer-preview fast path in core/streaming/prepare.py.

Only the new direct-URL branch is covered here — the pre-existing
download-orchestrator flow (Soulseek/YouTube/etc) is untouched by this
change and stays exercised by whatever already covers it elsewhere.
"""

from __future__ import annotations

import threading
from unittest.mock import AsyncMock, Mock, patch

import pytest

from core.streaming.prepare import PrepareStreamDeps, fetch_preview_url_to_stream_folder, prepare_stream_task


class _FakeResponse:
    def __init__(self, content=b'fake-mp3-bytes', status_code=200):
        self.content = content
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _make_deps(tmp_path, config=None):
    state = {}

    def get_state():
        return state

    def set_state(value):
        nonlocal state
        state = value

    return PrepareStreamDeps(
        config_manager=config or Mock(get=Mock(return_value='./downloads')),
        download_orchestrator=Mock(),
        stream_lock=threading.Lock(),
        project_root=str(tmp_path),
        docker_resolve_path=lambda p: p,
        find_streaming_download_in_all_downloads=Mock(),
        find_downloaded_file=Mock(),
        extract_filename=lambda p: p,
        cleanup_empty_directories=Mock(),
        _get_stream_state=get_state,
        _set_stream_state=set_state,
    ), lambda: state


# ---------------------------------------------------------------------------
# fetch_preview_url_to_stream_folder
# ---------------------------------------------------------------------------

def test_fetch_preview_saves_bytes_to_stream_folder(tmp_path):
    with patch('requests.get', return_value=_FakeResponse(b'hello')) as mock_get:
        path = fetch_preview_url_to_stream_folder(
            'https://cdn.deezer.com/preview/abc.mp3', 'Artist - Title.mp3', str(tmp_path)
        )
    mock_get.assert_called_once_with('https://cdn.deezer.com/preview/abc.mp3', timeout=15)
    assert path == str(tmp_path / 'Artist - Title.mp3')
    with open(path, 'rb') as f:
        assert f.read() == b'hello'


def test_fetch_preview_strips_illegal_filename_characters(tmp_path):
    with patch('requests.get', return_value=_FakeResponse()):
        path = fetch_preview_url_to_stream_folder(
            'https://cdn.deezer.com/preview/abc.mp3', 'AC/DC: Thunder?.mp3', str(tmp_path)
        )
    assert '/' not in path.rsplit('/', 1)[-1] if '\\' not in path else True
    assert ':' not in path.rsplit('\\', 1)[-1].rsplit('/', 1)[-1]
    assert '?' not in path


def test_fetch_preview_appends_mp3_extension_when_missing(tmp_path):
    with patch('requests.get', return_value=_FakeResponse()):
        path = fetch_preview_url_to_stream_folder(
            'https://cdn.deezer.com/preview/abc.mp3', 'preview', str(tmp_path)
        )
    assert path.endswith('.mp3')


def test_fetch_preview_raises_on_http_error(tmp_path):
    with patch('requests.get', return_value=_FakeResponse(status_code=404)):
        with pytest.raises(RuntimeError):
            fetch_preview_url_to_stream_folder('https://x/y.mp3', 'x.mp3', str(tmp_path))


# ---------------------------------------------------------------------------
# prepare_stream_task — the preview_url short circuit
# ---------------------------------------------------------------------------

def test_preview_task_marks_ready_and_skips_download_orchestrator(tmp_path):
    deps, get_state = _make_deps(tmp_path)
    track_data = {
        'result_type': 'preview_url',
        'preview_url': 'https://cdn.deezer.com/preview/abc.mp3',
        'filename': 'Pink Floyd - Money (preview).mp3',
    }

    with patch('requests.get', return_value=_FakeResponse(b'preview-bytes')):
        prepare_stream_task(track_data, deps)

    state = get_state()
    assert state['status'] == 'ready'
    assert state['progress'] == 100
    assert state['file_path'].endswith('Pink Floyd - Money (preview).mp3')
    deps.download_orchestrator.download.assert_not_called()


def test_preview_task_sets_error_state_on_fetch_failure(tmp_path):
    deps, get_state = _make_deps(tmp_path)
    track_data = {
        'result_type': 'preview_url',
        'preview_url': 'https://cdn.deezer.com/preview/dead.mp3',
        'filename': 'dead.mp3',
    }

    with patch('requests.get', side_effect=ConnectionError("no route")):
        prepare_stream_task(track_data, deps)

    state = get_state()
    assert state['status'] == 'error'
    assert 'no route' in state['error_message']


def test_preview_task_without_preview_url_falls_through_to_normal_flow(tmp_path):
    # result_type is 'preview_url' but no preview_url present -> must NOT
    # take the fast path (falls through to the pre-existing download flow,
    # which will fail fast here since download() returns a Mock that isn't
    # a usable download id — asserting only that the fast path was skipped).
    deps, get_state = _make_deps(tmp_path)
    deps.download_orchestrator.download = AsyncMock(return_value=None)
    track_data = {'result_type': 'preview_url', 'filename': 'x.mp3'}

    with patch('requests.get') as mock_get:
        prepare_stream_task(track_data, deps)
        mock_get.assert_not_called()

    state = get_state()
    assert state['status'] == 'error'
    assert 'uploader may be offline' in state['error_message']
