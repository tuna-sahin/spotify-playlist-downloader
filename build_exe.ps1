param(
    [string]$CredentialFile = ""
)

$ErrorActionPreference = "Stop"

$icon = "C:\Users\sahin\Downloads\icons8-music-64.ico"

function Clean-CredentialValue {
    param([object]$Value)

    if ($null -eq $Value) {
        return ""
    }

    $clean = "$Value".Trim()
    if ($clean.Length -ge 2) {
        $first = $clean[0]
        $last = $clean[$clean.Length - 1]
        if (($first -eq '"' -and $last -eq '"') -or ($first -eq "'" -and $last -eq "'")) {
            $clean = $clean.Substring(1, $clean.Length - 2).Trim()
        }
    }
    return $clean
}

function Normalize-CredentialKey {
    param([string]$Key)

    $normalized = $Key.Trim()
    if ($normalized.StartsWith("export ", [System.StringComparison]::OrdinalIgnoreCase)) {
        $normalized = $normalized.Substring(7).Trim()
    }
    $normalized = $normalized.ToLowerInvariant() -replace "[^a-z0-9]+", "_"
    return $normalized.Trim([char[]]"_")
}

function Get-MapCredentialValue {
    param(
        [hashtable]$Map,
        [string[]]$Keys
    )

    foreach ($key in $Keys) {
        if ($Map.ContainsKey($key)) {
            $value = Clean-CredentialValue $Map[$key]
            if ($value) {
                return $value
            }
        }
    }
    return $null
}

function Get-CredentialsFromMap {
    param([hashtable]$Map)

    $clientId = Get-MapCredentialValue $Map @(
        "spotify_client_id",
        "spotipy_client_id",
        "client_id",
        "spotify_id",
        "id"
    )
    $clientSecret = Get-MapCredentialValue $Map @(
        "spotify_client_secret",
        "spotipy_client_secret",
        "client_secret",
        "spotify_secret",
        "secret"
    )

    if ($clientId -and $clientSecret) {
        return [PSCustomObject]@{
            ClientId = $clientId
            ClientSecret = $clientSecret
        }
    }
    return $null
}

function Read-SpotifyCredentialFile {
    param([string]$Path)

    $text = Get-Content -LiteralPath $Path -Raw -Encoding UTF8
    if ([string]::IsNullOrWhiteSpace($text)) {
        return $null
    }

    $trimmed = $text.Trim()
    if ($trimmed.StartsWith("{")) {
        $json = $trimmed | ConvertFrom-Json
        $map = @{}
        foreach ($property in $json.PSObject.Properties) {
            $map[(Normalize-CredentialKey $property.Name)] = Clean-CredentialValue $property.Value
        }
        return Get-CredentialsFromMap $map
    }

    $map = @{}
    $bareValues = New-Object System.Collections.Generic.List[string]
    foreach ($rawLine in ($text -split "`r?`n")) {
        $line = $rawLine.Trim()
        if ([string]::IsNullOrWhiteSpace($line) -or $line.StartsWith("#") -or $line.StartsWith(";")) {
            continue
        }

        $match = [regex]::Match($line, "^\s*([^=:]+?)\s*=\s*(.*)$")
        if (-not $match.Success) {
            $match = [regex]::Match($line, "^\s*([A-Za-z_][A-Za-z0-9_\-\s]*?)\s*:\s*(.+)$")
        }

        if ($match.Success) {
            $map[(Normalize-CredentialKey $match.Groups[1].Value)] = Clean-CredentialValue $match.Groups[2].Value
        }
        else {
            $value = Clean-CredentialValue $line
            if ($value) {
                [void]$bareValues.Add($value)
            }
        }
    }

    $mappedCredentials = Get-CredentialsFromMap $map
    if ($mappedCredentials) {
        return $mappedCredentials
    }

    if ($bareValues.Count -ge 2) {
        return [PSCustomObject]@{
            ClientId = $bareValues[0]
            ClientSecret = $bareValues[1]
        }
    }

    return $null
}

function Resolve-CredentialFile {
    param([string]$Path)

    if (-not $Path) {
        return $null
    }

    $resolved = Resolve-Path -LiteralPath $Path -ErrorAction SilentlyContinue
    if ($resolved) {
        return $resolved.Path
    }

    $scriptRelative = Join-Path $PSScriptRoot $Path
    $resolved = Resolve-Path -LiteralPath $scriptRelative -ErrorAction SilentlyContinue
    if ($resolved) {
        return $resolved.Path
    }

    throw "Could not find credential file: $Path"
}

$ffmpeg = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Recurse -Filter ffmpeg.exe |
    Sort-Object FullName |
    Select-Object -Last 1

$ffprobe = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Recurse -Filter ffprobe.exe |
    Sort-Object FullName |
    Select-Object -Last 1

if (-not $ffmpeg) {
    throw "Could not find ffmpeg.exe. Install FFmpeg first, for example: winget install --id Gyan.FFmpeg -e"
}

if (-not $ffprobe) {
    throw "Could not find ffprobe.exe. Install FFmpeg first, for example: winget install --id Gyan.FFmpeg -e"
}

if (-not (Test-Path $icon)) {
    throw "Could not find icon file: $icon"
}

$credentialSourceFile = $null
if ($CredentialFile) {
    $credentialSourceFile = Resolve-CredentialFile $CredentialFile
}
else {
    foreach ($filename in @("spotify_credentials.txt", "spotify_credentials.json", "spotify_credentials.env", "spotify_credentials")) {
        $candidate = Join-Path $PSScriptRoot $filename
        if (Test-Path -LiteralPath $candidate) {
            $credentialSourceFile = $candidate
            break
        }
    }
}

$spotifyCredentials = $null
$credentialSource = ""
if ($env:SPOTIFY_CLIENT_ID -and $env:SPOTIFY_CLIENT_SECRET) {
    $spotifyCredentials = [PSCustomObject]@{
        ClientId = $env:SPOTIFY_CLIENT_ID
        ClientSecret = $env:SPOTIFY_CLIENT_SECRET
    }
    $credentialSource = "environment variables"
}
elseif ($credentialSourceFile) {
    $spotifyCredentials = Read-SpotifyCredentialFile $credentialSourceFile
    if (-not $spotifyCredentials) {
        throw "Could not read Spotify client ID and secret from: $credentialSourceFile"
    }
    $credentialSource = $credentialSourceFile
}

$credentialBuildDir = $null

try {
    $extraArgs = @()

    if ($spotifyCredentials) {
        $credentialBuildDir = Join-Path ([System.IO.Path]::GetTempPath()) ("spotify-downloader-build-" + [guid]::NewGuid().ToString("N"))
        New-Item -ItemType Directory -Force -Path $credentialBuildDir | Out-Null
        $bundleCredentialFile = Join-Path $credentialBuildDir "spotify_credentials.json"

        @{
            client_id = $spotifyCredentials.ClientId
            client_secret = $spotifyCredentials.ClientSecret
        } | ConvertTo-Json | Set-Content -LiteralPath $bundleCredentialFile -Encoding UTF8

        $extraArgs += @("--add-data", "$bundleCredentialFile;.")
        Write-Host "Bundling Spotify API credentials for long playlist support from $credentialSource."
    }
    else {
        Write-Host "No Spotify credentials found; long playlists over Spotify's public-page limit can still use spotify_credentials.txt next to the exe."
    }

    $pyinstallerArgs = @(
        "-m", "PyInstaller",
        "--clean",
        "--onefile",
        "--console",
        "--icon", $icon,
        "--name", "Spotify Playlist Downloader",
        "--add-binary", "$($ffmpeg.FullName);.",
        "--add-binary", "$($ffprobe.FullName);."
    ) + $extraArgs + @("playlist_downloader.py")

    & python @pyinstallerArgs

    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE"
    }
}
finally {
    if ($credentialBuildDir -and (Test-Path -LiteralPath $credentialBuildDir)) {
        $resolvedTempRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
        $resolvedBuildDir = [System.IO.Path]::GetFullPath($credentialBuildDir)
        if ($resolvedBuildDir.StartsWith($resolvedTempRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
            Remove-Item -LiteralPath $credentialBuildDir -Recurse -Force
        }
    }
}
