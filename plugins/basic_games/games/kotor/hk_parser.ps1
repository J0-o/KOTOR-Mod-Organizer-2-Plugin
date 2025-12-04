# ======================================================================
# HK Parser - Scans TSLPatcher Mods, Parses INIs, Writes CSV (Preserves Enabled State + Required + Table Files)
# ======================================================================

Write-Host "`n=== HK Parser ==="

# ======================================================================
# Scan Mods for TSLPatcher
# ======================================================================

Write-Host "`n=== Scanning for TSLPatcher Mods ==="

if (-not (Test-Path -LiteralPath $modsDir)) {
    Write-Host "Mods folder not found: $modsDir"
    exit 1
}

$tslpatchMods = Get-ChildItem -LiteralPath $modsDir -Directory -ErrorAction SilentlyContinue |
    Where-Object {
        (Test-Path -LiteralPath (Join-Path $_.FullName "tslpatchdata")) -or
        (Test-Path -LiteralPath (Join-Path $_.FullName "TSLPatcherData"))
    }

if ($tslpatchMods.Count -eq 0) {
    Write-Host "No TSLPatcher folders found under $modsDir."
    exit 0
}

# Sort by modlist order (descending = top of modlist = highest priority)
$tslpatchMods = $tslpatchMods | Sort-Object { $modOrder.IndexOf($_.Name) } -Descending

# ======================================================================
# Read existing CSV to preserve user Enabled states
# ======================================================================

$oldCsv = @{}
if (Test-Path -LiteralPath $outFile) {
    Write-Host "Found existing CSV: $outFile (preserving Enabled states)"
    try {
        $existing = Import-Csv -LiteralPath $outFile -Encoding UTF8
        foreach ($row in $existing) {
            $key = "$($row.ModName)|$($row.PatchName)"
            $oldCsv[$key] = $row.Enabled
        }
    } catch {
        Write-Host "Warning: Failed to read existing CSV, skipping preservation." -ForegroundColor Yellow
    }
}

# ======================================================================
# Function: Parse .ini for install info
# ======================================================================

function Parse-IniForFiles {
    param ($iniPath)

    if (-not (Test-Path -LiteralPath $iniPath)) { return @() }

    $lines = Get-Content -LiteralPath $iniPath -Encoding UTF8
    $currentSection = ""
    $collected = [System.Collections.Generic.List[object]]::new()
    $foundTLKSection = $false
    $hasTLKEntries = $false

    foreach ($line in $lines) {
        $trimmed = $line.Trim()

        # === Detect section changes ===
        if ($trimmed -match '^\[(.+)\]$') {
            # if we’re leaving TLKList and it had entries, note it
            if ($foundTLKSection -and $hasTLKEntries) {
                $collected.Add([PSCustomObject]@{
                    Type='File'; Section='TLKList'; Value='dialog.tlk'
                })
                $foundTLKSection = $false
                $hasTLKEntries = $false
            }

            $currentSection = $Matches[1]
            if ($currentSection -ieq 'TLKList') {
                $foundTLKSection = $true
                $hasTLKEntries = $false
            } else {
                $foundTLKSection = $false
            }
            continue
        }

        # === If inside [TLKList], detect any entries ===
        if ($foundTLKSection -and -not [string]::IsNullOrWhiteSpace($trimmed)) {
            if (-not ($trimmed -match '^\[')) {
                $hasTLKEntries = $true
            }
        }

        # === Parse known keys ===
        if ($trimmed -match '^!Destination\s*=\s*(.+)$') {
            $collected.Add([PSCustomObject]@{
                Type='File'; Section=$currentSection; Value=$Matches[1].Trim().ToLower()
            })
        }
        elseif ($trimmed -match '^install_folder\d+\s*=\s*(.+)$') {
            $val = $Matches[1].Trim()
            if ($val -match '\.[a-zA-Z0-9]{2,4}$') {
                $collected.Add([PSCustomObject]@{
                    Type='File'; Section=$currentSection; Value=$val.ToLower()
                })
            } else {
                $collected.Add([PSCustomObject]@{
                    Type='InstallFolder'; Section=$currentSection; Value=$val
                })
            }
        }
        elseif ($trimmed -match '^File\d+\s*=\s*(.+)$') {
            $collected.Add([PSCustomObject]@{
                Type='File'; Section=$currentSection; Value=$Matches[1].Trim().ToLower()
            })
        }
        elseif ($trimmed -match '^Replace\d+\s*=\s*(.+)$') {
            $collected.Add([PSCustomObject]@{
                Type='File'; Section=$currentSection; Value=$Matches[1].Trim().ToLower()
            })
        }
        elseif ($trimmed -match '^Table\d+\s*=\s*(.+)$') {
            $collected.Add([PSCustomObject]@{
                Type='File'; Section=$currentSection; Value=$Matches[1].Trim().ToLower()
            })
        }
        elseif ($trimmed -match '^Required\s*=\s*(.+)$') {
            $collected.Add([PSCustomObject]@{
                Type='Required'; Section=$currentSection; Value=$Matches[1].Trim().ToLower()
            })
        }
        elseif ($trimmed -match '^WindowCaption\s*=\s*(.+)$') {
            $collected.Add([PSCustomObject]@{
                Type='Description'; Section=$currentSection; Value=$Matches[1].Trim()
            })
        }
    }

    # If file ends while still in TLKList with entries, add dialog.tlk
    if ($foundTLKSection -and $hasTLKEntries) {
        $collected.Add([PSCustomObject]@{
            Type='File'; Section='TLKList'; Value='dialog.tlk'
        })
    }

    return $collected
}

# ======================================================================
# Build Patch List
# ======================================================================

$patchList = @()

foreach ($mod in $tslpatchMods) {
    $patchDir = Join-Path $mod.FullName "tslpatchdata"
    if (-not (Test-Path -LiteralPath $patchDir)) {
        $patchDir = Join-Path $mod.FullName "TSLPatcherData"
    }

    $namespaceIni = Join-Path $patchDir "namespaces.ini"

    if (Test-Path -LiteralPath $namespaceIni) {
        $lines = Get-Content -LiteralPath $namespaceIni -Encoding UTF8

        foreach ($line in $lines) {
            if ($line -match '^\s*Namespace\d+\s*=\s*(.+)$') {
                $nsName = $Matches[1].Trim()
                $iniName = ""
                $dataPath = ""
                $desc = ""

                $inSection = $false
                foreach ($inner in $lines) {
                    if ($inner -match '^\s*\[' + [Regex]::Escape($nsName) + '\]\s*$') {
                        $inSection = $true
                        continue
                    }
                    if ($inSection -and $inner -match '^\s*\[.+\]\s*$') { break }

                    if ($inSection) {
                        if ($inner -match '^\s*IniName\s*=\s*(.+)$') {
                            $iniName = $Matches[1].Trim()
                        } elseif ($inner -match '^\s*DataPath\s*=\s*(.*)$') {
                            $dataPath = $Matches[1].Trim()
                        } elseif ($inner -match '^\s*Description\s*=\s*(.+)$') {
                            $desc = $Matches[1].Trim()
                        }
                    }
                }

                if ([string]::IsNullOrWhiteSpace($dataPath)) {
                    $finalPath = $patchDir
                    $iniFull = Join-Path $patchDir $iniName
                } else {
                    $finalPath = Join-Path $patchDir $dataPath
                    $iniFull = Join-Path $finalPath $iniName
                }

                # --- Fallback: use changes.ini if iniFull not found ---
                if (-not (Test-Path -LiteralPath $iniFull)) {
                    $changesFallback = Join-Path $finalPath "changes.ini"
                    if (Test-Path -LiteralPath $changesFallback) {
                        Write-Host "  Fallback to changes.ini for $($mod.Name) / $nsName"
                        $iniFull = $changesFallback
                    } else {
                        Write-Host "  Missing INI for $($mod.Name) / $nsName (skipping)" -ForegroundColor Yellow
                        continue
                    }
                }

                # Parse ini for file data
                $parsed = Parse-IniForFiles $iniFull
                $dest      = ($parsed | Where-Object { $_.Type -eq 'Destination' } | Select-Object -ExpandProperty Value -Unique) -join '; '
                $folders   = ($parsed | Where-Object { $_.Type -eq 'InstallFolder' } | Select-Object -ExpandProperty Value -Unique) -join '; '
                $files     = ($parsed | Where-Object { $_.Type -eq 'File' } | Select-Object -ExpandProperty Value -Unique) -join '; '
                $required  = ($parsed | Where-Object { $_.Type -eq 'Required' } | Select-Object -ExpandProperty Value -Unique) -join '; '

                $key = "$($mod.Name)|$nsName"
                $enabledState = if ($oldCsv.ContainsKey($key)) { $oldCsv[$key] } else { 0 }

                $patchList += [PSCustomObject]@{
                    Enabled      = $enabledState
                    ModName      = $mod.Name
                    PatchName    = $nsName
                    Description  = if ($desc) { $desc } else { ($parsed | Where-Object { $_.Type -eq 'Description' } | Select-Object -ExpandProperty Value -First 1) }
                    IniShortPath = if ($dataPath) { (Join-Path $dataPath $iniName) } else { $iniName }
                    Destination  = $dest
                    InstallPaths = $folders
                    Files        = $files
                    Required     = $required
                }
            }
        }
    }
    else {
        # --- Fallback for mods without namespaces.ini ---
        $changesIni = Join-Path $patchDir "changes.ini"
        if (-not (Test-Path -LiteralPath $changesIni)) { continue }

        $parsed = Parse-IniForFiles $changesIni
        $desc = ($parsed | Where-Object { $_.Type -eq 'Description' } | Select-Object -ExpandProperty Value -First 1)
        $dest = ($parsed | Where-Object { $_.Type -eq 'Destination' } | Select-Object -ExpandProperty Value -Unique) -join '; '
        $folders = ($parsed | Where-Object { $_.Type -eq 'InstallFolder' } | Select-Object -ExpandProperty Value -Unique) -join '; '
        $files = ($parsed | Where-Object { $_.Type -eq 'File' } | Select-Object -ExpandProperty Value -Unique) -join '; '
        $required = ($parsed | Where-Object { $_.Type -eq 'Required' } | Select-Object -ExpandProperty Value -Unique) -join '; '

        $key = "$($mod.Name)|Default"
        $enabledState = if ($oldCsv.ContainsKey($key)) { $oldCsv[$key] } else { 0 }

        $patchList += [PSCustomObject]@{
            Enabled      = $enabledState
            ModName      = $mod.Name
            PatchName    = "Default"
            Description  = $desc
            IniShortPath = "changes.ini"
            Destination  = $dest
            InstallPaths = $folders
            Files        = $files
            Required     = $required
        }
    }
}

# ======================================================================
# Write CSV (Fresh Build)
# ======================================================================

if (-not $outFile) {
    $outFile = "tslpatch_order.csv"
}

$patchList | Export-Csv -LiteralPath $outFile -NoTypeInformation -Encoding UTF8
Write-Host "`nCSV rebuilt from scratch: $outFile"

# ======================================================================
# Build Duplicate File Touch Map
# ======================================================================

Write-Host "`n=== Building Duplicate File Map ==="

$dupMap = @{}   # filename -> set of mods

foreach ($entry in $patchList) {
    if (-not $entry.Files) { continue }

    $fileList = $entry.Files -split ';' | ForEach-Object { $_.Trim().ToLower() } | Where-Object { $_ -ne "" }

    foreach ($file in $fileList) {
        if (-not $dupMap.ContainsKey($file)) {
            $dupMap[$file] = [System.Collections.Generic.HashSet[string]]::new()
        }
        $null = $dupMap[$file].Add($entry.ModName)
    }
}

# keep only files touched by more than one mod
$duplicates = foreach ($kv in $dupMap.GetEnumerator()) {
    if ($kv.Value.Count -gt 1) {
        [PSCustomObject]@{
            FileName = $kv.Key
            Mods     = ($kv.Value | Sort-Object) -join '; '
            ModCount = $kv.Value.Count
        }
    }
}

if ($duplicates.Count -eq 0) {
    Write-Host "No duplicate file edits found."
} else {
    $dupFileOut = "duplicate_files.csv"
    $duplicates | Sort-Object FileName | Export-Csv -LiteralPath $dupFileOut -NoTypeInformation -Encoding UTF8
    Write-Host "Duplicate file list written to: $dupFileOut"
}



exit 0
