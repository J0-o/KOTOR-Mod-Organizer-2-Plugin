# ======================================================================
# HK Runner - Reassembler Mode (Indexed for Fast File Lookup)
# ======================================================================

Write-Host ""
Write-Host "=== HK Runner (Reassembler Mode - Indexed) ==="

# ----------------------------------------------------------------------
# Default directories
# ----------------------------------------------------------------------
if (-not $gameDir -or -not (Test-Path $gameDir)) {
    $gameDir = "G:\Program Files (x86)\Steam\steamapps\common\Knights of the Old Republic II"
}
if (-not $modsDir -or -not (Test-Path $modsDir)) {
    $modsDir = "K:\ModOrganizer2\mods"
}

# ----------------------------------------------------------------------
# Paths
# ----------------------------------------------------------------------
$exePath   = Join-Path $PSScriptRoot "holopatcher.exe"
$csvPath   = Join-Path $PSScriptRoot "tslpatch_order.csv"
$logPath   = Join-Path $PSScriptRoot "holopatcher_run.log"
$reasmDir  = Join-Path $modsDir "HK_REASSEMBLER"

if (-not (Test-Path $exePath)) { Write-Host "holopatcher.exe not found"; exit 1 }
if (-not (Test-Path $csvPath)) { Write-Host "CSV not found"; exit 1 }
if (-not (Test-Path $gameDir)) { Write-Host "Game dir not found"; exit 1 }

Add-Content $logPath "`n=== HK Runner Started $(Get-Date) ==="

# ----------------------------------------------------------------------
# Clean Reassembler Folder
# ----------------------------------------------------------------------
Write-Host ""
Write-Host "Cleaning HK_REASSEMBLER folder..."
if (Test-Path $reasmDir) {
    Remove-Item -LiteralPath $reasmDir -Recurse -Force -ErrorAction SilentlyContinue
}
New-Item -ItemType Directory -Path $reasmDir | Out-Null
Write-Host "  Clean folder ready: $reasmDir"

# ----------------------------------------------------------------------
# Prepare dummy swkotor2.exe (for HoloPatcher validation)
# ----------------------------------------------------------------------
Write-Host ""
Write-Host "Creating dummy swkotor2.exe for HoloPatcher validation..."
$dummyExe = Join-Path $reasmDir "swkotor2.exe"
[IO.File]::WriteAllBytes($dummyExe, [byte[]](0..255))
Write-Host "  Dummy EXE created: $dummyExe"

# ----------------------------------------------------------------------
# Load enabled mods
# ----------------------------------------------------------------------
$enabled = Import-Csv -LiteralPath $csvPath -Encoding UTF8 | Where-Object {
    $_.Enabled -match '^(1|true|yes)$'
}

if ($enabled.Count -eq 0) {
    Write-Host "No enabled mods found."
    exit 0
}

# ----------------------------------------------------------------------
# Helper: Hash function (MD5)
# ----------------------------------------------------------------------
function Get-FileHashHex {
    param ($path)
    if (-not (Test-Path $path)) { return $null }
    $md5 = [System.Security.Cryptography.MD5]::Create()
    $bytes = [System.IO.File]::ReadAllBytes($path)
    $hashBytes = $md5.ComputeHash($bytes)
    return ([System.BitConverter]::ToString($hashBytes) -replace "-", "").ToLower()
}

# ----------------------------------------------------------------------
# Build game file index
# ----------------------------------------------------------------------
Write-Host ""
Write-Host "=== Indexing game directory for quick file access ==="

$indexByName = @{}
$indexByRel  = @{}

$gameFiles = Get-ChildItem -LiteralPath $gameDir -Recurse -File -ErrorAction SilentlyContinue
foreach ($f in $gameFiles) {
    $rel = $f.FullName.Substring($gameDir.Length).TrimStart('\')
    $indexByName[$f.Name.ToLower()] = $f.FullName
    $indexByRel[$rel.ToLower()] = $f.FullName
}

Write-Host ("Indexed {0} files under {1}" -f $indexByName.Count, $gameDir)

# ----------------------------------------------------------------------
# Collect referenced game files
# ----------------------------------------------------------------------
Write-Host ""
Write-Host "=== Collecting target filenames (Files + Required) ==="

$targetNames = @()
foreach ($row in $enabled) {
    foreach ($col in @('Files', 'Required')) {
        if (-not [string]::IsNullOrWhiteSpace($row.$col)) {
            $split = $row.$col -split '[;,]' |
                ForEach-Object { $_.Trim() } |
                Where-Object { $_ -ne "" }
            $targetNames += $split
        }
    }
}
$targetNames = $targetNames | Sort-Object -Unique

if ($targetNames.Count -eq 0) {
    Write-Host "No file entries found in CSV."
    exit 0
}
Write-Host ("Found {0} unique filenames to verify via index." -f $targetNames.Count)

# ----------------------------------------------------------------------
# Copy referenced game files
# ----------------------------------------------------------------------
Write-Host ""
Write-Host "=== Copying referenced game files using index ==="

$requiredHashes = @{}
$requiredNames  = @()

foreach ($row in $enabled) {
    if ($row.Required) {
        $requiredNames += ($row.Required -split '[;,]' | ForEach-Object { $_.Trim() })
    }
}
$requiredNames = $requiredNames | Sort-Object -Unique

$totalCopied  = 0
$totalMissing = 0

foreach ($target in $targetNames) {

    $relTarget = $target.TrimStart('\','/').ToLower()
    $fileName  = Split-Path $relTarget -Leaf

    $found = $null
    if ($indexByRel.ContainsKey($relTarget)) {
        $found = $indexByRel[$relTarget]
    }
    elseif ($indexByName.ContainsKey($fileName)) {
        $found = $indexByName[$fileName]
    }

    if ($found) {
        $relativePath = $found.Substring($gameDir.Length).TrimStart('\')
        $dest = Join-Path $reasmDir $relativePath
        $destDir = Split-Path $dest -Parent
        if (-not (Test-Path $destDir)) {
            New-Item -ItemType Directory -Path $destDir -Force | Out-Null
        }

        Copy-Item -LiteralPath $found -Destination $dest -Force
        Write-Host "  Copied: $relativePath"
        $totalCopied++

        if ($requiredNames -contains (Split-Path $found -Leaf)) {
            $hash = Get-FileHashHex $dest
            if ($hash) { $requiredHashes[$dest] = $hash }
        }
    }
    else {
        Write-Host "  Missing in game dir: $target" -ForegroundColor Yellow
        $totalMissing++
    }
}

Write-Host ""
Write-Host "Reassembly copy complete."
Write-Host "  Total copied : $totalCopied"
Write-Host "  Missing files: $totalMissing"
Add-Content $logPath "Copied $totalCopied files. Missing $totalMissing."

# ----------------------------------------------------------------------
# Run HoloPatcher for each enabled mod
# ----------------------------------------------------------------------
Write-Host ""
Write-Host "=== Running HoloPatcher on enabled mods ==="

$totalMods   = $enabled.Count
$totalErrors = 0

$tempRoot = Join-Path $PSScriptRoot "temp"
$logDir   = Join-Path $PSScriptRoot "logs"

if (-not (Test-Path $tempRoot)) { New-Item -ItemType Directory -Path $tempRoot | Out-Null }
if (-not (Test-Path $logDir))   { New-Item -ItemType Directory -Path $logDir | Out-Null }

foreach ($mod in $enabled) {

    $modName  = $mod.ModName
    $iniRel   = $mod.IniShortPath.Trim()
    $modBase  = Join-Path $modsDir $modName

    Write-Host ""
    Write-Host "Checking mod: $modName"
    Write-Host "  IniShortPath: $iniRel"

    $safeModName = ($modName -replace '[^\w\-.]', '_')
    $tempMod     = Join-Path $tempRoot $safeModName
    $tempPatch   = Join-Path $tempMod "tslpatchdata"

    #
    # ------------------------------------------------------------------
    # FIND PATCH FOLDER (tslpatchdata, TSLPatcherData, patchdata)
    # ------------------------------------------------------------------
    #
    $patchFolder = Get-ChildItem -LiteralPath $modBase -Directory -ErrorAction SilentlyContinue |
                   Where-Object { $_.Name -match 'tslpatchdata|TSLPatcherData|patchdata' } |
                   Select-Object -First 1

    if (-not $patchFolder) {
        Write-Host "  No tslpatchdata folder found (skipping)" -ForegroundColor Yellow
        Add-Content $logPath "SKIPPED: $modName (no patch folder)"
        continue
    }

    $patchBase = $patchFolder.FullName

    #
    # ------------------------------------------------------------------
    # CSV INI PATHS ARE RELATIVE TO PATCH FOLDER (FIXED)
    # ------------------------------------------------------------------
    #
    $iniAbs = Join-Path $patchBase $iniRel

    if (-not (Test-Path -LiteralPath $iniAbs)) {
        Write-Host "  INI not found inside patch folder: $iniRel (skipping)" -ForegroundColor Yellow
        Add-Content $logPath "MISSING INI: $iniAbs"
        continue
    }

    Write-Host "  INI resolved: $iniAbs"

    #
    # ------------------------------------------------------------------
    # Copy only the INI’s parent folder → temp\tslpatchdata
    # ------------------------------------------------------------------
    #
    if (Test-Path $tempMod) {
        Remove-Item -LiteralPath $tempMod -Recurse -Force -ErrorAction SilentlyContinue
    }
    New-Item -ItemType Directory -Path $tempPatch -Force | Out-Null

    $iniFolder = Split-Path $iniAbs -Parent
    Write-Host "  Copying folder: $iniFolder"

    robocopy $iniFolder $tempPatch /E /NFL /NDL /NJH /NJS /NP | Out-Null
    # ------------------------------------------------------------------
    # Ensure info.rtf exists (HoloPatcher requires it)
    # ------------------------------------------------------------------
    $infoPath = Join-Path $tempPatch "info.rtf"

    if (-not (Test-Path -LiteralPath $infoPath)) {
        Write-Host "  info.rtf missing - creating dummy file"
        $rtfContent = "{\rtf1\ansi HK Runner auto-generated info.rtf}"
        Set-Content -LiteralPath $infoPath -Value $rtfContent -Encoding ASCII
    }
    else {
        Write-Host "  info.rtf present"
    }

    # ------------------------------------------------------------------
    # Remove namespace.ini if it exists (prevent HoloPatcher from using it)
    # ------------------------------------------------------------------
    $namespacePath = Join-Path $tempPatch "namespaces.ini"

    if (Test-Path -LiteralPath $namespacePath) {
        Write-Host "Removing namespaces.ini to prevent HoloPatcher confusion"
        Remove-Item -LiteralPath $namespacePath -Force -ErrorAction SilentlyContinue
    }


    #
    # ------------------------------------------------------------------
    # Rename INI to changes.ini
    # ------------------------------------------------------------------
    #
    $iniName   = Split-Path $iniAbs -Leaf
    $copiedIni = Join-Path $tempPatch $iniName
    $fixedIni  = Join-Path $tempPatch "changes.ini"

    if (Test-Path -LiteralPath $copiedIni) {

        $iniName = Split-Path $copiedIni -Leaf

        if ($iniName -ieq "changes.ini") {
            Write-Host "  INI already named changes.ini (no rename needed)"
        }
        else {
            Move-Item -LiteralPath $copiedIni -Destination $fixedIni -Force
            Write-Host "  INI renamed → changes.ini"
        }
    }
    else {
        Write-Host "  ERROR: INI missing after copy: $copiedIni" -ForegroundColor Red
        Add-Content $logPath "INI COPY FAILURE: expected $copiedIni"
        continue
    }

    #
    # ------------------------------------------------------------------
    # Run HoloPatcher
    # ------------------------------------------------------------------
    #
    $resolvedExe   = (Resolve-Path $exePath).Path
    $resolvedReasm = (Resolve-Path $reasmDir).Path
    $resolvedTemp  = (Resolve-Path $tempPatch).Path

    $cmdArgs = @("--install", "--game-dir", $resolvedReasm, "--tslpatchdata", $resolvedTemp)

    Write-Host ""
    Write-Host "Running HoloPatcher for: $modName"
    Write-Host ("{0} {1}" -f $resolvedExe, ($cmdArgs -join ' ')) -ForegroundColor Yellow
    Add-Content $logPath "`nExecuting: $resolvedExe $($cmdArgs -join ' ')"

    try {
        $process = Start-Process -FilePath $resolvedExe -ArgumentList $cmdArgs -NoNewWindow -Wait -PassThru
        if ($process.ExitCode -eq 0) {
            Write-Host "  Success: $modName" -ForegroundColor Green
            Add-Content $logPath "SUCCESS: $modName"
        } else {
            Write-Host "  Failed: $modName (exit $($process.ExitCode))" -ForegroundColor Red
            Add-Content $logPath "FAILED: $modName"
            $totalErrors++
        }
    }
    catch {
        Write-Host ("  Error running HoloPatcher: {0}" -f $_.Exception.Message) -ForegroundColor Red
        Add-Content $logPath "ERROR HP: $modName - $($_.Exception.Message)"
        $totalErrors++
    }

    #
    # ------------------------------------------------------------------
    # Save installlog.txt & cleanup temp
    # ------------------------------------------------------------------
    #
    try {
        $installLog = Join-Path $tempMod "installlog.txt"
        if (Test-Path -LiteralPath $installLog) {
            $destLog = Join-Path $logDir ("{0}.txt" -f $safeModName)
            Copy-Item -LiteralPath $installLog -Destination $destLog -Force
            Write-Host "  Saved install log."
        } else {
            Write-Host "  No installlog.txt found."
        }

        Remove-Item -LiteralPath $tempMod -Recurse -Force -ErrorAction SilentlyContinue
        Write-Host "  Temp cleaned."
    }
    catch {
        Write-Host "  Could not clean temp folder." -ForegroundColor Yellow
    }
}

# ----------------------------------------------------------------------
# Cleanup temp & unchanged required files
# ----------------------------------------------------------------------
Write-Host ""
Write-Host "Cleaning up temp root..."
Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
Write-Host "  Done."

Write-Host ""
Write-Host "=== Cleaning unchanged Required files ==="

foreach ($pair in $requiredHashes.GetEnumerator()) {
    $path = $pair.Key
    $oldHash = $pair.Value
    if (Test-Path $path) {
        $newHash = Get-FileHashHex $path
        if ($newHash -eq $oldHash) {
            Write-Host "  Unchanged Required removed: $path" -ForegroundColor DarkGray
            Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
        } else {
            Write-Host "  Required modified: $path" -ForegroundColor Cyan
        }
    }
}

# ----------------------------------------------------------------------
# Cleanup dummy EXE
# ----------------------------------------------------------------------
Write-Host ""
Write-Host "Removing dummy swkotor2.exe..."
if (Test-Path $dummyExe) {
    Remove-Item -LiteralPath $dummyExe -Force -ErrorAction SilentlyContinue
    Write-Host "  Dummy EXE removed."
}

# ----------------------------------------------------------------------
# Summary
# ----------------------------------------------------------------------
Write-Host ""
Write-Host "=== Patch Summary ==="
Write-Host ("Processed Mods : {0}" -f $totalMods)
Write-Host ("Errors Found   : {0}" -f $totalErrors) -ForegroundColor Red
Write-Host ""
Write-Host "All enabled mods processed."

Add-Content $logPath ("Summary: $totalMods mods processed, $totalErrors errors on $(Get-Date)")
exit 0
