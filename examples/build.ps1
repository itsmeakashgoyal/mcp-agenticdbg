# build.ps1 — Compile all crash examples with full debug symbols (PDB).
#
# Prerequisites:
#   Run from a "Developer Command Prompt for Visual Studio" (or
#   "Developer PowerShell") so that cl.exe and link.exe are on PATH.
#
# Output layout:
#   build\           — intermediate .obj files
#   build\out\       — .exe + .pdb files (ready for WinDbg)

$ErrorActionPreference = "Stop"

$examplesDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$buildDir    = Join-Path $examplesDir "build"
$outDir      = Join-Path $buildDir   "out"

New-Item -ItemType Directory -Path $buildDir -Force | Out-Null
New-Item -ItemType Directory -Path $outDir   -Force | Out-Null

$sources = Get-ChildItem -Path $examplesDir -Filter "*.cpp"
if ($sources.Count -eq 0) {
    Write-Warning "No .cpp files found in $examplesDir"
    exit 1
}

foreach ($src in $sources) {
    $base = $src.BaseName
    $obj  = Join-Path $buildDir "$base.obj"
    $exe  = Join-Path $outDir   "$base.exe"
    $pdb  = Join-Path $outDir   "$base.pdb"

    Write-Host "Building $($src.Name) -> $exe" -ForegroundColor Cyan

    # /Zi   — generate debug info (compiler PDB)
    # /Od   — disable optimisation (cleaner stack traces for demos)
    # /MT   — static CRT (self-contained exe)
    # /EHsc — standard C++ exception handling
    # /GS-  — disable buffer security checks so overruns show raw corruption
    # /link /DEBUG          — linker: emit full PDB
    # /link /INCREMENTAL:NO — linker: no incremental linking
    # /link /PDB:<path>     — linker: place PDB next to exe
    cl.exe /nologo /Zi /Od /MT /EHsc /GS- `
        /Fo:$obj /Fe:$exe $src.FullName `
        /link /DEBUG /INCREMENTAL:NO /PDB:$pdb

    if ($LASTEXITCODE -ne 0) {
        Write-Error "Failed to build $($src.Name)"
    }
}

Write-Host ""
Write-Host "Build complete.  Executables + PDBs in: $outDir" -ForegroundColor Green
Write-Host "PDB files ensure WinDbg can map offsets to function names and source lines."
