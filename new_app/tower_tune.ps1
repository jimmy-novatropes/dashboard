<#
Tunes the tower (initium) for the AWS Error Feed's local model, then restarts Ollama and the dashboard.

  - Ollama: flash attention + q8_0 KV cache (context needs half the VRAM), model kept loaded 24h
    (no reloads), one model at a time.
  - Finds the biggest context (16k -> 12k -> 8k) that still fits 100% on the GPU and writes it to
    new_app\config.json (local_llm.num_ctx).
  - Installs drain3 (shadow grouping stats in the Health tab).
  - Writes a "tuning_note" to config.json so the Health tab's day-by-day table shows what changed when.

Run on the tower, in a normal (not admin) PowerShell, from the dashboard folder:
  powershell -ExecutionPolicy Bypass -File ".\new_app\tower_tune.ps1"
Undo everything:
  powershell -ExecutionPolicy Bypass -File ".\new_app\tower_tune.ps1" -Undo
#>
param([switch]$Undo)

# ------------------------------------------------------------------ settings
$FlashAttention  = "1"
$KvCacheType     = "q8_0"          # "f16" is Ollama's default; q8_0 halves context memory, ~no quality loss
$KeepAlive       = "24h"           # keep the model in VRAM between calls
$NumParallel     = "1"             # 12 GB card: one call at a time (the dashboard puts Claude's requests first)
$MaxLoadedModels = "1"
$ContextsToTry   = @(16384, 12288, 8192)   # biggest first; first one that fits 100% on the GPU wins
$UndoNumCtx      = 8192
$Drain3Mode      = "shadow"        # "shadow" = measure only, "off" = disable
$Python          = "C:\Users\jagom\AppData\Local\Programs\Python\Python311\python.exe"
$AppDir          = $PSScriptRoot
$ConfigFile      = Join-Path $AppDir "config.json"
$TaskName        = "AWS Error Feed"
$OllamaUrl       = "http://127.0.0.1:11434"
$OllamaApp       = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama app.exe"
$OllamaCli       = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"

$ErrorActionPreference = "Stop"
function Say($t) { Write-Host "==> $t" -ForegroundColor Cyan }

# ------------------------------------------------------------------ 1. Ollama environment
$envVars = [ordered]@{
    "OLLAMA_FLASH_ATTENTION"   = $FlashAttention
    "OLLAMA_KV_CACHE_TYPE"     = $KvCacheType
    "OLLAMA_KEEP_ALIVE"        = $KeepAlive
    "OLLAMA_NUM_PARALLEL"      = $NumParallel
    "OLLAMA_MAX_LOADED_MODELS" = $MaxLoadedModels
}
foreach ($k in $envVars.Keys) {
    $v = if ($Undo) { $null } else { $envVars[$k] }
    [Environment]::SetEnvironmentVariable($k, $v, "User")
    if ($v) { Set-Item -Path "env:$k" -Value $v } else { Remove-Item -Path "env:$k" -ErrorAction SilentlyContinue }
}
Say ($(if ($Undo) { "Ollama settings removed" } else { "Ollama settings: " + (($envVars.GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)" }) -join " ") }))

# ------------------------------------------------------------------ 2. drain3
if (-not $Undo) {
    Say "Installing drain3"
    & $Python -m pip install --quiet --disable-pip-version-check drain3
}

# ------------------------------------------------------------------ 3. restart Ollama
function Restart-Ollama {
    Get-Process -Name "ollama app", "ollama" -ErrorAction SilentlyContinue | Stop-Process -Force
    Start-Sleep -Seconds 2
    if (Test-Path $OllamaApp) { Start-Process -FilePath $OllamaApp }
    elseif (Test-Path $OllamaCli) { Start-Process -FilePath $OllamaCli -ArgumentList "serve" -WindowStyle Hidden }
    else { Start-Process -FilePath "ollama" -ArgumentList "serve" -WindowStyle Hidden }
    for ($i = 0; $i -lt 30; $i++) {
        try { Invoke-RestMethod "$OllamaUrl/api/version" -TimeoutSec 2 | Out-Null; return $true } catch { Start-Sleep 1 }
    }
    return $false
}
Say "Restarting Ollama"
if (-not (Restart-Ollama)) { Write-Warning "Ollama didn't come back within 30 s - start it from the Start menu and re-run." ; exit 1 }

# ------------------------------------------------------------------ 4. config.json
if (Test-Path $ConfigFile) {
    Copy-Item $ConfigFile "$ConfigFile.bak-$(Get-Date -Format yyyyMMdd-HHmm)"
    $cfg = Get-Content $ConfigFile -Raw | ConvertFrom-Json
} else { $cfg = [pscustomobject]@{} }
if (-not $cfg.local_llm) { $cfg | Add-Member -Force local_llm ([pscustomobject]@{}) }
if (-not $cfg.grouping)  { $cfg | Add-Member -Force grouping ([pscustomobject]@{}) }
$model = if ($cfg.local_llm.model) { $cfg.local_llm.model } else { "qwen3:14b" }

function Fits($ctx) {
    # load the model with this context and check how much of it Ollama put on the GPU
    $body = @{ model = $model; prompt = "ok"; stream = $false; keep_alive = $KeepAlive;
               options = @{ num_ctx = $ctx; num_predict = 1 } } | ConvertTo-Json -Depth 4
    try { Invoke-RestMethod "$OllamaUrl/api/generate" -Method Post -Body $body -ContentType "application/json" -TimeoutSec 300 | Out-Null }
    catch { Write-Warning "  load with num_ctx=$ctx failed: $_"; return $null }
    $m = (Invoke-RestMethod "$OllamaUrl/api/ps").models | Where-Object { $_.name -like "$($model.Split(':')[0])*" } | Select-Object -First 1
    if (-not $m) { return $null }
    $pct = [math]::Round(100.0 * [double]$m.size_vram / [math]::Max(1.0, [double]$m.size))
    Write-Host ("  num_ctx {0}: {1}% on GPU ({2:N1} of {3:N1} GB)" -f $ctx, $pct, ([double]$m.size_vram / 1e9), ([double]$m.size / 1e9))
    return $pct
}

if ($Undo) {
    $numCtx = $UndoNumCtx
    $cfg.PSObject.Properties.Remove("tuning_note")
} else {
    Say "Finding the biggest context that fits on the GPU for $model"
    $numCtx = $null
    foreach ($c in $ContextsToTry) {
        $p = Fits $c
        if ($p -ne $null -and $p -ge 99) { $numCtx = $c; break }
    }
    if (-not $numCtx) { $numCtx = $ContextsToTry[-1]; Write-Warning "Even $numCtx doesn't fit fully - consider a smaller model/quant." }
    $cfg | Add-Member -Force tuning_note "fa=$FlashAttention kv=$KvCacheType keep=$KeepAlive par=$NumParallel ctx=$numCtx $(Get-Date -Format yyyy-MM-dd)"
}
$cfg.local_llm | Add-Member -Force num_ctx $numCtx
$cfg.local_llm | Add-Member -Force parallel ([int]$NumParallel)
$cfg.grouping  | Add-Member -Force drain3 $(if ($Undo) { "off" } else { $Drain3Mode })
$cfg | ConvertTo-Json -Depth 10 | Set-Content -Path $ConfigFile -Encoding UTF8
Say "config.json: local_llm.num_ctx=$numCtx, parallel=$NumParallel, grouping.drain3=$($cfg.grouping.drain3)"

# ------------------------------------------------------------------ 5. restart the dashboard
Say "Restarting the dashboard"
Get-CimInstance Win32_Process -Filter "name='pythonw.exe' or name='python.exe'" |
    Where-Object { $_.CommandLine -match 'aws_error_feed' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Start-Sleep -Seconds 2
Start-ScheduledTask $TaskName
Start-Sleep -Seconds 8
Get-Content (Join-Path $AppDir "dashboard.log") -Tail 8

Say "Done. Open the dashboard's Health tab (it fills in as the model is used)."
