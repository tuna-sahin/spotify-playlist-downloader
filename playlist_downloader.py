#!/usr/bin/env python3
"""Download a Spotify playlist as numbered MP3 files from YouTube.

Use this only for audio that you own, have permission to download, or is
otherwise lawful to download in your location.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import dataclasses
import hashlib
import html
import json
import os
import re
import secrets
import shutil
import socket
import ssl
import struct
import subprocess
import string
import sys
import tempfile
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    import requests
except ImportError:
    requests = None  # type: ignore[assignment]

try:
    from yt_dlp import YoutubeDL
except ImportError:
    YoutubeDL = None  # type: ignore[assignment]


SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_PLAYLIST_URL = "https://api.spotify.com/v1/playlists/{playlist_id}/items"
SPOTIFY_PLAYLIST_DETAILS_URL = "https://api.spotify.com/v1/playlists/{playlist_id}"
SPOTIFY_ME_URL = "https://api.spotify.com/v1/me"
SPOTIFY_PRIVATE_PLAYLIST_SCOPE = "playlist-read-private playlist-read-collaborative"
SPOTIFY_PUBLIC_PLAYLIST_URL = "https://open.spotify.com/playlist/{playlist_id}?nd=1"
SPOTIFY_EMBED_PLAYLIST_URL = "https://open.spotify.com/embed/playlist/{playlist_id}"
SPOTIFY_PARTNER_QUERY_URL = "https://api-partner.spotify.com/pathfinder/v1/query"
SPOTIFY_QUERY_PLAYLIST_OPERATION = "queryPlaylist"
SPOTIFY_QUERY_PLAYLIST_HASH = (
    "908a5597b4d0af0489a9ad6a2d41bc3b416ff47c0884016d92bbd6822d0eb6d8"
)
APP_NAME = "spotify playlist downloader - Tuna"
APP_VERSION = "1.1.0"
GITHUB_REPOSITORY = "tuna-sahin/spotify-playlist-downloader"
GITHUB_RELEASE_ASSET = "Spotify Playlist Downloader.exe"
GITHUB_RELEASES_API = "https://api.github.com/repos/{repository}/releases/latest"
GITHUB_RELEASES_PAGE = "https://github.com/{repository}/releases/latest"

# Optional build-time fallback credentials for long playlists/private API fallback.
# Public playlists up to Spotify's public-page limit do not need these.
EMBEDDED_SPOTIFY_CLIENT_ID = ""
EMBEDDED_SPOTIFY_CLIENT_SECRET = ""
SPOTIFY_CREDENTIAL_FILENAMES = (
    "spotify_credentials.json",
    "spotify_credentials.txt",
    "spotify_credentials.env",
    "spotify_credentials",
)

PREFERRED_TERMS = (
    "lyric",
    "lyrics",
    "official audio",
    "audio",
    "topic",
    "provided to youtube",
    "visualizer",
)

DISCOURAGED_TERMS = (
    "official video",
    "music video",
    "video clip",
    "clip officiel",
    "official mv",
    "mv",
    "live",
    "concert",
    "cover",
    "karaoke",
    "reaction",
    "remix",
    "sped up",
    "slowed",
    "nightcore",
    "8d",
    "instrumental",
)

TITLE_NOISE = re.compile(
    r"\s*(?:\[[^\]]+\]|\([^\)]*(?:official|lyrics?|audio|video|visualizer|hd|4k)[^\)]*\))",
    re.IGNORECASE,
)
SAFE_CHARS = f"-_.() {string.ascii_letters}{string.digits}"
PRINT_LOCK = threading.Lock()


def app_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def bundle_dir() -> Path | None:
    bundle_path = getattr(sys, "_MEIPASS", None)
    if bundle_path:
        return Path(bundle_path)
    return None


def app_binary_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve()
    return Path(__file__).resolve()


def normalize_version(value: str) -> tuple[int, ...]:
    numbers = re.findall(r"\d+", value)
    return tuple(int(number) for number in numbers) or (0,)


def is_newer_version(candidate: str, current: str) -> bool:
    candidate_parts = normalize_version(candidate)
    current_parts = normalize_version(current)
    width = max(len(candidate_parts), len(current_parts))
    candidate_parts = candidate_parts + (0,) * (width - len(candidate_parts))
    current_parts = current_parts + (0,) * (width - len(current_parts))
    return candidate_parts > current_parts


def download_url(url: str, destination: Path) -> None:
    request = Request(url, headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"})
    with urlopen(request, timeout=60) as response:
        with destination.open("wb") as output:
            shutil.copyfileobj(response, output)


def asset_download_url(release: dict[str, Any], asset_name: str | None) -> tuple[str, str] | None:
    assets = release.get("assets") or []
    if not isinstance(assets, list):
        return None
    exe_assets = [
        asset
        for asset in assets
        if isinstance(asset, dict)
        and str(asset.get("name", "")).lower().endswith(".exe")
        and asset.get("browser_download_url")
    ]
    if asset_name:
        asset_name_lower = asset_name.lower()
        for asset in exe_assets:
            if str(asset.get("name", "")).lower() == asset_name_lower:
                return str(asset["browser_download_url"]), str(asset.get("name") or asset_name)
    if len(exe_assets) == 1:
        asset = exe_assets[0]
        return str(asset["browser_download_url"]), str(asset.get("name") or "update.exe")
    return None


def install_exe_update(downloaded_exe: Path, current_exe: Path) -> None:
    script_path = Path(tempfile.gettempdir()) / f"spotify-downloader-update-{os.getpid()}.ps1"
    script = f"""
$ErrorActionPreference = "Stop"
$source = {json.dumps(str(downloaded_exe))}
$target = {json.dumps(str(current_exe))}
$backup = "$target.old"
Start-Sleep -Seconds 2
for ($i = 0; $i -lt 30; $i++) {{
    try {{
        if (Test-Path -LiteralPath $backup) {{ Remove-Item -LiteralPath $backup -Force }}
        Move-Item -LiteralPath $target -Destination $backup -Force
        Move-Item -LiteralPath $source -Destination $target -Force
        Start-Process -FilePath $target
        if (Test-Path -LiteralPath $backup) {{ Remove-Item -LiteralPath $backup -Force }}
        break
    }}
    catch {{
        Start-Sleep -Seconds 1
    }}
}}
Remove-Item -LiteralPath $PSCommandPath -Force
"""
    script_path.write_text(script.strip() + "\n", encoding="utf-8")
    subprocess.Popen(  # noqa: S603 - local handoff script replaces the running exe.
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-WindowStyle",
            "Hidden",
            "-File",
            str(script_path),
        ],
        close_fds=True,
    )


def check_for_updates(auto_install: bool) -> bool:
    repository = GITHUB_REPOSITORY.strip()
    if not repository:
        log("Update check skipped: set GITHUB_REPOSITORY in the app before building.")
        return False

    log("Checked for updates.")
    api_url = GITHUB_RELEASES_API.format(repository=repository)
    request = Request(api_url, headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"})
    try:
        with urlopen(request, timeout=20) as response:
            release = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code == 404:
            log("No updates are required: no GitHub release has been published yet.")
            return False
        log(f"Update check failed: {exc}")
        return False
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        log(f"Update check failed: {exc}")
        return False

    latest_version = str(release.get("tag_name") or release.get("name") or "").strip()
    if not latest_version:
        log("Update check failed: GitHub release has no version tag.")
        return False
    if not is_newer_version(latest_version, APP_VERSION):
        log(f"No updates are required. Current version: {APP_VERSION}. Latest version: {latest_version}.")
        return False

    release_page = GITHUB_RELEASES_PAGE.format(repository=repository)
    log(f"Update is available: {APP_VERSION} -> {latest_version}")

    if not auto_install:
        log(f"Download it here: {release_page}")
        return False

    if not getattr(sys, "frozen", False):
        log("Automatic self-update only works in the built exe. Update from source instead.")
        log(f"Latest release: {release_page}")
        return False

    asset = asset_download_url(release, GITHUB_RELEASE_ASSET)
    if not asset:
        log(f"Automatic update could not find release asset: {GITHUB_RELEASE_ASSET}")
        log(f"Latest release: {release_page}")
        return False

    download_url_value, asset_name = asset
    destination = app_base_dir() / f"{asset_name}.download"
    try:
        log(f"Downloading update asset: {asset_name}")
        download_url(download_url_value, destination)
        install_exe_update(destination, app_binary_path())
    except Exception as exc:  # noqa: BLE001 - keep the current app usable.
        log(f"Automatic update failed: {exc}")
        log(f"Latest release: {release_page}")
        return False

    log("Update downloaded. The app will restart to finish installing it.")
    return True


def require_dependencies() -> None:
    missing = []
    if requests is None:
        missing.append("requests")
    if YoutubeDL is None:
        missing.append("yt-dlp")

    if missing:
        raise RuntimeError(
            "Missing Python package(s): "
            + ", ".join(missing)
            + ". Install them with: python -m pip install -r requirements.txt"
        )


def find_ffmpeg_location() -> str | None:
    explicit = os.getenv("FFMPEG_LOCATION")
    if explicit:
        path = Path(explicit).expanduser()
        if path.is_file():
            return str(path.parent)
        if path.is_dir():
            return str(path)

    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path:
        return str(Path(ffmpeg_path).parent)

    for base in (bundle_dir(), app_base_dir()):
        if not base:
            continue
        bundled_ffmpeg = base / "ffmpeg.exe"
        if bundled_ffmpeg.exists():
            return str(base)
        bundled_ffmpeg = base / "ffmpeg" / "ffmpeg.exe"
        if bundled_ffmpeg.exists():
            return str(bundled_ffmpeg.parent)

    winget_root = Path(os.getenv("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
    if winget_root.exists():
        matches = sorted(winget_root.glob("Gyan.FFmpeg*/*/bin/ffmpeg.exe"))
        if matches:
            return str(matches[-1].parent)

    return None


@dataclasses.dataclass(frozen=True)
class Track:
    index: int
    title: str
    artists: tuple[str, ...]
    album: str
    duration_seconds: int

    @property
    def artist_text(self) -> str:
        return ", ".join(self.artists)

    @property
    def query(self) -> str:
        return f"{self.artist_text} {self.title}"


@dataclasses.dataclass(frozen=True)
class Candidate:
    url: str
    title: str
    channel: str
    duration_seconds: int | None
    score: float


@dataclasses.dataclass(frozen=True)
class TokenInfo:
    access_token: str
    mode: str
    scope: str = ""


@dataclasses.dataclass(frozen=True)
class PlaylistMetadata:
    tracks: list[Track]
    total_count: int | None
    source: str


class SpotifyUserAuthRequired(RuntimeError):
    """Raised when Spotify requires an authorization-code user token."""


class SpotifyAppTokenRejected(RuntimeError):
    """Raised when Spotify rejects app-only credentials for a public-data request."""


def log(message: str) -> None:
    with PRINT_LOCK:
        encoding = sys.stdout.encoding or "utf-8"
        safe_message = message.encode(encoding, errors="replace").decode(encoding)
        print(safe_message, flush=True)


def parse_playlist_id(value: str) -> str:
    value = value.strip()
    markdown_url = re.search(r"\((https://open\.spotify\.com/playlist/[^\s)]+)\)", value)
    if markdown_url:
        value = markdown_url.group(1)
    else:
        plain_url = re.search(r"https://open\.spotify\.com/playlist/[^\s)]+", value)
        if plain_url:
            value = plain_url.group(0)

    if re.fullmatch(r"[A-Za-z0-9]{22}", value):
        return value

    parsed = urlparse(value)
    match = re.search(r"/playlist/([A-Za-z0-9]{22})", parsed.path)
    if match:
        return match.group(1)

    raise ValueError("Could not find a Spotify playlist ID in the supplied link.")


def spotify_credential_bases() -> list[Path]:
    bases = [app_base_dir()]
    bundled = bundle_dir()
    if bundled and bundled not in bases:
        bases.append(bundled)
    return bases


def clean_spotify_credential_value(value: Any) -> str:
    credential = str(value).strip()
    if (
        len(credential) >= 2
        and credential[0] == credential[-1]
        and credential[0] in {'"', "'"}
    ):
        credential = credential[1:-1].strip()
    return credential


def normalize_spotify_credential_key(key: str) -> str:
    key = key.strip()
    if key.lower().startswith("export "):
        key = key[7:].strip()
    return re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")


def spotify_credentials_from_mapping(mapping: dict[str, Any]) -> tuple[str, str] | None:
    normalized = {
        normalize_spotify_credential_key(str(key)): clean_spotify_credential_value(value)
        for key, value in mapping.items()
        if value is not None
    }
    client_id = next(
        (
            normalized[key]
            for key in (
                "spotify_client_id",
                "spotipy_client_id",
                "client_id",
                "spotify_id",
                "id",
            )
            if normalized.get(key)
        ),
        "",
    )
    client_secret = next(
        (
            normalized[key]
            for key in (
                "spotify_client_secret",
                "spotipy_client_secret",
                "client_secret",
                "spotify_secret",
                "secret",
            )
            if normalized.get(key)
        ),
        "",
    )
    if client_id and client_secret:
        return client_id, client_secret
    return None


def parse_spotify_credentials_text(text: str) -> tuple[str, str] | None:
    text = text.strip()
    if not text:
        return None

    if text.startswith("{"):
        data = json.loads(text)
        if isinstance(data, dict):
            return spotify_credentials_from_mapping(data)
        return None

    credential_map: dict[str, str] = {}
    bare_values: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue

        key = ""
        value = ""
        if "=" in line:
            key, value = line.split("=", 1)
        else:
            match = re.match(r"^([A-Za-z_][A-Za-z0-9_\-\s]*?):\s*(.+)$", line)
            if match:
                key, value = match.groups()

        if key:
            credential_map[normalize_spotify_credential_key(key)] = (
                clean_spotify_credential_value(value)
            )
        else:
            bare_value = clean_spotify_credential_value(line)
            if bare_value:
                bare_values.append(bare_value)

    mapped_credentials = spotify_credentials_from_mapping(credential_map)
    if mapped_credentials:
        return mapped_credentials

    if len(bare_values) >= 2:
        return bare_values[0], bare_values[1]

    return None


def read_spotify_credentials_file(path: Path) -> tuple[str, str] | None:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return None

    try:
        return parse_spotify_credentials_text(text)
    except (ValueError, TypeError):
        return None


def file_embedded_spotify_credentials() -> tuple[str, str] | None:
    explicit_path = os.getenv("SPOTIFY_CREDENTIAL_FILE") or os.getenv(
        "SPOTIFY_CREDENTIALS_FILE"
    )
    if explicit_path:
        credentials = read_spotify_credentials_file(Path(explicit_path).expanduser())
        if credentials:
            return credentials

    for base in spotify_credential_bases():
        for filename in SPOTIFY_CREDENTIAL_FILENAMES:
            path = base / filename
            if not path.exists():
                continue
            credentials = read_spotify_credentials_file(path)
            if credentials:
                return credentials
    return None


def spotify_credentials() -> tuple[str, str]:
    file_credentials = file_embedded_spotify_credentials()
    client_id = (
        os.getenv("SPOTIFY_CLIENT_ID")
        or os.getenv("SPOTIPY_CLIENT_ID")
        or (file_credentials[0] if file_credentials else "")
        or EMBEDDED_SPOTIFY_CLIENT_ID
    )
    client_secret = (
        os.getenv("SPOTIFY_CLIENT_SECRET")
        or os.getenv("SPOTIPY_CLIENT_SECRET")
        or (file_credentials[1] if file_credentials else "")
        or EMBEDDED_SPOTIFY_CLIENT_SECRET
    )

    if not client_id or not client_secret:
        raise RuntimeError(
            "Missing Spotify credentials. Add spotify_credentials.txt next to "
            "the exe/script, set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET, "
            "or rebuild the exe with bundled credentials."
        )

    return client_id, client_secret


def spotify_credentials_available() -> bool:
    file_credentials = file_embedded_spotify_credentials()
    return bool(
        (
            os.getenv("SPOTIFY_CLIENT_ID")
            or os.getenv("SPOTIPY_CLIENT_ID")
            or (file_credentials[0] if file_credentials else "")
            or EMBEDDED_SPOTIFY_CLIENT_ID
        )
        and (
            os.getenv("SPOTIFY_CLIENT_SECRET")
            or os.getenv("SPOTIPY_CLIENT_SECRET")
            or (file_credentials[1] if file_credentials else "")
            or EMBEDDED_SPOTIFY_CLIENT_SECRET
        )
    )


def spotify_auth_header(client_id: str, client_secret: str) -> str:
    raw = f"{client_id}:{client_secret}".encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def spotify_api_error(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text

    error = payload.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        reason = error.get("reason")
        return " - ".join(part for part in (message, reason) if part)
    if isinstance(error, str):
        return error
    return str(payload)


def spotify_client_credentials_token() -> TokenInfo:
    client_id, client_secret = spotify_credentials()
    response = requests.post(
        SPOTIFY_TOKEN_URL,
        headers={"Authorization": f"Basic {spotify_auth_header(client_id, client_secret)}"},
        data={"grant_type": "client_credentials"},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    return TokenInfo(access_token=payload["access_token"], mode="client_credentials")


def spotify_user_access_token(redirect_uri: str) -> TokenInfo:
    client_id, client_secret = spotify_credentials()
    state = secrets.token_urlsafe(24)
    query = urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": SPOTIFY_PRIVATE_PLAYLIST_SCOPE,
            "state": state,
        }
    )
    auth_url = f"{SPOTIFY_AUTHORIZE_URL}?{query}"

    log("\nSpotify account login required.")
    log("Open this URL, approve access, then paste the full redirected URL here:")
    log(auth_url)
    redirected_url = input("\nRedirected URL: ").strip()

    parsed = urlparse(redirected_url)
    values = parse_qs(parsed.query)
    returned_state = values.get("state", [""])[0]
    if returned_state != state:
        raise RuntimeError("Spotify login failed because the returned state did not match.")

    code = values.get("code", [""])[0]
    if not code:
        error = values.get("error", ["unknown_error"])[0]
        raise RuntimeError(f"Spotify login failed: {error}")

    response = requests.post(
        SPOTIFY_TOKEN_URL,
        headers={"Authorization": f"Basic {spotify_auth_header(client_id, client_secret)}"},
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        },
        timeout=30,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Spotify login failed: {spotify_api_error(response)}")

    payload = response.json()
    return TokenInfo(
        access_token=payload["access_token"],
        mode="user_auth",
        scope=payload.get("scope", ""),
    )


def spotify_get_json(
    url: str,
    token: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = requests.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        params=params,
        timeout=30,
    )
    if response.status_code == 401:
        raise SpotifyUserAuthRequired(
            "Spotify requires user authentication for this playlist API request. "
            "Client ID/secret credentials are present, but this endpoint also "
            "needs a logged-in Spotify user token."
        )
    if response.status_code >= 400:
        raise RuntimeError(
            f"Spotify API request failed ({response.status_code}) for {url}: "
            f"{spotify_api_error(response)}"
        )
    return response.json()


def spotify_debug_summary(playlist_id: str, token_info: TokenInfo) -> None:
    log("\nSpotify debug")
    log(f"Token mode: {token_info.mode}")
    if token_info.scope:
        log(f"Granted scopes: {token_info.scope}")

    if token_info.mode == "user_auth":
        try:
            me = spotify_get_json(SPOTIFY_ME_URL, token_info.access_token)
            log(
                "Logged in as: "
                f"{me.get('display_name') or '(no display name)'} "
                f"[{me.get('id')}]"
            )
        except RuntimeError as exc:
            log(f"Could not read /me: {exc}")

    try:
        playlist = spotify_get_json(
            SPOTIFY_PLAYLIST_DETAILS_URL.format(playlist_id=playlist_id),
            token_info.access_token,
            {
                "fields": (
                    "name,owner(id,display_name),public,collaborative,"
                    "tracks.total,tracks.href,tracks.items(track(name))"
                )
            },
        )
        owner = playlist.get("owner") or {}
        tracks = playlist.get("tracks") or {}
        log(f"Playlist: {playlist.get('name')}")
        log(f"Owner: {owner.get('display_name') or '(no display name)'} [{owner.get('id')}]")
        log(f"Public: {playlist.get('public')}")
        log(f"Collaborative: {playlist.get('collaborative')}")
        log(f"Track total: {tracks.get('total')}")
        log(f"Embedded track items: {len(tracks.get('items') or [])}")
        log("")
    except RuntimeError as exc:
        log(f"Could not read playlist details: {exc}\n")


def track_from_item(item: dict[str, Any], index: int) -> Track | None:
    track = item.get("track")
    if not track or track.get("type", "track") != "track" or track.get("is_local"):
        return None

    artists = tuple(artist["name"] for artist in track.get("artists", []) if artist.get("name"))
    if not artists:
        return None

    return Track(
        index=index,
        title=track["name"],
        artists=artists,
        album=track.get("album", {}).get("name", ""),
        duration_seconds=round(track["duration_ms"] / 1000),
    )


def track_from_public_item(item: dict[str, Any], index: int) -> Track | None:
    try:
        track = item["itemV2"]["data"]
        if track.get("__typename") != "Track":
            return None

        artists = tuple(
            artist["profile"]["name"]
            for artist in track["artists"]["items"]
            if artist.get("profile", {}).get("name")
        )
        if not artists:
            return None

        return Track(
            index=index,
            title=track["name"],
            artists=artists,
            album=track.get("albumOfTrack", {}).get("name", ""),
            duration_seconds=round(track["duration"]["totalMilliseconds"] / 1000),
        )
    except (KeyError, TypeError):
        return None


def fetch_metadata_from_public_page(playlist_id: str) -> PlaylistMetadata:
    url = SPOTIFY_PUBLIC_PLAYLIST_URL.format(playlist_id=playlist_id)
    response = requests.get(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
                "Mobile/15E148 Safari/604.1"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
        timeout=30,
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"Spotify public page request failed ({response.status_code}): "
            f"{response.text[:300]}"
        )

    match = re.search(
        r'<script id="initialState" type="text/plain">(.*?)</script>',
        response.text,
        re.DOTALL,
    )
    if not match:
        raise RuntimeError("Spotify public page did not include the initialState block.")

    encoded_state = html.unescape(match.group(1).strip())
    padding = "=" * (-len(encoded_state) % 4)
    state = json.loads(base64.b64decode(encoded_state + padding))
    playlist = state.get("entities", {}).get("items", {}).get(f"spotify:playlist:{playlist_id}")
    if not playlist:
        raise RuntimeError("Spotify public page did not include the playlist payload.")

    content = playlist.get("content", {})
    items = content.get("items", [])
    total_count = content.get("totalCount")
    tracks: list[Track] = []
    for item in items:
        track = track_from_public_item(item, len(tracks) + 1)
        if track:
            tracks.append(track)

    return PlaylistMetadata(
        tracks=tracks,
        total_count=total_count if isinstance(total_count, int) else None,
        source="public page",
    )


def fetch_tracks_from_public_page(playlist_id: str) -> list[Track]:
    return fetch_metadata_from_public_page(playlist_id).tracks


def track_from_embed_item(item: dict[str, Any], index: int) -> Track | None:
    if item.get("entityType") != "track":
        return None

    title = item.get("title")
    artist_text = item.get("subtitle")
    duration_ms = item.get("duration")
    if not title or not artist_text or not isinstance(duration_ms, int):
        return None

    artists = tuple(part.strip() for part in str(artist_text).split(",") if part.strip())
    if not artists:
        return None

    return Track(
        index=index,
        title=str(title),
        artists=artists,
        album="",
        duration_seconds=round(duration_ms / 1000),
    )


def fetch_metadata_from_embed_page(playlist_id: str) -> PlaylistMetadata:
    url = SPOTIFY_EMBED_PLAYLIST_URL.format(playlist_id=playlist_id)
    response = requests.get(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/128.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
        timeout=30,
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"Spotify embed page request failed ({response.status_code}): "
            f"{response.text[:300]}"
        )

    match = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        response.text,
        re.DOTALL,
    )
    if not match:
        raise RuntimeError("Spotify embed page did not include the __NEXT_DATA__ block.")

    data = json.loads(html.unescape(match.group(1)))
    entity = (
        data.get("props", {})
        .get("pageProps", {})
        .get("state", {})
        .get("data", {})
        .get("entity", {})
    )
    track_list = entity.get("trackList", [])
    if not isinstance(track_list, list):
        raise RuntimeError("Spotify embed page did not include a track list.")

    tracks: list[Track] = []
    for item in track_list:
        if isinstance(item, dict):
            track = track_from_embed_item(item, len(tracks) + 1)
            if track:
                tracks.append(track)

    return PlaylistMetadata(
        tracks=tracks,
        total_count=None,
        source="embed page",
    )


def find_chrome_executable() -> str | None:
    candidates = [
        os.getenv("CHROME_PATH"),
        shutil.which("chrome"),
        shutil.which("chrome.exe"),
        shutil.which("msedge"),
        shutil.which("msedge.exe"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


def http_json(url: str, timeout: float = 5) -> Any:
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


class MinimalWebSocket:
    def __init__(self, url: str, timeout: float = 10) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in {"ws", "wss"}:
            raise ValueError(f"Unsupported WebSocket URL: {url}")
        self.host = parsed.hostname or ""
        self.port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        self.path = parsed.path or "/"
        if parsed.query:
            self.path += f"?{parsed.query}"
        raw_socket = socket.create_connection((self.host, self.port), timeout=timeout)
        if parsed.scheme == "wss":
            self.sock = ssl.create_default_context().wrap_socket(raw_socket, server_hostname=self.host)
        else:
            self.sock = raw_socket
        self.sock.settimeout(timeout)
        self._handshake()

    def _handshake(self) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(request.encode("ascii"))
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            response += chunk
        if b" 101 " not in response.split(b"\r\n", 1)[0]:
            raise RuntimeError("Chrome DevTools WebSocket handshake failed.")

    def send_text(self, message: str) -> None:
        payload = message.encode("utf-8")
        header = bytearray([0x81])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.extend([0x80 | 126, *struct.pack("!H", length)])
        else:
            header.extend([0x80 | 127, *struct.pack("!Q", length)])
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(bytes(header) + mask + masked)

    def recv_text(self) -> str:
        while True:
            first = self.sock.recv(2)
            if len(first) < 2:
                raise RuntimeError("Chrome DevTools WebSocket closed.")
            opcode = first[0] & 0x0F
            length = first[1] & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._recv_exact(8))[0]
            masked = bool(first[1] & 0x80)
            mask = self._recv_exact(4) if masked else b""
            payload = self._recv_exact(length)
            if masked:
                payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
            if opcode == 0x8:
                raise RuntimeError("Chrome DevTools WebSocket closed.")
            if opcode == 0x9:
                self._send_pong(payload)
                continue
            if opcode == 0x1:
                return payload.decode("utf-8")

    def _send_pong(self, payload: bytes) -> None:
        header = bytearray([0x8A, 0x80 | len(payload)])
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(bytes(header) + mask + masked)

    def _recv_exact(self, length: int) -> bytes:
        data = b""
        while len(data) < length:
            chunk = self.sock.recv(length - len(data))
            if not chunk:
                raise RuntimeError("Chrome DevTools WebSocket closed.")
            data += chunk
        return data

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class ChromeDevTools:
    def __init__(self, websocket_url: str) -> None:
        self.ws = MinimalWebSocket(websocket_url)
        self.next_id = 0

    def call(self, method: str, params: dict[str, Any] | None = None, timeout: float = 20) -> Any:
        self.next_id += 1
        message_id = self.next_id
        self.ws.sock.settimeout(timeout)
        self.ws.send_text(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        while True:
            message = json.loads(self.ws.recv_text())
            if message.get("id") != message_id:
                continue
            if "error" in message:
                raise RuntimeError(f"Chrome DevTools error for {method}: {message['error']}")
            return message.get("result")

    def close(self) -> None:
        self.ws.close()


def wait_for_chrome_target(port: int, playlist_id: str, timeout_seconds: float = 15) -> str:
    deadline = time.time() + timeout_seconds
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            targets = http_json(f"http://127.0.0.1:{port}/json/list", timeout=1)
            for target in targets:
                if playlist_id in target.get("url", "") and target.get("webSocketDebuggerUrl"):
                    return str(target["webSocketDebuggerUrl"])
            for target in targets:
                if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
                    return str(target["webSocketDebuggerUrl"])
        except Exception as exc:  # noqa: BLE001 - Chrome may not be ready yet.
            last_error = exc
        time.sleep(0.25)
    raise RuntimeError(f"Chrome DevTools target was not ready: {last_error}")


def rendered_row_to_track(row: dict[str, Any]) -> Track | None:
    index = row.get("index")
    title = row.get("title")
    artists = row.get("artists")
    duration_text = row.get("duration")
    if not isinstance(index, int) or not title or not isinstance(artists, list):
        return None
    if not artists:
        return None

    duration_seconds = 0
    if isinstance(duration_text, str):
        parts = duration_text.split(":")
        if len(parts) == 2 and all(part.isdigit() for part in parts):
            duration_seconds = int(parts[0]) * 60 + int(parts[1])
        elif len(parts) == 3 and all(part.isdigit() for part in parts):
            duration_seconds = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])

    return Track(
        index=index,
        title=str(title),
        artists=tuple(str(artist) for artist in artists if artist),
        album=str(row.get("album") or ""),
        duration_seconds=duration_seconds,
    )


def fetch_metadata_from_rendered_spotify_page(
    playlist_id: str,
    expected_total: int | None,
) -> PlaylistMetadata:
    chrome_path = find_chrome_executable()
    if not chrome_path:
        raise RuntimeError("Chrome or Edge was not found for rendered Spotify fallback.")

    with tempfile.TemporaryDirectory(prefix="spotify-render-") as profile_dir:
        playlist_url = SPOTIFY_PUBLIC_PLAYLIST_URL.format(playlist_id=playlist_id)
        port = 9222 + (os.getpid() % 1000)
        process = subprocess.Popen(
            [
                chrome_path,
                "--headless=new",
                "--disable-gpu",
                "--disable-extensions",
                "--mute-audio",
                "--no-first-run",
                "--no-default-browser-check",
                "--lang=en-US",
                "--window-size=1280,900",
                f"--remote-debugging-port={port}",
                f"--user-data-dir={profile_dir}",
                playlist_url,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        client: ChromeDevTools | None = None
        try:
            websocket_url = wait_for_chrome_target(port, playlist_id)
            client = ChromeDevTools(websocket_url)
            client.call("Page.enable")
            client.call("Runtime.enable")
            time.sleep(4)
            expression = r"""
                (async () => {
                    const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
                    const findScroller = () => [...document.querySelectorAll('*')]
                        .filter((element) => element.scrollHeight > element.clientHeight + 1000)
                        .sort((left, right) => (right.scrollHeight - right.clientHeight) - (left.scrollHeight - left.clientHeight))[0];
                    const rowData = (row) => {
                        const cells = [...row.querySelectorAll('[role="gridcell"], [role="cell"]')];
                        const number = Number((cells[0]?.textContent || '').trim());
                        const titleLink = row.querySelector('a[data-testid="internal-track-link"], a[href^="/track/"], a[href*="/track/"]');
                        const artistLinks = [...row.querySelectorAll('a[href^="/artist/"], a[href*="/artist/"]')];
                        const albumLink = row.querySelector('a[href^="/album/"], a[href*="/album/"]');
                        const duration = (cells[cells.length - 1]?.textContent || '').match(/\d{1,2}:\d{2}(?=$|[^\d])/);
                        if (!number || !titleLink || !artistLinks.length) return null;
                        return {
                            index: number,
                            title: (titleLink.textContent || '').trim(),
                            artists: artistLinks.map((link) => (link.textContent || '').trim()).filter(Boolean),
                            album: albumLink ? (albumLink.textContent || '').trim() : '',
                            duration: duration ? duration[0] : '',
                        };
                    };
                    const scroller = findScroller();
                    if (!scroller) {
                        return { error: 'No Spotify scroll container found.', rows: [] };
                    }
                    const seen = {};
                    const readRows = () => [...document.querySelectorAll('[role="row"]')]
                        .map(rowData)
                        .filter(Boolean);
                    scroller.scrollTo(0, 0);
                    await sleep(500);
                    const maxSteps = 80;
                    let stableSteps = 0;
                    let previousY = -1;
                    for (let index = 0; index < maxSteps; index += 1) {
                        for (const row of readRows()) {
                            seen[row.index] = row;
                        }
                        if (scroller.scrollTop === previousY) {
                            stableSteps += 1;
                        } else {
                            stableSteps = 0;
                            previousY = scroller.scrollTop;
                        }
                        if (stableSteps >= 5) break;
                        scroller.scrollTo(0, Math.min(scroller.scrollTop + 350, scroller.scrollHeight));
                        await sleep(120);
                    }
                    for (const row of readRows()) seen[row.index] = row;
                    return {
                        rows: Object.entries(seen)
                            .map(([, row]) => row)
                            .sort((left, right) => left.index - right.index),
                        scrollHeight: scroller.scrollHeight,
                    };
                })()
            """
            result = client.call(
                "Runtime.evaluate",
                {"expression": expression, "awaitPromise": True, "returnByValue": True},
                timeout=30,
            )
            value = result.get("result", {}).get("value", {})
            if value.get("error"):
                raise RuntimeError(str(value["error"]))
            rows = value.get("rows", [])
            tracks: list[Track] = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                track = rendered_row_to_track(row)
                if track:
                    tracks.append(track)
            if expected_total is not None and len(tracks) < expected_total:
                raise RuntimeError(
                    "Rendered Spotify page only exposed "
                    f"{len(tracks)} of {expected_total} tracks."
                )
            return PlaylistMetadata(tracks=tracks, total_count=len(tracks), source="rendered web page")
        finally:
            if client:
                client.close()
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()


def fetch_partner_playlist_page(
    playlist_id: str,
    token: str,
    offset: int,
    limit: int = 30,
) -> PlaylistMetadata:
    response = requests.post(
        SPOTIFY_PARTNER_QUERY_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/128.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
            "Content-Type": "application/json",
            "app-platform": "WebPlayer",
        },
        json={
            "operationName": SPOTIFY_QUERY_PLAYLIST_OPERATION,
            "variables": {
                "uri": f"spotify:playlist:{playlist_id}",
                "limit": limit,
                "offset": offset,
            },
            "extensions": {
                "persistedQuery": {
                    "version": 1,
                    "sha256Hash": SPOTIFY_QUERY_PLAYLIST_HASH,
                }
            },
        },
        timeout=30,
    )
    if response.status_code == 401:
        raise SpotifyAppTokenRejected(
            "Spotify rejected the app-only token for its public playlist "
            "pagination endpoint."
        )
    if response.status_code >= 400:
        raise RuntimeError(
            f"Spotify public playlist pagination failed ({response.status_code}): "
            f"{spotify_api_error(response)}"
        )

    payload = response.json()
    playlist = payload.get("data", {}).get("playlistV2")
    if not isinstance(playlist, dict):
        raise RuntimeError("Spotify public playlist pagination returned no playlist data.")

    if playlist.get("__typename") == "NotFound":
        raise RuntimeError("Spotify public playlist pagination could not find this playlist.")
    if playlist.get("__typename") == "GenericError":
        raise RuntimeError(
            "Spotify public playlist pagination failed: "
            f"{playlist.get('message') or 'unknown error'}"
        )

    content = playlist.get("content", {})
    items = content.get("items", [])
    tracks: list[Track] = []
    for item in items:
        track = track_from_public_item(item, offset + len(tracks) + 1)
        if track:
            tracks.append(track)

    return PlaylistMetadata(
        tracks=tracks,
        total_count=content.get("totalCount") if isinstance(content.get("totalCount"), int) else None,
        source="public web API",
    )


def fetch_remaining_public_tracks_with_app_token(
    playlist_id: str,
    token: str,
    initial_metadata: PlaylistMetadata,
) -> list[Track]:
    tracks = list(initial_metadata.tracks)
    total_count = initial_metadata.total_count
    if total_count is None:
        return tracks

    offset = len(tracks)
    while offset < total_count:
        page = fetch_partner_playlist_page(playlist_id, token, offset)
        if not page.tracks:
            raise RuntimeError(
                "Spotify public playlist pagination stopped before returning "
                f"all tracks ({len(tracks)} of {total_count})."
            )
        tracks.extend(page.tracks)
        offset = len(tracks)

    return tracks


def fetch_tracks_from_items_endpoint(playlist_id: str, token: str) -> list[Track]:
    fields = (
        "items(track(type,name,duration_ms,album(name),artists(name),is_local)),"
        "next,total"
    )
    params = {"limit": 100, "fields": fields}
    headers = {"Authorization": f"Bearer {token}"}
    url = SPOTIFY_PLAYLIST_URL.format(playlist_id=playlist_id)

    tracks: list[Track] = []
    while url:
        response = requests.get(url, headers=headers, params=params, timeout=30)
        if response.status_code == 401:
            raise SpotifyUserAuthRequired(
                "Spotify requires user authentication for this playlist API request. "
                "Client ID/secret credentials are present, but this endpoint also "
                "needs a logged-in Spotify user token."
            )
        if response.status_code == 403:
            raise PermissionError(
                "Spotify refused access to this playlist's items. Spotify's "
                "current playlist-items API only exposes playlist contents when "
                "the logged-in account owns the playlist or is a collaborator. "
                "A playlist can still be visible publicly in the Spotify app and "
                "return 403 here. Copy/duplicate the playlist into your own "
                "Spotify account, then run this script with the new playlist link "
                "and --user-auth. "
                f"Spotify said: {spotify_api_error(response)}"
            )
        if response.status_code >= 400:
            raise RuntimeError(
                f"Spotify API request failed ({response.status_code}): "
                f"{spotify_api_error(response)}"
            )
        data = response.json()
        params = None

        for item in data.get("items", []):
            track = track_from_item(item, len(tracks) + 1)
            if track:
                tracks.append(track)

        url = data.get("next")

    return tracks


def fetch_tracks_from_playlist_details(playlist_id: str, token: str) -> list[Track]:
    fields = (
        "tracks.total,tracks.next,tracks.items(track(type,name,duration_ms,"
        "album(name),artists(name),is_local))"
    )
    params = {"fields": fields}
    playlist = spotify_get_json(
        SPOTIFY_PLAYLIST_DETAILS_URL.format(playlist_id=playlist_id),
        token,
        params,
    )
    tracks_page = playlist.get("tracks") or {}
    tracks: list[Track] = []

    for item in tracks_page.get("items", []):
        track = track_from_item(item, len(tracks) + 1)
        if track:
            tracks.append(track)

    next_url = tracks_page.get("next")
    while next_url:
        page = spotify_get_json(next_url, token)
        for item in page.get("items", []):
            track = track_from_item(item, len(tracks) + 1)
            if track:
                tracks.append(track)
        next_url = page.get("next")

    return tracks


def fetch_playlist_tracks(playlist_id: str, token: str) -> list[Track]:
    tracks = fetch_tracks_from_playlist_details(playlist_id, token)
    if not tracks:
        tracks = fetch_tracks_from_items_endpoint(playlist_id, token)

    if not tracks:
        raise RuntimeError(
            "Spotify returned no track metadata for this playlist, so there is "
            "nothing to search on YouTube. If the playlist definitely has songs, "
            "run with --spotify-debug and send the debug output."
        )

    return tracks


def normalize_text(value: str) -> str:
    value = TITLE_NOISE.sub(" ", value)
    value = re.sub(r"[^\w\s'-]", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip().lower()


def ascii_fallback(value: str) -> str:
    return (
        unicodedata.normalize("NFKD", value)
        .encode("ascii", "ignore")
        .decode("ascii")
    )


def simplified_title(title: str) -> str:
    title = re.sub(
        r"\s*[-–—]\s*(?:unplugged|live|remaster(?:ed)?|radio edit|acoustic|from ).*$",
        "",
        title,
        flags=re.IGNORECASE,
    )
    title = re.sub(
        r"\s*\((?:unplugged|live|remaster(?:ed)?|radio edit|acoustic|from )[^\)]*\)",
        "",
        title,
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", title).strip() or title


def youtube_queries(track: Track) -> list[str]:
    primary_artist = track.artists[0]
    title = track.title
    short_title = simplified_title(title)
    raw_queries = [
        f"{track.artist_text} {title}",
        f"{primary_artist} {title}",
        f"{track.artist_text} {short_title}",
        f"{primary_artist} {short_title}",
        f"{primary_artist} {short_title} audio",
        f"{primary_artist} {short_title} lyrics",
        f"{track.artist_text} {title} official audio",
    ]

    queries: list[str] = []
    seen: set[str] = set()
    for query in raw_queries:
        for candidate in (query, ascii_fallback(query)):
            candidate = re.sub(r"\s+", " ", candidate).strip()
            key = candidate.lower()
            if candidate and key not in seen:
                seen.add(key)
                queries.append(candidate)
    return queries


def sanitize_filename(value: str, max_length: int = 160) -> str:
    cleaned = "".join(char if char in SAFE_CHARS else "_" for char in value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ._")
    cleaned = re.sub(r"_+", "_", cleaned)
    return cleaned[:max_length].rstrip(" ._") or "track"


def duration_score(candidate_duration: int | None, target_duration: int) -> float:
    if not candidate_duration:
        return 0

    diff = abs(candidate_duration - target_duration)
    if diff <= 2:
        return 45
    if diff <= 5:
        return 36
    if diff <= 10:
        return 24
    if diff <= 20:
        return 10
    return -min(60, diff * 1.8)


def score_candidate(entry: dict[str, Any], track: Track) -> float:
    raw_title = entry.get("title") or ""
    channel = entry.get("channel") or entry.get("uploader") or ""
    text = f"{raw_title} {channel}".lower()
    normalized = normalize_text(text)
    score = duration_score(entry.get("duration"), track.duration_seconds)

    title_norm = normalize_text(track.title)
    if title_norm and title_norm in normalized:
        score += 24
    else:
        for word in title_norm.split():
            if len(word) > 2 and word in normalized:
                score += 2

    for artist in track.artists:
        artist_norm = normalize_text(artist)
        if artist_norm and artist_norm in normalized:
            score += 12
            break

    if "topic" in channel.lower():
        score += 18
    for term in PREFERRED_TERMS:
        if term in text:
            score += 12
    for term in DISCOURAGED_TERMS:
        if term in text:
            score -= 22

    if entry.get("duration") and entry["duration"] > track.duration_seconds + 45:
        score -= 35

    view_count = entry.get("view_count") or 0
    if view_count:
        score += min(10, view_count / 10_000_000)

    return score


def entry_to_candidate(entry: dict[str, Any], track: Track) -> Candidate:
    video_id = entry.get("id")
    webpage_url = entry.get("webpage_url")
    url = webpage_url or f"https://www.youtube.com/watch?v={video_id}"
    return Candidate(
        url=url,
        title=entry.get("title") or "Untitled",
        channel=entry.get("channel") or entry.get("uploader") or "Unknown channel",
        duration_seconds=entry.get("duration"),
        score=score_candidate(entry, track),
    )


def search_youtube_candidates(track: Track, max_results: int) -> list[Candidate]:
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
    }

    entries_by_id: dict[str, dict[str, Any]] = {}
    per_query = max(3, min(max_results, 8))
    with YoutubeDL(ydl_opts) as ydl:
        for query in youtube_queries(track):
            result = ydl.extract_info(f"ytsearch{per_query}:{query}", download=False)
            for entry in result.get("entries", []) or []:
                if not entry:
                    continue
                key = entry.get("id") or entry.get("url") or entry.get("title")
                if key and key not in entries_by_id:
                    entries_by_id[key] = entry

    entries = list(entries_by_id.values())
    if not entries:
        raise RuntimeError("no YouTube results")

    ranked = sorted(
        entries,
        key=lambda entry: score_candidate(entry, track),
        reverse=True,
    )
    candidates = [entry_to_candidate(entry, track) for entry in ranked[:max_results]]
    best_score = candidates[0].score
    close_candidates = [
        candidate for candidate in candidates if candidate.score >= best_score - 30
    ]
    return close_candidates or candidates[:1]


def search_youtube(track: Track, max_results: int) -> Candidate:
    return search_youtube_candidates(track, max_results=max_results)[0]


def output_template(output_dir: Path, track: Track, width: int) -> str:
    number = str(track.index).zfill(width)
    base = sanitize_filename(f"{number} - {track.artist_text} - {track.title}")
    return str(output_dir / f"{base}.%(ext)s")


def download_track(
    track: Track,
    output_dir: Path,
    width: int,
    max_results: int,
    bitrate: str,
    ffmpeg_location: str | None,
    dry_run: bool,
) -> tuple[int, bool, str]:
    try:
        candidates = search_youtube_candidates(track, max_results=max_results)

        if dry_run:
            candidate = candidates[0]
            candidate_duration = (
                f"{candidate.duration_seconds}s" if candidate.duration_seconds else "unknown"
            )
            log(
                f"{str(track.index).zfill(width)}. {track.artist_text} - {track.title} "
                f"-> {candidate.title} [{candidate.channel}, {candidate_duration}, "
                f"target {track.duration_seconds}s, score {candidate.score:.1f}]"
            )
            return track.index, True, "dry run"

        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": output_template(output_dir, track, width),
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "retries": 5,
            "fragment_retries": 5,
            "concurrent_fragment_downloads": 4,
            "ffmpeg_location": ffmpeg_location,
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": bitrate,
                },
                {"key": "FFmpegMetadata"},
            ],
            "postprocessor_args": [
                "-metadata",
                f"title={track.title}",
                "-metadata",
                f"artist={track.artist_text}",
                "-metadata",
                f"album={track.album}",
                "-metadata",
                f"track={track.index}",
            ],
        }

        failures: list[str] = []
        for attempt, candidate in enumerate(candidates[:2], start=1):
            candidate_duration = (
                f"{candidate.duration_seconds}s" if candidate.duration_seconds else "unknown"
            )
            prefix = "alternative " if attempt == 2 else ""
            log(
                f"{str(track.index).zfill(width)}. {track.artist_text} - {track.title} "
                f"-> {prefix}{candidate.title} [{candidate.channel}, {candidate_duration}, "
                f"target {track.duration_seconds}s, score {candidate.score:.1f}]"
            )
            try:
                with YoutubeDL(ydl_opts) as ydl:
                    ydl.download([candidate.url])
                return track.index, True, "downloaded"
            except Exception as exc:  # noqa: BLE001 - try a close alternative once.
                failures.append(f"{candidate.title}: {exc}")
                if attempt == 1 and len(candidates) > 1:
                    log(
                        f"{str(track.index).zfill(width)}. First match failed; "
                        "trying a close alternative."
                    )

        return track.index, False, " | ".join(failures) or "download failed"
    except Exception as exc:  # noqa: BLE001 - CLI should report every failed track.
        return track.index, False, str(exc)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download Spotify playlist tracks from YouTube as ordered MP3s."
    )
    parser.add_argument(
        "playlist",
        nargs="?",
        help="Spotify playlist URL or playlist ID. If omitted, you will be prompted.",
    )
    parser.add_argument(
        "-o",
        "--output",
        default=None,
        help="Output folder. Default: the folder containing this script/exe.",
    )
    parser.add_argument(
        "-w",
        "--workers",
        type=int,
        default=3,
        help="Parallel downloads. Default: 3",
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=12,
        help="YouTube search results to score per song. Default: 12",
    )
    parser.add_argument(
        "--bitrate",
        default="192",
        help="MP3 bitrate passed to ffmpeg/yt-dlp. Default: 192",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Download only the first N tracks, useful for testing.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Search and score matches without downloading.",
    )
    parser.add_argument(
        "--user-auth",
        action="store_true",
        help="Log in with your Spotify account. Needed for private playlists.",
    )
    parser.add_argument(
        "--redirect-uri",
        default=os.getenv("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback"),
        help=(
            "Spotify redirect URI for --user-auth. It must be added to your "
            "Spotify developer app. Default: http://127.0.0.1:8888/callback"
        ),
    )
    parser.add_argument(
        "--spotify-debug",
        action="store_true",
        help="Print Spotify account and playlist diagnostics before downloading.",
    )
    parser.add_argument(
        "--no-update-check",
        action="store_true",
        help="Skip the GitHub release update check at startup.",
    )
    parser.add_argument(
        "--check-updates",
        action="store_true",
        help="Check GitHub releases for an update and exit.",
    )
    return parser


def prompt_for_missing_inputs(args: argparse.Namespace) -> None:
    while not args.playlist:
        value = input("Paste the Spotify playlist URL: ").strip()
        if value:
            args.playlist = value
        else:
            log("Please enter a Spotify playlist URL.")

    if args.output is None:
        default_output = app_base_dir()
        value = input(f"Output folder [{default_output}]: ").strip().strip('"')
        args.output = value or str(default_output)


def main(argv: list[str] | None = None) -> int:
    raw_argv = sys.argv[1:] if argv is None else argv
    interactive = len(raw_argv) == 0
    args = build_parser().parse_args(raw_argv)

    log(APP_NAME)
    log(f"Version {APP_VERSION}")
    log("")

    if args.check_updates:
        check_for_updates(auto_install=True)
        return 0

    if not args.no_update_check:
        update_started = check_for_updates(auto_install=True)
        if update_started and getattr(sys, "frozen", False):
            return 0

    if interactive:
        prompt_for_missing_inputs(args)
    elif not args.playlist:
        raise ValueError("Please provide a Spotify playlist URL.")

    require_dependencies()
    playlist_id = parse_playlist_id(args.playlist)
    output_dir = Path(args.output or app_base_dir()).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    log("Fetching Spotify playlist...")
    tracks: list[Track] = []
    playlist_total_count: int | None = None
    public_metadata: PlaylistMetadata | None = None
    public_error: Exception | None = None

    if not args.user_auth:
        try:
            public_metadata = fetch_metadata_from_public_page(playlist_id)
            tracks = public_metadata.tracks
            playlist_total_count = public_metadata.total_count
            if args.spotify_debug:
                total_text = (
                    f" of {playlist_total_count}"
                    if playlist_total_count is not None
                    else ""
                )
                log(f"Spotify metadata source: public page ({len(tracks)}{total_text} tracks)")
        except Exception as exc:  # noqa: BLE001 - fall back to API and report if needed.
            public_error = exc
            if args.spotify_debug:
                log(f"Spotify public page fetch failed: {exc}")

    public_page_was_partial = (
        playlist_total_count is not None and len(tracks) < playlist_total_count
    )
    if public_page_was_partial:
        try:
            embed_metadata = fetch_metadata_from_embed_page(playlist_id)
            if len(embed_metadata.tracks) > len(tracks):
                tracks = embed_metadata.tracks
                if args.spotify_debug:
                    log(
                        "Spotify metadata source: embed page "
                        f"({len(tracks)} of {playlist_total_count} tracks)"
                    )
        except Exception as exc:  # noqa: BLE001 - continue to API fallback.
            if args.spotify_debug:
                log(f"Spotify embed page fetch failed: {exc}")

    public_page_was_partial = (
        playlist_total_count is not None and len(tracks) < playlist_total_count
    )
    if public_page_was_partial:
        try:
            rendered_metadata = fetch_metadata_from_rendered_spotify_page(
                playlist_id,
                playlist_total_count,
            )
            if len(rendered_metadata.tracks) > len(tracks):
                tracks = rendered_metadata.tracks
                playlist_total_count = rendered_metadata.total_count or playlist_total_count
                if args.spotify_debug:
                    log(
                        "Spotify metadata source: rendered web page "
                        f"({len(tracks)} tracks)"
                    )
        except Exception as exc:  # noqa: BLE001 - continue to API fallback.
            if args.spotify_debug:
                log(f"Spotify rendered web page fetch failed: {exc}")

    public_page_was_partial = (
        playlist_total_count is not None and len(tracks) < playlist_total_count
    )
    if public_page_was_partial:
        if spotify_credentials_available():
            if args.spotify_debug:
                log(
                    "Spotify public sources only exposed "
                    f"{len(tracks)} of {playlist_total_count} tracks; "
                    "using app-token public web pagination."
                )
            try:
                token_info = spotify_client_credentials_token()
                if args.spotify_debug:
                    log(f"Token mode: {token_info.mode}")
                tracks = fetch_remaining_public_tracks_with_app_token(
                    playlist_id,
                    token_info.access_token,
                    PlaylistMetadata(tracks, playlist_total_count, "public sources"),
                )
            except SpotifyAppTokenRejected as exc:
                raise RuntimeError(
                    f"{exc}\n\n"
                    "Spotify accepted the client ID/secret token, but would not "
                    "allow that token to page through the public web playlist. "
                    f"The public page/embed routes exposed {len(tracks)} of "
                    f"{playlist_total_count} tracks. This playlist cannot "
                    "currently be expanded completely with app-only credentials "
                    "on this machine."
                ) from exc
            playlist_total_count = len(tracks)
        else:
            raise RuntimeError(
                "Spotify's public page only exposed "
                f"{len(tracks)} of {playlist_total_count} tracks. To download "
                "playlists longer than that, add spotify_credentials.txt next to "
                "the exe/script, set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET, "
                "or rebuild the exe with bundled credentials."
            )

    if not tracks:
        if args.user_auth:
            token_info = spotify_user_access_token(args.redirect_uri)
        elif spotify_credentials_available():
            token_info = spotify_client_credentials_token()
        else:
            raise RuntimeError(
                "Could not read tracks from Spotify's public page, and no Spotify "
                "API credentials are set for fallback. Add spotify_credentials.txt "
                "next to the exe/script, set SPOTIFY_CLIENT_ID and "
                "SPOTIFY_CLIENT_SECRET, or rebuild the exe with bundled "
                "credentials.\n"
                f"Public page error: {public_error}"
            )

        if args.spotify_debug:
            spotify_debug_summary(playlist_id, token_info)

        try:
            tracks = fetch_playlist_tracks(playlist_id, token_info.access_token)
            playlist_total_count = len(tracks)
        except SpotifyUserAuthRequired:
            raise RuntimeError(
                "Spotify requires user authentication for the official playlist "
                "API request, and the no-login public-page route did not return "
                "tracks. The downloader will not try Spotify account login unless "
                "you explicitly run with --user-auth."
            )
        except PermissionError as exc:
            if args.user_auth:
                raise
            raise RuntimeError(
                f"{exc}\n\n"
                "The no-login public-page route also failed. For private playlists, "
                f"try: python playlist_downloader.py \"{args.playlist}\" --user-auth"
            ) from exc

    if args.limit:
        tracks = tracks[: args.limit]

    ffmpeg_location = find_ffmpeg_location()
    if not args.dry_run and not ffmpeg_location:
        raise RuntimeError(
            "ffmpeg is required for MP3 conversion but was not found. Install ffmpeg "
            "or set FFMPEG_LOCATION to the folder containing ffmpeg.exe."
        )
    if args.spotify_debug and ffmpeg_location:
        log(f"ffmpeg location: {ffmpeg_location}")

    numbering_total = playlist_total_count or len(tracks)
    width = max(2, len(str(numbering_total)))
    log(f"Found {len(tracks)} tracks. Output: {output_dir}")

    failures: list[tuple[int, str]] = []
    worker_count = max(1, args.workers)
    with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [
            executor.submit(
                download_track,
                track,
                output_dir,
                width,
                args.max_results,
                args.bitrate,
                ffmpeg_location,
                args.dry_run,
            )
            for track in tracks
        ]

        for future in concurrent.futures.as_completed(futures):
            index, ok, detail = future.result()
            if not ok:
                failures.append((index, detail))
                log(f"{str(index).zfill(width)}. FAILED: {detail}")

    if failures:
        log("\nSome tracks failed:")
        for index, detail in sorted(failures):
            log(f"  {str(index).zfill(width)}: {detail}")
        return 1

    log("\nDone.")
    return 0


if __name__ == "__main__":
    interactive_mode = len(sys.argv) == 1
    try:
        exit_code = main()
        if interactive_mode:
            input("\nPress Enter to close...")
        sys.exit(exit_code)
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        if interactive_mode:
            input("\nPress Enter to close...")
        sys.exit(130)
    except (PermissionError, RuntimeError, ValueError) as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        if interactive_mode:
            input("\nPress Enter to close...")
        sys.exit(1)
