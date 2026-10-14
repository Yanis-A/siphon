# Siphon

A private, local alternative to NoTube-style converters. Paste a link from YouTube, SoundCloud, Vimeo, Bandcamp and [hundreds of other sites](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md), pick a format, get the file. No ads, no upload, no file size limit: everything runs on your machine.

- **Audio:** MP3 (320 / 256 / 192 / 128 kbps), WAV, FLAC, M4A, with title, artist and a square cover embedded (MP3, FLAC, M4A)
- **Video:** MP4 from 360p up to 4K, H.264 + AAC whenever the site offers it (plays everywhere and in any editor)
- **Playlists:** whole playlists into their own numbered folder
- Parallel downloads, live progress, cancel and retry
- English and French interface

## Run it

**Windows:** double-click `Siphon.bat`.
**macOS / Linux:** run `Siphon.command`.

Needs [Python 3.10+](https://www.python.org/downloads/) (on Windows, tick *Add python.exe to PATH* during the install). The first launch installs the engine into a private `.venv` folder (about 80 MB, once). After that, Siphon opens in your browser in a second.

Files go to `Downloads/Siphon` by default. Change it from the settings (slider icon) or the *Change* link under the download button.

Siphon quits on its own 10 minutes after you close its tab (never while something is downloading), or right away with the power button.

## Engine

Siphon drives [yt-dlp](https://github.com/yt-dlp/yt-dlp), with [Deno](https://deno.com) to solve YouTube's JavaScript challenges and a bundled [FFmpeg](https://ffmpeg.org) for conversion. Nothing to install by hand.

YouTube changes often, so yt-dlp is updated automatically about once a day (Deno once a week) when you open Siphon. If a site suddenly starts failing, open settings and press **Update engine**.

## When a download fails

| Message | What to do |
| --- | --- |
| *The site blocked the download* | Update the engine, then retry. |
| *YouTube wants to check you're not a bot* | Wait a few minutes, or switch networks (VPNs often trigger it). |
| *Age-restricted / members only / private* | Needs a signed-in account; Siphon doesn't use your cookies. |

The raw error from yt-dlp is shown under each failed download, and the full log is in `data/siphon.log`.

## Files

```
Siphon.bat / Siphon.command   launchers
Siphon.ico                    icon, for a desktop or Start Menu shortcut
siphon/app.py                 local server (127.0.0.1 only) and download queue
siphon/setup_env.py           first-run engine install / repair
siphon/web/index.html         interface
data/                         settings, logs, cache, temp files (created at runtime)
.venv/                        private Python environment (created at runtime)
```

Delete the folder to uninstall.

Download only what you have the right to use.

## Credits

[yt-dlp](https://github.com/yt-dlp/yt-dlp) (Unlicense), [Deno](https://deno.com) (MIT), FFmpeg via [imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg), [Lucide](https://lucide.dev) icons (ISC), [Archivo](https://github.com/Omnibus-Type/Archivo) typeface (SIL OFL 1.1).
