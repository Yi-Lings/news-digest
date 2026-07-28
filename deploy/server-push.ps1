# server-push.ps1 -- news-digest stage 7: upload deploy artifacts and run remote scripts.
# PowerShell 5.1 compatible; ASCII-only file, because PS 5.1 misreads non-ASCII
# .ps1 files without a BOM and mixed codepages garble console output.
# Flow: ssh mkdir incoming -> scp artifacts -> ssh preflight (read-only) ->
#       pause for confirmation -> ssh bootstrap. Any non-zero step aborts.
# Secrets never pass through this script: .env is edited on the server itself,
# and the GHCR token is pasted by the user into docker login on the server.

param(
    [switch]$AutoYes,        # -AutoYes: skip the pause between preflight and bootstrap
    [switch]$NoPrompt,       # automation: native SSH/SCP must fail, never prompt
    [string]$Version,        # release tag to deploy; passed through to bootstrap as ND_VERSION
    [string]$WorkerDigest,   # immutable worker digest from this tag's Release asset
    [string]$WebDigest       # immutable web digest from this tag's Release asset
)

# ---- variables ----
$KeyPath = "C:\Users\Admin\.ssh\id_ed25519"
$Server  = "root@cheapcoding.top"
$AppDir  = "/srv/news-digest"

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
if (-not (Test-Path $KeyPath)) {
    Write-Host "[FAIL] SSH key not found: $KeyPath" -ForegroundColor Red
    exit 1
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
& ssh @SshArgs $Server "cd $Incoming && sed -i 's/\r$//' compose.yaml news-digest.service news-digest.timer news.conf preflight.sh bootstrap.sh && chmod +x preflight.sh bootstrap.sh"
Stop-OnError $LASTEXITCODE "normalize line endings"

# ---- step 3: read-only preflight, shown verbatim ----
Write-Host "[3/4] Running preflight.sh (read-only health check)..."
& ssh @SshArgs $Server "bash $Incoming/preflight.sh" 2>&1 | Tee-Object -FilePath $LogPath -Append
Stop-OnError $LASTEXITCODE "preflight"

Write-Host ""
if (-not $AutoYes) {
    Read-Host "Preflight passed. Press Enter to run bootstrap.sh, or Ctrl+C to abort" | Out-Null
} else {
    Write-Host "Preflight passed; -AutoYes set, continuing to bootstrap."
}

# ---- step 4: idempotent bootstrap ----
# Pin the deploy to the resolved tag via ND_VERSION so bootstrap pulls the exact image
# we just built, rather than its own in-script default. Single word, no secret, safe on argv.
$bootstrapEnv = "ND_VERSION='$Version' ND_WORKER_DIGEST='$WorkerDigest' ND_WEB_DIGEST='$WebDigest'"
Write-Host "[4/4] Running bootstrap.sh (idempotent immutable deploy, ND_VERSION=$Version)..."
& ssh @SshArgs $Server "$bootstrapEnv bash $Incoming/bootstrap.sh" 2>&1 | Tee-Object -FilePath $LogPath -Append
$BootstrapExit = $LASTEXITCODE

# Bootstrap stops itself on purpose when manual input is required
# (exit 2: fill .env, exit 3: docker login). Print the follow-ups either way.
Write-Host ""
Write-Host "== Follow-up steps (manual, on the server) =="
Write-Host "If bootstrap stopped on purpose, finish the matching step and re-run:"
Write-Host "  1. exit 2 - fill secrets:  ssh -i $KeyPath $Server"
Write-Host "     then edit $AppDir/config/.env with an editor (secrets never pass through this script)"
Write-Host "  2. exit 3 - GHCR login:    docker login ghcr.io -u yi-lings"
Write-Host "     paste a read:packages token at the prompt, then: chmod 600 /root/.docker/config.json"
Write-Host "  3. Re-run this script so the same release version and digests are passed again."
Write-Host "     and repeat until bootstrap reports completion."

if ($BootstrapExit -ne 0) {
    Write-Host ""
    Write-Host "[NOTE] bootstrap.sh exited with code $BootstrapExit - see messages above." -ForegroundColor Yellow
    exit $BootstrapExit
}

Write-Host ""
Write-Host "[DONE] All steps completed."
exit 0
