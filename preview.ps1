# preview.ps1 - build latest fetched news (if any) and serve the site on 127.0.0.1:8618.
# Launched by preview.bat (double-click).
$ErrorActionPreference = "Continue"
$root = $PSScriptRoot
Set-Location $root

$fetched = Get-ChildItem "var\data\fetched\*.json" -ErrorAction SilentlyContinue
if ($fetched) {
    "[build] publishing latest fetched news..."
    uv run news-digest build
} elseif (-not (Test-Path "var\site\current\index.html")) {
    "[build] no fetched data yet, building demo fixtures..."
    uv run news-digest build --fixtures tests/fixtures/demo
}

$serveDir = Join-Path $root "var\site\current"
if (-not (Test-Path (Join-Path $serveDir "index.html"))) {
    "ERROR: nothing to serve. Run daily.bat (fetch + build) first."
    exit 1
}

$existing = Get-NetTCPConnection -LocalPort 8618 -State Listen -ErrorAction SilentlyContinue
if ($existing) {
    "[serve] port 8618 already serving, reusing it."
} else {
    Start-Process -FilePath "uv" `
        -ArgumentList @("run", "news-digest", "preview", "--port", "8618") `
        -WorkingDirectory $root -WindowStyle Minimized
    Start-Sleep -Seconds 2
}
Start-Process "http://127.0.0.1:8618/"
"Site : http://127.0.0.1:8618/"
"Admin: http://127.0.0.1:8618/admin/  (model provider switcher)"
"Server window is minimized; closing it stops the preview."
