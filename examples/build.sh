#!/bin/bash
# Build crash examples on Linux / macOS.
#
# Usage:
#   cd examples
#   chmod +x build.sh
#   ./build.sh
#
# Requires: gcc or clang with debug info support.
# Output: build/out/<name>  (executable with DWARF debug info)

set -e

OUTDIR="build/out"
mkdir -p "$OUTDIR"

# Detect compiler
if command -v gcc &>/dev/null; then
    CC=gcc
    CXX=g++
elif command -v clang &>/dev/null; then
    CC=clang
    CXX=clang++
else
    echo "ERROR: Neither gcc nor clang found on PATH."
    exit 1
fi

echo "Using compiler: $CXX"
echo "Output directory: $OUTDIR"
echo ""

SOURCES=(
    stack-overflow.cpp
    use-after-free.cpp
    double-free.cpp
    vtable-corruption.cpp
    stack-buffer-overrun.cpp
    heap-corruption.cpp
)

for src in "${SOURCES[@]}"; do
    base="${src%.cpp}"
    echo "  Compiling $src -> $OUTDIR/$base"
    $CXX -g -O0 -o "$OUTDIR/$base" "$src" 2>/dev/null || \
    $CXX -g -O0 -o "$OUTDIR/$base" "$src" -std=c++17
done

echo ""
echo "Build complete. Executables with debug symbols in $OUTDIR/"
echo ""
echo "To generate core dumps, ensure core dumps are enabled:"
echo "  ulimit -c unlimited"
echo ""
echo "Then run an example:"
echo "  ./$OUTDIR/stack-overflow"
echo ""
echo "Core dumps will be written to the current directory or /var/crash/ depending on your system."
