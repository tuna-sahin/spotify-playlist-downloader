# Spotify Playlist YouTube MP3 Downloader

Downloads a Spotify playlist in order by searching YouTube, preferring lyric/audio/topic-style videos, matching durations closely, and saving numbered MP3 files with leading zeros.

YouTube matching first compares both the song title and primary artist, tolerating small
spelling differences, accents, punctuation, and video-title extras. All results from the
search variants are checked, up to `--max-results` per query (default 12). Results meeting
both name thresholds are ranked by similarity, with the existing audio/duration preferences
breaking ties. If none qualifies, the previous scoring algorithm chooses the fallback.
Among name matches, acoustic versions are preferred only when the Spotify title marks
the song as acoustic (including `accoustic` and `akustik`); otherwise normal versions
are preferred. The other recording type remains available if no preferred match exists.

Use this only for audio that you own, have permission to download, or is otherwise lawful to download in your location.

## Setup

If you are using the built `.exe`, you can skip this setup section.

1. Install Python dependencies:

   ```powershell
   python -m pip install -r requirements.txt
   ```

2. Install `ffmpeg` and make sure it is on your `PATH`.

   The script also auto-detects FFmpeg installed by `winget`.

3. Public Spotify playlists do not need Spotify credentials unless Spotify only exposes the first chunk of a long playlist. For long playlists or private playlist fallback, create a Spotify developer app and set credentials:

   ```powershell
   $env:SPOTIFY_CLIENT_ID="your_client_id"
   $env:SPOTIFY_CLIENT_SECRET="your_client_secret"
   ```

4. For private playlists, add this Redirect URI to that Spotify app:

   ```text
   http://127.0.0.1:8888/callback
   ```

## Usage

### Easy Mode

Send this file to the user:

```text
dist\Spotify Playlist Downloader.exe
```

They can double-click it, paste a Spotify playlist URL, then press Enter for the output folder prompt to save next to the `.exe`. On launch it prints:

```text
spotify playlist downloader --- Tuna
```

The downloader now tries a close alternative YouTube result when the first selected song fails. If that second attempt also fails, the track is reported as failed.

### GitHub updates

The app checks GitHub Releases at startup. Set these constants in `playlist_downloader.py` before building:

```python
GITHUB_REPOSITORY = "tuna-sahin/spotify-playlist-downloader"
GITHUB_RELEASE_ASSET = "Spotify Playlist Downloader.exe"
```

Create GitHub releases with version tags such as `v1.2.0`, and attach the built `.exe` as a release asset. The attached file name must match `GITHUB_RELEASE_ASSET`.

The built `.exe` can update itself automatically. Python/source mode checks the latest release and prints the download link, but does not replace source files.

Useful update options:

```powershell
.\dist\"Spotify Playlist Downloader.exe" --check-updates
.\dist\"Spotify Playlist Downloader.exe" --no-update-check
```

For long playlists, put `spotify_credentials.txt` next to the `.exe` using this format:

```text
SPOTIFY_CLIENT_ID=your_client_id
SPOTIFY_CLIENT_SECRET=your_client_secret
```

The app also accepts `spotify_credentials.json`, `spotify_credentials.env`, or a two-line txt file where line 1 is the client ID and line 2 is the client secret.

### Python Mode

```powershell
python playlist_downloader.py "https://open.spotify.com/playlist/PLAYLIST_ID" -o downloads
```

Useful options:

```powershell
python playlist_downloader.py "SPOTIFY_PLAYLIST_LINK" --dry-run
python playlist_downloader.py "SPOTIFY_PLAYLIST_LINK" --user-auth
python playlist_downloader.py "SPOTIFY_PLAYLIST_LINK" --user-auth --spotify-debug
python playlist_downloader.py "SPOTIFY_PLAYLIST_LINK" --limit 5
python playlist_downloader.py "SPOTIFY_PLAYLIST_LINK" --workers 5 --max-results 20
```

## Build The EXE

For playlists longer than Spotify's public-page limit, use either an external credentials file next to the `.exe` or bundle credentials into the `.exe` at build time.

To bundle credentials from a ready file, create `spotify_credentials.txt` in this folder:

```text
SPOTIFY_CLIENT_ID=your_client_id
SPOTIFY_CLIENT_SECRET=your_client_secret
```

Then build:

```powershell
powershell -ExecutionPolicy Bypass -File .\build_exe.ps1
```

The build script auto-detects `spotify_credentials.txt`, `spotify_credentials.json`, `spotify_credentials.env`, or `spotify_credentials`.

You can also pass a file explicitly:

```powershell
powershell -ExecutionPolicy Bypass -File .\build_exe.ps1 -CredentialFile .\spotify_credentials.txt
```

Or set Spotify app credentials before building:

```powershell
$env:SPOTIFY_CLIENT_ID="your_client_id"
$env:SPOTIFY_CLIENT_SECRET="your_client_secret"
```

Then build:

```powershell
powershell -ExecutionPolicy Bypass -File .\build_exe.ps1
```

Or use the helper. It uses `spotify_credentials.txt` automatically when present, otherwise it prompts for credentials locally:

```powershell
powershell -ExecutionPolicy Bypass -File .\build_exe_with_spotify_credentials.ps1
```

The result is:

```text
dist\Spotify Playlist Downloader.exe
```

## YouTube search and format errors

YouTube searches use the standard search page with bounded results. The app stops
optional searches after a strong title, artist, and duration match, spaces searches
two seconds apart, and pauses search workers for 30 seconds after a 403 or 429.
Service-side blocking can still occur; existing MP3s are skipped when rerunning.

For Python installs, update with `python -m pip install -U -r requirements.txt`.
YouTube format extraction requires the `yt-dlp[default]` challenge scripts plus
Deno >= 2.3 or Node >= 22. The build script requires and bundles both the scripts
and an installed runtime. Runtime warnings are shown to help diagnose failures.

## Spotify playlist authentication

The script reads public playlist metadata from Spotify's public web page first, so public playlists should work without browser login or Spotify API credentials.

Spotify's public page may expose only the first chunk of a long playlist. If the script detects that, it uses a credentials file, bundled credentials, or environment Spotify API credentials to request the remaining public playlist pages through Spotify's public web playlist operation. If no credentials are available, add `spotify_credentials.txt` next to the `.exe` or rebuild the exe with credentials bundled.

If Spotify returns `Valid user authentication required` after the public-page route fails, app-only credentials are not enough for that playlist through the official playlist API. Account login is only attempted when you explicitly run with `--user-auth`. The Spotify app must include this Redirect URI for `--user-auth`:

```text
http://127.0.0.1:8888/callback
```

Files are named like:

```text
Playlist Name/01 - Song - Artist.mp3
Playlist Name/02 - Song - Artist.mp3
```

### Download settings

Type `config` at the playlist URL prompt (or run `playlist_downloader.py config`).
Enter an option number to toggle it, then press Enter to save and return to the URL prompt.
Settings are saved in `config.json` beside the script/executable:

- Include album art: **true** by default. Uses Spotify covers when available; skips unavailable covers.
- Convert non-ASCII characters to ASCII: **false** by default. Applies to song filenames, playlist folder names, and metadata (for example, ü → u and ı → i).
- Create M3U playlist: **false** by default. Creates `Playlist Name.m3u` beside the `Playlist Name/` folder, with relative paths to successful downloads in playlist order. Dry runs do not create it.
- Sort by artist: **false** by default, preserving Spotify's custom playlist order. When enabled, sorts alphabetically by artist (case-insensitive), preserving the original order within the same artist. Filenames, track-number tags, and M3U entries follow the sorted order. Sorting happens before `--limit` is applied.

M3U files use UTF-8 with a BOM, CRLF line endings, backslash-separated relative paths,
and `#EXTENC`, `#PLAYLIST`, and `#EXTINF` entries for device compatibility.

Songs are saved beneath the selected output directory in a folder named after the playlist.
Version 1.4 checks that folder before downloading and skips nonempty MP3 files whose
title and artist match the downloader's filenames, even if their track numbers changed.
Both original and ASCII filenames are recognized. Only missing songs are downloaded;
empty files and partial downloads are retried. Existing files keep their filenames and
tags, and M3U playlists include their actual paths. The app reports how many songs are
already present and how many are missing.
Artwork is a 500 × 500 RGB baseline JPEG under 100,000 bytes, embedded as Front Cover
(APIC type 3, `image/jpeg`). Tags use ID3v2.3 with UTF-16 text. Updating tags preserves
the MP3 audio bytes; the existing YouTube-to-MP3 conversion still happens when needed.
The downloader prefers large Spotify artwork sources, tries the known 640px CDN variant
for thumbnail URLs, and caches prepared covers for up to 128 source URLs. If only a small
image is available, a warning reports its original dimensions before enlarging it.
Install the updated dependencies with `python -m pip install -r requirements.txt`.
Rebuild the executable to include these changes when using the packaged application.
