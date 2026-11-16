"""Mix music and looping ambience into the one PCM stream Discord accepts.

A voice connection plays a single :class:`discord.AudioSource`. ``MixedAudioSource``
reads 20 ms PCM frames from the current song and from each ambience layer, scales
them, and adds them with saturation so peaks do not wrap into clicks.

The audio thread is the only caller of :meth:`MixedAudioSource.read`. Control
changes take the same lock, but only long enough to swap source pointers; ffmpeg
is started and killed outside that lock so a layer change does not stall the
20 ms cadence.
"""
from __future__ import annotations

import logging
import tempfile
import threading
import time
import warnings
from pathlib import Path
from typing import Callable

import discord

with warnings.catch_warnings():
    warnings.simplefilter('ignore', DeprecationWarning)
    import audioop

log = logging.getLogger('jukebox.mix')

# discord.py's Opus encoder consumes exactly one 20 ms stereo s16le frame.
FRAME_SIZE = discord.opus.Encoder.FRAME_SIZE
SILENCE = bytes(FRAME_SIZE)
# If the asyncio side never stops a fully idle mixer, end the stream after 1 s
# of silence so the bot does not stay "speaking" forever.
IDLE_TIMEOUT_FRAMES = 50

MusicEnded = Callable[[int, float], None]
LayerFailed = Callable[[str], None]


def apply_volume(frame: bytes, volume: float) -> bytes:
    """Scale one s16le frame. Volume is clamped to 0..1."""
    if not frame or volume == 1.0:
        return frame
    if volume <= 0.0:
        return bytes(len(frame))
    return audioop.mul(frame, 2, min(volume, 1.0))


def mix_pcm(frames: list[bytes]) -> bytes:
    """Saturating mix of equal-length s16le frames. Empty input returns ``b''``."""
    mixed: bytes | None = None
    for frame in frames:
        if not frame:
            continue
        if mixed is None:
            mixed = frame
            continue
        if len(frame) < len(mixed):
            frame = frame + bytes(len(mixed) - len(frame))
        elif len(mixed) < len(frame):
            mixed = mixed + bytes(len(frame) - len(mixed))
        # audioop.add clips to the sample width instead of wrapping.
        mixed = audioop.add(mixed, frame, 2)
    return mixed if mixed is not None else b''


def _fit_frame(frame: bytes) -> bytes:
    if len(frame) == FRAME_SIZE:
        return frame
    if len(frame) > FRAME_SIZE:
        return frame[:FRAME_SIZE]
    return frame + bytes(FRAME_SIZE - len(frame))


def _safe_cleanup(source: discord.AudioSource | None) -> None:
    if source is None:
        return
    try:
        source.cleanup()
    except Exception:
        log.exception('Audio source cleanup failed')


def _concat_line(path: Path) -> str:
    text = path.resolve().as_posix().replace("'", r"'\''")
    return f"file '{text}'"


def _write_concat_list(paths: list[Path]) -> Path:
    handle = tempfile.NamedTemporaryFile(
        mode='w', suffix='.txt', prefix='gmah-ambience-', delete=False, encoding='utf-8'
    )
    try:
        handle.write('\n'.join(_concat_line(path) for path in paths))
        handle.write('\n')
    finally:
        handle.close()
    return Path(handle.name)


def open_music_source(path: Path, *, executable: str) -> discord.FFmpegPCMAudio:
    """One-shot decode of a soundtrack file to 48 kHz stereo PCM."""
    return discord.FFmpegPCMAudio(
        str(path),
        executable=executable,
        before_options='-nostdin',
        options='-vn -loglevel warning',
    )


class LoopingFFmpegPCM(discord.AudioSource):
    """Decode one or more files forever via ffmpeg ``-stream_loop -1``.

    Several files are concatenated in sorted order and that playlist loops.
    A seamless codec (WAV, FLAC, OGG) loops more cleanly than MP3, which can
    have a short seam where the encoder padding is.
    """

    def __init__(self, paths: list[Path], *, executable: str = 'ffmpeg'):
        if not paths:
            raise ValueError('No audio files to loop.')
        self.paths = [Path(path) for path in paths]
        self.executable = executable
        self._source: discord.FFmpegPCMAudio | None = None
        self._concat_path: Path | None = None
        self._stopped = False
        self._restarts = 0
        try:
            self._open()
        except Exception:
            self.cleanup()
            raise

    def _open(self) -> None:
        concat: Path | None = None
        try:
            if len(self.paths) == 1:
                target = str(self.paths[0])
                before = '-nostdin -stream_loop -1'
            else:
                concat = _write_concat_list(self.paths)
                target = str(concat)
                before = '-nostdin -stream_loop -1 -f concat -safe 0'
            source = discord.FFmpegPCMAudio(
                target,
                executable=self.executable,
                before_options=before,
                options='-vn -loglevel warning',
            )
        except Exception:
            if concat is not None:
                concat.unlink(missing_ok=True)
            raise
        previous = self._concat_path
        self._concat_path = concat
        self._source = source
        if previous is not None and previous != concat:
            try:
                previous.unlink(missing_ok=True)
            except OSError:
                log.warning('Could not delete old concat list %s', previous)

    def read(self) -> bytes:
        if self._stopped or self._source is None:
            return b''
        data = self._source.read()
        if data or self._stopped:
            return data
        error = getattr(self._source, '_current_error', None)
        self._restarts += 1
        if self._restarts > 2:
            log.error('Looping ambience failed after a restart: %s', error or 'EOF')
            return b''
        log.warning('Looping ambience stream ended (%s); restarting', error or 'EOF')
        _safe_cleanup(self._source)
        self._source = None
        try:
            self._open()
        except Exception:
            log.exception('Could not restart the ambience loop')
            return b''
        if self._source is None or self._stopped:
            return b''
        return self._source.read()

    def cleanup(self) -> None:
        self._stopped = True
        source = self._source
        self._source = None
        _safe_cleanup(source)
        path = self._concat_path
        self._concat_path = None
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                log.warning('Could not delete concat list %s', path)


class MixedAudioSource(discord.AudioSource):
    """One PCM source that is the sum of the song and any ambience layers."""

    def __init__(
        self,
        *,
        on_music_ended: MusicEnded | None = None,
        on_layer_failed: LayerFailed | None = None,
    ):
        self.on_music_ended = on_music_ended
        self.on_layer_failed = on_layer_failed
        self._lock = threading.Lock()
        self._music: discord.AudioSource | None = None
        self._music_generation = 0
        self._music_started = 0.0
        self._music_volume = 1.0
        self._ambience_volume = 0.30
        self._music_paused = False
        self._ambiences: dict[str, discord.AudioSource] = {}
        self.keep_music_alive = False
        self.closed = False
        self._idle_frames = 0
        self.diag_reads = 0
        self.diag_nonzero = 0
        self.diag_bytes = 0

    def set_music_volume(self, volume: float) -> None:
        with self._lock:
            self._music_volume = max(0.0, min(1.0, float(volume)))

    def set_ambience_volume(self, volume: float) -> None:
        with self._lock:
            self._ambience_volume = max(0.0, min(1.0, float(volume)))

    def set_music_paused(self, paused: bool) -> None:
        with self._lock:
            self._music_paused = bool(paused)

    def set_keep_music_alive(self, keep: bool) -> None:
        with self._lock:
            self.keep_music_alive = bool(keep)
            if keep:
                self._idle_frames = 0

    @property
    def music_paused(self) -> bool:
        with self._lock:
            return self._music_paused

    def has_music(self) -> bool:
        with self._lock:
            return self._music is not None

    def has_ambience(self) -> bool:
        with self._lock:
            return bool(self._ambiences)

    def layer_names(self) -> list[str]:
        with self._lock:
            return list(self._ambiences)

    def busy(self) -> bool:
        with self._lock:
            return self._busy_locked()

    def _busy_locked(self) -> bool:
        if self.closed:
            return False
        return self._music is not None or bool(self._ambiences) or self.keep_music_alive

    def add_layer(self, name: str, source: discord.AudioSource) -> str:
        with self._lock:
            if self.closed:
                return 'closed'
            if name in self._ambiences:
                return 'exists'
            self._ambiences[name] = source
            self._idle_frames = 0
            return 'ok'

    def pop_layer(self, name: str) -> discord.AudioSource | None:
        with self._lock:
            return self._ambiences.pop(name, None)

    def pop_layers(self) -> dict[str, discord.AudioSource]:
        with self._lock:
            layers = self._ambiences
            self._ambiences = {}
            return layers

    def replace_music(self, source: discord.AudioSource, generation: int) -> bool:
        with self._lock:
            if self.closed:
                return False
            old = self._music
            self._music = source
            self._music_generation = generation
            self._music_started = time.monotonic()
            self._music_paused = False
            self._idle_frames = 0
        _safe_cleanup(old)
        return True

    def clear_music(self) -> None:
        with self._lock:
            old = self._music
            self._music = None
            self._music_paused = False
        _safe_cleanup(old)

    def clear_all(self) -> None:
        with self._lock:
            sources = [self._music, *self._ambiences.values()]
            self._music = None
            self._ambiences = {}
            self._music_paused = False
            self.keep_music_alive = False
        for source in sources:
            _safe_cleanup(source)

    def read(self) -> bytes:
        ended: tuple[discord.AudioSource, int, float] | None = None
        failed: list[tuple[str, discord.AudioSource]] = []
        with self._lock:
            if self.closed:
                return b''
            frames: list[bytes] = []
            if self._music is not None and not self._music_paused:
                data = self._read_source(self._music, 'music')
                if not data:
                    ended = (self._music, self._music_generation, self._music_started)
                    self._music = None
                else:
                    frames.append(apply_volume(data, self._music_volume))
            for name, source in list(self._ambiences.items()):
                data = self._read_source(source, name)
                if not data:
                    failed.append((name, source))
                    self._ambiences.pop(name, None)
                    continue
                frames.append(apply_volume(data, self._ambience_volume))
            if frames:
                self._idle_frames = 0
                output = _fit_frame(mix_pcm(frames))
            elif self._busy_locked():
                # Between songs, or while a layer is still attached, keep the
                # voice player alive. Returning b'' would tear down ambience too.
                self._idle_frames = 0
                output = SILENCE
            else:
                self._idle_frames += 1
                if self._idle_frames > IDLE_TIMEOUT_FRAMES:
                    self.closed = True
                    output = b''
                else:
                    output = SILENCE
            if output:
                self._note_diag(output)
        if ended is not None:
            source, generation, started = ended
            _safe_cleanup(source)
            self._emit_music_ended(generation, started)
        for name, source in failed:
            _safe_cleanup(source)
            self._emit_layer_failed(name)
        return output

    def _read_source(self, source: discord.AudioSource, label: str) -> bytes:
        try:
            return source.read()
        except Exception:
            log.exception('Reading %s failed', label)
            return b''

    def _note_diag(self, output: bytes) -> None:
        self.diag_reads += 1
        self.diag_bytes += len(output)
        if any(output):
            self.diag_nonzero += 1
        if self.diag_reads in (1, 50, 250):
            log.info(
                'AUDIO DIAG PCM: frames=%d non_silent=%d bytes=%d frame_size=%d',
                self.diag_reads, self.diag_nonzero, self.diag_bytes, len(output),
            )

    def _emit_music_ended(self, generation: int, started_at: float) -> None:
        callback = self.on_music_ended
        if callback is None or self.closed:
            return
        try:
            callback(generation, started_at)
        except Exception:
            log.exception('Music-ended callback failed')

    def _emit_layer_failed(self, name: str) -> None:
        callback = self.on_layer_failed
        if callback is None or self.closed:
            return
        try:
            callback(name)
        except Exception:
            log.exception('Ambience-failed callback failed')

    def retire(self) -> None:
        """Drop every layer and make further reads end the voice player."""
        with self._lock:
            if self.closed:
                sources: list[discord.AudioSource | None] = []
            else:
                self.closed = True
                sources = [self._music, *self._ambiences.values()]
                self._music = None
                self._ambiences = {}
                self.keep_music_alive = False
        for source in sources:
            _safe_cleanup(source)

    def cleanup(self) -> None:
        self.retire()
