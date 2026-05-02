[CmdletBinding()]
param(
    [Parameter(Position = 0, Mandatory = $true)]
    [string]$InputPath,

    [Parameter(Position = 1, Mandatory = $true)]
    [string]$OutputPath,

    [Parameter()]
    [string]$PreparedPayloadPath,

    [Parameter()]
    [switch]$PrepareOnly
)

$ErrorActionPreference = "Stop"

$ExpectedInstallerHash = "94C99C4807DA4B304DE6E0EFED1BE55E0F43D13CC5A274582ED3323AC0E2F1A6"
$ExpectedChunkMarker = [byte[]](0x7A, 0x6C, 0x62, 0x1A)
$ManifestPath = Join-Path $PSScriptRoot "_resources\tslrcm2022-manifest.tsv"
$InstallRoots = @(
    "dialog.tlk",
    "modders resource shuttle - readme.txt",
    "readme.rtf",
    "lips",
    "modules",
    "movies",
    "override",
    "streammusic",
    "streamvoice"
)
$SkipPaths = @(
    "override/000level.dlg",
    "override/areatrans_p.gui",
    "override/container_p.gui",
    "override/custom.txt",
    "override/d2xfnt_d16x16b.tga",
    "override/d2xfont16x16b.tga",
    "override/d2xfont16x16b_ps.tga",
    "override/d3xfnt_d16x16b.tga",
    "override/d3xfont16x16b.tga",
    "override/d3xfont16x16b_ps.tga",
    "override/dialogfont10x10b.tga",
    "override/dialogfont10x10b.txi",
    "override/dialogfont16x16.tga",
    "override/dialogfont16x16.txi",
    "override/dialogfont16x16b.tga",
    "override/dialogfont16x16b.txi",
    "override/fnt_d16x16b.tga",
    "override/fnt_d16x16b.txi",
    "override/gamepad.txt",
    "override/optresolution_p.gui",
    "override/savefont16x16b.tga",
    "override/savefont16x16b.txi",
    "override/saveload_p.gui"
)
$RenameMap = @{
    "dialog.tlk" = "dialog,1.tlk"
    "lips/101per_loc.mod" = "lips/101per_loc,1.mod"
    "lips/301nar_loc.mod" = "lips/301nar_loc,1.mod"
    "lips/303nar_loc.mod" = "lips/303nar_loc,1.mod"
    "lips/602dan_loc.mod" = "lips/602dan_loc,1.mod"
    "lips/605dan_loc.mod" = "lips/605dan_loc,1.mod"
    "lips/610dan_loc.mod" = "lips/610dan_loc,1.mod"
    "lips/650dan_loc.mod" = "lips/650dan_loc,1.mod"
    "modules/003ebo.mod" = "modules/003ebo,6.mod"
    "modules/004ebo.mod" = "modules/004ebo,6.mod"
    "modules/005ebo.mod" = "modules/005ebo,1.mod"
    "modules/006ebo.mod" = "modules/006ebo,6.mod"
    "modules/012ebo.mod" = "modules/012ebo,1.mod"
    "modules/101per.mod" = "modules/101per,1.mod"
    "modules/102per.mod" = "modules/102per,1.mod"
    "modules/103per.mod" = "modules/103per,4.mod"
    "modules/104per.mod" = "modules/104per,1.mod"
    "modules/106per.mod" = "modules/106per,1.mod"
    "modules/151har.mod" = "modules/151har,1.mod"
    "modules/152har.mod" = "modules/152har,1.mod"
    "modules/154har.mod" = "modules/154har,1.mod"
    "modules/201tel.mod" = "modules/201tel,1.mod"
    "modules/202tel.mod" = "modules/202tel,6.mod"
    "modules/203tel.mod" = "modules/203tel,1.mod"
    "modules/204tel.mod" = "modules/204tel,1.mod"
    "modules/205tel.mod" = "modules/205tel,1.mod"
    "modules/207tel.mod" = "modules/207tel,6.mod"
    "modules/208tel.mod" = "modules/208tel,1.mod"
    "modules/209tel.mod" = "modules/209tel,6.mod"
    "modules/221tel.mod" = "modules/221tel,1.mod"
    "modules/222tel.mod" = "modules/222tel,1.mod"
    "modules/231tel.mod" = "modules/231tel,1.mod"
    "modules/233tel.mod" = "modules/233tel,1.mod"
    "modules/261tel.mod" = "modules/261tel,6.mod"
    "modules/262tel.mod" = "modules/262tel,6.mod"
    "modules/298tel.mod" = "modules/298tel,4.mod"
    "modules/299tel.mod" = "modules/299tel,4.mod"
    "modules/301nar.mod" = "modules/301nar,1.mod"
    "modules/302nar.mod" = "modules/302nar,1.mod"
    "modules/303nar.mod" = "modules/303nar,1.mod"
    "modules/304nar.mod" = "modules/304nar,1.mod"
    "modules/305nar.mod" = "modules/305nar,1.mod"
    "modules/306nar.mod" = "modules/306nar,1.mod"
    "modules/307nar.mod" = "modules/307nar,1.mod"
    "modules/350nar.mod" = "modules/350nar,1.mod"
    "modules/351nar.mod" = "modules/351nar,1.mod"
    "modules/352nar.mod" = "modules/352nar,1.mod"
    "modules/401dxn_dlg.erf" = "modules/401dxn_dlg,1.erf"
    "modules/402dxn.mod" = "modules/402dxn,6.mod"
    "modules/403dxn.mod" = "modules/403dxn,6.mod"
    "modules/410dxn.mod" = "modules/410dxn,6.mod"
    "modules/411dxn.mod" = "modules/411dxn,6.mod"
    "modules/501ond.mod" = "modules/501ond,1.mod"
    "modules/502ond.mod" = "modules/502ond,2.mod"
    "modules/503ond.mod" = "modules/503ond,2.mod"
    "modules/504ond.mod" = "modules/504ond,2.mod"
    "modules/506ond.mod" = "modules/506ond,2.mod"
    "modules/511ond.mod" = "modules/511ond,1.mod"
    "modules/512ond.mod" = "modules/512ond,2.mod"
    "modules/601dan.mod" = "modules/601dan,1.mod"
    "modules/602dan.mod" = "modules/602dan,6.mod"
    "modules/604dan.mod" = "modules/604dan,6.mod"
    "modules/605dan.mod" = "modules/605dan,6.mod"
    "modules/610dan.mod" = "modules/610dan,6.mod"
    "modules/650dan.mod" = "modules/650dan,1.mod"
    "modules/701kor.mod" = "modules/701kor,1.mod"
    "modules/702kor_dlg.erf" = "modules/702kor_dlg,1.erf"
    "modules/702kor_s.rim" = "modules/702kor_s,1.rim"
    "modules/710kor.mod" = "modules/710kor,1.mod"
    "modules/711kor.mod" = "modules/711kor,4.mod"
    "modules/851nih.mod" = "modules/851nih,1.mod"
    "modules/852nih.mod" = "modules/852nih,4.mod"
    "modules/901mal.mod" = "modules/901mal,1.mod"
    "modules/902mal.mod" = "modules/902mal,4.mod"
    "modules/903mal.mod" = "modules/903mal,1.mod"
    "modules/904mal.mod" = "modules/904mal,4.mod"
    "modules/905mal.mod" = "modules/905mal,1.mod"
    "modules/906mal.mod" = "modules/906mal,4.mod"
    "modules/907mal.mod" = "modules/907mal,4.mod"
    "modules/909mal.mod" = "modules/909mal,1.mod"
    "modules/950cor.mod" = "modules/950cor,4.mod"
    "override/000react.dlg" = "override/000react,1.dlg"
    "override/003attkreia045.lip" = "override/003attkreia045,1.lip"
    "override/003attkreia046.lip" = "override/003attkreia046,1.lip"
    "override/003attkreia047.lip" = "override/003attkreia047,1.lip"
    "override/003attkreia048.lip" = "override/003attkreia048,1.lip"
    "override/003attkreia049.lip" = "override/003attkreia049,1.lip"
    "override/003attkreia050.lip" = "override/003attkreia050,1.lip"
    "override/903903atton001.lip" = "override/903903atton001,1.lip"
    "override/903903atton002.lip" = "override/903903atton002,1.lip"
    "override/903903atton003.lip" = "override/903903atton003,1.lip"
    "override/903903atton004.lip" = "override/903903atton004,1.lip"
    "override/903903atton005.lip" = "override/903903atton005,1.lip"
    "override/903903atton006.lip" = "override/903903atton006,1.lip"
    "override/903903atton007.lip" = "override/903903atton007,1.lip"
    "override/903903atton008.lip" = "override/903903atton008,1.lip"
    "override/903903atton009.lip" = "override/903903atton009,1.lip"
    "override/903903atton010.lip" = "override/903903atton010,1.lip"
    "override/903903atton011.lip" = "override/903903atton011,1.lip"
    "override/903903atton012.lip" = "override/903903atton012,1.lip"
    "override/903903atton013.lip" = "override/903903atton013,1.lip"
    "override/903903atton014.lip" = "override/903903atton014,1.lip"
    "override/903attsion002.lip" = "override/903attsion002,1.lip"
    "override/903attsion003.lip" = "override/903attsion003,1.lip"
    "override/903attsion004.lip" = "override/903attsion004,1.lip"
    "override/903attsion005.lip" = "override/903attsion005,1.lip"
    "override/903attsion006.lip" = "override/903attsion006,1.lip"
    "override/903attsion007.lip" = "override/903attsion007,1.lip"
    "override/903attsion008.lip" = "override/903attsion008,1.lip"
    "override/atton.dlg" = "override/atton,1.dlg"
    "override/baodur.dlg" = "override/baodur,1.dlg"
    "override/disciple.dlg" = "override/disciple,1.dlg"
    "override/g0t0.dlg" = "override/g0t0,1.dlg"
    "override/gblkreia001.lip" = "override/gblkreia001,1.lip"
    "override/global.jrl" = "override/global,6.jrl"
    "override/handmaiden.dlg" = "override/handmaiden,1.dlg"
    "override/hanharr.dlg" = "override/hanharr,1.dlg"
    "override/hk47.dlg" = "override/hk47,1.dlg"
    "override/kotor2logo.tga" = "override/kotor2logo,1.tga"
    "override/kreia.dlg" = "override/kreia,1.dlg"
    "override/lrn_form.dlg" = "override/lrn_form,1.dlg"
    "override/mandalore.dlg" = "override/mandalore,1.dlg"
    "override/mira.dlg" = "override/mira,1.dlg"
    "override/remote.dlg" = "override/remote,3.dlg"
    "override/statussummary_p.gui" = "override/statussummary_p,1.gui"
    "override/t3m4.dlg" = "override/t3m4,1.dlg"
    "override/visasmarr.dlg" = "override/visasmarr,1.dlg"
    "streamvoice/gbl/kreia/gblkreia001.mp3" = "streamvoice/gbl/kreia/gblkreia001,1.mp3"
    "streamvoice/904/904kreia/904904kreia994.mp3" = "streamvoice/904/904kreia/904904kreia994,1.mp3"
    "streamvoice/904/904kreia/904904kreia995.mp3" = "streamvoice/904/904kreia/904904kreia995,1.mp3"
    "streamvoice/904/904kreia/904904kreia996.mp3" = "streamvoice/904/904kreia/904904kreia996,1.mp3"
    "streamvoice/904/904kreia/904904kreia997.mp3" = "streamvoice/904/904kreia/904904kreia997,1.mp3"
    "streamvoice/904/904kreia/904904kreia998.mp3" = "streamvoice/904/904kreia/904904kreia998,1.mp3"
    "streamvoice/904/904kreia/904904kreia999.mp3" = "streamvoice/904/904kreia/904904kreia999,1.mp3"
}

function Get-SevenZipExe {
    $exe = Join-Path $PSScriptRoot "7z.exe"
    $dll = Join-Path $PSScriptRoot "7z.dll"
    if (-not (Test-Path -LiteralPath $exe) -or -not (Test-Path -LiteralPath $dll)) {
        throw "Bundled 7-Zip is missing. Expected '$exe' and '$dll'."
    }
    return $exe
}

function Get-InstallerHash([string]$Path) {
    return [System.BitConverter]::ToString(
        [System.Security.Cryptography.SHA256]::Create().ComputeHash([System.IO.File]::ReadAllBytes($Path))
    ).Replace("-", "")
}

function Get-ManifestData {
    if (-not (Test-Path -LiteralPath $ManifestPath)) {
        throw "Could not find manifest '$ManifestPath'."
    }

    $lines = Get-Content -LiteralPath $ManifestPath
    $meta = @{}
    $headerIndex = -1
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i].StartsWith("Meta`t")) {
            $parts = $lines[$i].Split("`t")
            if ($parts.Count -ge 3) {
                $meta[$parts[1]] = $parts[2]
            }
            continue
        }
        if ($lines[$i].StartsWith("Path`t")) {
            $headerIndex = $i
            break
        }
    }

    if ($headerIndex -lt 0) {
        throw "Manifest '$ManifestPath' does not contain a payload table."
    }

    $entries = @($lines[$headerIndex..($lines.Count - 1)] | ConvertFrom-Csv -Delimiter "`t")
    if (-not $entries) {
        throw "Manifest '$ManifestPath' does not contain payload entries."
    }

    $compressedSize = [int64]$entries[0].ChunkCompressedSize
    $startOffset = [int64]$entries[0].StartOffset
    $decompressedSize = 0L
    foreach ($entry in $entries) {
        if ([int64]$entry.ChunkCompressedSize -ne $compressedSize -or [int64]$entry.StartOffset -ne $startOffset) {
            throw "Manifest contains multiple chunk layouts."
        }
        if ([int]$entry.FirstSlice -ne 0 -or [int]$entry.LastSlice -ne 0) {
            throw "Manifest contains sliced payload entries."
        }
        $endOffset = [int64]$entry.ChunkSuboffset + [int64]$entry.OriginalSize
        if ($endOffset -gt $decompressedSize) {
            $decompressedSize = $endOffset
        }
    }

    return @{
        Meta = $meta
        Entries = $entries
        ChunkCompressedSize = $compressedSize
        ChunkStartOffset = $startOffset
        ChunkDecompressedSize = $decompressedSize
    }
}

function Read-ChunkBytes([string]$InstallerPath, [long]$AbsoluteOffset, [int]$CompressedSize) {
    $stream = [System.IO.File]::Open($InstallerPath, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::Read)
    try {
        $stream.Position = $AbsoluteOffset
        $marker = New-Object byte[] 4
        if ($stream.Read($marker, 0, $marker.Length) -ne $marker.Length) {
            throw "Could not read chunk marker."
        }
        for ($i = 0; $i -lt $marker.Length; $i++) {
            if ($marker[$i] -ne $ExpectedChunkMarker[$i]) {
                throw "Unexpected chunk marker at offset $AbsoluteOffset."
            }
        }

        $chunkBytes = New-Object byte[] $CompressedSize
        $totalRead = 0
        while ($totalRead -lt $chunkBytes.Length) {
            $read = $stream.Read($chunkBytes, $totalRead, $chunkBytes.Length - $totalRead)
            if ($read -le 0) {
                throw "Unexpected end of file while reading compressed payload."
            }
            $totalRead += $read
        }
        return ,$chunkBytes
    }
    finally {
        $stream.Dispose()
    }
}

function Write-PreparedLzmaPayload([byte[]]$ChunkBytes, [long]$OutputSize, [string]$Path) {
    $resolvedPath = [System.IO.Path]::GetFullPath($Path)
    $parent = Split-Path -Parent $resolvedPath
    if (-not [string]::IsNullOrWhiteSpace($parent) -and -not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Path $parent | Out-Null
    }

    $stream = [System.IO.File]::Open($resolvedPath, [System.IO.FileMode]::Create, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
    try {
        $stream.Write($ChunkBytes, 0, 5)
        $sizeBytes = [System.BitConverter]::GetBytes([int64]$OutputSize)
        $stream.Write($sizeBytes, 0, $sizeBytes.Length)
        $stream.Write($ChunkBytes, 5, $ChunkBytes.Length - 5)
    }
    finally {
        $stream.Dispose()
    }

    return $resolvedPath
}

function Expand-PreparedLzmaPayload([string]$PreparedPayloadPath, [string]$OutputDirectory) {
    if (-not (Test-Path -LiteralPath $OutputDirectory)) {
        New-Item -ItemType Directory -Path $OutputDirectory | Out-Null
    }

    & (Get-SevenZipExe) x -y "-o$OutputDirectory" $PreparedPayloadPath | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "7-Zip failed to extract '$PreparedPayloadPath'."
    }

    $expandedFiles = @(Get-ChildItem -LiteralPath $OutputDirectory -File)
    if ($expandedFiles.Count -ne 1) {
        throw "7-Zip did not produce exactly one expanded payload file."
    }
    return $expandedFiles[0].FullName
}

function Resolve-RelativePayloadPath([string]$ManifestEntryPath) {
    if ($ManifestEntryPath.StartsWith("{app}\")) {
        return $ManifestEntryPath.Substring(6)
    }
    throw "Unsupported manifest path '$ManifestEntryPath'."
}

function Write-RawPayloadFiles([string]$ExpandedPayloadPath, [object[]]$Entries, [string]$OutputDirectory) {
    $stream = [System.IO.File]::Open($ExpandedPayloadPath, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::Read)
    try {
        foreach ($entry in $Entries) {
            $relativePath = Resolve-RelativePayloadPath -ManifestEntryPath $entry.Path
            $targetPath = Join-Path $OutputDirectory $relativePath
            $targetDir = Split-Path -Parent $targetPath
            if (-not [string]::IsNullOrWhiteSpace($targetDir) -and -not (Test-Path -LiteralPath $targetDir)) {
                New-Item -ItemType Directory -Path $targetDir | Out-Null
            }

            $offset = [int64]$entry.ChunkSuboffset
            $length = [int]$entry.OriginalSize
            $buffer = New-Object byte[] $length
            $stream.Position = $offset
            $totalRead = 0
            while ($totalRead -lt $length) {
                $read = $stream.Read($buffer, $totalRead, $length - $totalRead)
                if ($read -le 0) {
                    throw "Unexpected end of expanded payload while reading '$relativePath'."
                }
                $totalRead += $read
            }

            [System.IO.File]::WriteAllBytes($targetPath, $buffer)
        }
    }
    finally {
        $stream.Dispose()
    }
}

function Test-VariantPath([string]$RelativePath) {
    return [bool]($RelativePath -match '^[^/\\]+(?:[/\\].+)?,[0-9]+\.[^.]+$')
}

function Copy-NormalizedTree([string]$SourceDirectory, [string]$OutputDirectory) {
    $renameSources = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($sourcePath in $RenameMap.Values) {
        [void]$renameSources.Add($sourcePath.Replace('\', '/'))
    }

    $installRootsSet = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($root in $InstallRoots) {
        [void]$installRootsSet.Add($root)
    }

    $skipSet = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($path in $SkipPaths) {
        [void]$skipSet.Add($path.Replace('\', '/'))
    }

    $sourceRoot = ((Resolve-Path -LiteralPath $SourceDirectory).Path).TrimEnd('\', '/')
    Get-ChildItem -LiteralPath $SourceDirectory -Recurse -File | ForEach-Object {
        $relativePath = ((Resolve-Path -LiteralPath $_.FullName).Path).Substring($sourceRoot.Length).TrimStart('\', '/').Replace('\', '/')
        $topLevel = ($relativePath -split '/', 2)[0]
        if (-not $installRootsSet.Contains($topLevel)) { return }
        if ($skipSet.Contains($relativePath)) { return }
        if ($renameSources.Contains($relativePath)) { return }
        if (Test-VariantPath -RelativePath $relativePath) { return }

        $targetPath = Join-Path $OutputDirectory $relativePath
        $targetDir = Split-Path -Parent $targetPath
        if (-not [string]::IsNullOrWhiteSpace($targetDir) -and -not (Test-Path -LiteralPath $targetDir)) {
            New-Item -ItemType Directory -Path $targetDir | Out-Null
        }
        Copy-Item -LiteralPath $_.FullName -Destination $targetPath -Force
    }

    foreach ($targetRelativePath in $RenameMap.Keys) {
        $sourcePath = Join-Path $SourceDirectory $RenameMap[$targetRelativePath]
        if (-not (Test-Path -LiteralPath $sourcePath)) {
            throw "Expected normalized source file is missing: '$($RenameMap[$targetRelativePath])'."
        }
        $targetPath = Join-Path $OutputDirectory $targetRelativePath
        $targetDir = Split-Path -Parent $targetPath
        if (-not [string]::IsNullOrWhiteSpace($targetDir) -and -not (Test-Path -LiteralPath $targetDir)) {
            New-Item -ItemType Directory -Path $targetDir | Out-Null
        }
        Copy-Item -LiteralPath $sourcePath -Destination $targetPath -Force
    }
}

function New-ZipFromDirectory([string]$SourceDirectory, [string]$ArchivePath) {
    $resolvedArchivePath = [System.IO.Path]::GetFullPath($ArchivePath)
    $parent = Split-Path -Parent $resolvedArchivePath
    if (-not [string]::IsNullOrWhiteSpace($parent) -and -not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Path $parent | Out-Null
    }
    if (Test-Path -LiteralPath $resolvedArchivePath) {
        Remove-Item -LiteralPath $resolvedArchivePath -Force
    }

    Push-Location $SourceDirectory
    try {
        & (Get-SevenZipExe) a -tzip -mx=0 $resolvedArchivePath ".\*" | Out-Null
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $resolvedArchivePath)) {
            throw "7-Zip failed to create ZIP archive '$resolvedArchivePath'."
        }
    }
    finally {
        Pop-Location
    }

    return $resolvedArchivePath
}

function Invoke-TslrcmExtract {
    $installerPath = (Resolve-Path -LiteralPath $InputPath).Path
    if ((Get-InstallerHash -Path $installerPath) -ne $ExpectedInstallerHash) {
        throw "Unexpected installer hash for '$installerPath'."
    }

    $manifest = Get-ManifestData
    $loaderOffset = [int64]$manifest.Meta["SetupLdrOffset1"]
    [byte[]]$chunkBytes = Read-ChunkBytes `
        -InstallerPath $installerPath `
        -AbsoluteOffset ($loaderOffset + [int64]$manifest.ChunkStartOffset) `
        -CompressedSize ([int]$manifest.ChunkCompressedSize)

    $preparedPath = if (-not [string]::IsNullOrWhiteSpace($PreparedPayloadPath)) {
        Write-PreparedLzmaPayload -ChunkBytes $chunkBytes -OutputSize $manifest.ChunkDecompressedSize -Path $PreparedPayloadPath
    }
    else {
        Write-PreparedLzmaPayload `
            -ChunkBytes $chunkBytes `
            -OutputSize $manifest.ChunkDecompressedSize `
            -Path (Join-Path ([System.IO.Path]::GetTempPath()) ("tslrcm_payload_{0}.lzma" -f [guid]::NewGuid().ToString("N")))
    }

    if ($PrepareOnly) {
        Write-Host "Prepared '$preparedPath' for $($manifest.Entries.Count) files."
        return
    }

    $resolvedOutputPath = [System.IO.Path]::GetFullPath($OutputPath)
    $writeArchive = [System.IO.Path]::GetExtension($resolvedOutputPath).Equals(".zip", [System.StringComparison]::OrdinalIgnoreCase)
    $normalizedOutputPath = if ($writeArchive) {
        Join-Path ([System.IO.Path]::GetTempPath()) ("tslrcm_output_{0}" -f [guid]::NewGuid().ToString("N"))
    }
    else {
        $resolvedOutputPath
    }

    if (-not (Test-Path -LiteralPath $normalizedOutputPath)) {
        New-Item -ItemType Directory -Path $normalizedOutputPath | Out-Null
    }
    else {
        Get-ChildItem -LiteralPath $normalizedOutputPath -Force | Remove-Item -Recurse -Force
    }

    $workingRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("tslrcm_extract_{0}" -f [guid]::NewGuid().ToString("N"))
    try {
        $expandedDirectory = Join-Path $workingRoot "expanded"
        $rawDirectory = Join-Path $workingRoot "raw"
        New-Item -ItemType Directory -Path $expandedDirectory -Force | Out-Null
        New-Item -ItemType Directory -Path $rawDirectory -Force | Out-Null

        $expandedPayloadPath = Expand-PreparedLzmaPayload -PreparedPayloadPath $preparedPath -OutputDirectory $expandedDirectory
        Write-RawPayloadFiles -ExpandedPayloadPath $expandedPayloadPath -Entries $manifest.Entries -OutputDirectory $rawDirectory
        Copy-NormalizedTree -SourceDirectory $rawDirectory -OutputDirectory $normalizedOutputPath
        $fileCount = @(Get-ChildItem -LiteralPath $normalizedOutputPath -Recurse -File).Count

        if ($writeArchive) {
            $archivePath = New-ZipFromDirectory -SourceDirectory $normalizedOutputPath -ArchivePath $resolvedOutputPath
            Write-Host "Created ZIP archive with $fileCount files at '$archivePath'."
        }
        else {
            Write-Host "Extracted $fileCount files to '$normalizedOutputPath'."
        }

        Write-Host "Prepared payload at '$preparedPath'."
    }
    finally {
        if (Test-Path -LiteralPath $workingRoot) {
            Remove-Item -LiteralPath $workingRoot -Recurse -Force
        }
        if ($writeArchive -and (Test-Path -LiteralPath $normalizedOutputPath)) {
            Remove-Item -LiteralPath $normalizedOutputPath -Recurse -Force
        }
        if ([string]::IsNullOrWhiteSpace($PreparedPayloadPath) -and $preparedPath -and (Test-Path -LiteralPath $preparedPath)) {
            Remove-Item -LiteralPath $preparedPath -Force
        }
    }
}

Invoke-TslrcmExtract
