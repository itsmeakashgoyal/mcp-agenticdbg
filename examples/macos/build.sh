#!/bin/bash
# Build crash examples on macOS.
#
# Usage:
#   cd examples/macos
#   chmod +x build.sh
#   ./build.sh
#
# Requires: clang++ (Xcode Command Line Tools).
# Output: build/out/<name>  (executable with DWARF debug info)

set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
COMMON_DIR="$SCRIPT_DIR/../common"
OUTDIR="$SCRIPT_DIR/build/out"
mkdir -p "$OUTDIR"

# Detect compiler (or honor explicit override)
if [[ -n "${CXX:-}" ]]; then
    if ! command -v "$CXX" &>/dev/null; then
        echo "ERROR: CXX is set to '$CXX', but that compiler is not on PATH."
        exit 1
    fi
elif command -v clang++ &>/dev/null; then
    CXX=clang++
elif command -v g++ &>/dev/null; then
    CXX=g++
else
    echo "ERROR: Neither clang++ nor g++ found on PATH."
    echo "Install Xcode Command Line Tools:  xcode-select --install"
    exit 1
fi

BASE_FLAGS=(-g -O0 -std=c++17 "-I$COMMON_DIR")
EXTRA_CXXFLAGS=()
if [[ -n "${CXXFLAGS:-}" ]]; then
    # shellcheck disable=SC2206
    EXTRA_CXXFLAGS=(${CXXFLAGS})
fi

echo "Using compiler: $CXX"
echo "Source directory: $COMMON_DIR"
echo "Output directory: $OUTDIR"
echo ""

# Standard single-file sources
SOURCES=(
    stack-overflow.cpp
    use-after-free.cpp
    double-free.cpp
    vtable-corruption.cpp
    stack-buffer-overrun.cpp
    heap-corruption.cpp
    deep-callchain-nullptr.cpp
    heap-metadata-corruption.cpp
    multi-inheritance-crash.cpp
)

for src in "${SOURCES[@]}"; do
    base="${src%.cpp}"
    echo "  Compiling $src -> $OUTDIR/$base"
    "$CXX" "${BASE_FLAGS[@]}" "${EXTRA_CXXFLAGS[@]}" -o "$OUTDIR/$base" "$COMMON_DIR/$src"
done

# Sources requiring extra link flags
THREADED_SOURCES=(
    thread-uaf.cpp
)

for src in "${THREADED_SOURCES[@]}"; do
    base="${src%.cpp}"
    echo "  Compiling $src -> $OUTDIR/$base  (+ -lpthread)"
    "$CXX" "${BASE_FLAGS[@]}" "${EXTRA_CXXFLAGS[@]}" -o "$OUTDIR/$base" "$COMMON_DIR/$src" -lpthread
done

echo ""
echo "Build complete. Executables with debug symbols in $OUTDIR/"
echo ""
echo "To generate core dumps, use gen_core_mac.sh:"
echo "  ./gen_core_mac.sh use-after-free   # core -> $OUTDIR/core.use-after-free"
