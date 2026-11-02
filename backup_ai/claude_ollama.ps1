# Claude Code on Ollama - the backup for when the Claude plan's limit is used up.
#
# Double-click "Claude on Ollama.cmd" (or run this file in PowerShell). It opens the same Claude Code
# (terminal, reads/edits files, runs commands, CLAUDE.md, the dashboard tools), but every request goes to
# Ollama - your Ollama Pro cloud models - so it does NOT use your Claude limit.
# Only this window is switched; your normal Claude app and Claude Code stay on your Claude plan.
# Works on the tower and on the laptop (the laptop needs Ollama installed + `ollama signin`, no GPU needed).

# ------------------------------------------------------------------ settings
$MODEL          = "glm-5.3:cloud"         # main model (coding / agent work). Cheaper: "gpt-oss:120b-cloud"
$FAST_MODEL     = "gpt-oss:120b-cloud"    # Claude Code's small background calls (titles, summaries)
$FALLBACK_MODEL = "gpt-oss:120b-cloud"    # used when $MODEL can't be pulled
$OLLAMA_URL     = "http://127.0.0.1:11434"
$WORK_DIR       = ""                      # "" = the "Openflow Repos" folder (the folder above dashboard)
$ADD_DASHBOARD_TOOLS = $true              # add the aws-error-feed tools (ask_tower, find_code, code_change...) once
$FEED_URL       = "https://initium.tail11109.ts.net,http://127.0.0.1:8766"   # dashboard, tried in this order
$PYTHON         = "python"
$INSTALL_CLAUDE_CODE = "irm https://claude.ai/install.ps1 | iex"
# ----------------------------------------------------------------------------

$ErrorActionPreference = "Stop"
$Here  = $PSScriptRoot
$Dash  = Split-Path $Here -Parent
$Repos = Split-Path $Dash -Parent
if (-not $WORK_DIR) { $WORK_DIR = $Repos }

function Say($text, $color = "Gray") { Write-Host $text -ForegroundColor $color }
function Stop-With($text) { Say $text "Red"; Read-Host "Press Enter to close"; exit 1 }

# 1. Ollama running?
try {
    $ver = (Invoke-RestMethod "$OLLAMA_URL/api/version" -TimeoutSec 5).version
    Say "Ollama $ver at $OLLAMA_URL" "Green"
} catch {
    Stop-With ("Ollama isn't running at $OLLAMA_URL.`n" +
               "Install it from https://ollama.com/download, open it, run ``ollama signin`` (your Ollama Pro account), then run this again.")
}

# 2. Models available? (cloud models are a tiny download; they need `ollama signin`)
function Have-Model($m) {
    $names = @((Invoke-RestMethod "$OLLAMA_URL/api/tags" -TimeoutSec 10).models | ForEach-Object { $_.name })
    return ($names -contains $m) -or ($names -contains "$m`:latest")
}
foreach ($m in @($MODEL, $FAST_MODEL) | Select-Object -Unique) {
    if (-not (Have-Model $m)) {
        Say "Getting $m ..." "Yellow"
        & ollama pull $m
        if ($LASTEXITCODE -ne 0 -or -not (Have-Model $m)) {
            if ($m -eq $MODEL -and $MODEL -ne $FALLBACK_MODEL) {
                Say "Couldn't get $MODEL - using $FALLBACK_MODEL instead. (Signed in? Run: ollama signin)" "Yellow"
                $MODEL = $FALLBACK_MODEL
                if (-not (Have-Model $MODEL)) { & ollama pull $MODEL }
            } elseif (-not (Have-Model $m)) {
                Stop-With "Couldn't get $m. Run ``ollama signin`` (Ollama Pro account) and try again."
            }
        }
    }
}

# 3. Claude Code installed?
if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
    $env:Path += ";$env:USERPROFILE\.local\bin"
}
if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
    $ans = Read-Host "Claude Code isn't installed. Install it now? (Y/n)"
    if ($ans -match "^[nN]") { Stop-With "Install it later with: $INSTALL_CLAUDE_CODE" }
    Invoke-Expression $INSTALL_CLAUDE_CODE
    $env:Path += ";$env:USERPROFILE\.local\bin"
    if (-not (Get-Command claude -ErrorAction SilentlyContinue)) { Stop-With "Claude Code still not found - open a new window and run this again." }
}

# 4. Dashboard tools (once, for every Claude Code session on this computer)
if ($ADD_DASHBOARD_TOOLS) {
    $ErrorActionPreference = "Continue"
    & claude mcp get aws-error-feed *> $null
    if ($LASTEXITCODE -ne 0) {
        Say "Adding the dashboard tools (aws-error-feed) to Claude Code..." "Yellow"
        & claude mcp add aws-error-feed --scope user `
            -e "AWS_ERROR_FEED_URL=$FEED_URL" -e "AWS_ERROR_FEED_REPOS=$Repos" -e "AWS_ERROR_FEED_AUTOSTART=0" `
            -- $PYTHON "$Dash\new_app\mcp_server.py"
    }
    $ErrorActionPreference = "Stop"
}

# 5. Point this window's Claude Code at Ollama
Remove-Item Env:ANTHROPIC_API_KEY -ErrorAction SilentlyContinue
$env:ANTHROPIC_BASE_URL             = $OLLAMA_URL
$env:ANTHROPIC_AUTH_TOKEN           = "ollama"
$env:ANTHROPIC_MODEL                = $MODEL
$env:ANTHROPIC_DEFAULT_OPUS_MODEL   = $MODEL
$env:ANTHROPIC_DEFAULT_SONNET_MODEL = $MODEL
$env:ANTHROPIC_DEFAULT_HAIKU_MODEL  = $FAST_MODEL
$env:ANTHROPIC_SMALL_FAST_MODEL     = $FAST_MODEL
$env:CLAUDE_CODE_SUBAGENT_MODEL     = $MODEL
$env:CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC = "1"
$env:API_TIMEOUT_MS                 = "600000"

$Host.UI.RawUI.WindowTitle = "Claude Code on Ollama ($MODEL)"
Say ""
Say "Claude Code on Ollama - model $MODEL (background: $FAST_MODEL)" "Cyan"
Say "Folder: $WORK_DIR" "Cyan"
Say "Not using your Claude limit. Same habits: ask_tower / find_code / review_change, then edit and local_run." "Cyan"
Say "Weaker than Claude on tricky changes: keep tasks small and check every diff." "DarkCyan"
Say ""
Set-Location $WORK_DIR
& claude @args
