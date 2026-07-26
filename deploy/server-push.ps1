# server-push.ps1 -- news-digest stage 7: upload deploy artifacts and run remote scripts.
# PowerShell 5.1 compatible; ASCII-only file, because PS 5.1 misreads non-ASCII
# .ps1 files without a BOM and mixed codepages garble console output.
# Flow: ssh mkdir incoming -> scp artifacts -> ssh preflight (read-only) ->
#       pause for confirmation -> ssh bootstrap. Any non-zero step aborts.
# Secrets never pass through this script: .env is edited on the server itself,
# and the GHCR token is pasted by the user into docker login on the server.

# ---- variables ----
$KeyPath = "C:\Users\Admin\.ssh\id_ed25519"
$Server  = "root@cheapcoding.top"
$AppDir  = "/srv/news-digest"

# Server-side scripts print UTF-8 Chinese; make the console render it correctly
# while this file itself stays ASCII.
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

# Resolve the deploy directory even if the script body is pasted interactively.
if ($PSScriptRoot) { $DeployDir = $PSScriptRoot } else { $DeployDir = (Get-Location).Path }

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
ssh -i $KeyPath $Server "mkdir -p $Incoming"
Stop-OnError $LASTEXITCODE "ssh mkdir incoming"

# ---- step 2: upload artifacts, then normalize them ----
Write-Host "[2/4] Uploading deploy artifacts..."
scp -i $KeyPath $Files $Dest
Stop-OnError $LASTEXITCODE "scp upload"

# Strip CR from every uploaded text file: a Windows checkout may carry CRLF,
# and a trailing CR breaks bash scripts, systemd units and nginx conf files.
ssh -i $KeyPath $Server "cd $Incoming && sed -i 's/\r$//' compose.yaml news-digest.service news-digest.timer news.conf preflight.sh bootstrap.sh && chmod +x preflight.sh bootstrap.sh"
Stop-OnError $LASTEXITCODE "normalize line endings"

# ---- step 3: read-only preflight, shown verbatim ----
Write-Host "[3/4] Running preflight.sh (read-only health check)..."
ssh -i $KeyPath $Server "bash $Incoming/preflight.sh"
Stop-OnError $LASTEXITCODE "preflight"

Write-Host ""
Read-Host "Preflight passed. Press Enter to run bootstrap.sh, or Ctrl+C to abort" | Out-Null

# ---- step 4: idempotent bootstrap ----
Write-Host "[4/4] Running bootstrap.sh (idempotent deploy)..."
ssh -i $KeyPath $Server "bash $Incoming/bootstrap.sh"
$BootstrapExit = $LASTEXITCODE

# Bootstrap stops itself on purpose when manual input is required
# (exit 2: fill .env, exit 3: docker login). Print the follow-ups either way.
Write-Host ""
Write-Host "== Follow-up steps (manual, on the server) =="
Write-Host "If bootstrap stopped on purpose, finish the matching step and re-run:"
Write-Host "  1. exit 2 - fill secrets:  ssh -i $KeyPath $Server"
Write-Host "     then edit $AppDir/.env with an editor (secrets never pass through this script)"
Write-Host "  2. exit 3 - GHCR login:    docker login ghcr.io -u yi-lings"
Write-Host "     paste a read:packages token at the prompt, then: chmod 600 /root/.docker/config.json"
Write-Host "  3. Re-run this script (or on the server: bash $Incoming/bootstrap.sh)"
Write-Host "     and repeat until bootstrap reports completion."

if ($BootstrapExit -ne 0) {
    Write-Host ""
    Write-Host "[NOTE] bootstrap.sh exited with code $BootstrapExit - see messages above." -ForegroundColor Yellow
    exit $BootstrapExit
}

Write-Host ""
Write-Host "[DONE] All steps completed."
exit 0
