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

opus_path = os.path.join(
    os.path.dirname(discord.__file__),
    "bin",
    "libopus-0.x64.dll"
)
discord.opus.load_opus(opus_path)
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / '.env', override=True)
TOKEN = os.getenv('DISCORD_TOKEN', '').strip()
MUSIC = ROOT / 'music'
STATE_FILE = ROOT / 'settings.json'
FORMATS = {'.mp3', '.ogg', '.wav', '.flac', '.m4a', '.opus'}
FFMPEG = os.getenv('FFMPEG_PATH', 'ffmpeg').strip() or 'ffmpeg'
MAX_OPTIONS = 25
logging.basicConfig(level=logging.DEBUG if os.getenv('GMAH_DEBUG', '1') == '1' else logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
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


class MusicLibrary:
    def __init__(self):
        self.groups: dict[str, list[Path]] = {}
        self.scan()

    def scan(self):
        MUSIC.mkdir(exist_ok=True)
        self.groups = {}
        for folder in sorted(MUSIC.iterdir(), key=lambda p: p.name.casefold()):
            if folder.is_dir() and not folder.name.startswith('.'):
                tracks = sorted((p for p in folder.rglob('*') if p.is_file() and p.suffix.lower() in FORMATS), key=lambda p: str(p).casefold())
                if tracks:
                    self.groups[folder.name] = tracks


class DiagnosticPCMVolumeTransformer(discord.PCMVolumeTransformer):
    """Count audio frames and report whether PCM contains audible samples."""

    def __init__(self, original, volume=1.0):
        super().__init__(original, volume=volume)
        self.diag_reads = 0
        self.diag_nonzero = 0
        self.diag_bytes = 0

    def read(self):
        data = super().read()
        if data:
            self.diag_reads += 1
            self.diag_bytes += len(data)
            if any(data):
                self.diag_nonzero += 1
            if self.diag_reads in (1, 50, 250):
                log.info('AUDIO DIAG PCM: frames=%d non_silent=%d bytes=%d frame_size=%d',
                         self.diag_reads, self.diag_nonzero, self.diag_bytes, len(data))
        else:
            log.info('AUDIO DIAG PCM: end of stream after %d frames', self.diag_reads)
        return data


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
                log.info('AUDIO DIAG RTP: packets_sent=%d input_bytes=%d encode=%s',
                         voice._gmah_diag_packets, len(data), encode)
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
        self.remaining: deque[Path] = deque()
        self.volume = float(bot.settings.get(str(guild_id), {}).get('volume', 0.35))
        self.volume = max(0.0, min(1.0, self.volume))
        self.generation = 0
        self.lock = asyncio.Lock()
        self.started_at = 0.0
        self.failed_starts = 0

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

    def _play_next(self):
        if not self.voice or not self.voice.is_connected():
            log.warning('Cannot start audio: voice client disconnected (guild %s)', self.guild_id)
            return
        track = self._next_track()
        if track is None:
            self.track = None
            return
        self.track = track
        self.generation += 1
        generation = self.generation
        try:
            # Diagnostic: decode to PCM and let discord.py encode Opus.
            # This requires the system Opus library (PyNaCl alone is not enough).
            pcm = discord.FFmpegPCMAudio(
                str(track), executable=FFMPEG,
                before_options='-nostdin',
                options='-vn -loglevel warning',
            )
            source = DiagnosticPCMVolumeTransformer(pcm, volume=self.volume)
            install_audio_packet_diagnostics(self.voice)
            self.started_at = time.monotonic()
            log.info('Starting audio: %s (guild=%s, channel=%s, volume=%.2f)',
                     track.name, self.guild_id, self.voice.channel, self.volume)

            def after(error):
                if error:
                    log.error('Audio player error for %s: %r', track.name, error)
                elapsed = time.monotonic() - self.started_at
                log.info('Audio ended: %s after %.1fs; error=%r', track.name, elapsed, error)
                future = asyncio.run_coroutine_threadsafe(
                    self._after_track(generation, elapsed), self.bot.loop
                )
                future.add_done_callback(lambda f: log.error('Track callback failed: %r', f.exception()) if not f.cancelled() and f.exception() else None)

            packet_start = self.voice._gmah_diag_packets
            self.voice.play(source, after=after)

            async def diagnostic_snapshot():
                await asyncio.sleep(8)
                log.info('AUDIO DIAG 8s: voice_connected=%s playing=%s PCM_frames=%d PCM_non_silent=%d RTP_packets=%d RTP_failures=%d opus_loaded=%s',
                         self.voice.is_connected() if self.voice else False,
                         self.voice.is_playing() if self.voice else False,
                         source.diag_reads, source.diag_nonzero,
                         (self.voice._gmah_diag_packets - packet_start) if self.voice else 0,
                         self.voice._gmah_diag_failures if self.voice else 0,
                         discord.opus.is_loaded())

            self.bot.loop.create_task(diagnostic_snapshot())
        except Exception:
            log.exception('Could not start track %s', track)
            self.track = None
            self.category = None

    async def _after_track(self, generation: int, elapsed: float):
        async with self.lock:
            if generation != self.generation or not self.category:
                return
            if elapsed < 3.0:
                self.failed_starts += 1
                log.warning('Track ended unusually quickly (%.1fs). Consecutive failures: %d',
                            elapsed, self.failed_starts)
                if self.failed_starts >= 2:
                    log.error('Stopping playback to avoid an infinite retry loop. Check audio and voice logs.')
                    self.category = None
                    self.track = None
                    return
                await asyncio.sleep(1)
            else:
                self.failed_starts = 0
            self._play_next()

    async def choose(self, category: str):
        async with self.lock:
            self.category = category
            self.remaining.clear()
            self.failed_starts = 0
            self.generation += 1
            if self.voice and (self.voice.is_playing() or self.voice.is_paused()):
                self.voice.stop()
            self._play_next()

    async def skip(self):
        async with self.lock:
            if not self.category:
                return False
            self.generation += 1
            if self.voice and (self.voice.is_playing() or self.voice.is_paused()):
                self.voice.stop()
            self._play_next()
            return True

    async def stop(self):
        async with self.lock:
            self.generation += 1
            self.category = None
            self.track = None
            self.remaining.clear()
            if self.voice and (self.voice.is_playing() or self.voice.is_paused()):
                self.voice.stop()

    async def disconnect(self):
        await self.stop()
        if self.voice and self.voice.is_connected():
            await self.voice.disconnect(force=True)
        self.voice = None

    def change_volume(self, delta: float):
        self.volume = round(max(0.0, min(1.0, self.volume + delta)), 2)
        # PCMVolumeTransformer supports live volume changes without restarting.
        if self.voice and isinstance(self.voice.source, discord.PCMVolumeTransformer):
            self.voice.source.volume = self.volume
        self.bot.settings.setdefault(str(self.guild_id), {})['volume'] = self.volume
        save_settings(self.bot.settings)


class CategorySelect(discord.ui.Select):
    def __init__(self, bot: 'Jukebox', page: int):
        names = list(bot.library.groups)
        start = page * MAX_OPTIONS
        options = [discord.SelectOption(label=n[:100], value=str(i), description=f'{len(bot.library.groups[n])} tracks') for i, n in enumerate(names[start:start+MAX_OPTIONS], start)]
        if not options:
            options = [discord.SelectOption(label='No music found', value='empty')]
        super().__init__(placeholder='🎵 Choose a soundtrack category', options=options, custom_id='jukebox:category', disabled=not bool(names))
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
            await interaction.followup.send(f'▶️ Playing **{discord.utils.escape_markdown(category)}** — {len(self.bot.library.groups[category])} tracks.\nNow playing: **{discord.utils.escape_markdown(player.track.stem) if player.track else "Unavailable"}**', ephemeral=True)
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
        if not await authorized(interaction): return
        player = self.bot.player(interaction.guild_id)
        voice = player.voice
        if voice and voice.is_paused():
            voice.resume()
            msg = '▶️ Resumed.'
        elif voice and voice.is_playing():
            voice.pause()
            msg = '⏸ Paused.'
        else:
            msg = 'Nothing is playing.'
        await interaction.response.send_message(msg, ephemeral=True)

    @discord.ui.button(label='⏭ Skip', style=discord.ButtonStyle.primary, custom_id='jukebox:skip', row=1)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await authorized(interaction): return
        player = self.bot.player(interaction.guild_id)
        ok = await player.skip()
        await interaction.response.send_message(f'⏭ {discord.utils.escape_markdown(player.track.stem) if player.track else "Skipped."}' if ok else 'Nothing to skip.', ephemeral=True)

    @discord.ui.button(label='⏹ Stop', style=discord.ButtonStyle.danger, custom_id='jukebox:stop', row=1)
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await authorized(interaction): return
        await self.bot.player(interaction.guild_id).stop()
        await interaction.response.send_message('⏹ Music stopped.', ephemeral=True)

    @discord.ui.button(label='🔉 -10%', style=discord.ButtonStyle.secondary, custom_id='jukebox:quieter', row=2)
    async def quieter(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await authorized(interaction): return
        player = self.bot.player(interaction.guild_id)
        player.change_volume(-0.1)
        await interaction.response.send_message(f'Volume: {round(player.volume*100)}%', ephemeral=True)

    @discord.ui.button(label='🔊 +10%', style=discord.ButtonStyle.secondary, custom_id='jukebox:louder', row=2)
    async def louder(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await authorized(interaction): return
        player = self.bot.player(interaction.guild_id)
        player.change_volume(0.1)
        await interaction.response.send_message(f'Volume: {round(player.volume*100)}%', ephemeral=True)

    @discord.ui.button(label='📋 Status', style=discord.ButtonStyle.secondary, custom_id='jukebox:status', row=2)
    async def status(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await authorized(interaction): return
        p = self.bot.player(interaction.guild_id)
        state = 'Paused' if p.voice and p.voice.is_paused() else ('Playing' if p.voice and p.voice.is_playing() else 'Stopped')
        await interaction.response.send_message(f'**{state}** | Category: {p.category or "None"} | Track: {p.track.name if p.track else "None"} | Volume: {round(p.volume*100)}%', ephemeral=True)

    @discord.ui.button(label='◀ Categories', style=discord.ButtonStyle.secondary, custom_id='jukebox:prev', row=3)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await authorized(interaction): return
        page = max(0, self.page - 1)
        await interaction.response.edit_message(embed=panel_embed(self.bot, page), view=MusicPanel(self.bot, page))

    @discord.ui.button(label='Categories ▶', style=discord.ButtonStyle.secondary, custom_id='jukebox:next', row=3)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await authorized(interaction): return
        maxpage = max(0, (len(self.bot.library.groups)-1)//MAX_OPTIONS)
        page = min(maxpage, self.page + 1)
        await interaction.response.edit_message(embed=panel_embed(self.bot, page), view=MusicPanel(self.bot, page))


async def authorized(interaction: discord.Interaction) -> bool:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message('Use this in a server.', ephemeral=True)
        return False
    user = interaction.user
    role_name = os.getenv('DM_ROLE_NAME', 'DM').strip()
    allowed = user.guild_permissions.manage_guild or (role_name and any(r.name.casefold() == role_name.casefold() for r in user.roles))
    if not allowed:
        await interaction.response.send_message(f'Only server managers or members with the **{role_name}** role can control music.', ephemeral=True)
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
    pages = max(1, (total + MAX_OPTIONS - 1)//MAX_OPTIONS)
    embed = discord.Embed(title='🎵 GMAH — GM Audio Helper', description='Select a category to start a shuffled, continuous soundtrack.\nUse the buttons to control playback. Only the DM role or server managers can operate this panel.', color=discord.Color.dark_red())
    embed.add_field(name='Library', value=f'{total} categories • {sum(map(len, bot.library.groups.values()))} tracks', inline=True)
    embed.add_field(name='Category page', value=f'{page+1}/{pages}', inline=True)
    embed.set_footer(text='Add files to music/<category>/ and run /rescan to update the library. Use /panel to refresh categories.')
    return embed


class Jukebox(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix='!', intents=intents)
        self.library = MusicLibrary()
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
    log.info('Found %d music categories', len(bot.library.groups))
    log.info('discord.py version: %s', discord.__version__)
    log.info('Opus encoder loaded: %s', discord.opus.is_loaded())


@bot.tree.command(name='panel', description='Post the D&D soundtrack control panel')
@app_commands.guild_only()
async def panel(interaction: discord.Interaction):
    if not await authorized(interaction): return
    await interaction.response.send_message(embed=panel_embed(bot), view=MusicPanel(bot))


@bot.tree.command(name='rescan', description='Reload music folders after adding tracks or categories')
@app_commands.guild_only()
async def rescan(interaction: discord.Interaction):
    if not await authorized(interaction): return
    bot.library.scan()
    await interaction.response.send_message(f'📁 Found **{len(bot.library.groups)}** categories and **{sum(map(len, bot.library.groups.values()))}** tracks. Run `/panel` to show new categories.', ephemeral=True)


@bot.tree.command(name='join', description='Join your current voice channel')
@app_commands.guild_only()
async def join(interaction: discord.Interaction):
    if not await authorized(interaction): return
    await interaction.response.defer(ephemeral=True)
    try:
        await ensure_voice(interaction, bot.player(interaction.guild_id))
        await interaction.followup.send('🔊 Connected to your voice channel.', ephemeral=True)
    except (discord.DiscordException, RuntimeError) as exc:
        await interaction.followup.send(f'Cannot join: {exc}', ephemeral=True)


@bot.tree.command(name='leave', description='Stop playback and leave the voice channel')
@app_commands.guild_only()
async def leave(interaction: discord.Interaction):
    if not await authorized(interaction): return
    await bot.player(interaction.guild_id).disconnect()
    await interaction.response.send_message('👋 Disconnected.', ephemeral=True)


if __name__ == '__main__':
    if not TOKEN or TOKEN == 'PASTE_YOUR_BOT_TOKEN_HERE':
        raise SystemExit('Missing DISCORD_TOKEN. Copy .env.example to .env and add your bot token.')
    bot.run(TOKEN, log_handler=None)
