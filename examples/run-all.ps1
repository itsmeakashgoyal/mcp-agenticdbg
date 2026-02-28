# run-all.ps1 — Execute every crash example and collect the dumps.
#
# Each example self-dumps via crashdump.h, writing a .dmp file into
# <exe-dir>\dumps\ before terminating.
#
# Usage:
#   .\run-all.ps1            — run all examples
#   .\run-all.ps1 -Name foo  — run only examples whose name matches *foo*

param(
    [string]$Name = "*"
)

$ErrorActionPreference = "Continue"

$examplesDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$outDir      = Join-Path $examplesDir "build\out"
$dumpDir     = Join-Path $outDir      "dumps"

if (-not (Test-Path $outDir)) {
    Write-Error "Build output not found at $outDir — run build.ps1 first."
    exit 1
}

New-Item -ItemType Directory -Path $dumpDir -Force | Out-Null

$exes = Get-ChildItem -Path $outDir -Filter "*.exe" |
        Where-Object { $_.BaseName -like $Name }

if ($exes.Count -eq 0) {
    Write-Warning "No executables matching '$Name' found in $outDir"
    exit 1
}

$results = @()

foreach ($exe in $exes) {
    Write-Host "`n========================================" -ForegroundColor Yellow
    Write-Host " Running: $($exe.Name)" -ForegroundColor Yellow
    Write-Host "========================================" -ForegroundColor Yellow

    $before = @(Get-ChildItem -Path $dumpDir -Filter "*.dmp" -ErrorAction SilentlyContinue)

    $proc = Start-Process -FilePath $exe.FullName `
                          -WorkingDirectory $outDir `
                          -PassThru `
                          -NoNewWindow `
                          -Wait

    $exitCode = $proc.ExitCode

    $after = @(Get-ChildItem -Path $dumpDir -Filter "*.dmp" -ErrorAction SilentlyContinue)
    $newDumps = $after | Where-Object { $_.FullName -notin $before.FullName }

    $dumpFile = if ($newDumps.Count -gt 0) { $newDumps[-1].Name } else { "(none)" }

    $results += [PSCustomObject]@{
        Example  = $exe.BaseName
        ExitCode = $exitCode
        DumpFile = $dumpFile
    }

    if ($newDumps.Count -gt 0) {
        Write-Host "  -> Dump: $($newDumps[-1].FullName)" -ForegroundColor Green
    } else {
        Write-Host "  -> No dump file created" -ForegroundColor Red
    }
}

Write-Host "`n========================================" -ForegroundColor Cyan
Write-Host " Summary" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
$results | Format-Table -AutoSize

Write-Host "Dump directory: $dumpDir"
Write-Host "PDB  directory: $outDir"
Write-Host ""
Write-Host "To analyze a dump with win_crashdbg, use:" -ForegroundColor Cyan
Write-Host "  open_windbg_dump  <path-to-.dmp>"
Write-Host "  run_windbg_cmd    .ecxr; kv"
