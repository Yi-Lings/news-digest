# deploy-all.ps1 -- one-click stage 7 deployment orchestrator (run via deploy.bat).
# PowerShell 5.1 compatible; ASCII-only source.
# Flow: ssh-agent (passphrase once) -> git push (triggers CI on tag) ->
#       wait for GitHub Actions build -> provision server .env from local
#       .env.local (secrets go Windows -> server directly, never through chat
#       or the repo) -> server GHCR login via `gh auth token` pipe ->
#       server-push.ps1 -AutoYes (upload + preflight + bootstrap) -> smoke check.

param([switch]$Elevated)  # 内部使用：提权重启后置位，用于收尾时暂停窗口

$KeyPath = "C:\Users\Admin\.ssh\id_ed25519"
$Server  = "root@cheapcoding.top"
$Owner   = "yi-lings"
$AppDir  = "/srv/news-digest"
$Version = "v0.6.0rc6"

try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$RepoDir = Split-Path $PSScriptRoot -Parent
Set-Location $RepoDir

function Stop-OnError {
    param([int]$Code, [string]$Step)
    if ($Code -ne 0) {
        Write-Host ""
        Write-Host "[FAIL] Step '$Step' exited with code $Code. Aborting." -ForegroundColor Red
        if ($Elevated) { Read-Host "Press Enter to close" | Out-Null }
        exit $Code
    }
}

foreach ($tool in @("ssh", "scp", "git", "gh")) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        Write-Host "[FAIL] '$tool' not found in PATH." -ForegroundColor Red
        exit 1
    }
}

# ---- GitHub reachability: fall back to the local proxy when direct 443 fails ----
# git and gh both honor HTTPS_PROXY; ssh/scp to the server are unaffected.
if (-not $env:HTTPS_PROXY) {
    $direct = Test-NetConnection github.com -Port 443 -InformationLevel Quiet -WarningAction SilentlyContinue
    if (-not $direct) {
        $proxyUp = Test-NetConnection 127.0.0.1 -Port 2231 -InformationLevel Quiet -WarningAction SilentlyContinue
        if ($proxyUp) {
            Write-Host "[NOTE] github.com direct connect failed; routing git/gh via local proxy 127.0.0.1:2231." -ForegroundColor Yellow
            $env:HTTPS_PROXY = "http://127.0.0.1:2231"
            $env:HTTP_PROXY  = "http://127.0.0.1:2231"
        } else {
            Write-Host "[FAIL] Cannot reach github.com directly and local proxy 127.0.0.1:2231 is not running." -ForegroundColor Red
            Write-Host "       Start your proxy client, then re-run deploy.bat." -ForegroundColor Red
            exit 1
        }
    }
}

# ---- [0/6] ssh-agent: enter the key passphrase once ----
Write-Host "[0/6] ssh-agent setup..."
$svc = Get-Service ssh-agent -ErrorAction SilentlyContinue
$agentReady = $false
if ($svc) {
    try {
        if ($svc.StartType -eq "Disabled") {
            Set-Service ssh-agent -StartupType Manual -ErrorAction Stop
        }
        if ((Get-Service ssh-agent).Status -ne "Running") {
            Start-Service ssh-agent -ErrorAction Stop
        }
        $agentReady = $true
    } catch {
        $identity = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
        $isAdmin = $identity.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
        if (-not $isAdmin) {
            Write-Host "Enabling ssh-agent needs admin once; relaunching elevated - accept the UAC prompt." -ForegroundColor Yellow
            Start-Process powershell -Verb RunAs -ArgumentList @(
                "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"", "-Elevated"
            )
            exit 0
        }
        Write-Host "[FAIL] Cannot enable ssh-agent even as admin: $($_.Exception.Message)" -ForegroundColor Red
        exit 1
    }
} else {
    Write-Host "[NOTE] ssh-agent service not found; ssh may prompt for the passphrase several times." -ForegroundColor Yellow
}
if ($agentReady) {
    ssh-add -l *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Enter the SSH key passphrase once; it stays in ssh-agent afterwards."
        ssh-add $KeyPath
        Stop-OnError $LASTEXITCODE "ssh-add"
    }
}

# ---- [1/6] push branches, then the release tag ALONE ----
# GitHub suppresses push events when more than 3 tags arrive in one push,
# so the version tag must be pushed by itself to trigger the CI build.
Write-Host "[1/6] git push (branches, then release tag $Version)..."
git push origin main phase/5-email phase/6-release phase/7-deploy
Stop-OnError $LASTEXITCODE "git push branches"
git push origin phase-2-accepted phase-3-accepted phase-4-accepted phase-5-accepted phase-6-accepted 2>$null
git push origin $Version
Stop-OnError $LASTEXITCODE "git push release tag"

# ---- [2/6] wait for the GitHub Actions release build ----
Write-Host "[2/6] Waiting for GitHub Actions to build and publish images..."
$runId = $null
for ($i = 0; $i -lt 30 -and -not $runId; $i++) {
    Start-Sleep -Seconds 5
    $json = gh run list --workflow release.yml --limit 1 --json databaseId,status 2>$null
    if ($json) {
        $run = ($json | ConvertFrom-Json) | Select-Object -First 1
        if ($run) { $runId = $run.databaseId }
    }
}
if (-not $runId) {
    Write-Host "[FAIL] No workflow run appeared; check the repo Actions tab." -ForegroundColor Red
    exit 1
}
gh run watch $runId --exit-status
Stop-OnError $LASTEXITCODE "CI build (gh run watch)"

# ---- [3/6] provision the server .env from local .env.local ----
Write-Host "[3/6] Provisioning $AppDir/config/.env from local .env.local..."
$envLocal = Join-Path $RepoDir ".env.local"
if (-not (Test-Path $envLocal)) {
    Write-Host "[FAIL] .env.local not found." -ForegroundColor Red
    exit 1
}
$pairs = @{}
foreach ($line in Get-Content $envLocal) {
    $t = "$line".Trim()
    if (-not $t -or $t.StartsWith("#") -or -not $t.Contains("=")) { continue }
    $parts = $t.Split("=", 2)
    $k = $parts[0].Trim()
    $v = $parts[1].Trim().Trim('"').Trim("'")
    if ($k) { $pairs[$k] = $v }
}
$keys = @(
    "TRANSLATION_API_BASE_URL", "TRANSLATION_API_KEY", "TRANSLATION_MODEL",
    "SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD",
    "SMTP_FROM", "SMTP_RECIPIENTS"
)
$missing = @($keys | Where-Object { -not $pairs.ContainsKey($_) -or -not $pairs[$_] })
if ($missing.Count -gt 0) {
    Write-Host "[FAIL] .env.local is missing: $($missing -join ', ')" -ForegroundColor Red
    exit 1
}
$lines = @("# generated by deploy-all.ps1 from local .env.local")
$lines += "NEWS_SITE_URL=https://news.cheapcoding.top"
foreach ($k in $keys) { $lines += "$k=$($pairs[$k])" }
$tmp = [IO.Path]::GetTempFileName()
[IO.File]::WriteAllText($tmp, (($lines -join "`n") + "`n"))
# config/ subdir is the only host path bind-mounted into the admin container;
# bootstrap re-tightens ownership/permissions idempotently later in step [5/6].
ssh -i $KeyPath $Server "mkdir -p $AppDir/config && chmod 750 $AppDir/config"
Stop-OnError $LASTEXITCODE "ssh mkdir config dir"
scp -i $KeyPath $tmp "${Server}:$AppDir/config/.env"
$scpCode = $LASTEXITCODE
Remove-Item $tmp -Force
Stop-OnError $scpCode "scp .env"
ssh -i $KeyPath $Server "chmod 600 $AppDir/config/.env"
Stop-OnError $LASTEXITCODE "chmod .env"

# ---- [4/6] server GHCR login: token flows Windows -> server directly ----
Write-Host "[4/6] Logging the server into GHCR (gh auth token pipe)..."
gh auth token | ssh -i $KeyPath $Server "docker login ghcr.io -u $Owner --password-stdin && chmod 600 /root/.docker/config.json"
Stop-OnError $LASTEXITCODE "server GHCR login"

# ---- [5/6] upload artifacts, preflight, bootstrap ----
Write-Host "[5/6] Running server-push (upload + preflight + bootstrap)..."
& (Join-Path $PSScriptRoot "server-push.ps1") -AutoYes
Stop-OnError $LASTEXITCODE "server-push"

# ---- [6/6] smoke check ----
Write-Host "[6/6] Final smoke check..."
ssh -i $KeyPath $Server "curl -sk -o /dev/null -w 'https status: %{http_code}\n' https://news.cheapcoding.top/ ; systemctl is-enabled news-digest.timer ; systemctl list-timers news-digest.timer --no-pager | head -3"

Write-Host ""
Write-Host "[DONE] https://news.cheapcoding.top/  (daily rebuild: 08:00 Asia/Shanghai)"
if ($Elevated) { Read-Host "Press Enter to close" | Out-Null }
exit 0
