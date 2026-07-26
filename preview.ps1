# preview.ps1 - start local preview, open browser, collect diagnostics.
# Launched by preview.bat (double-click). Writes var\diag-report.txt for Claude.
$ErrorActionPreference = "Continue"
$root = $PSScriptRoot
Set-Location $root
New-Item -ItemType Directory -Force -Path (Join-Path $root "var") | Out-Null
$report = Join-Path $root "var\diag-report.txt"
$lines = @()
$lines += "generated: $(Get-Date -Format s)"

# choose directory to serve
$serveDir = Join-Path $root "var\site\current"
if (-not (Test-Path (Join-Path $serveDir "index.html"))) {
    $serveDir = Join-Path $root "var\preview"
}
$lines += "serving: $serveDir"

# listeners before start
$lines += "### listeners (before) ###"
$net = netstat -ano | Select-String "LISTENING"
foreach ($port in 8000, 8080, 8618) {
    $rows = $net | Where-Object { $_ -match ":$port\s" }
    if ($rows) {
        foreach ($row in $rows) {
            $procId = ("$row" -split "\s+")[-1]
            $name = "unknown"
            try { $name = (Get-Process -Id $procId -ErrorAction Stop).ProcessName } catch { }
            $lines += "port ${port} : pid=$procId name=$name"
        }
    } else {
        $lines += "port ${port} : free"
    }
}

# system proxy
$lines += "### proxy ###"
$proxy = Get-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings"
$lines += "ProxyEnable=$($proxy.ProxyEnable) ProxyServer=$($proxy.ProxyServer)"
$lines += "ProxyOverride=$($proxy.ProxyOverride)"

# start server in its own window (stays running after this script ends)
$lines += "### server ###"
$python = "py"
if (Get-Command python -ErrorAction SilentlyContinue) { $python = "python" }
try {
    Start-Process -FilePath $python `
        -ArgumentList @("-m", "http.server", "8618", "--bind", "127.0.0.1", "--directory", "`"$serveDir`"") `
        -WorkingDirectory $root -WindowStyle Normal
    $lines += "started: $python -m http.server 8618 --bind 127.0.0.1"
} catch {
    $lines += "start FAILED: $($_.Exception.Message)"
}
Start-Sleep -Seconds 3

# open browser right away
Start-Process "http://127.0.0.1:8618/"

# self-test with curl.exe, bypassing any proxy
$lines += "### curl self-test ###"
$code = & curl.exe -s -o NUL -w "%{http_code}" --noproxy "*" http://127.0.0.1:8618/ 2>&1
$lines += "http_code: $code"
$body = & curl.exe -s --noproxy "*" http://127.0.0.1:8618/ 2>&1
$lines += "bytes: $("$body".Length)"
if ("$body" -match "Cheapcoding") { $lines += "content: Cheapcoding News confirmed" }

# acceptance tests (includes the Windows junction switch test)
$lines += "### pytest ###"
$pytestOut = uv run pytest 2>&1 | Select-Object -Last 3
foreach ($line in $pytestOut) { $lines += "$line" }
$lines += "pytest exit: $LASTEXITCODE"

$lines | Set-Content -Path $report -Encoding UTF8
""
"Done. Report written to var\diag-report.txt"
"If the browser did not open, visit http://127.0.0.1:8618/ manually."
