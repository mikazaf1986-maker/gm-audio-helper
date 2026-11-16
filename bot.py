"""Strahd's Jukebox: a local-file Discord soundtrack bot."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from collections import deque
from pathlib import Path

import discord

# Windows ships a private Opus build inside discord.py. Other platforms use the
# system library (the unit tests run there). davey stays pinned in requirements.
if os.name == 'nt':
    opus_path = os.path.join(
        os.path.dirname(discord.__file__),
        'bin',
        'libopus-0.x64.dll',
    )
    discord.opus.load_opus(opus_path)

from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from audio_mix import LoopingFFmpegPCM, MixedAudioSource, open_music_source

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / '.env', override=True)
TOKEN = os.getenv('DISCORD_TOKEN', '').strip()
MUSIC = ROOT / 'music'
STATE_FILE = ROOT / 'settings.json'
FORMATS = {'.mp3', '.ogg', '.wav', '.flac', '.m4a', '.opus'}
FFMPEG = os.getenv('FFMPEG_PATH', 'ffmpeg').strip() or 'ffmpeg'
MAX_OPTIONS = 25
MAX_AMBIENCE_LAYERS = 6
DEFAULT_VOLUME = 0.35
DEFAULT_AMBIENCE_VOLUME = 0.30
logging.basicConfig(
    level=logging.DEBUG if os.getenv('GMAH_DEBUG', '1') == '1' else logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
)
log = logging.getLogger('jukebox')


def load_settings() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError, OSError):
        return {}


def save_settings(data: dict) -> None:
    tmp = STATE_FILE.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, indent=2), encoding='utf-8')
    tmp.replace(STATE_FILE)


def clamp_volume(value: object, default: float) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        number = default
    return round(max(0.0, min(1.0, number)), 2)


def audio_files_in(folder: Path) -> list[Path]:
    tracks = [p for p in folder.rglob('*') if p.is_file() and p.suffix.lower() in FORMATS]
    tracks.sort(key=lambda p: str(p).casefold())
    return tracks


def match_name(name: str, options: list[str] | dict[str, object]) -> str | None:
    key = name.strip().casefold()
    if not key:
        return None
    for existing in options:
        if existing.casefold() == key:
            return existing
    return None


def find_ambience_root(music_root: Path) -> Path:
    if music_root.is_dir():
        for child in music_root.iterdir():
            if child.is_dir() and child.name.casefold() == 'ambience':
                return child
    return music_root / 'ambience'


class MusicLibrary:
    def __init__(self, root: Path | None = None):
        self.root = root or MUSIC
        self.groups: dict[str, list[Path]] = {}
        self.scan()

    def scan(self):
        self.root.mkdir(exist_ok=True)
        self.groups = {}
        for folder in sorted(self.root.iterdir(), key=lambda p: p.name.casefold()):
            if not folder.is_dir() or folder.name.startswith('.'):
                continue
            # Reserved for looping beds. It is not a soundtrack category.
            if folder.name.casefold() == 'ambience':
                continue
            tracks = audio_files_in(folder)
            if tracks:
                self.groups[folder.name] = tracks


class AmbienceLibrary:
    """Looping beds in ``music/ambience/<name>/``."""

    def __init__(self, root: Path | None = None, music_root: Path | None = None):
        self.root = root if root is not None else find_ambience_root(music_root or MUSIC)
        self.groups: dict[str, list[Path]] = {}
        self.empty: list[str] = []
        self.scan()

    def scan(self):
        self.root.mkdir(parents=True, exist_ok=True)
        self.groups = {}
        self.empty = []
        for folder in sorted(self.root.iterdir(), key=lambda p: p.name.casefold()):
            if not folder.is_dir() or folder.name.startswith('.'):
                continue
            tracks = audio_files_in(folder)
            if tracks:
                self.groups[folder.name] = tracks
            else:
                self.empty.append(folder.name)

    def resolve(self, name: str) -> str | None:
        return match_name(name, self.groups)

    def resolve_empty(self, name: str) -> str | None:
        return match_name(name, self.empty)


def install_audio_packet_diagnostics(voice):
    """Instrument packet submission without changing packet contents."""
    if getattr(voice, '_gmah_diag_installed', False):
        return
    original_send = voice.send_audio_packet
    voice._gmah_diag_packets = 0
    voice._gmah_diag_failures = 0

    def diagnostic_send(data, *, encode=True):
        try:
            result = original_send(data, encode=encode)
            voice._gmah_diag_packets += 1
            if voice._gmah_diag_packets in (1, 50, 250):
                log.info(
                    'AUDIO DIAG RTP: packets_sent=%d input_bytes=%d encode=%s',
                    voice._gmah_diag_packets, len(data), encode,
                )
            return result
        except Exception:
            voice._gmah_diag_failures += 1
            log.exception('AUDIO DIAG RTP: packet send/encode FAILED')
            raise

    voice.send_audio_packet = diagnostic_send
    voice._gmah_diag_installed = True


class GuildPlayer:
    def __init__(self, bot: 'Jukebox', guild_id: int):
        self.bot = bot
        self.guild_id = guild_id
        self.voice: discord.VoiceClient | None = None
        self.category: str | None = None
        self.track: Path | None = None
        stored = bot.settings.get(str(guild_id), {})
        if not isinstance(stored, dict):
            stored = {}
        self.volume = clamp_volume(stored.get('volume', DEFAULT_VOLUME), DEFAULT_VOLUME)
        self.ambience_volume = clamp_volume(
            stored.get('ambience_volume', DEFAULT_AMBIENCE_VOLUME), DEFAULT_AMBIENCE_VOLUME
        )
        self.mixer: MixedAudioSource | None = None
        self.music_paused = False
        self.remaining: deque[Path] = deque()
        self.generation = 0
        self.lock = asyncio.Lock()
        self.started_at = 0.0
        self.failed_starts = 0

    def active_ambience(self) -> list[str]:
        if self.mixer is None or self.mixer.closed:
            return []
        return self.mixer.layer_names()

    def _guild_settings(self) -> dict:
        entry = self.bot.settings.setdefault(str(self.guild_id), {})
        if not isinstance(entry, dict):
            entry = {}
            self.bot.settings[str(self.guild_id)] = entry
        return entry

    def _next_track(self) -> Path | None:
        tracks = self.bot.library.groups.get(self.category or '', [])
        if not tracks:
            return None
        # Discard queued files removed by a library rescan.
        self.remaining = deque(p for p in self.remaining if p in tracks)
        if not self.remaining:
            shuffled = list(tracks)
            random.shuffle(shuffled)
            if len(shuffled) > 1 and self.track == shuffled[0]:
                shuffled.append(shuffled.pop(0))
            self.remaining.extend(shuffled)
        return self.remaining.popleft()

    def _ensure_mixer(self) -> MixedAudioSource:
        if self.mixer is None or self.mixer.closed:
            self.mixer = MixedAudioSource(
                on_music_ended=self._schedule_music_ended,
                on_layer_failed=self._schedule_layer_failed,
            )
            self.mixer.set_music_volume(self.volume)
            self.mixer.set_ambience_volume(self.ambience_volume)
            self.mixer.set_music_paused(self.music_paused)
        return self.mixer

    def _player_running(self) -> bool:
        voice = self.voice
        return bool(voice and (voice.is_playing() or voice.is_paused()))

    def _sync_transport(self) -> None:
        """Keep the single voice player aligned with whichever layers are live."""
        voice = self.voice
        mixer = self.mixer
        if voice is None or not voice.is_connected() or mixer is None or mixer.closed:
            return
        # Pausing music must not pause rain. Only pause the socket when the
        # song is the only thing that would have been heard.
        if self.music_paused and mixer.has_music() and not mixer.has_ambience():
            if voice.is_playing():
                voice.pause()
            return
        if voice.is_paused():
            voice.resume()
            return
        if voice.is_playing():
            if voice.source is mixer:
                return
            # The previous mix is ending itself. Drop it before starting this one.
            voice.stop()
        install_audio_packet_diagnostics(voice)
        try:
            voice.play(mixer, after=self._player_after)
        except discord.ClientException as exc:
            if 'Already playing' not in str(exc):
                raise
            voice.stop()
            voice.play(mixer, after=self._player_after)

    def _shutdown_player(self) -> None:
        mixer = self.mixer
        self.mixer = None
        if mixer is not None:
            mixer.retire()
        if self._player_running() and self.voice is not None:
            self.voice.stop()

    def _shutdown_if_idle(self) -> None:
        if self.mixer is not None and not self.mixer.closed and self.mixer.busy():
            self._sync_transport()
            return
        self._shutdown_player()

    def _arm_diagnostics(self, source: MixedAudioSource) -> None:
        voice = self.voice
        if voice is None:
            return
        install_audio_packet_diagnostics(voice)
        packet_start = voice._gmah_diag_packets

        async def diagnostic_snapshot():
            await asyncio.sleep(8)
            current = self.voice
            has_packets = current is not None and hasattr(current, '_gmah_diag_packets')
            log.info(
                'AUDIO DIAG 8s: voice_connected=%s playing=%s PCM_frames=%d PCM_non_silent=%d RTP_packets=%d RTP_failures=%d opus_loaded=%s ambience=%s',
                current.is_connected() if current else False,
                current.is_playing() if current else False,
                source.diag_reads,
                source.diag_nonzero,
                (current._gmah_diag_packets - packet_start) if has_packets else 0,
                current._gmah_diag_failures if has_packets else 0,
                discord.opus.is_loaded(),
                ','.join(source.layer_names()) or '-',
            )

        try:
            self.bot.loop.create_task(diagnostic_snapshot())
        except RuntimeError:
            log.debug('No running loop for the audio diagnostic', exc_info=True)

    def _schedule_music_ended(self, generation: int, started_at: float) -> None:
        elapsed = max(0.0, time.monotonic() - started_at)
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._after_track(generation, elapsed), self.bot.loop
            )
        except Exception:
            log.exception('Could not schedule the next track')
            return
        future.add_done_callback(
            lambda f: log.error('Track callback failed: %r', f.exception())
            if not f.cancelled() and f.exception() else None
        )

    def _schedule_layer_failed(self, name: str) -> None:
        try:
            future = asyncio.run_coroutine_threadsafe(self._on_layer_failed(name), self.bot.loop)
        except Exception:
            log.exception('Could not schedule ambience failure handling')
            return
        future.add_done_callback(
            lambda f: log.error('Ambience callback failed: %r', f.exception())
            if not f.cancelled() and f.exception() else None
        )

    def _player_after(self, error: BaseException | None) -> None:
        if error:
            log.error('Voice player stopped with an error: %r', error)
        try:
            future = asyncio.run_coroutine_threadsafe(self._on_player_stopped(), self.bot.loop)
        except Exception:
            log.exception('Could not schedule player shutdown')
            return
        future.add_done_callback(
            lambda f: log.error('Player shutdown callback failed: %r', f.exception())
            if not f.cancelled() and f.exception() else None
        )

    async def _on_player_stopped(self) -> None:
        async with self.lock:
            if self.mixer is not None and self.mixer.closed and not self._player_running():
                self.mixer = None

    async def _on_layer_failed(self, name: str) -> None:
        async with self.lock:
            log.error('Ambience layer %s ended unexpectedly. Check that file and ffmpeg.', name)
            self._shutdown_if_idle()

    async def _after_track(self, generation: int, elapsed: float) -> None:
        async with self.lock:
            if generation != self.generation or not self.category:
                return
            log.info(
                'Audio ended: %s after %.1fs; error=%r',
                self.track.name if self.track else '?', elapsed, None,
            )
            if self.mixer is None or self.mixer.closed:
                return
            if elapsed < 3.0:
                self.failed_starts += 1
                log.warning(
                    'Track ended unusually quickly (%.1fs). Consecutive failures: %d',
                    elapsed, self.failed_starts,
                )
                if self.failed_starts >= 2:
                    log.error('Stopping playback to avoid an infinite retry loop. Check audio and voice logs.')
                    self.category = None
                    self.track = None
                    self.mixer.set_keep_music_alive(False)
                    self.mixer.clear_music()
                    self._shutdown_if_idle()
                    return
                should_wait = True
            else:
                self.failed_starts = 0
                should_wait = False
        if should_wait:
            await asyncio.sleep(1)
        async with self.lock:
            if generation != self.generation or not self.category:
                return
            self._play_next()

    def _play_next(self):
        if not self.voice or not self.voice.is_connected():
            log.warning('Cannot start audio: voice client disconnected (guild %s)', self.guild_id)
            return
        track = self._next_track()
        if track is None:
            self.track = None
            if self.mixer is not None and not self.mixer.closed:
                self.mixer.set_keep_music_alive(False)
                self.mixer.clear_music()
            self._shutdown_if_idle()
            return
        self.track = track
        self.generation += 1
        generation = self.generation
        self.music_paused = False
        self.started_at = time.monotonic()
        try:
            source = open_music_source(track, executable=FFMPEG)
        except Exception:
            log.exception('Could not start track %s', track)
            self.track = None
            self.category = None
            if self.mixer is not None and not self.mixer.closed:
                self.mixer.set_keep_music_alive(False)
            self._shutdown_if_idle()
            return
        mixer = self._ensure_mixer()
        mixer.set_keep_music_alive(True)
        mixer.set_music_volume(self.volume)
        if not mixer.replace_music(source, generation):
            mixer = self._ensure_mixer()
            mixer.set_keep_music_alive(True)
            mixer.set_music_volume(self.volume)
            if not mixer.replace_music(source, generation):
                source.cleanup()
                log.error('Could not attach track %s', track.name)
                self.track = None
                return
        log.info(
            'Starting audio: %s (guild=%s, channel=%s, volume=%.2f)',
            track.name, self.guild_id, self.voice.channel, self.volume,
        )
        try:
            self._sync_transport()
        except (discord.DiscordException, OSError):
            log.exception('Could not start voice playback for %s', track.name)
            if self.mixer is not None and not self.mixer.closed:
                self.mixer.set_keep_music_alive(False)
                self.mixer.clear_music()
            self.track = None
            self.category = None
            self._shutdown_if_idle()
            return
        self._arm_diagnostics(mixer)

    async def choose(self, category: str):
        async with self.lock:
            self.category = category
            self.remaining.clear()
            self.failed_starts = 0
            self.generation += 1
            self.music_paused = False
            self._play_next()

    async def skip(self):
        async with self.lock:
            if not self.category:
                return False
            self.generation += 1
            self.music_paused = False
            self.failed_starts = 0
            self._play_next()
            return True

    async def stop(self):
        """Stop the soundtrack only. Ambience keeps looping."""
        async with self.lock:
            self.generation += 1
            self.category = None
            self.track = None
            self.remaining.clear()
            self.music_paused = False
            self.failed_starts = 0
            if self.mixer is not None and not self.mixer.closed:
                self.mixer.set_keep_music_alive(False)
                self.mixer.clear_music()
            self._shutdown_if_idle()

    async def toggle_pause(self) -> str:
        async with self.lock:
            has_music = self.track is not None or (
                self.mixer is not None and not self.mixer.closed and self.mixer.has_music()
            )
            if not has_music:
                if self.active_ambience():
                    return 'No music is playing. Ambience keeps looping — use `/ambience stop` to stop it.'
                return 'Nothing is playing.'
            self.music_paused = not self.music_paused
            if self.mixer is not None and not self.mixer.closed:
                self.mixer.set_music_paused(self.music_paused)
            self._sync_transport()
            if not self.music_paused:
                return '▶️ Resumed.'
            if self.active_ambience():
                return '⏸ Music paused. Ambience is still playing.'
            return '⏸ Paused.'

    async def add_ambience(self, name: str) -> str:
        paths = list(self.bot.ambience.groups.get(name, []))
        if not paths:
            raise RuntimeError(f'No audio files for {name}.')
        async with self.lock:
            active = self.active_ambience()
            if name in active:
                return 'already'
            if len(active) >= MAX_AMBIENCE_LAYERS:
                return 'full'
        source = LoopingFFmpegPCM(paths, executable=FFMPEG)
        attached = False
        try:
            async with self.lock:
                active = self.active_ambience()
                if name in active:
                    return 'already'
                if len(active) >= MAX_AMBIENCE_LAYERS:
                    return 'full'
                mixer = self._ensure_mixer()
                added = mixer.add_layer(name, source)
                if added == 'closed':
                    mixer = self._ensure_mixer()
                    added = mixer.add_layer(name, source)
                if added == 'exists':
                    return 'already'
                if added != 'ok':
                    raise RuntimeError('Could not start ambience.')
                attached = True
                mixer.set_ambience_volume(self.ambience_volume)
                try:
                    self._sync_transport()
                except (discord.DiscordException, OSError):
                    mixer.pop_layer(name)
                    attached = False
                    raise
            self._arm_diagnostics(mixer)
            log.info(
                'Ambience layer started: %s (%d file(s), volume=%.2f)',
                name, len(paths), self.ambience_volume,
            )
            return 'ok'
        finally:
            if not attached:
                source.cleanup()

    async def remove_ambience(self, name: str) -> bool:
        async with self.lock:
            if self.mixer is None or self.mixer.closed:
                return False
            source = self.mixer.pop_layer(name)
            if source is None:
                return False
            source.cleanup()
            self._sync_transport()
            self._shutdown_if_idle()
            log.info('Ambience layer removed: %s', name)
            return True

    async def stop_ambience(self) -> int:
        async with self.lock:
            if self.mixer is None or self.mixer.closed:
                return 0
            layers = self.mixer.pop_layers()
            for source in layers.values():
                source.cleanup()
            if layers:
                self._sync_transport()
                self._shutdown_if_idle()
                log.info('Ambience stopped (%d layer(s))', len(layers))
            return len(layers)

    async def disconnect(self):
        async with self.lock:
            self.generation += 1
            self.category = None
            self.track = None
            self.remaining.clear()
            self.music_paused = False
            self.failed_starts = 0
            if self.mixer is not None and not self.mixer.closed:
                self.mixer.set_keep_music_alive(False)
                self.mixer.clear_all()
            self._shutdown_player()
            voice = self.voice
            self.voice = None
        if voice is not None and voice.is_connected():
            await voice.disconnect(force=True)

    def change_volume(self, delta: float):
        self.volume = clamp_volume(self.volume + delta, self.volume)
        if self.mixer is not None and not self.mixer.closed:
            self.mixer.set_music_volume(self.volume)
        self._guild_settings()['volume'] = self.volume
        save_settings(self.bot.settings)

    def set_ambience_volume(self, level: float):
        self.ambience_volume = clamp_volume(level, self.ambience_volume)
        if self.mixer is not None and not self.mixer.closed:
            self.mixer.set_ambience_volume(self.ambience_volume)
        self._guild_settings()['ambience_volume'] = self.ambience_volume
        save_settings(self.bot.settings)

    def status_text(self) -> str:
        if self.music_paused and (self.track is not None or self.active_ambience() or self.category):
            if self.track is not None or (self.mixer is not None and self.mixer.has_music()):
                music_state = 'Paused'
            else:
                music_state = 'Stopped'
        elif self.track is not None and self.voice is not None and (self.voice.is_playing() or self.voice.is_paused()):
            music_state = 'Playing'
        else:
            music_state = 'Stopped'
        names = self.active_ambience()
        ambience = ', '.join(names) if names else 'None'
        return (
            f'**Music:** {music_state} | Category: {self.category or "None"} | '
            f'Track: {self.track.name if self.track else "None"} | Volume: {round(self.volume * 100)}%\n'
            f'**Ambience:** {ambience} | Volume: {round(self.ambience_volume * 100)}%'
        )


class CategorySelect(discord.ui.Select):
    def __init__(self, bot: 'Jukebox', page: int):
        names = list(bot.library.groups)
        start = page * MAX_OPTIONS
        options = [
            discord.SelectOption(label=n[:100], value=str(i), description=f'{len(bot.library.groups[n])} tracks')
            for i, n in enumerate(names[start:start + MAX_OPTIONS], start)
        ]
        if not options:
            options = [discord.SelectOption(label='No music found', value='empty')]
        super().__init__(
            placeholder='🎵 Choose a soundtrack category',
            options=options,
            custom_id='jukebox:category',
            disabled=not bool(names),
        )
        self.bot = bot

    async def callback(self, interaction: discord.Interaction):
        if not await authorized(interaction):
            return
        names = list(self.bot.library.groups)
        try:
            category = names[int(self.values[0])]
        except (ValueError, IndexError):
            await interaction.response.send_message('Category changed. Use `/panel` to refresh.', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        player = self.bot.player(interaction.guild_id)
        try:
            await ensure_voice(interaction, player)
            await player.choose(category)
            await interaction.followup.send(
                f'▶️ Playing **{discord.utils.escape_markdown(category)}** — {len(self.bot.library.groups[category])} tracks.\n'
                f'Now playing: **{discord.utils.escape_markdown(player.track.stem) if player.track else "Unavailable"}**',
                ephemeral=True,
            )
        except (discord.DiscordException, RuntimeError, OSError) as exc:
            await interaction.followup.send(f'Could not play music: {exc}', ephemeral=True)


class MusicPanel(discord.ui.View):
    def __init__(self, bot: 'Jukebox', page: int = 0):
        super().__init__(timeout=None)
        self.bot = bot
        self.page = page
        self.add_item(CategorySelect(bot, page))

    @discord.ui.button(label='⏯ Pause / Resume', style=discord.ButtonStyle.secondary, custom_id='jukebox:pause', row=1)
    async def pause(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await authorized(interaction):
            return
        message = await self.bot.player(interaction.guild_id).toggle_pause()
        await interaction.response.send_message(message, ephemeral=True)

    @discord.ui.button(label='⏭ Skip', style=discord.ButtonStyle.primary, custom_id='jukebox:skip', row=1)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await authorized(interaction):
            return
        player = self.bot.player(interaction.guild_id)
        ok = await player.skip()
        await interaction.response.send_message(
            f'⏭ {discord.utils.escape_markdown(player.track.stem) if player.track else "Skipped."}' if ok else 'Nothing to skip.',
            ephemeral=True,
        )

    @discord.ui.button(label='⏹ Stop', style=discord.ButtonStyle.danger, custom_id='jukebox:stop', row=1)
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await authorized(interaction):
            return
        player = self.bot.player(interaction.guild_id)
        await player.stop()
        if player.active_ambience():
            message = '⏹ Music stopped. Ambience is still playing.'
        else:
            message = '⏹ Music stopped.'
        await interaction.response.send_message(message, ephemeral=True)

    @discord.ui.button(label='🔉 -10%', style=discord.ButtonStyle.secondary, custom_id='jukebox:quieter', row=2)
    async def quieter(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await authorized(interaction):
            return
        player = self.bot.player(interaction.guild_id)
        player.change_volume(-0.1)
        await interaction.response.send_message(f'Music volume: {round(player.volume * 100)}%', ephemeral=True)

    @discord.ui.button(label='🔊 +10%', style=discord.ButtonStyle.secondary, custom_id='jukebox:louder', row=2)
    async def louder(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await authorized(interaction):
            return
        player = self.bot.player(interaction.guild_id)
        player.change_volume(0.1)
        await interaction.response.send_message(f'Music volume: {round(player.volume * 100)}%', ephemeral=True)

    @discord.ui.button(label='📋 Status', style=discord.ButtonStyle.secondary, custom_id='jukebox:status', row=2)
    async def status(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await authorized(interaction):
            return
        await interaction.response.send_message(
            self.bot.player(interaction.guild_id).status_text(), ephemeral=True
        )

    @discord.ui.button(label='◀ Categories', style=discord.ButtonStyle.secondary, custom_id='jukebox:prev', row=3)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await authorized(interaction):
            return
        page = max(0, self.page - 1)
        await interaction.response.edit_message(embed=panel_embed(self.bot, page), view=MusicPanel(self.bot, page))

    @discord.ui.button(label='Categories ▶', style=discord.ButtonStyle.secondary, custom_id='jukebox:next', row=3)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await authorized(interaction):
            return
        maxpage = max(0, (len(self.bot.library.groups) - 1) // MAX_OPTIONS)
        page = min(maxpage, self.page + 1)
        await interaction.response.edit_message(embed=panel_embed(self.bot, page), view=MusicPanel(self.bot, page))


async def authorized(interaction: discord.Interaction) -> bool:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message('Use this in a server.', ephemeral=True)
        return False
    user = interaction.user
    role_name = os.getenv('DM_ROLE_NAME', 'DM').strip()
    allowed = user.guild_permissions.manage_guild or (
        role_name and any(r.name.casefold() == role_name.casefold() for r in user.roles)
    )
    if not allowed:
        await interaction.response.send_message(
            f'Only server managers or members with the **{role_name}** role can control music.',
            ephemeral=True,
        )
    return bool(allowed)


async def ensure_voice(interaction: discord.Interaction, player: GuildPlayer):
    member = interaction.user
    if not isinstance(member, discord.Member) or not member.voice or not member.voice.channel:
        raise RuntimeError('Join a voice channel first.')
    target = member.voice.channel
    if player.voice and player.voice.is_connected():
        if player.voice.channel != target:
            await player.voice.move_to(target)
    else:
        # A stale VoiceClient can remain registered after a failed connection.
        existing = discord.utils.get(player.bot.voice_clients, guild=interaction.guild)
        if existing and existing.is_connected():
            player.voice = existing
            if existing.channel != target:
                await existing.move_to(target)
        else:
            if existing:
                log.warning('Cleaning up stale voice connection for guild %s', interaction.guild_id)
                try:
                    await existing.disconnect(force=True)
                except Exception:
                    log.exception('Stale voice cleanup failed')
            log.info('Connecting to voice channel %s (guild %s)', target, interaction.guild_id)
            player.voice = await target.connect(timeout=30, reconnect=True)
            log.info('Voice connected: %s', player.voice.is_connected())


def panel_embed(bot: 'Jukebox', page: int = 0) -> discord.Embed:
    total = len(bot.library.groups)
    pages = max(1, (total + MAX_OPTIONS - 1) // MAX_OPTIONS)
    embed = discord.Embed(
        title='🎵 GMAH — GM Audio Helper',
        description=(
            'Select a category to start a shuffled, continuous soundtrack.\n'
            'Layer weather or room tone with `/ambience` — it keeps looping when you skip, pause, or stop the music.\n'
            'Use the buttons to control the music. Only the DM role or server managers can operate this panel.'
        ),
        color=discord.Color.dark_red(),
    )
    embed.add_field(name='Library', value=f'{total} categories • {sum(map(len, bot.library.groups.values()))} tracks', inline=True)
    embed.add_field(name='Category page', value=f'{page + 1}/{pages}', inline=True)
    packs = len(bot.ambience.groups)
    embed.add_field(name='Ambience', value=f'{packs} looping pack{"s" if packs != 1 else ""}', inline=True)
    embed.set_footer(text='Music: music/<category>/. Ambience: music/ambience/<name>/. Then /rescan. Use /panel to refresh categories.')
    return embed


class Jukebox(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix='!', intents=intents)
        self.library = MusicLibrary()
        self.ambience = AmbienceLibrary()
        self.settings = load_settings()
        self.players: dict[int, GuildPlayer] = {}

    def player(self, guild_id: int) -> GuildPlayer:
        if guild_id not in self.players:
            self.players[guild_id] = GuildPlayer(self, guild_id)
        return self.players[guild_id]

    async def setup_hook(self):
        # Registers a persistent panel after restart; refresh with /panel when categories change.
        self.add_view(MusicPanel(self))
        test_guild = os.getenv('TEST_GUILD_ID', '').strip()
        if test_guild:
            guild = discord.Object(id=int(test_guild))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info('Commands synced to test server %s', test_guild)
        else:
            await self.tree.sync()
            log.info('Global commands synced (may take time to appear)')


bot = Jukebox()


@bot.event
async def on_ready():
    log.info('Logged in as %s (%s)', bot.user, bot.user.id if bot.user else '?')
    log.info(
        'Found %d music categories and %d ambience packs',
        len(bot.library.groups), len(bot.ambience.groups),
    )
    log.info('discord.py version: %s', discord.__version__)
    log.info('Opus encoder loaded: %s', discord.opus.is_loaded())


@bot.tree.command(name='panel', description='Post the D&D soundtrack control panel')
@app_commands.guild_only()
async def panel(interaction: discord.Interaction):
    if not await authorized(interaction):
        return
    await interaction.response.send_message(embed=panel_embed(bot), view=MusicPanel(bot))


@bot.tree.command(name='rescan', description='Reload music and ambience folders')
@app_commands.guild_only()
async def rescan(interaction: discord.Interaction):
    if not await authorized(interaction):
        return
    bot.library.scan()
    bot.ambience.scan()
    tracks = sum(map(len, bot.library.groups.values()))
    await interaction.response.send_message(
        f'📁 Found **{len(bot.library.groups)}** categories and **{tracks}** tracks. '
        f'Ambience packs with audio: **{len(bot.ambience.groups)}**. '
        f'Run `/panel` to show new categories.',
        ephemeral=True,
    )


@bot.tree.command(name='join', description='Join your current voice channel')
@app_commands.guild_only()
async def join(interaction: discord.Interaction):
    if not await authorized(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    try:
        await ensure_voice(interaction, bot.player(interaction.guild_id))
        await interaction.followup.send('🔊 Connected to your voice channel.', ephemeral=True)
    except (discord.DiscordException, RuntimeError) as exc:
        await interaction.followup.send(f'Cannot join: {exc}', ephemeral=True)


@bot.tree.command(name='leave', description='Stop playback and leave the voice channel')
@app_commands.guild_only()
async def leave(interaction: discord.Interaction):
    if not await authorized(interaction):
        return
    await bot.player(interaction.guild_id).disconnect()
    await interaction.response.send_message('👋 Disconnected.', ephemeral=True)


ambience = app_commands.Group(
    name='ambience',
    description='Loop weather and room tone under the music',
)


def _choice_list(names: list[str], current: str) -> list[app_commands.Choice[str]]:
    query = current.casefold()
    choices: list[app_commands.Choice[str]] = []
    for name in names:
        if query in name.casefold():
            label = name[:100]
            choices.append(app_commands.Choice(name=label, value=label))
        if len(choices) >= 25:
            break
    return choices


def _quoted_names(names: list[str]) -> str:
    if not names:
        return 'none'
    return ', '.join(f'**{discord.utils.escape_markdown(name)}**' for name in names)


@ambience.command(name='add', description='Add a looping ambience layer without stopping the music')
@app_commands.describe(name='Pack under music/ambience, such as rain or fireplace')
@app_commands.guild_only()
async def ambience_add(interaction: discord.Interaction, name: str):
    if not await authorized(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    library = bot.ambience
    resolved = library.resolve(name)
    if resolved is None:
        empty = library.resolve_empty(name)
        if empty:
            await interaction.followup.send(
                f'**{discord.utils.escape_markdown(empty)}** has no audio yet. '
                f'Add a file to `music/ambience/{empty}/` and run `/rescan`.',
                ephemeral=True,
            )
            return
        known = _quoted_names([*library.groups, *library.empty])
        await interaction.followup.send(
            f'Unknown ambience **{discord.utils.escape_markdown(name.strip() or "?")}**. Folders: {known}.',
            ephemeral=True,
        )
        return
    player = bot.player(interaction.guild_id)
    try:
        await ensure_voice(interaction, player)
        status = await player.add_ambience(resolved)
    except (discord.DiscordException, RuntimeError, OSError) as exc:
        await interaction.followup.send(f'Could not start ambience: {exc}', ephemeral=True)
        return
    label = discord.utils.escape_markdown(resolved)
    if status == 'already':
        await interaction.followup.send(f'**{label}** is already playing.', ephemeral=True)
        return
    if status == 'full':
        await interaction.followup.send(
            f'You already have {MAX_AMBIENCE_LAYERS} ambience layers. Remove one with `/ambience remove`.',
            ephemeral=True,
        )
        return
    playing = _quoted_names(player.active_ambience())
    await interaction.followup.send(
        f'🌫️ Looping **{label}**. Now playing: {playing}. '
        f'Ambience volume is {round(player.ambience_volume * 100)}%.',
        ephemeral=True,
    )


@ambience_add.autocomplete('name')
async def ambience_add_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    return _choice_list(list(bot.ambience.groups), current)


@ambience.command(name='remove', description='Remove one ambience layer. Music keeps playing')
@app_commands.describe(name='Pack to stop, such as thunder')
@app_commands.guild_only()
async def ambience_remove(interaction: discord.Interaction, name: str):
    if not await authorized(interaction):
        return
    player = bot.player(interaction.guild_id)
    resolved = match_name(name, player.active_ambience())
    if resolved is None:
        await interaction.response.send_message(
            f'**{discord.utils.escape_markdown(name.strip() or "?")}** is not playing. '
            f'Active: {_quoted_names(player.active_ambience())}.',
            ephemeral=True,
        )
        return
    await player.remove_ambience(resolved)
    remaining = player.active_ambience()
    label = discord.utils.escape_markdown(resolved)
    if remaining:
        await interaction.response.send_message(
            f'Removed **{label}**. Still looping: {_quoted_names(remaining)}.',
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(f'Removed **{label}**. No ambience is playing.', ephemeral=True)


@ambience_remove.autocomplete('name')
async def ambience_remove_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    if interaction.guild_id is None:
        return []
    return _choice_list(bot.player(interaction.guild_id).active_ambience(), current)


@ambience.command(name='list', description='Show ambience packs, what is looping, and the volume')
@app_commands.guild_only()
async def ambience_list(interaction: discord.Interaction):
    if not await authorized(interaction):
        return
    player = bot.player(interaction.guild_id)
    library = bot.ambience
    lines = [
        f'**Ambience volume:** {round(player.ambience_volume * 100)}%',
        f'**Playing:** {_quoted_names(player.active_ambience())}',
    ]
    if library.groups:
        available = ', '.join(
            f'**{discord.utils.escape_markdown(name)}** ({len(files)} file{"s" if len(files) != 1 else ""})'
            for name, files in library.groups.items()
        )
        lines.append(f'**Available:** {available}')
    else:
        lines.append('**Available:** none yet. Add audio under `music/ambience/<name>/` and run `/rescan`.')
    if library.empty:
        lines.append(f'**Empty folders:** {_quoted_names(library.empty)}')
    lines.append(f'Music volume is separate: {round(player.volume * 100)}% (panel ±10% buttons).')
    await interaction.response.send_message('\n'.join(lines), ephemeral=True)


@ambience.command(name='stop', description='Stop every ambience layer. Music keeps playing')
@app_commands.guild_only()
async def ambience_stop(interaction: discord.Interaction):
    if not await authorized(interaction):
        return
    count = await bot.player(interaction.guild_id).stop_ambience()
    if count:
        await interaction.response.send_message(
            f'🌫️ Stopped {count} ambience layer{"s" if count != 1 else ""}. Music is unchanged.',
            ephemeral=True,
        )
    else:
        await interaction.response.send_message('No ambience is playing.', ephemeral=True)


@ambience.command(name='volume', description='Set ambience volume from 0 to 100')
@app_commands.describe(level='Percent from 0 to 100. Music volume is unchanged.')
@app_commands.guild_only()
async def ambience_volume(
    interaction: discord.Interaction,
    level: app_commands.Range[int, 0, 100],
):
    if not await authorized(interaction):
        return
    player = bot.player(interaction.guild_id)
    player.set_ambience_volume(level / 100)
    await interaction.response.send_message(
        f'Ambience volume: {round(player.ambience_volume * 100)}%. '
        f'Music volume is still {round(player.volume * 100)}%.',
        ephemeral=True,
    )


bot.tree.add_command(ambience)


if __name__ == '__main__':
    if not TOKEN or TOKEN == 'PASTE_YOUR_BOT_TOKEN_HERE':
        raise SystemExit('Missing DISCORD_TOKEN. Copy .env.example to .env and add your bot token.')
    bot.run(TOKEN, log_handler=None)
