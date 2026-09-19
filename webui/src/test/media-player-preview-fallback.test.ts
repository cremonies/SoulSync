import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { extractFunction } from './vanilla-extract';

/**
 * When auto-download is off, a queued track with no file_path used to throw
 * "Auto-download is disabled for missing queue tracks" and get skipped —
 * even for a track a stream preview could have played instantly. playQueueItem
 * now tries a short preview first (npFetchStreamPreview, the same
 * /api/enhanced-search/stream-track call playTrackByMetadata's fallback uses)
 * before giving up. These tests exercise the REAL playQueueItem body so a
 * refactor that quietly drops the preview branch fails loudly.
 */

const source = readFileSync(resolve(process.cwd(), 'static/media-player.js'), 'utf8');
const functions = ['playQueueItem', 'npFetchStreamPreview']
  .map((name) => extractFunction(name, source))
  .join('\n');

function playerHarness(options: {
  autoDownload?: boolean;
  previewResult?: Record<string, unknown> | null;
  previewFetchFails?: boolean;
} = {}) {
  const audio = Object.assign(new EventTarget(), { paused: true, readyState: 3 });
  const toast = vi.fn();
  const startStream = vi.fn(async () => {});
  const npEnsureQueueTrackReady = vi.fn(async () => {
    throw new Error('download failed');
  });

  const fetch = vi.fn(async (url: string) => {
    if (String(url).includes('/api/enhanced-search/stream-track')) {
      if (options.previewFetchFails) throw new Error('network down');
      return {
        json: async () =>
          options.previewResult === null
            ? { success: false }
            : { success: true, result: options.previewResult ?? { result_type: 'preview_url', preview_url: 'https://x/y.mp3' } },
      };
    }
    return { json: async () => ({ success: true }) };
  });

  const globals = source.slice(
    source.indexOf('let npLoadingQueueItem = false;'),
    source.indexOf('\n};', source.indexOf('window.cancelPendingPlayback =')) + 3,
  );

  const bridge = new Function(
    'audioPlayer',
    'fetch',
    'showToast',
    'startStream',
    'npEnsureQueueTrackReady',
    'window',
    'document',
    `
    ${globals}
    let npQueue = [], npQueueIndex = 0, npRepeatMode = 'off', npRadioMode = false;
    let npAutoDownloadQueue = ${options.autoDownload ? 'true' : 'false'};
    const npCancelCrossfade = () => {}, setTrackInfo = () => {}, showLoadingAnimation = () => {}, hideLoadingAnimation = () => {}, renderNpQueue = () => {}, updateNpPrevNextButtons = () => {}, setPlayingState = () => {}, clearTrack = () => {}, npSetPlayContext = () => {}, npScheduleQueuePrefetch = () => {};
    const clearQueue = () => { npQueue = []; };
    const stopStream = async () => {};
    const startAudioPlayback = async () => ({ status: 'played' });
    ${functions}
    return { playQueueItem, setQueue: (tracks) => { npQueue = tracks; } };
  `,
  )(audio, fetch, toast, startStream, npEnsureQueueTrackReady, {}, document) as {
    playQueueItem: (index: number) => Promise<{ status: string; error?: string }>;
    setQueue: (tracks: unknown[]) => void;
  };
  return { bridge, audio, toast, fetch, startStream, npEnsureQueueTrackReady };
}

afterEach(() => vi.useRealTimers());

describe('playQueueItem preview fallback (auto-download off)', () => {
  const missingTrack = { title: 'Money', artist: 'Pink Floyd', album: 'The Dark Side of the Moon', file_path: '' };

  it('plays a stream preview instead of throwing when auto-download is off', async () => {
    const h = playerHarness({ autoDownload: false });
    h.bridge.setQueue([missingTrack]);
    const result = await h.bridge.playQueueItem(0);
    expect(result.status).toBe('preview');
    expect(h.npEnsureQueueTrackReady).not.toHaveBeenCalled();
    expect(h.startStream).toHaveBeenCalledWith(
      expect.objectContaining({ result_type: 'preview_url', preview_url: 'https://x/y.mp3' }),
    );
  });

  it('still fails (and would skip) when no preview is found either', async () => {
    const h = playerHarness({ autoDownload: false, previewResult: null });
    h.bridge.setQueue([missingTrack]);
    const result = await h.bridge.playQueueItem(0);
    expect(result.status).toBe('failed');
    expect(result.error).toBe('Not in your library and no preview available');
    expect(h.startStream).not.toHaveBeenCalled();
  });

  it('fails gracefully when the preview lookup itself errors', async () => {
    const h = playerHarness({ autoDownload: false, previewFetchFails: true });
    h.bridge.setQueue([missingTrack]);
    const result = await h.bridge.playQueueItem(0);
    expect(result.status).toBe('failed');
    expect(h.startStream).not.toHaveBeenCalled();
  });

  it('still uses the full download flow when auto-download is on', async () => {
    const h = playerHarness({ autoDownload: true });
    h.bridge.setQueue([missingTrack]);
    const result = await h.bridge.playQueueItem(0);
    expect(h.npEnsureQueueTrackReady).toHaveBeenCalledOnce();
    expect(h.startStream).not.toHaveBeenCalled();
    expect(result.status).toBe('failed');
  });
});
