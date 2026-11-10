# Siphon

A private, local alternative to NoTube-style converters. Paste a link from YouTube, SoundCloud, Vimeo, Bandcamp and [hundreds of other sites](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md), pick a format, get the file. No ads, no upload, no file size limit: everything runs on your machine.

- **Audio:** MP3 (320 / 256 / 192 / 128 kbps), WAV, FLAC, M4A, with title, artist and a square cover embedded (MP3, FLAC, M4A)
- **Video:** MP4 from 360p up to 4K, H.264 + AAC whenever the site offers it (plays everywhere and in any editor)
- **Playlists:** whole playlists into their own numbered folder
- **Spotify:** paste a track, an album or a public playlist. Spotify's own audio is DRM-protected and left alone: Siphon reads the track list and fetches each song from YouTube Music's songs shelf, the label-delivered studio version, keeping only a match with the same title, artist and length to within two seconds. The music video, a live take or a demo is never taken for the song. Tracks with no sure match are skipped and named; running a playlist again only fetches what was added. Without an account Spotify shows the first 100 tracks of a playlist, and none of a private one.
- **Destinations:** named folders that carry their own format: a *Samples* folder in WAV, a *Music* folder in MP3 320. One click sets where the file goes and what it is.
- **Tracks:** split a mix or a DJ set into one file per track. Siphon uses the site's chapter markers when there are any, and otherwise reads the tracklist out of the description: `12:34 Artist – Title` and the dozen shapes that line comes in. The cut loses no quality and every piece keeps the cover and tags. In WAV and FLAC it lands on the exact sample, which is what you want for sampling; MP3 and M4A are stream-copied, so their cuts snap to the format's ~20 ms frames.
- Parallel downloads, live progress, cancel and retry
- English and French interface

## Run it

Download the archive for your system from the [latest release](https://github.com/Yanis-A/siphon/releases/latest), unpack it anywhere, and launch Siphon from the folder. It carries its own Python, so there is nothing to install first. The first launch fetches the download engine (yt-dlp, Deno, FFmpeg: about 80 MB, once). After that, Siphon opens in your browser in a second.

| System | Archive |
|---|---|
| Windows | `Siphon-…-windows-x64.zip` |
| macOS, Apple Silicon (M1 and later) | `Siphon-…-macos-arm64.tar.gz` |
| macOS, Intel | `Siphon-…-macos-x64.tar.gz` |
| Linux | `Siphon-…-linux-x64.tar.gz` |

Siphon is not signed with a paid certificate, so Windows and macOS each warn once that it comes from an unknown developer. That is all the warning means.

**Windows:** right-click the zip, *Extract All*, then double-click `Siphon.bat` in the extracted folder (from inside the zip it cannot run). If Windows says it protected your PC, click *More info*, then *Run anyway*.

**macOS:** double-click the archive to unpack it, then double-click `Siphon.command`. The first time, macOS refuses to open it: go to *System Settings → Privacy & Security*, scroll to the line about `Siphon.command`, click *Open Anyway* and confirm. On macOS 14 and older, right-click `Siphon.command` → *Open* does the same. Every launch after that is a plain double-click.

**Linux:** `tar -xzf Siphon-*-linux-x64.tar.gz`, then `./Siphon/Siphon.command`.

**From the source:** clone the repository and run the same launcher. Without a bundled Python it uses yours, 3.10 or newer: on Windows, tick *Add python.exe to PATH* when installing it; on Debian and Ubuntu, `sudo apt install python3-venv python3-tk` (the second one only for the folder picker).

Files go to `Downloads/Siphon` by default. Add more destinations from the settings (slider icon): each one is a folder plus the format it exists for, and they show up as a row of buttons under the download button.

Siphon quits on its own 10 minutes after you close its tab (never while something is downloading), or right away with the power button.

## Browser extension

`extension/` holds a Firefox add-on that sends the page you are on to Siphon, so you never copy a link. Click its toolbar button and the panel already shows the current tab and your destinations: one click picks the folder and the format together. There is also a right-click entry for a page, a link or a media element, and the toolbar icon carries a badge while downloads run.

**Load it:** open `about:debugging#/runtime/this-firefox` → *Load Temporary Add-on* → pick `extension/manifest.json`.

That install lasts until you quit Firefox. Mozilla only keeps add-ons installed permanently if they are signed, so for something that survives a restart you have two options: run Firefox Developer Edition or ESR with `xpinstall.signatures.required` set to `false` in `about:config`, or submit the folder to [addons.mozilla.org](https://addons.mozilla.org/developers/) as an unlisted add-on, which signs it for free and hands back an `.xpi` you install normally.

**One setting matters.** Siphon quits ten minutes after you close its tab, and an extension cannot wake a local program. Turn on *Keep Siphon running* in the settings so it is there when you click. Add `Siphon.lnk` to your Startup folder (`Win`+`R`, `shell:startup`) and it will be running from login.

The extension talks only to `127.0.0.1`. The server accepts it by origin and still requires the same header it requires of its own page, so an ordinary web page cannot reach Siphon.

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
runtime/                      Python, in the downloaded archives only
siphon/app.py                 local server (127.0.0.1 only) and download queue
siphon/setup_env.py           first-run engine install / repair
siphon/web/index.html         interface, including the waveform editor
extension/                    Firefox add-on: send the current tab to Siphon
data/                         settings, logs, cache, temp files (created at runtime)
.venv/                        private Python environment (created at runtime)
tools/build_release.py        builds the archives (repository only)
```

Delete the folder to uninstall.

Download only what you have the right to use.

## Releasing

`python tools/build_release.py` builds the four archives into `dist/`, each with a standalone CPython from [python-build-standalone](https://github.com/astral-sh/python-build-standalone), checked against its published checksums. Pushing a tag that matches `VERSION` in `siphon/app.py` (`v1.1.0`) makes the *Release* workflow build them, launch each one on Windows, macOS and Linux until it answers with its engine installed, and prepare a draft release with them.

## License

Siphon is MIT licensed: do what you like with it, keep the notice, and it comes with no warranty. See [LICENSE](LICENSE).

That covers Siphon's own code. The engine it drives keeps its own terms, listed below.

## Credits

[yt-dlp](https://github.com/yt-dlp/yt-dlp) (Unlicense), [Deno](https://deno.com) (MIT), FFmpeg via [imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg), [Lucide](https://lucide.dev) icons (ISC), [Archivo](https://github.com/Omnibus-Type/Archivo) typeface (SIL OFL 1.1).
