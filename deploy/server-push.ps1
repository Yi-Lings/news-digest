# server-push.ps1 -- news-digest stage 7: upload deploy artifacts and run remote scripts.
# PowerShell 5.1 compatible; ASCII-only file, because PS 5.1 misreads non-ASCII
# .ps1 files without a BOM and mixed codepages garble console output.
# Flow: ssh mkdir incoming -> scp artifacts -> ssh preflight (read-only) ->
#       pause for confirmation -> ssh bootstrap. Any non-zero step aborts.
# API/SMTP secrets never pass through this script; first install leaves them disabled
# for later Admin configuration. A standalone run may still require server GHCR login.

param(
    [switch]$AutoYes,        # -AutoYes: skip the pause between preflight and bootstrap
    [switch]$NoPrompt,       # automation: native SSH/SCP must fail, never prompt
    [string]$Version,        # release tag to deploy; passed through to bootstrap as ND_VERSION
    [string]$WorkerDigest,   # immutable worker digest from this tag's Release asset
    [string]$WebDigest,      # immutable web digest from this tag's Release asset
    [string]$Server = $env:ND_SERVER,
    [string]$KeyPath = $env:ND_KEY_PATH,
    [string]$Owner = $env:ND_OWNER,
    [string]$AppDir = $env:ND_APP_DIR,
    [string]$Domain = $env:ND_DOMAIN,
    [string]$CertbotEmail = $env:ND_CERTBOT_EMAIL,
    [string]$WebPort = $env:ND_WEB_PORT,
    [string]$AdminPort = $env:ND_ADMIN_PORT,
    [string]$SitePort = $env:ND_SITE_PORT
)

if (-not $WebPort) { $WebPort = "8618" }
if (-not $AdminPort) { $AdminPort = "8619" }
if (-not $SitePort) { $SitePort = "8620" }

$requiredTargets = [ordered]@{
    Server = $Server; KeyPath = $KeyPath; Owner = $Owner; AppDir = $AppDir;
    Domain = $Domain; CertbotEmail = $CertbotEmail
}
$missingTargets = @($requiredTargets.Keys | Where-Object { -not "$($requiredTargets[$_])".Trim() })
if ($missingTargets.Count -gt 0) {
    Write-Host "[FAIL] Missing deployment targets: $($missingTargets -join ', ')." -ForegroundColor Red
    Write-Host "       Pass parameters or set ND_SERVER, ND_KEY_PATH, ND_OWNER, ND_APP_DIR, ND_DOMAIN and ND_CERTBOT_EMAIL." -ForegroundColor Red
    exit 1
}
if ($Server -notmatch '^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+$') {
    Write-Host "[FAIL] Invalid SSH target: $Server" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path -LiteralPath $KeyPath -PathType Leaf)) {
    Write-Host "[FAIL] SSH key not found: $KeyPath" -ForegroundColor Red
    exit 1
}
if ($Owner -notmatch '^[a-z0-9][a-z0-9-]*$') {
    Write-Host "[FAIL] Invalid GHCR owner: $Owner" -ForegroundColor Red
    exit 1
}
if ($AppDir -notmatch '^/[A-Za-z0-9._/-]+$' -or $AppDir -match '(^|/)\.\.?(?:/|$)' -or $AppDir -match '//') {
    Write-Host "[FAIL] Invalid absolute server app directory: $AppDir" -ForegroundColor Red
    exit 1
}
if ($Domain -notmatch '^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$') {
    Write-Host "[FAIL] Invalid site domain: $Domain" -ForegroundColor Red
    exit 1
}
if ($CertbotEmail -notmatch '^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}$') {
    Write-Host "[FAIL] Invalid certificate email." -ForegroundColor Red
    exit 1
}
foreach ($port in @($WebPort, $AdminPort, $SitePort)) {
    if ($port -notmatch '^[0-9]{1,5}$' -or [int]$port -lt 1 -or [int]$port -gt 65535) {
        Write-Host "[FAIL] Invalid deploy port: $port" -ForegroundColor Red
        exit 1
    }
}
if (($WebPort -eq $AdminPort) -or ($WebPort -eq $SitePort) -or ($AdminPort -eq $SitePort)) {
    Write-Host "[FAIL] WebPort, AdminPort and SitePort must differ." -ForegroundColor Red
    exit 1
}

# Single-source the version: deploy-all.ps1 passes -Version, but when this script is run
# standalone, derive the same tag ('v' + __version__) so we never fall back to a stale
# hardcoded default that could deploy the wrong image.
if (-not $Version) {
    if ($PSScriptRoot) { $scriptDir = $PSScriptRoot } else { $scriptDir = (Get-Location).Path }
    $initPy = Join-Path (Split-Path $scriptDir -Parent) "src\news_digest\__init__.py"
    $verMatch = @(Select-String -Path $initPy -Pattern '__version__\s*=\s*"([^"]+)"' -ErrorAction SilentlyContinue)
    if ($verMatch.Count -eq 0) {
        Write-Host "[FAIL] -Version not given and cannot parse __version__ from $initPy" -ForegroundColor Red
        exit 1
    }
    $Version = "v" + $verMatch[0].Matches[0].Groups[1].Value
}
if ($Version -notmatch '^v[A-Za-z0-9_][A-Za-z0-9_.-]{0,126}$') {
    Write-Host "[FAIL] Invalid release version: $Version" -ForegroundColor Red
    exit 1
}
if (-not $WorkerDigest -or -not $WebDigest) {
    Write-Host "[FAIL] WorkerDigest and WebDigest are both required; production tag fallback is forbidden." -ForegroundColor Red
    exit 1
}
foreach ($digest in @($WorkerDigest, $WebDigest)) {
    if ($digest -and $digest -notmatch '^sha256:[0-9a-f]{64}$') {
        Write-Host "[FAIL] Invalid image digest: $digest" -ForegroundColor Red
        exit 1
    }
}
Write-Host "[i] deploying release version: $Version"

# Server-side scripts print UTF-8 Chinese; make the console render it correctly
# while this file itself stays ASCII.
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

# Resolve the deploy directory even if the script body is pasted interactively.
if ($PSScriptRoot) { $DeployDir = $PSScriptRoot } else { $DeployDir = (Get-Location).Path }

# All remote output is mirrored into var\deploy-log.txt so failures can be
# diagnosed from the file instead of scrolling the console.
$LogDir = Join-Path (Split-Path $DeployDir -Parent) "var"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }
$LogPath = Join-Path $LogDir "deploy-log.txt"
"== deploy log $(Get-Date -Format s) ==" | Out-File -FilePath $LogPath

$Files = @(
    (Join-Path $DeployDir "compose.yaml"),
    (Join-Path $DeployDir "systemd\news-digest.service"),
    (Join-Path $DeployDir "systemd\news-digest-resume.service"),
    (Join-Path $DeployDir "systemd\news-digest-wakeup.path"),
    (Join-Path $DeployDir "systemd\news-digest.timer"),
    (Join-Path $DeployDir "nginx\news.conf"),
    (Join-Path $DeployDir "preflight.sh"),
    (Join-Path $DeployDir "bootstrap.sh")
)

function Stop-OnError {
    param([int]$Code, [string]$Step)
    if ($Code -ne 0) {
        Write-Host ""
        Write-Host "[FAIL] Step '$Step' exited with code $Code. Aborting." -ForegroundColor Red
        exit $Code
    }
}

Write-Host "== news-digest server push =="
Write-Host "Server : $Server"
Write-Host "AppDir : $AppDir"
Write-Host "Key    : $KeyPath"
Write-Host ""

# ---- local sanity checks ----
foreach ($tool in @("ssh", "scp")) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        Write-Host "[FAIL] '$tool' not found in PATH. Install the Windows OpenSSH client first." -ForegroundColor Red
        exit 1
    }
}
$SshArgs = @("-i", $KeyPath)
$ScpArgs = @("-i", $KeyPath)
if ($NoPrompt) {
    $transportArgs = @(
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", "ConnectTimeout=20",
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=2"
    )
    $SshArgs = $transportArgs + @("-i", $KeyPath)
    $ScpArgs = $transportArgs + @("-i", $KeyPath)
}
foreach ($f in $Files) {
    if (-not (Test-Path $f)) {
        Write-Host "[FAIL] Missing local file: $f" -ForegroundColor Red
        exit 1
    }
}

$Incoming = "$AppDir/incoming"
$Dest = "${Server}:$Incoming/"

# ---- step 1: create the incoming directory on the server ----
Write-Host "[1/4] Creating $Incoming on the server..."
& ssh @SshArgs $Server "mkdir -p $Incoming"
Stop-OnError $LASTEXITCODE "ssh mkdir incoming"

# ---- step 2: upload artifacts, then normalize them ----
Write-Host "[2/4] Uploading deploy artifacts..."
& scp @ScpArgs $Files $Dest
Stop-OnError $LASTEXITCODE "scp upload"

# Strip CR from every uploaded text file: a Windows checkout may carry CRLF,
# and a trailing CR breaks bash scripts, systemd units and nginx conf files.
& ssh @SshArgs $Server "cd $Incoming && sed -i 's/\r$//' compose.yaml news-digest.service news-digest-resume.service news-digest-wakeup.path news-digest.timer news.conf preflight.sh bootstrap.sh && chmod +x preflight.sh bootstrap.sh"
Stop-OnError $LASTEXITCODE "normalize line endings"

# These values are allow-list validated above before entering the remote POSIX shell.
$deployEnv = "ND_OWNER='$Owner' ND_APP_DIR='$AppDir' ND_DOMAIN='$Domain' " +
    "ND_CERTBOT_EMAIL='$CertbotEmail' ND_WEB_PORT='$WebPort' ND_ADMIN_PORT='$AdminPort' ND_SITE_PORT='$SitePort' " +
    "ND_VERSION='$Version' ND_WORKER_DIGEST='$WorkerDigest' ND_WEB_DIGEST='$WebDigest'"

# ---- step 3: read-only preflight, shown verbatim ----
Write-Host "[3/4] Running preflight.sh (read-only health check)..."
& ssh @SshArgs $Server "$deployEnv bash $Incoming/preflight.sh" 2>&1 |
    ForEach-Object { $_.ToString() } | Tee-Object -FilePath $LogPath -Append
Stop-OnError $LASTEXITCODE "preflight"

Write-Host ""
if (-not $AutoYes) {
    Read-Host "Preflight passed. Press Enter to run bootstrap.sh, or Ctrl+C to abort" | Out-Null
} else {
    Write-Host "Preflight passed; -AutoYes set, continuing to bootstrap."
}

# ---- step 4: idempotent bootstrap ----
# Pin the deploy to the exact validated target, tag and image digests.
Write-Host "[4/4] Running bootstrap.sh (idempotent immutable deploy, ND_VERSION=$Version)..."
& ssh @SshArgs $Server "$deployEnv bash $Incoming/bootstrap.sh" 2>&1 |
    ForEach-Object { $_.ToString() } | Tee-Object -FilePath $LogPath -Append
$BootstrapExit = $LASTEXITCODE

if ($BootstrapExit -eq 3) {
    Write-Host ""
    Write-Host "[NOTE] The server still needs GHCR read access:" -ForegroundColor Yellow
    Write-Host "  ssh -i $KeyPath $Server"
    Write-Host "  docker login ghcr.io -u $Owner"
    Write-Host "  chmod 600 /root/.docker/config.json"
    Write-Host "Then re-run this command with the same release version and digests."
    exit 3
}
if ($BootstrapExit -ne 0) {
    Write-Host ""
    Write-Host "[FAIL] bootstrap.sh exited with code $BootstrapExit - see messages above." -ForegroundColor Red
    exit $BootstrapExit
}

Write-Host ""
Write-Host "[DONE] Deployment completed. Configure API/SMTP in Admin; automatic delivery remains disabled until enabled there."
exit 0
