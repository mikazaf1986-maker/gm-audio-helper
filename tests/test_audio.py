"""Offline checks for the music/ambience mixer. No Discord connection."""
from __future__ import annotations

import array
import math
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

import discord

os.environ.setdefault('GMAH_DEBUG', '0')

import bot
from audio_mix import (
    FRAME_SIZE,
    IDLE_TIMEOUT_FRAMES,
    SILENCE,
    LoopingFFmpegPCM,
    MixedAudioSource,
    apply_volume,
    mix_pcm,
    open_music_source,
)

ROOT = Path(__file__).resolve().parents[1]
SAMPLE_RATE = 48000


def tone_frame(value: int) -> bytes:
    return array.array('h', [value] * (FRAME_SIZE // 2)).tobytes()


def samples_of(frames: list[bytes], channel: int = 0) -> list[int]:
    merged = b''.join(frames)
    data = array.array('h')
    data.frombytes(merged[: len(merged) // 2 * 2])
    return list(data[channel::2])


def goertzel_power(samples: list[int], sample_rate: int, freq: float) -> float:
    count = len(samples)
    if count == 0:
        return 0.0
    k = int(0.5 + (count * freq) / sample_rate)
    omega = 2.0 * math.pi * k / count
    coeff = 2.0 * math.cos(omega)
    s0 = s1 = s2 = 0.0
    for sample in samples:
        s0 = sample + coeff * s1 - s2
        s2 = s1
        s1 = s0
    return s1 * s1 + s2 * s2 - coeff * s1 * s2


def render_tone(path: Path, frequency: float, duration: float) -> None:
    subprocess.run(
        [
            'ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
            '-f', 'lavfi',
            '-i', f'sine=frequency={frequency}:duration={duration}:sample_rate={SAMPLE_RATE}',
            '-ac', '2',
            str(path),
        ],
        check=True,
    )


def read_frames(source, count: int) -> list[bytes]:
    frames = []
    for _ in range(count):
        frame = source.read()
        if not frame:
            break
        frames.append(frame)
    return frames


class MixMathTests(unittest.TestCase):
    def test_volume_scales_and_identity_is_exact(self):
        frame = tone_frame(10000)
        self.assertEqual(apply_volume(frame, 1.0), frame)
        halved = array.array('h')
        halved.frombytes(apply_volume(frame, 0.5))
        self.assertTrue(all(sample == 5000 for sample in halved))
        silenced = apply_volume(frame, 0.0)
        self.assertEqual(silenced, bytes(len(frame)))

    def test_saturating_add_does_not_wrap(self):
        loud = tone_frame(30000)
        mixed = array.array('h')
        mixed.frombytes(mix_pcm([loud, loud]))
        self.assertTrue(all(sample == 32767 for sample in mixed))
        quiet = tone_frame(-30000)
        mixed = array.array('h')
        mixed.frombytes(mix_pcm([quiet, quiet]))
        self.assertTrue(all(sample == -32768 for sample in mixed))
        stacked = array.array('h')
        stacked.frombytes(mix_pcm([tone_frame(1000), tone_frame(2000)]))
        self.assertTrue(all(sample == 3000 for sample in stacked))

    def test_mix_is_fast_enough_for_the_frame_budget(self):
        frames = [tone_frame(8000), tone_frame(4000), tone_frame(2000)]
        started = time.perf_counter()
        iterations = 300
        for _ in range(iterations):
            mix_pcm(frames)
        per_frame_ms = (time.perf_counter() - started) / iterations * 1000
        self.assertLess(per_frame_ms, 1.0, f'{per_frame_ms:.3f} ms per mix')


class MixerBehaviorTests(unittest.TestCase):
    def test_layers_are_independent(self):
        music_frame = tone_frame(1000)
        ambience_frame = tone_frame(2000)
        music = FakePCM([music_frame, music_frame, music_frame])
        rain = FakePCM([ambience_frame], loop=True)
        ended: list[int] = []
        mixer = MixedAudioSource(on_music_ended=lambda generation, started: ended.append(generation))
        self.addCleanup(mixer.cleanup)
        mixer.set_music_volume(1.0)
        mixer.set_ambience_volume(1.0)
        mixer.set_keep_music_alive(True)
        self.assertTrue(mixer.replace_music(music, 7))
        self.assertEqual(mixer.add_layer('rain', rain), 'ok')

        both = array.array('h')
        both.frombytes(mixer.read())
        self.assertTrue(all(sample == 3000 for sample in both))
        self.assertEqual(music.reads, 1)

        mixer.set_music_paused(True)
        paused = mixer.read()
        self.assertEqual(paused, ambience_frame)
        self.assertEqual(music.reads, 1, 'pausing music must not pull the next song frame')

        mixer.set_music_paused(False)
        self.assertIs(mixer.pop_layer('rain'), rain)
        rain.cleanup()
        music_only = mixer.read()
        self.assertEqual(music_only, music_frame)
        self.assertEqual(ended, [])

        # One frame left, then EOF. Ambience is gone; the song ending must not
        # look like "stop everything" while the playlist is still active.
        self.assertTrue(mixer.read())
        self.assertEqual(mixer.read(), SILENCE)
        self.assertEqual(ended, [7])
        self.assertEqual(mixer.read(), SILENCE)
        self.assertEqual(ended, [7])
        self.assertTrue(music.cleaned)
        self.assertEqual(music.reads, 4)

    def test_ambience_can_play_alone_and_idle_eventually_ends(self):
        wind = FakePCM([tone_frame(1500)], loop=True)
        mixer = MixedAudioSource()
        self.addCleanup(mixer.cleanup)
        mixer.set_ambience_volume(1.0)
        self.assertEqual(mixer.add_layer('wind', wind), 'ok')
        frame = mixer.read()
        self.assertEqual(frame, tone_frame(1500))
        self.assertIs(mixer.pop_layer('wind'), wind)
        wind.cleanup()
        for _ in range(IDLE_TIMEOUT_FRAMES):
            self.assertEqual(mixer.read(), SILENCE)
        self.assertEqual(mixer.read(), b'')
        self.assertTrue(mixer.closed)

    def test_keep_music_alive_does_not_end_the_stream(self):
        mixer = MixedAudioSource()
        self.addCleanup(mixer.cleanup)
        mixer.set_keep_music_alive(True)
        for _ in range(IDLE_TIMEOUT_FRAMES + 5):
            self.assertEqual(mixer.read(), SILENCE)
        self.assertFalse(mixer.closed)

    def test_retire_is_idempotent(self):
        mixer = MixedAudioSource()
        mixer.add_layer('fire', FakePCM([tone_frame(1)], loop=True))
        mixer.retire()
        mixer.retire()
        mixer.cleanup()
        self.assertEqual(mixer.read(), b'')


class FakePCM(discord.AudioSource):
    def __init__(self, frames: list[bytes], loop: bool = False):
        self.frames = list(frames)
        self.loop = loop
        self.index = 0
        self.reads = 0
        self.cleaned = False

    def read(self) -> bytes:
        self.reads += 1
        if self.index >= len(self.frames):
            if self.loop and self.frames:
                self.index = 0
            else:
                return b''
        frame = self.frames[self.index]
        self.index += 1
        return frame

    def cleanup(self) -> None:
        self.cleaned = True


class FfmpegMixTests(unittest.TestCase):
    def test_loop_and_mix_keep_up_with_realtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            music = folder / 'song.wav'
            rain = folder / 'steady rain.wav'
            thunder = folder / 'thunder.wav'
            render_tone(music, 440, 0.35)
            render_tone(rain, 1000, 0.30)
            render_tone(thunder, 1800, 0.30)
            song = open_music_source(music, executable='ffmpeg')
            loops = [
                LoopingFFmpegPCM([rain], executable='ffmpeg'),
                LoopingFFmpegPCM([thunder], executable='ffmpeg'),
            ]
            ended: list[float] = []
            mixer = MixedAudioSource(on_music_ended=lambda generation, started: ended.append(started))
            self.addCleanup(mixer.cleanup)
            mixer.set_keep_music_alive(True)
            mixer.replace_music(song, 1)
            mixer.add_layer('rain', loops[0])
            mixer.add_layer('thunder', loops[1])
            try:
                for _ in range(3):
                    primer = mixer.read()
                    self.assertEqual(len(primer), FRAME_SIZE)
                frames = 150  # 3 seconds
                started = time.perf_counter()
                captured = []
                for _ in range(frames):
                    frame = mixer.read()
                    self.assertEqual(len(frame), FRAME_SIZE)
                    captured.append(frame)
                elapsed = time.perf_counter() - started
                per_frame_ms = elapsed / frames * 1000
                self.assertLess(per_frame_ms, 10.0, f'{per_frame_ms:.2f} ms/frame over {frames} frames')
                self.assertTrue(ended, 'the short music file should finish while ambience continues')
                tail = samples_of(captured[-25:])
                rain_power = goertzel_power(tail, SAMPLE_RATE, 1000)
                thunder_power = goertzel_power(tail, SAMPLE_RATE, 1800)
                leftover_music = goertzel_power(tail, SAMPLE_RATE, 440)
                self.assertGreater(rain_power, leftover_music * 3, (rain_power, thunder_power, leftover_music))
                self.assertGreater(thunder_power, leftover_music * 3, (rain_power, thunder_power, leftover_music))
            finally:
                mixer.cleanup()
                for source in loops:
                    source.cleanup()
                song.cleanup()

    def test_short_file_loops_without_a_silent_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'loop.wav'
            render_tone(path, 440, 0.25)
            source = LoopingFFmpegPCM([path], executable='ffmpeg')
            self.addCleanup(source.cleanup)
            silent = 0
            for _ in range(100):
                frame = source.read()
                self.assertEqual(len(frame), FRAME_SIZE)
                if not any(frame):
                    silent += 1
            self.assertEqual(silent, 0)

    def test_playlist_loop_includes_every_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            low = folder / 'low tone.wav'
            high = folder / 'high tone.wav'
            render_tone(low, 330, 0.4)
            render_tone(high, 990, 0.4)
            source = LoopingFFmpegPCM([low, high], executable='ffmpeg')
            self.addCleanup(source.cleanup)
            frames = read_frames(source, 75)  # 1.5 s covers both clips
            self.assertEqual(len(frames), 75)
            heard = samples_of(frames)
            low_power = goertzel_power(heard, SAMPLE_RATE, 330)
            high_power = goertzel_power(heard, SAMPLE_RATE, 990)
            other = goertzel_power(heard, SAMPLE_RATE, 2000)
            self.assertGreater(low_power, other * 5, (low_power, high_power, other))
            self.assertGreater(high_power, other * 5, (low_power, high_power, other))


class LibraryTests(unittest.TestCase):
    def test_ambience_is_not_a_music_category(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'Tavern').mkdir()
            (root / 'Tavern' / 'song.mp3').write_bytes(b'not-audio')
            (root / 'ambience' / 'rain').mkdir(parents=True)
            (root / 'ambience' / 'rain' / 'steady.ogg').write_bytes(b'x')
            (root / 'ambience' / 'thunder').mkdir()
            (root / 'ambience' / 'thunder' / '.add_your_music_here').write_bytes(b'')
            music = bot.MusicLibrary(root)
            beds = bot.AmbienceLibrary(root / 'ambience')
            self.assertEqual(list(music.groups), ['Tavern'])
            self.assertEqual(list(beds.groups), ['rain'])
            self.assertEqual(beds.empty, ['thunder'])
            self.assertEqual(beds.resolve('RAIN'), 'rain')
            self.assertIsNone(beds.resolve('nope'))
            self.assertEqual(beds.resolve_empty('Thunder'), 'thunder')

    def test_repo_placeholders(self):
        folded = {name.casefold() for name in bot.bot.library.groups}
        self.assertNotIn('ambience', folded)
        for name in ('rain', 'thunder', 'fireplace', 'wind', 'forest', 'crowd'):
            self.assertIn(name, bot.bot.ambience.empty)
            self.assertNotIn(name, bot.bot.ambience.groups)

    def test_gitignore_tracks_placeholders_only(self):
        tracked = [
            'music/Tavern/.add_your_music_here',
            'music/ambience/rain/.add_your_music_here',
            'music/ambience/fireplace/.add_your_music_here',
            'music/ambience/crowd/nested/.add_your_music_here',
        ]
        ignored = [
            'music/Tavern/song.mp3',
            'music/Tavern/nested/song.wav',
            'music/ambience/rain/rain.mp3',
            'music/ambience/rain/nested/storm.ogg',
            'music/ambience/wind/gust.flac',
        ]
        for path in tracked:
            self.assertFalse(self._ignored(path), path)
        for path in ignored:
            self.assertTrue(self._ignored(path), path)

    def _ignored(self, path: str) -> bool:
        result = subprocess.run(
            ['git', 'check-ignore', '-q', '--no-index', path],
            cwd=ROOT,
            check=False,
        )
        if result.returncode not in (0, 1):
            raise AssertionError(f'git check-ignore failed for {path}: {result.returncode}')
        return result.returncode == 0


class SyntaxTests(unittest.TestCase):
    def test_modules_import(self):
        self.assertEqual(FRAME_SIZE, 3840)
        self.assertTrue(hasattr(bot, 'ambience'))
        names = {command.qualified_name for command in bot.bot.tree.walk_commands()}
        self.assertTrue({'panel', 'rescan', 'join', 'leave'} <= names)
        self.assertTrue(
            {'ambience add', 'ambience remove', 'ambience list', 'ambience stop', 'ambience volume'} <= names
        )


if __name__ == '__main__':
    unittest.main()
