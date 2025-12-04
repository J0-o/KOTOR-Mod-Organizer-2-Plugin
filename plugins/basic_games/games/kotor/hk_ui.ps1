# ======================================================================
# HK UI - TSLPatcher Multi-Patcher Selection (Fixed Scrollbar Layout)
# ======================================================================

param(
    [string]$CsvPath = (Join-Path $PSScriptRoot 'tslpatch_order.csv'),
    [string]$OutPath = (Join-Path $PSScriptRoot 'tslpatch_selected.csv')
)

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

if (-not (Test-Path -LiteralPath $CsvPath)) {
    [System.Windows.Forms.MessageBox]::Show("CSV not found: $CsvPath","Error",
        [System.Windows.Forms.MessageBoxButtons]::OK,[System.Windows.Forms.MessageBoxIcon]::Error)
    exit
}

# --- Font setup ---
$font = New-Object System.Drawing.Font('Segoe UI',10)
$lineHeight = [math]::Ceiling($font.GetHeight())
$rowHeight  = ($lineHeight * 5) + 6

# ======================================================================
# Form setup
# ======================================================================
$form = New-Object System.Windows.Forms.Form
$form.Text = 'TSLPatcher Multi-Patcher Selection'
$form.Size = New-Object System.Drawing.Size(1200,700)
$form.MinimumSize = New-Object System.Drawing.Size(1000,600)
$form.StartPosition = 'CenterScreen'
$form.BackColor = [System.Drawing.Color]::White

# ======================================================================
# TableLayoutPanel container
# ======================================================================
$table = New-Object System.Windows.Forms.TableLayoutPanel
$table.Dock = 'Fill'
$table.RowCount = 2
$table.ColumnCount = 1
$table.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Percent, 100)))
$table.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Absolute, 50)))
$form.Controls.Add($table)

# ======================================================================
# DataGridView inside TableLayoutPanel
# ======================================================================
$grid = New-Object System.Windows.Forms.DataGridView
$grid.Dock = 'Fill'
$grid.Margin = '10,10,10,5'
$grid.AllowUserToAddRows = $false
$grid.AllowUserToDeleteRows = $false
$grid.AllowUserToResizeRows = $false
$grid.RowHeadersVisible = $false
$grid.EditMode = 'EditProgrammatically'
$grid.AutoSizeColumnsMode = 'None'
$grid.BackgroundColor = [System.Drawing.Color]::White
$grid.DefaultCellStyle.WrapMode = 'True'
$grid.DefaultCellStyle.Font = $font
$grid.ColumnHeadersDefaultCellStyle.Font = $font
$grid.RowTemplate.Height = $rowHeight
$grid.MultiSelect = $false
$grid.SelectionMode = 'FullRowSelect'
$grid.EnableHeadersVisualStyles = $false
$grid.ColumnHeadersDefaultCellStyle.BackColor = [System.Drawing.Color]::FromArgb(230,230,230)
$grid.DefaultCellStyle.SelectionBackColor = $grid.DefaultCellStyle.BackColor
$grid.DefaultCellStyle.SelectionForeColor = $grid.DefaultCellStyle.ForeColor
$grid.DefaultCellStyle.BackColor = [System.Drawing.Color]::White
$grid.AlternatingRowsDefaultCellStyle.BackColor = [System.Drawing.Color]::FromArgb(245,248,255)
$table.Controls.Add($grid, 0, 0)

# ======================================================================
# Columns
# ======================================================================
$columns = @(
    @{Name='Enabled'; Header='Checked'; ReadOnly=$false},
    @{Name='ModName'; Header='Mod'; ReadOnly=$true},
    @{Name='PatchName'; Header='Patch'; ReadOnly=$true},
    @{Name='Description'; Header='Description'; ReadOnly=$true},
    @{Name='IniShortPath'; Header='Path'; ReadOnly=$true; Visible=$false},
    @{Name='Files'; Header='Files'; ReadOnly=$true}
)
foreach ($c in $columns) {
    $col = New-Object System.Windows.Forms.DataGridViewTextBoxColumn
    $col.Name = $c.Name
    $col.HeaderText = $c.Header
    $col.ReadOnly = $c.ReadOnly
    if ($c.ContainsKey('Visible')) { $col.Visible = $c.Visible }
    [void]$grid.Columns.Add($col)
}

# ======================================================================
# Load CSV
# ======================================================================
$rows = @(Import-Csv -LiteralPath $CsvPath -Encoding UTF8)
foreach ($r in $rows) {
    $i = $grid.Rows.Add()
    $row = $grid.Rows[$i]
    $row.Cells['Enabled'].Value      = if ($r.Enabled -eq '1') { 'True' } else { 'False' }
    $row.Cells['ModName'].Value      = $r.ModName
    $row.Cells['PatchName'].Value    = $r.PatchName
    $row.Cells['Description'].Value  = $r.Description
    $row.Cells['IniShortPath'].Value = $r.IniShortPath
    $row.Cells['Files'].Value        = $r.Files
    $row.Height = $rowHeight
}
$grid.ClearSelection()
$grid.CurrentCell = $null

# ======================================================================
# Header counter
# ======================================================================
function Update-Header {
    $checked = ($grid.Rows | Where-Object { $_.Cells['Enabled'].Value -eq 'True' }).Count
    $total   = $grid.Rows.Count
    $grid.Columns['Enabled'].HeaderText = "$checked/$total"
    $grid.Refresh()
}
Update-Header

# ======================================================================
# Checkbox drawing
# ======================================================================
$checkSize = 24
$grid.Add_CellPainting({
    param($sender, $e)
    if ($e.ColumnIndex -eq 0 -and $e.RowIndex -ge 0) {
        $e.Handled = $true
        $e.PaintBackground($e.ClipBounds, $true)
        $x = $e.CellBounds.X + [math]::Floor(($e.CellBounds.Width - $checkSize) / 2)
        $y = $e.CellBounds.Y + [math]::Floor(($e.CellBounds.Height - $checkSize) / 2)
        $rect = [System.Drawing.Rectangle]::FromLTRB($x,$y,$x+$checkSize,$y+$checkSize)
        $g = $e.Graphics
        $g.DrawRectangle([System.Drawing.Pens]::Gray, $rect)
        $value = $e.FormattedValue
        if ($value -eq 'True' -or $value -eq $true) {
            $inner = [System.Drawing.Rectangle]::Inflate($rect,-4,-4)
            $g.FillRectangle([System.Drawing.Brushes]::ForestGreen, $inner)
            $g.DrawRectangle([System.Drawing.Pens]::DarkGreen, $inner)
        }
        $e.Paint($e.ClipBounds, [System.Windows.Forms.DataGridViewPaintParts]::Border)
    }
})

# ======================================================================
# Click & double-click toggles
# ======================================================================
$grid.Add_MouseDown({
    param($sender, $e)
    $hit = $grid.HitTest($e.X, $e.Y)
    if ($hit.Type -eq 'Cell' -and $hit.ColumnIndex -eq 0 -and $hit.RowIndex -ge 0) {
        $cell = $grid.Rows[$hit.RowIndex].Cells[$hit.ColumnIndex]
        $val = if ($cell.Value -eq 'True' -or $cell.Value -eq $true) { 'False' } else { 'True' }
        $cell.Value = $val
        $grid.InvalidateCell($cell)
        Update-Header
    }
})

$grid.Add_CellDoubleClick({
    param($sender, $e)
    if ($e.RowIndex -ge 0 -and $e.ColumnIndex -ne 0) {
        $cell = $grid.Rows[$e.RowIndex].Cells[0]
        $val = if ($cell.Value -eq 'True' -or $cell.Value -eq $true) { 'False' } else { 'True' }
        $cell.Value = $val
        $grid.InvalidateCell($cell)
        Update-Header
    }
})

$grid.Add_SelectionChanged({ $grid.ClearSelection(); $grid.CurrentCell = $null })

# ======================================================================
# Save & Exit Button Panel (fixed)
# ======================================================================
$btnPanel = New-Object System.Windows.Forms.Panel
$btnPanel.Dock = 'Top'        # Fill causes overflow; use Top so height is respected
$btnPanel.Height = 50         # matches the RowStyle absolute height
$btnPanel.Padding = '0,0,15,10'
$table.Controls.Add($btnPanel, 0, 1)


# ======================================================================
# Duplicate Files Panel (simple text list)
# ======================================================================
$dupPanel = New-Object System.Windows.Forms.Panel
$dupPanel.Dock = 'Bottom'
$dupPanel.Height = 160
$dupPanel.Padding = '10,0,10,10'
$form.Controls.Add($dupPanel)

$dupLabel = New-Object System.Windows.Forms.Label
$dupLabel.Text = "Duplicate Files (conflicts across mods):"
$dupLabel.Font = $font
$dupLabel.AutoSize = $true
$dupPanel.Controls.Add($dupLabel)

$dupBox = New-Object System.Windows.Forms.TextBox
$dupBox.Multiline = $true
$dupBox.ReadOnly = $true
$dupBox.ScrollBars = "Vertical"
$dupBox.Font = $font
$dupBox.BackColor = [System.Drawing.Color]::White
$dupBox.Width = $dupPanel.ClientSize.Width - 20
$dupBox.Height = $dupPanel.ClientSize.Height - 30
$dupBox.Location = New-Object System.Drawing.Point(10,25)
$dupPanel.Controls.Add($dupBox)

# Resize handling
$dupPanel.Add_Resize({
    $dupBox.Width = $dupPanel.ClientSize.Width - 20
    $dupBox.Height = $dupPanel.ClientSize.Height - 30
})

# Load duplicate_files.csv into the box
$dupCsv = Join-Path $PSScriptRoot 'duplicate_files.csv'
if (Test-Path -LiteralPath $dupCsv) {
    $items = Import-Csv -LiteralPath $dupCsv -Encoding UTF8
    $lines = foreach ($i in $items) {
        "{0} - {1}" -f $i.FileName, $i.Mods
        ""   # blank line after each entry
    }
    $dupBox.Text = ($lines -join [Environment]::NewLine)
} else {
    $dupBox.Text = "No duplicate file data found."
}
###########

$btnOK = New-Object System.Windows.Forms.Button
$btnOK.Text = 'Save and Exit'
$btnOK.Size = New-Object System.Drawing.Size(140,30)
$btnOK.Anchor = 'Bottom,Right'
$btnOK.Location = New-Object System.Drawing.Point(($btnPanel.ClientSize.Width - $btnOK.Width - 10),10)
$btnPanel.Controls.Add($btnOK)

$btnPanel.Add_Resize({
    $btnOK.Location = New-Object System.Drawing.Point(($btnPanel.ClientSize.Width - $btnOK.Width - 10),10)
})

$btnOK.Add_Click({
    # Load existing CSV (preserve all columns)
    $existingRows = Import-Csv -LiteralPath $CsvPath -Encoding UTF8

    foreach ($row in $existingRows) {
        # Find the matching row in the grid
        $match = $grid.Rows | Where-Object {
            $_.Cells['ModName'].Value -eq $row.ModName -and
            $_.Cells['PatchName'].Value -eq $row.PatchName
        }

        # Only update the Enabled column based on checkbox state
        if ($match) {
            $isChecked = ($match[0].Cells['Enabled'].Value -eq 'True' -or $match[0].Cells['Enabled'].Value -eq $true)
            $row.Enabled = if ($isChecked) { 1 } else { 0 }
        }
    }

    # Overwrite CSV with preserved structure
    $existingRows | Export-Csv -LiteralPath $CsvPath -NoTypeInformation -Encoding UTF8

    [System.Windows.Forms.MessageBox]::Show("Selections saved to:`n$CsvPath","Saved",
        [System.Windows.Forms.MessageBoxButtons]::OK,[System.Windows.Forms.MessageBoxIcon]::Information)

    # Run the runner after saving
    . (Join-Path $PSScriptRoot "hk_runner.ps1")
    $form.Close()
})


# ======================================================================
# Column width ratios
# ======================================================================
$columnRatios = @{
    Enabled     = 0.06
    ModName     = 0.25
    PatchName   = 0.15
    Description = 0.35
    Files       = 0.17
}

function Resize-Columns {
    $availableWidth = $grid.ClientSize.Width
    foreach ($col in $grid.Columns) {
        if ($columnRatios.ContainsKey($col.Name)) {
            $col.Width = [math]::Floor($availableWidth * $columnRatios[$col.Name])
        }
    }
}
$form.Add_Resize({ Resize-Columns })
Resize-Columns

# ======================================================================
# Show
# ======================================================================
$form.ShowDialog()
