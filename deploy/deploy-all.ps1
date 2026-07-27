# deploy-all.ps1 -- one-click stage 7 deployment orchestrator (run via deploy.bat).
# PowerShell 5.1 compatible; ASCII-only source.
# Flow: ssh-agent (passphrase once) -> tag preflight (never move a published tag) ->
#       git push (triggers CI on tag) -> wait for GitHub Actions build -> stage
#       managed .env keys as config/.env.incoming (bootstrap merges, never overwrites;
#       secrets go Windows -> server directly, never through chat or the repo) ->
#       server GHCR login with a READ-ONLY read:packages PAT ->
#       server-push.ps1 -AutoYes -Version (upload + preflight + bootstrap) -> smoke check.

param([switch]$Elevated)  # 内部使用：提权重启后置位，用于收尾时暂停窗口

$KeyPath = "C:\Users\Admin\.ssh\id_ed25519"
$Server  = "root@cheapcoding.top"
$Owner   = "yi-lings"
$AppDir  = "/srv/news-digest"
# $Version is single-sourced from the package below (not hardcoded), so a release
# version only ever changes in src/news_digest/__init__.py.

try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$RepoDir = Split-Path $PSScriptRoot -Parent
Set-Location $RepoDir

# ---- version single-sourced from the package: tag = 'v' + __version__ ----
# Deploy, CI (release.yml's tag==__version__ gate) and the server bootstrap all key off
# this same value; bumping __init__.py is the only place a release version changes.
$initPy = Join-Path $RepoDir "src\news_digest\__init__.py"
$verMatch = @(Select-String -Path $initPy -Pattern '__version__\s*=\s*"([^"]+)"')
if ($verMatch.Count -eq 0) {
    Write-Host "[FAIL] cannot parse __version__ from $initPy" -ForegroundColor Red
    exit 1
}
$Version = "v" + $verMatch[0].Matches[0].Groups[1].Value
Write-Host "[i] release version (from __init__.py): $Version"

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

# ---- tag preflight: release the current commit; never move a published tag ----
$headSha = "$(git rev-parse HEAD 2>$null)".Trim()
$localTagSha = "$(git rev-list -n 1 $Version 2>$null)".Trim()
if (-not $localTagSha) {
    Write-Host "[FAIL] local tag $Version does not exist. Tag the release commit first:" -ForegroundColor Red
    Write-Host "         git tag -a $Version -m $Version" -ForegroundColor Red
    exit 1
}
if ($localTagSha -ne $headSha) {
    Write-Host "[WARN] tag $Version is at $($localTagSha.Substring(0,12)), not HEAD $($headSha.Substring(0,12))." -ForegroundColor Yellow
    Write-Host "         You will deploy the tagged commit, not your current checkout." -ForegroundColor Yellow
}
# A published tag is immutable: if the remote already carries $Version at a different
# commit, refuse rather than force-move it (moving a released tag breaks provenance).
$remoteCommit = $null
$peeled = git ls-remote origin ("refs/tags/" + $Version + "^{}") 2>$null
if ($peeled) { $remoteCommit = (("$peeled" -split "\s+")[0]).Trim() }
else {
    $plain = git ls-remote origin ("refs/tags/" + $Version) 2>$null
    if ($plain) { $remoteCommit = (("$plain" -split "\s+")[0]).Trim() }
}
if ($remoteCommit -and $remoteCommit -ne $localTagSha) {
    Write-Host "[FAIL] remote tag $Version already points at $($remoteCommit.Substring(0,12)) (local $($localTagSha.Substring(0,12)))." -ForegroundColor Red
    Write-Host "         A published tag must never be moved. Bump __version__ to a new rc and retag." -ForegroundColor Red
    exit 1
}
if ($remoteCommit) { Write-Host "[i] remote tag $Version already present at the same commit; tag push is a no-op." }

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

# ---- [3/6] stage managed .env keys from local .env.local (bootstrap merges them) ----
Write-Host "[3/6] Staging managed keys to $AppDir/config/.env.incoming from local .env.local..."
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
$lines = @("# managed keys staged by deploy-all.ps1 from .env.local; bootstrap upserts these")
$lines += "# into config/.env, preserving any other keys/comments already on the server."
$lines += "NEWS_SITE_URL=https://news.cheapcoding.top"
foreach ($k in $keys) { $lines += "$k=$($pairs[$k])" }
$tmp = [IO.Path]::GetTempFileName()
[IO.File]::WriteAllText($tmp, (($lines -join "`n") + "`n"))
# Ship managed keys as .env.incoming and let bootstrap merge+delete it. Never overwrite
# .env wholesale -- that would drop operator-added keys. Secrets ride the scp'd file
# only, never a command line. config/ is the admin container's only bind mount;
# bootstrap re-tightens ownership/permissions idempotently later in step [5/6].
ssh -i $KeyPath $Server "mkdir -p $AppDir/config && chmod 750 $AppDir/config"
Stop-OnError $LASTEXITCODE "ssh mkdir config dir"
scp -i $KeyPath $tmp "${Server}:$AppDir/config/.env.incoming"
$scpCode = $LASTEXITCODE
Remove-Item $tmp -Force
Stop-OnError $scpCode "scp .env.incoming"
ssh -i $KeyPath $Server "chmod 600 $AppDir/config/.env.incoming"
Stop-OnError $LASTEXITCODE "chmod .env.incoming"
Write-Host "[i] Staged managed keys to config/.env.incoming; bootstrap will merge into .env."

# ---- [4/6] server GHCR login: a READ-ONLY PAT flows Windows -> server directly ----
# Never `gh auth token` here: that is the full-scope user token (repo write, workflow,
# delete:packages). The server only ever pulls images, so it must receive a PAT scoped
# to read:packages only. Prefer $env:ND_GHCR_TOKEN (non-interactive); else prompt hidden.
Write-Host "[4/6] Logging the server into GHCR (read-only PAT)..."
$ghcrToken = $env:ND_GHCR_TOKEN
if (-not $ghcrToken) {
    $sec = Read-Host "Paste a GHCR read:packages PAT for the server (input hidden)" -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
    try { $ghcrToken = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}
if (-not $ghcrToken) {
    Write-Host "[FAIL] No GHCR token provided (set ND_GHCR_TOKEN or paste at the prompt)." -ForegroundColor Red
    exit 1
}
# Token rides stdin into docker login (never on argv or in logs); tighten config.json after.
$ghcrToken | ssh -i $KeyPath $Server "docker login ghcr.io -u $Owner --password-stdin && chmod 600 /root/.docker/config.json"
$loginCode = $LASTEXITCODE
$ghcrToken = $null
Stop-OnError $loginCode "server GHCR login"

# ---- [5/6] upload artifacts, preflight, bootstrap ----
# Pass the single-sourced $Version through so bootstrap deploys the exact tag we just
# built/pushed, instead of server-push falling back to its own hardcoded default.
Write-Host "[5/6] Running server-push (upload + preflight + bootstrap)..."
& (Join-Path $PSScriptRoot "server-push.ps1") -AutoYes -Version $Version
Stop-OnError $LASTEXITCODE "server-push"

# ---- [6/6] smoke check ----
Write-Host "[6/6] Final smoke check..."
ssh -i $KeyPath $Server "curl -sk -o /dev/null -w 'https status: %{http_code}\n' https://news.cheapcoding.top/ ; systemctl is-enabled news-digest.timer ; systemctl list-timers news-digest.timer --no-pager | head -3"

Write-Host ""
Write-Host "[DONE] https://news.cheapcoding.top/  (daily rebuild: 08:00 Asia/Shanghai)"
if ($Elevated) { Read-Host "Press Enter to close" | Out-Null }
exit 0
