param(
    [string]$CredentialFile = ""
)

$ErrorActionPreference = "Stop"

if ($CredentialFile) {
    powershell -ExecutionPolicy Bypass -File "$PSScriptRoot\build_exe.ps1" -CredentialFile $CredentialFile
    exit $LASTEXITCODE
}

$defaultCredentialFile = Join-Path $PSScriptRoot "spotify_credentials.txt"
if (Test-Path -LiteralPath $defaultCredentialFile) {
    powershell -ExecutionPolicy Bypass -File "$PSScriptRoot\build_exe.ps1" -CredentialFile $defaultCredentialFile
    exit $LASTEXITCODE
}

$clientId = Read-Host "Spotify Client ID"
$secureSecret = Read-Host "Spotify Client Secret" -AsSecureString
$secretPtr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureSecret)

try {
    $clientSecret = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($secretPtr)
    $env:SPOTIFY_CLIENT_ID = $clientId
    $env:SPOTIFY_CLIENT_SECRET = $clientSecret

    powershell -ExecutionPolicy Bypass -File "$PSScriptRoot\build_exe.ps1"
}
finally {
    if ($secretPtr -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($secretPtr)
    }
    Remove-Item Env:\SPOTIFY_CLIENT_ID -ErrorAction SilentlyContinue
    Remove-Item Env:\SPOTIFY_CLIENT_SECRET -ErrorAction SilentlyContinue
}
