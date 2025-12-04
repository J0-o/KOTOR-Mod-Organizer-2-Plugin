# ======================================================================
# HK Reassembler
# ======================================================================

Write-Host "`n=== HK Reassembler Launcher ==="

# --- Basic path setup (PowerShell 5 compatible) ---
if (-not $PSScriptRoot) {
    $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
} else {
    $scriptDir = $PSScriptRoot
}

$outFile = Join-Path $scriptDir "tslpatch_order.csv"

# ======================================================================
# Environment Detection (MO2 / Game Paths)
# ======================================================================

Write-Host "`n=== Environment Detection ==="

$mo2Dir = Resolve-Path (Join-Path $scriptDir "..\..\..\..")
$iniPath = Join-Path $mo2Dir "ModOrganizer.ini"
$modsDir = Join-Path $mo2Dir "mods"

# --- Parse game directory ---
$gameDir = $null
if (Test-Path -LiteralPath $iniPath) {
    $lines = Get-Content -LiteralPath $iniPath
    $line = $lines | Where-Object { $_ -match '^gamePath=' } | Select-Object -First 1
    if ($line) {
        $match = [regex]::Match($line, 'ByteArray\((.+)\)|\((.+)\)')
        if ($match.Success) {
            $gameDir = ($match.Groups[1].Value, $match.Groups[2].Value) | Where-Object { $_ -ne "" } | Select-Object -First 1
            $gameDir = $gameDir -replace '\\\\', '\'
        }
    }
}

# --- Parse selected profile ---
$selectedProfile = "Unknown"
if (Test-Path -LiteralPath $iniPath) {
    $profileLine = (Get-Content -LiteralPath $iniPath | Where-Object { $_ -match '^selected_profile=' } | Select-Object -First 1)
    if ($profileLine -match '@ByteArray\(([^)]+)\)') {
        $selectedProfile = $Matches[1]
    }
}

# --- Modlist path ---
$profileDir = Join-Path $mo2Dir "profiles\$selectedProfile"
$modlistPath = Join-Path $profileDir "modlist.txt"

if (Test-Path -LiteralPath $modlistPath) {
    $modlistLines = Get-Content -LiteralPath $modlistPath -Encoding UTF8
    $modOrder = @()
    foreach ($line in $modlistLines) {
        if ($line -match '^[#]') { continue }
        if ($line -match '^[\+\-](.+)$') {
            $modOrder += $Matches[1].Trim()
        }
    }
    # If HK_REASSEMBLER mod is enabled, abort and inform the user.
    if ($modlistLines | Where-Object { $_ -match '^\+HK_REASSEMBLER\b' }) {
        Write-Host "`nHK_REASSEMBLER mod is enabled. Please disable it before running HK Reassembler." -ForegroundColor Yellow
        Write-Host "Press any key to exit..."
        [void][System.Console]::ReadKey($true)
        exit
    }
} else {
    Write-Host "modlist.txt not found for profile: $selectedProfile"
    $modOrder = @()
}

Write-Host "MO2 Directory   : $mo2Dir"
Write-Host "INI Path        : $iniPath"
Write-Host "Game Directory  : $gameDir"
Write-Host "Mods Directory  : $modsDir"
Write-Host "Selected Profile: $selectedProfile"
Write-Host "Modlist Path    : $modlistPath"
Write-Host "Script Path     : $scriptDir"
Write-Host "==================================="

# --- Resolve paths ---
if (-not $PSScriptRoot) {
    $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
} else {
    $scriptDir = $PSScriptRoot
}

$parserScript = Join-Path $scriptDir "hk_parser.ps1"
$uiScript     = Join-Path $scriptDir "hk_ui.ps1"

# --- Verify required scripts ---
if (-not (Test-Path -LiteralPath $parserScript)) {
    Write-Host "Parser script missing: $parserScript"
    pause
    exit
}
if (-not (Test-Path -LiteralPath $uiScript)) {
    Write-Host "UI script missing: $uiScript"
    pause
    exit
}

# --- Run parser in current session ---
Write-Host "`Running parser..."
. $parserScript
if ($LASTEXITCODE -ne 0) {
    Write-Host "Parser exited with code $LASTEXITCODE"
    pause
    exit
}

# --- Launch UI in current session ---
Write-Host "`Launching UI..."
. $uiScript
