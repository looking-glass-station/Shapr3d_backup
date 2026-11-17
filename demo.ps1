<#
.SYNOPSIS
    Demo wrapper for the Shapr3D backup utility (main.py).

.DESCRIPTION
    Runs main.py to export your local Shapr3D projects as .shapr files.
    Uses uv if it's installed (it picks the Python version from pyproject.toml);
    otherwise falls back to the python on PATH.

.PARAMETER ExportDir
    Where backups are written. Defaults to Documents\Shapr3D Backups.

.PARAMETER SkipTrashed
    Don't export trashed (TempState) projects.

.PARAMETER NoRevision
    Don't add [rev-X] to file names. Each project then keeps a single backup that is
    never refreshed, which saves space but means newer revisions are not captured.

.EXAMPLE
    .\demo.ps1

.EXAMPLE
    .\demo.ps1 -ExportDir D:\Backups\Shapr3D -SkipTrashed

.EXAMPLE
    # Run it every day at 9pm (run once from your own PowerShell window):
    $action  = New-ScheduledTaskAction -Execute "pwsh.exe" -Argument "-NoProfile -File `"$PWD\demo.ps1`""
    $trigger = New-ScheduledTaskTrigger -Daily -At 9pm
    Register-ScheduledTask -TaskName "Shapr3D Backup" -Action $action -Trigger $trigger
#>
[CmdletBinding()]
param(
    [string]$ExportDir = (Join-Path ([Environment]::GetFolderPath("MyDocuments")) "Shapr3D Backups"),
    [switch]$SkipTrashed,
    [switch]$NoRevision
)

$ErrorActionPreference = "Stop"

$script = Join-Path $PSScriptRoot "main.py"
if (-not (Test-Path $script)) {
    throw "main.py not found next to this script ($PSScriptRoot)."
}

$pyArgs = @($script, "--export-dir", $ExportDir)
if ($SkipTrashed) { $pyArgs += "--no-include-tempstate" }
if ($NoRevision)  { $pyArgs += "--no-add-revision" }

if (Get-Command uv -ErrorAction SilentlyContinue) {
    $exe = "uv"
    $pyArgs = @("run", "--project", $PSScriptRoot, "python") + $pyArgs
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $exe = "python"
} else {
    throw "Neither uv nor python was found on PATH."
}

Write-Host "Backing up Shapr3D projects to: $ExportDir" -ForegroundColor Cyan
$start = Get-Date

& $exe @pyArgs
$code = $LASTEXITCODE

if ($code -ne 0) {
    Write-Host "Backup failed (exit code $code)." -ForegroundColor Red
    exit $code
}

$files = @(Get-ChildItem $ExportDir -Recurse -Filter *.shapr -ErrorAction SilentlyContinue)
$sizeMB = [math]::Round((($files | Measure-Object Length -Sum).Sum / 1MB), 1)
$elapsed = [math]::Round(((Get-Date) - $start).TotalSeconds, 1)

Write-Host ""
Write-Host "Backup complete in ${elapsed}s: $($files.Count) .shapr files, $sizeMB MB total." -ForegroundColor Green
Write-Host "Location: $ExportDir"
