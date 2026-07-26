# daily.ps1 - one click: fetch real news -> build site -> open preview.
# Launched by daily.bat (double-click).
$ErrorActionPreference = "Continue"
$root = $PSScriptRoot
Set-Location $root

if (-not $env:NEWS_HTTP_PROXY -and -not $env:HTTPS_PROXY) {
    # Local proxy for reaching news sources; edit the port here if yours changes.
    $env:NEWS_HTTP_PROXY = "http://127.0.0.1:2231"
}

"[1/3] fetch real news"
uv run news-digest fetch
if ($LASTEXITCODE -ne 0) {
    "fetch failed - see the per-source report above."
    exit 1
}

"[2/3] build site"
uv run news-digest build
if ($LASTEXITCODE -ne 0) {
    "build failed."
    exit 1
}

"[3/3] open preview"
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root "preview.ps1")
