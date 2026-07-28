# deploy-all.ps1 -- one-click stage 7 deployment orchestrator (run via deploy.bat).
# PowerShell 5.1 compatible; ASCII-only source.
# Flow: ssh-agent (passphrase once) -> tag preflight (never move a published tag) ->
#       git push (triggers CI on tag) -> wait for GitHub Actions build -> initialize
#       config/.env only when the server has no runtime config yet ->
#       server GHCR login with a READ-ONLY read:packages PAT ->
#       server-push.ps1 -AutoYes -Version (upload + preflight + bootstrap) -> smoke check.

param(
    [switch]$Elevated,   # internal: set after the interactive UAC relaunch
    [switch]$Interactive # opt in to prompts; direct deploy-all/Hermes defaults to automation
)
$NoPrompt = -not $Interactive

$KeyPath = "C:\Users\Admin\.ssh\id_ed25519"
$Server  = "root@cheapcoding.top"
$Owner   = "yi-lings"
$AppDir  = "/srv/news-digest"
# $Version is single-sourced from the package below (not hardcoded), so a release
# version only ever changes in src/news_digest/__init__.py.

try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$RepoDir = Split-Path $PSScriptRoot -Parent
Set-Location $RepoDir

# In automation, native tools must never open credential/confirmation prompts. PowerShell's
# -NonInteractive flag does not constrain git/gh/ssh themselves, so disable their prompts too.
if ($NoPrompt) {
    $env:GIT_TERMINAL_PROMPT = "0"
    $env:GH_PROMPT_DISABLED = "1"
}

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
# These values later enter a remote POSIX shell. Strict allow-lists make single-quoted
# image references safe and also enforce the Docker tag grammar expected by GHCR.
if ($Version -notmatch '^v[A-Za-z0-9_][A-Za-z0-9_.-]{0,126}$') {
    Write-Host "[FAIL] Invalid Docker tag derived from __version__: $Version" -ForegroundColor Red
    exit 1
}
if ($Owner -notmatch '^[A-Za-z0-9][A-Za-z0-9-]*$') {
    Write-Host "[FAIL] Invalid GHCR owner: $Owner" -ForegroundColor Red
    exit 1
}
Write-Host "[i] release version (from __init__.py): $Version"

function Stop-OnError {
    param([int]$Code, [string]$Step)
    if ($Code -ne 0) {
        Write-Host ""
        Write-Host "[FAIL] Step '$Step' exited with code $Code. Aborting." -ForegroundColor Red
        if ($Elevated -and -not $NoPrompt) { Read-Host "Press Enter to close" | Out-Null }
        exit $Code
    }
}

function ConvertFrom-ProjectDotEnvValue {
    param([string]$Value)
    $trimmed = $Value.Trim()
    if ($trimmed.Length -ge 2 -and $trimmed[0] -eq '"' -and $trimmed[$trimmed.Length - 1] -eq '"') {
        try {
            $parsed = $trimmed | ConvertFrom-Json -ErrorAction Stop
            if ($parsed -is [string]) { return $parsed }
        } catch { }
    }
    if ($trimmed.Length -ge 2 -and $trimmed[0] -eq "'" -and $trimmed[$trimmed.Length - 1] -eq "'") {
        return $trimmed.Substring(1, $trimmed.Length - 2)
    }
    return $trimmed
}

function ConvertFrom-SmtpPasswordEnvValue {
    param([string]$Value)
    $prefix = "nd-b64-v1:"
    if (-not $Value.StartsWith($prefix)) { return $Value }
    $encoded = $Value.Substring($prefix.Length)
    if (-not $encoded) { return $Value }
    try {
        $decoded = [Convert]::FromBase64String($encoded)
        if ([Convert]::ToBase64String($decoded) -cne $encoded) { return $Value }
        $strictUtf8 = New-Object System.Text.UTF8Encoding -ArgumentList $false, $true
        return $strictUtf8.GetString($decoded)
    } catch {
        return $Value
    }
}

function ConvertTo-SmtpPasswordEnvValue {
    param([string]$Value)
    if (-not $Value) { return "" }
    return "nd-b64-v1:" + [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Value))
}

foreach ($tool in @("ssh", "scp", "git", "gh")) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        Write-Host "[FAIL] '$tool' not found in PATH." -ForegroundColor Red
        exit 1
    }
}

# Native OpenSSH options for every call below. In Hermes mode, forbid all password,
# passphrase and host-key prompts; bound dead connections and detect stalls quickly.
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
            if ($NoPrompt) {
                Write-Host "[FAIL] ssh-agent needs one-time administrator setup; -NoPrompt cannot accept a UAC prompt." -ForegroundColor Red
                Write-Host "       Run deploy.bat once interactively, or enable/start ssh-agent before automation." -ForegroundColor Red
                exit 1
            }
            Write-Host "Enabling ssh-agent needs admin once; relaunching elevated - accept the UAC prompt." -ForegroundColor Yellow
            Start-Process powershell -Verb RunAs -ArgumentList @(
                "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"", "-Elevated", "-Interactive"
            )
            exit 0
        }
        Write-Host "[FAIL] Cannot enable ssh-agent even as admin: $($_.Exception.Message)" -ForegroundColor Red
        exit 1
    }
} else {
    if ($NoPrompt) {
        Write-Host "[FAIL] Windows ssh-agent service not found; -NoPrompt forbids SSH passphrase/password prompts." -ForegroundColor Red
        exit 1
    }
    Write-Host "[NOTE] ssh-agent service not found; ssh may prompt for the passphrase several times." -ForegroundColor Yellow
}
if ($agentReady) {
    ssh-add -l *> $null
    if ($LASTEXITCODE -ne 0) {
        if ($NoPrompt) {
            Write-Host "[FAIL] SSH key is not loaded; -NoPrompt cannot ask for its passphrase." -ForegroundColor Red
            Write-Host "       Run 'ssh-add $KeyPath' interactively first, then retry." -ForegroundColor Red
            exit 1
        }
        Write-Host "Enter the SSH key passphrase once; it stays in ssh-agent afterwards."
        ssh-add $KeyPath
        Stop-OnError $LASTEXITCODE "ssh-add"
    }
}

# Fail before touching Git/CI/config if automation cannot authenticate to the server.
if ($NoPrompt) {
    & ssh @SshArgs $Server "true" *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[FAIL] Non-interactive SSH preflight failed." -ForegroundColor Red
        Write-Host "       Load $KeyPath into ssh-agent and ensure the server host key is already trusted." -ForegroundColor Red
        exit 1
    }
}

# ---- tag preflight: release the current commit; never move a published tag ----
$headSha = "$(git rev-parse HEAD 2>$null)".Trim()
$mainSha = "$(git rev-parse main 2>$null)".Trim()
if (-not $headSha -or -not $mainSha) {
    Write-Host "[FAIL] cannot resolve HEAD and main." -ForegroundColor Red
    exit 1
}
if ($mainSha -ne $headSha) {
    Write-Host "[FAIL] release HEAD is not the current main commit." -ForegroundColor Red
    Write-Host "       Merge or fast-forward the reviewed release commit into main before tagging." -ForegroundColor Red
    exit 1
}
$worktreeChanges = @(git status --porcelain --untracked-files=normal 2>$null)
if ($LASTEXITCODE -ne 0) {
    Write-Host "[FAIL] cannot inspect git worktree state." -ForegroundColor Red
    exit 1
}
if ($worktreeChanges.Count -gt 0) {
    Write-Host "[FAIL] release worktree is dirty; commit or remove every change before publishing $Version." -ForegroundColor Red
    exit 1
}
$localTagSha = "$(git rev-list -n 1 $Version 2>$null)".Trim()
if (-not $localTagSha) {
    Write-Host "[FAIL] local tag $Version does not exist. Tag the release commit first:" -ForegroundColor Red
    Write-Host "         git tag -a $Version -m $Version" -ForegroundColor Red
    exit 1
}
if ($localTagSha -ne $headSha) {
    Write-Host "[FAIL] tag $Version is at $($localTagSha.Substring(0,12)), not HEAD $($headSha.Substring(0,12))." -ForegroundColor Red
    Write-Host "       Bump __version__, commit, then create a new immutable release tag at HEAD." -ForegroundColor Red
    exit 1
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
Write-Host "[1/6] git push (reviewed main, then release tag $Version)..."
git push origin main
Stop-OnError $LASTEXITCODE "git push main"
git push origin $Version
Stop-OnError $LASTEXITCODE "git push release tag"

# ---- [2/6] locate THIS tag's release run; completed reruns are idempotent ----
# The old `--limit 1` query could pick an unrelated newer tag. A retry after the tag has
# already been pushed creates no new workflow, so find the existing run by tag+event and
# verify its commit. If it already succeeded, do not spend Hermes' timeout watching it.
Write-Host "[2/6] Locating GitHub Actions release run for $Version..."
$run = $null
for ($i = 0; $i -lt 30 -and -not $run; $i++) {
    $json = gh run list --workflow release.yml --branch $Version --event push --limit 1 --json databaseId,status,conclusion,headBranch,headSha 2>$null
    $listCode = $LASTEXITCODE
    if ($listCode -ne 0) {
        Write-Host "[FAIL] Cannot query the GitHub Actions release run for $Version." -ForegroundColor Red
        exit $listCode
    }
    if ($json) {
        $candidate = @($json | ConvertFrom-Json) | Select-Object -First 1
        if ($candidate) {
            if ($candidate.headBranch -ne $Version) {
                Write-Host "[FAIL] Release run belongs to '$($candidate.headBranch)', not '$Version'." -ForegroundColor Red
                exit 1
            }
            if ($candidate.headSha -ne $localTagSha) {
                Write-Host "[FAIL] Release run SHA $($candidate.headSha) does not match tag SHA $localTagSha." -ForegroundColor Red
                exit 1
            }
            $run = $candidate
        }
    }
    if (-not $run -and $i -lt 29) { Start-Sleep -Seconds 5 }
}
if (-not $run) {
    Write-Host "[FAIL] No release workflow run appeared for $Version; check the Actions tab." -ForegroundColor Red
    exit 1
}
$runId = $run.databaseId
if ($run.status -eq "completed") {
    if ($run.conclusion -ne "success") {
        Write-Host "[FAIL] Release run $runId concluded '$($run.conclusion)'." -ForegroundColor Red
        exit 1
    }
    Write-Host "[i] Release run $runId already completed successfully; continuing without watch."
} else {
    if ($NoPrompt) {
        Write-Host "[NOTE] Release run $runId is '$($run.status)'. Images are not ready yet; retry after CI completes." -ForegroundColor Yellow
        exit 4
    }
    Write-Host "[i] Release run $runId is '$($run.status)'; waiting for completion..."
    gh run watch $runId --exit-status
    Stop-OnError $LASTEXITCODE "CI build (gh run watch)"
}

# The release job publishes the exact build outputs as a small machine-readable asset.
# Resolve them from this tag's Release rather than from mutable image tags or registry
# listing order, then carry the same immutable references through every deployment step.
$digestDir = Join-Path ([IO.Path]::GetTempPath()) ("news-digest-digests-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $digestDir | Out-Null
try {
    gh release download $Version --pattern "digests.env" --dir $digestDir --clobber
    Stop-OnError $LASTEXITCODE "download release digests"
    $digestValues = @{}
    foreach ($line in Get-Content (Join-Path $digestDir "digests.env")) {
        $parts = "$line".Trim().Split("=", 2)
        if ($parts.Count -ne 2 -or $digestValues.ContainsKey($parts[0])) {
            Write-Host "[FAIL] malformed or duplicate entry in release digests.env." -ForegroundColor Red
            exit 1
        }
        $digestValues[$parts[0]] = $parts[1]
    }
    $allowedDigestKeys = @("ND_VERSION", "ND_WORKER_DIGEST", "ND_WEB_DIGEST")
    $unknownDigestKeys = @($digestValues.Keys | Where-Object { $_ -notin $allowedDigestKeys })
    if ($unknownDigestKeys.Count -gt 0 -or $digestValues.Count -ne 3) {
        Write-Host "[FAIL] release digests.env has an unexpected key set." -ForegroundColor Red
        exit 1
    }
    if ($digestValues["ND_VERSION"] -ne $Version) {
        Write-Host "[FAIL] release digest version does not match $Version." -ForegroundColor Red
        exit 1
    }
    $WorkerDigest = $digestValues["ND_WORKER_DIGEST"]
    $WebDigest = $digestValues["ND_WEB_DIGEST"]
    if ($WorkerDigest -notmatch '^sha256:[0-9a-f]{64}$' -or $WebDigest -notmatch '^sha256:[0-9a-f]{64}$') {
        Write-Host "[FAIL] release image digest is invalid." -ForegroundColor Red
        exit 1
    }
} finally {
    Remove-Item $digestDir -Recurse -Force -ErrorAction SilentlyContinue
}

# Automation must prove it has the dedicated read:packages PAT BEFORE staging the
# temporary secret file. Previous timed-out runs may have left .env.incoming behind;
# remove that deploy-all-owned staging file when the prerequisite is missing.
if ($NoPrompt -and -not $env:ND_GHCR_TOKEN) {
    & ssh @SshArgs $Server "rm -f '$AppDir/config/.env.incoming'" *> $null
    Write-Host "[FAIL] ND_GHCR_TOKEN is not set; deployment stopped before staging secrets." -ForegroundColor Red
    Write-Host "       Set it to a dedicated read:packages PAT in this process, then retry." -ForegroundColor Red
    exit 1
}

# ---- [3/6] initialize runtime config once; Admin owns it after installation ----
Write-Host "[3/6] Checking runtime config ownership..."
& ssh @SshArgs $Server "test -f '$AppDir/config/.env'"
$remoteConfigStatus = $LASTEXITCODE
if ($remoteConfigStatus -eq 0) {
    & ssh @SshArgs $Server "rm -f '$AppDir/config/.env.incoming'"
    Stop-OnError $LASTEXITCODE "remove stale incoming runtime config"
    Write-Host "[i] Existing server config is Admin/operator-owned; deployment will not overwrite it."
} elseif ($remoteConfigStatus -eq 1) {
    Write-Host "[i] First installation: staging initial runtime keys from local .env.local..."
    $envLocal = Join-Path $RepoDir ".env.local"
    if (-not (Test-Path $envLocal)) {
        Write-Host "[FAIL] .env.local not found for first-install initialization." -ForegroundColor Red
        exit 1
    }
    $pairs = @{}
    foreach ($line in Get-Content $envLocal) {
        $t = "$line".Trim()
        if (-not $t -or $t.StartsWith("#") -or -not $t.Contains("=")) { continue }
        $parts = $t.Split("=", 2)
        $k = $parts[0].Trim()
        $v = ConvertFrom-ProjectDotEnvValue $parts[1]
        if ($k -eq "SMTP_PASSWORD") { $v = ConvertFrom-SmtpPasswordEnvValue $v }
        if ($k) { $pairs[$k] = $v }
    }
    $defaults = @{
        "TRANSLATION_API_TYPE" = "openai_chat"; "TRANSLATION_STREAM" = "true";
        "EMAIL_DELIVERY_ENABLED" = "false"; "SMTP_USERNAME" = ""; "SMTP_PASSWORD" = "";
        "SMTP_HOST" = ""; "SMTP_PORT" = "465"; "SMTP_FROM" = ""; "SMTP_RECIPIENTS" = "";
        "EMAIL_MAINS_ENABLED" = "true"; "EMAIL_BRIEFS_ENABLED" = "true";
        "EMAIL_MAIN_LIMIT" = "6"; "EMAIL_BRIEF_LIMIT" = "5"; "EMAIL_LANGUAGE" = "bi";
        "EMAIL_SOURCE_FILTERS" = ""; "EMAIL_LAYOUT" = "digest";
        "EMAIL_SUMMARY_LENGTH" = "standard"; "EMAIL_CATCHUP_WINDOW_HOURS" = "6";
        "PUBLIC_SUBSCRIPTION_ENABLED" = "false"
    }
    foreach ($key in $defaults.Keys) {
        if (-not $pairs.ContainsKey($key)) { $pairs[$key] = $defaults[$key] }
    }
    if ($pairs["EMAIL_DELIVERY_ENABLED"] -notin @("true", "false")) {
        Write-Host "[FAIL] EMAIL_DELIVERY_ENABLED must be true or false." -ForegroundColor Red
        exit 1
    }
    if (-not $pairs.ContainsKey("SMTP_SECURITY")) {
        if ($pairs.ContainsKey("SMTP_USE_TLS") -and $pairs["SMTP_USE_TLS"] -notin @("true", "false")) {
            Write-Host "[FAIL] legacy SMTP_USE_TLS must be true or false." -ForegroundColor Red
            exit 1
        }
        $pairs["SMTP_SECURITY"] = if ($pairs["SMTP_USE_TLS"] -eq "false") { "starttls" } else { "implicit_tls" }
    }
    if ($pairs["SMTP_SECURITY"] -notin @("implicit_tls", "starttls")) {
        Write-Host "[FAIL] SMTP_SECURITY must be implicit_tls or starttls." -ForegroundColor Red
        exit 1
    }
    $required = @("TRANSLATION_API_BASE_URL", "TRANSLATION_API_KEY", "TRANSLATION_MODEL")
    if ($pairs["EMAIL_DELIVERY_ENABLED"] -eq "true") {
        $required += @("SMTP_HOST", "SMTP_PORT", "SMTP_FROM")
    }
    $missing = @($required | Where-Object { -not $pairs.ContainsKey($_) -or -not $pairs[$_] })
    if ($missing.Count -gt 0) {
        Write-Host "[FAIL] .env.local is missing first-install values: $($missing -join ', ')" -ForegroundColor Red
        exit 1
    }
    $keys = @(
        "TRANSLATION_API_BASE_URL", "TRANSLATION_API_KEY", "TRANSLATION_MODEL",
        "TRANSLATION_API_TYPE", "TRANSLATION_STREAM", "EMAIL_DELIVERY_ENABLED",
        "SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_SECURITY",
        "SMTP_FROM", "SMTP_RECIPIENTS", "EMAIL_MAINS_ENABLED", "EMAIL_BRIEFS_ENABLED",
        "EMAIL_MAIN_LIMIT", "EMAIL_BRIEF_LIMIT", "EMAIL_LANGUAGE", "EMAIL_SOURCE_FILTERS",
        "EMAIL_LAYOUT", "EMAIL_SUMMARY_LENGTH", "EMAIL_CATCHUP_WINDOW_HOURS",
        "PUBLIC_SUBSCRIPTION_ENABLED"
    )
    $lines = @("# first-install runtime keys; Admin/operator owns config/.env after bootstrap")
    $lines += "NEWS_SITE_URL=https://news.cheapcoding.top"
    $pairs["SMTP_PASSWORD"] = ConvertTo-SmtpPasswordEnvValue $pairs["SMTP_PASSWORD"]
    foreach ($key in $keys) { $lines += "$key=$($pairs[$key])" }
    $tmp = [IO.Path]::GetTempFileName()
    try {
        [IO.File]::WriteAllText($tmp, (($lines -join "`n") + "`n"))
        & ssh @SshArgs $Server "mkdir -p $AppDir/config && chmod 750 $AppDir/config"
        Stop-OnError $LASTEXITCODE "ssh mkdir config dir"
        & scp @ScpArgs $tmp "${Server}:$AppDir/config/.env.incoming"
        Stop-OnError $LASTEXITCODE "scp .env.incoming"
        & ssh @SshArgs $Server "chmod 600 $AppDir/config/.env.incoming"
        Stop-OnError $LASTEXITCODE "chmod .env.incoming"
    } finally {
        Remove-Item $tmp -Force -ErrorAction SilentlyContinue
    }
    Write-Host "[i] Initial runtime config staged; later deployments preserve server values."
} else {
    Stop-OnError $remoteConfigStatus "check server runtime config"
}

# ---- [4/6] install and verify a dedicated READ-ONLY server credential ----
# P0-3 deliberately forbids reusing an unknown old Docker credential: registry access
# proves readability, not token scope. The operator must supply a PAT created with only
# read:packages. Automation supplies it via the process environment; interactive runs may
# paste it into a hidden prompt. It never appears on argv or in logs.
Write-Host "[4/6] Installing and verifying the server GHCR read-only credential..."
$workerImage = "ghcr.io/$Owner/news-digest-worker@$WorkerDigest"
$webImage = "ghcr.io/$Owner/news-digest-web@$WebDigest"
$ghcrToken = $env:ND_GHCR_TOKEN
if ($ghcrToken) {
    # Stop later child processes inheriting the PAT. This cannot alter the parent process.
    Remove-Item Env:ND_GHCR_TOKEN -ErrorAction SilentlyContinue
} elseif ($NoPrompt) {
    Write-Host "[FAIL] ND_GHCR_TOKEN is not set; unattended deployment cannot open the hidden PAT prompt." -ForegroundColor Red
    Write-Host "       Set it to a dedicated read:packages PAT for this process, then retry." -ForegroundColor Red
    exit 1
} else {
    $sec = Read-Host "Paste a GHCR read:packages PAT for the server (input hidden)" -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
    try { $ghcrToken = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}
if (-not $ghcrToken) {
    Write-Host "[FAIL] No GHCR token provided." -ForegroundColor Red
    exit 1
}
try {
    # A successful login alone does not prove package access; probe both exact release
    # images before continuing. GNU timeout bounds registry hangs on the server.
    $remoteLogin = "set -e; umask 077; " +
        "docker login ghcr.io -u '$Owner' --password-stdin; " +
        "chmod 600 /root/.docker/config.json; " +
        "timeout 90 docker manifest inspect '$workerImage' >/dev/null 2>&1; " +
        "timeout 90 docker manifest inspect '$webImage' >/dev/null 2>&1"
    $ghcrToken | & ssh @SshArgs $Server $remoteLogin
    $loginCode = $LASTEXITCODE
} finally {
    $ghcrToken = $null
}
Stop-OnError $loginCode "server GHCR login/image access"
Write-Host "[i] GHCR credential can read both exact $Version images."

# ---- [5/6] upload artifacts, preflight, bootstrap ----
# Pass the single-sourced $Version through so bootstrap deploys the exact tag we just
# built/pushed, instead of server-push falling back to its own hardcoded default.
Write-Host "[5/6] Running server-push (upload + preflight + bootstrap)..."
& (Join-Path $PSScriptRoot "server-push.ps1") -AutoYes -NoPrompt:$NoPrompt `
    -Version $Version -WorkerDigest $WorkerDigest -WebDigest $WebDigest
Stop-OnError $LASTEXITCODE "server-push"

# ---- [6/6] smoke check: every failure is fatal; print DONE only after all checks ----
Write-Host "[6/6] Final smoke check..."
$smokeCommand = "set -e; " +
    "code=`$(curl -sk --max-time 15 -o /dev/null -w '%{http_code}' https://news.cheapcoding.top/); " +
    "test `"`$code`" = 200; echo `"https status: `$code`"; " +
    "systemctl is-enabled --quiet news-digest.timer; " +
    "systemctl is-active --quiet news-digest.timer; " +
    "systemctl list-timers news-digest.timer --no-pager | head -3"
& ssh @SshArgs $Server $smokeCommand
Stop-OnError $LASTEXITCODE "final smoke check"

Write-Host ""
Write-Host "[DONE] https://news.cheapcoding.top/  (daily rebuild: 08:00 Asia/Shanghai)"
if ($Elevated -and -not $NoPrompt) { Read-Host "Press Enter to close" | Out-Null }
exit 0
