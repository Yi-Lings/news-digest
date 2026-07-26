# daily.ps1 - one click: fetch real news -> build site -> open preview.
# Launched by daily.bat (double-click).
$ErrorActionPreference = "Continue"
$root = $PSScriptRoot
Set-Location $root

if (-not $env:NEWS_HTTP_PROXY -and -not $env:HTTPS_PROXY) {
    # Local proxy for reaching news sources; edit the port here if yours changes.
    $env:NEWS_HTTP_PROXY = "http://127.0.0.1:2231"
}

"[1/2] full pipeline: fetch -> select -> translate -> build"
uv run news-digest run --yes
if ($LASTEXITCODE -ne 0) {
    "pipeline reported failures - see output above (site may still have been built)."
}

"[2/2] open preview"
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root "preview.ps1")
