# GM Audio Helper — Discord D&D Music Bot

A Windows-friendly Discord bot that plays your **local music files** in a voice channel. Choose a music category using a Discord dropdown; the bot shuffles its tracks and plays continuously until you stop or change categories. You can also loop ambience — rain, a fireplace, a crowd — underneath the music, or on its own.

## Features

- Automatically discovers folders and audio files (MP3, OGG, WAV, FLAC, M4A, OPUS)
- Unlimited categories, displayed 25 at a time
- Shuffled continuous playback, avoiding immediate repeats across cycles
- Pause/resume, skip, stop, music volume ±10%, and playback status
- A separate looping ambience layer (rain, thunder, fireplace, and so on) mixed under the music, or on its own
- Stack ambience packs (rain + thunder, fireplace + wind). Each pack loops until you remove it
- Music and ambience have separate volumes. Skipping, pausing, or stopping the music leaves ambience running
- Automatically joins the DM's voice channel when selecting a category or adding ambience
- DM role or Manage Server permission required to control playback
- Remembers music volume and ambience volume per Discord server
- `/panel`, `/rescan`, `/join`, `/leave`, and `/ambience` slash commands
- Runs locally on Windows; no hosting subscription

## Setup (Windows 11)

### 1. Install Python

Install **Python 3.12** from https://www.python.org/downloads/windows/ . During installation, enable **Add Python to PATH** if offered. Verify with `py -3.12 --version` in Command Prompt. (Python 3.12 is required for now: the voice-encryption library version the bot needs has no build for newer Python versions yet.)

### 2. Install FFmpeg

Install FFmpeg from https://ffmpeg.org/download.html . Make sure `ffmpeg.exe` is accessible on your PATH, or set `FFMPEG_PATH=C:\\path\\to\\ffmpeg.exe` in `.env`. Verify in Command Prompt with `ffmpeg -version`. FFmpeg is required for playback.

### 3. Register the bot with Discord

1. Visit https://discord.com/developers/applications and select **New Application**.
2. Give it a name such as `GMAH`.
3. Under **Bot**, create/reset and **copy the token**. Never share this token, commit it to Git, or send it in chat.
4. Under **Installation**, set up a Guild Install link with scopes **bot** and **applications.commands**. Give the bot permissions **View Channels**, **Send Messages**, **Embed Links**, **Connect**, and **Speak**. You may also need **Use Application Commands**, depending on server permissions.
5. Open the installation link and add it to a Discord server you manage.
6. Create a Discord server role named `DM` and assign it to yourself (or set `DM_ROLE_NAME` in `.env`). Members with **Manage Server** permission are also authorized.

**Important:** Do not turn on privileged Message Content Intent. This bot uses slash commands and components, so it doesn't need it.

### 4. Configure the program

1. Extract this ZIP to a convenient folder, such as `C:\\DND\\GMAH`.
2. Copy `.env.example` to `.env` (enable filename extensions in Windows Explorer if needed).
3. Open `.env` in Notepad and replace `PASTE_YOUR_BOT_TOKEN_HERE` with your Discord bot token.
4. Optional but recommended: set `TEST_GUILD_ID` to your Discord server ID. Enable **Developer Mode** under Discord Settings > Advanced, right-click your server icon, and select **Copy Server ID**. This makes slash commands register in your server promptly.

### 5. Add music

Put audio files inside category folders under `music/`, for example:

```
music/
  Tavern/
    Warm Fireplace.mp3
    Lively Inn.ogg
  Combat/
    Ambush.mp3
    Swordfight.mp3
  Horror/
    Castle Hallway.mp3
```

The ZIP includes empty starter folders. You must supply your own audio files that you have permission to use.

### 6. Start the bot

Double-click `start_bot.bat`. The first run creates a Python virtual environment and installs dependencies (internet required). Keep the Command Prompt window open during the session. You should see a message like `Logged in as GMAH`.

### 7. Play in Discord

1. Join your usual voice channel.
2. In a Discord text channel, type `/panel`.
3. Select a category from the dropdown. The bot joins your channel and plays a random track.
4. Use the panel buttons for skip, pause/resume, stop, and music volume. Pause and stop affect the music only.
5. Optional: add weather or room tone with `/ambience add rain` (and another pack, such as `/ambience add thunder`). It loops under the song. `/ambience stop` clears just the ambience.
6. Use `/leave` when finished. That stops both layers and disconnects.

## Adding new songs or categories

- **New song:** copy an audio file into an existing category folder, then run `/rescan`.
- **New category:** create a new folder in `music/`, add at least one audio file, run `/rescan`, and post a new `/panel` to refresh the dropdown.
- Category names are folder names. A folder with no supported audio files is ignored.
- Subfolders inside categories are included recursively.
- Category navigation uses 25 categories per page.
- The folder `music/ambience/` is reserved for looping beds. It does not appear in the category menu.

## Adding ambience

Ambience is a second layer: tavern songs with rain outside, combat with a fireplace still crackling, a crowd under exploration, and so on. Discord can only send **one** audio stream to a voice channel, so the bot mixes the song and the ambience on your PC and sends the combined audio.

Put looping files in a pack folder:

```
music/
  Tavern/
    Warm Fireplace.mp3
  ambience/
    rain/
      Steady Rain.ogg
    thunder/
      Distant Thunder.wav
    fireplace/
      Fireplace.flac
    wind/
    forest/
    crowd/
```

The repo includes empty starter packs: `rain`, `thunder`, `fireplace`, `wind`, `forest`, and `crowd`. Add your own audio that you have permission to use. One long looping file per pack is the usual case. If a pack has several files, they play in name order and then the whole pack loops.

WAV, FLAC, and OGG loop most cleanly. MP3 works, but some files have a short seam at the loop point.

Then run `/rescan`. You can stack up to 6 packs. Each one is its own ffmpeg decode, so a couple of layers is plenty for a session.

| Command | What it does |
| --- | --- |
| `/ambience add` | Start looping a pack. Joins your voice channel. Does not restart the music. |
| `/ambience remove` | Drop one pack. Other packs and the music keep going. |
| `/ambience list` | Show packs on disk, what is looping, and both volumes. |
| `/ambience stop` | Stop every ambience pack. Music is unchanged. |
| `/ambience volume` | Set ambience loudness from 0 to 100. Default is 30%. |

Music volume is still the panel **±10%** buttons (default 35%) and is stored separately. Either layer can play alone: ambience with no category selected, or music with no ambience. `/leave` stops both.

## Troubleshooting

- **Slash commands don't appear:** set `TEST_GUILD_ID` to your server ID and restart. Make sure the app has `applications.commands` scope and you can use application commands in that channel.
- **Bot doesn't join voice:** ensure you are in a voice channel and the bot has Connect and Speak permissions. Voice channels with special restrictions may need additional permissions.
- **Bot joins, shows the green speaking ring, but no one hears anything:** Discord now requires end-to-end voice encryption (DAVE). `davey` 0.2.0 does not work with discord.py 2.7.1 ([discord.py issue #10549](https://github.com/Rapptz/discord.py/issues/10549)), so `requirements.txt` pins `davey>=0.1.6,<0.2`. If you installed packages manually, run `pip install "davey<0.2"` inside the virtual environment.
- **Bot joins but no sound (other causes):** verify `ffmpeg -version` and that the audio file is playable. Check the terminal for errors. Check your Discord bot volume and local Discord client volume.
- **`ModuleNotFoundError`:** use `start_bot.bat` to install dependencies in its virtual environment.
- **`PrivilegedIntentsRequired`:** the app should not require privileged intents; check that you're running the included `bot.py`.
- **A category is missing:** check that its folder contains a supported file, run `/rescan`, then run `/panel` again. `music/ambience/` is not a music category.
- **Ambience command can't find a pack:** the folder needs at least one supported audio file. Put it in `music/ambience/<name>/`, run `/rescan`, then `/ambience add`.
- **Loop has a click or a short gap:** use WAV, FLAC, or OGG for ambience beds. MP3 loop points are often imperfect.
- **Audio stutters with several packs:** each pack is decoded live. Remove a layer with `/ambience remove`, or use shorter/simpler files.
- **Playback fails after a while:** inspect the terminal. Keep your PC awake and online.
- **Bot is offline:** run `start_bot.bat`; it only stays online while that program and PC are running.

## Technical notes / limitations

- Music switches immediately; **crossfading between songs is not implemented**.
- Ambience is mixed in PCM with the current song (20 ms frames) before Opus encoding, because a Discord voice connection carries only one audio stream. Pausing or stopping the music does not stop ambience. Pause with no ambience running pauses the whole stream, so the bot is not marked as speaking over silence.
- Up to 6 ambience packs can loop at once. They share one ambience volume.
- Discord component menus allow at most 25 options; this project paginates category choices.
- One player per Discord server; it can serve multiple servers independently.
- Existing posted panels may retain their old category lists until you run `/panel` again. On restart, the default persistent panel is restored, but a fresh `/panel` is recommended if you were using category pages beyond the first.
- The bot uses FFmpeg to decode audio and discord.py with PyNaCl for voice encryption. Audio is sent to Discord voice channels; music files stay on your computer.
- This bot does not download or stream music from YouTube or Spotify.
- Avoid running multiple copies of the bot simultaneously with the same token.

## Safety

Never share `.env` or your Discord bot token. If it leaks, reset the token in Discord Developer Portal and update `.env`.

## Author

Built by Michael Zafeirellis as a personal project for my own D&D table: a former professional cook, now studying programming at the Open University. Feedback and ideas are welcome.
