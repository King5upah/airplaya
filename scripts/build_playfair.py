"""Build the native `playfair` shared library.

The C sources in `native/playfair/` have no dependencies beyond libc, so any
C compiler will do. We look for `zig cc` first because it is a single portable
download on Windows and needs no Visual Studio install.

    python scripts/build_playfair.py

The result lands next to the Python binding, at
`src/airplaya/crypto/playfair.<dll|so|dylib>`.
"""

from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIR = ROOT / "native" / "playfair"
OUTPUT_DIR = ROOT / "src" / "airplaya" / "crypto"

SOURCES = [
    "hand_garble.c",
    "modified_md5.c",
    "omg_hax.c",
    "playfair.c",
    "sap_hash.c",
]


def library_name() -> str:
    if sys.platform == "win32":
        return "playfair.dll"
    if sys.platform == "darwin":
        return "libplayfair.dylib"
    return "libplayfair.so"


def find_compiler() -> list[str]:
    """Return an argv prefix that compiles C, or exit with instructions."""
    zig = shutil.which("zig")
    if zig:
        return [zig, "cc"]
    for name in ("clang", "gcc", "cc"):
        found = shutil.which(name)
        if found:
            return [found]
    if sys.platform == "win32" and shutil.which("cl"):
        return ["cl"]

    raise SystemExit(
        "No C compiler found. Install one of:\n"
        "  zig      winget install zig.zig        (smallest, no admin needed)\n"
        "  clang    winget install LLVM.LLVM\n"
        "  gcc      via MSYS2 or your package manager"
    )


def build(compiler: list[str], output: Path, verbose: bool) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    sources = [str(SOURCE_DIR / name) for name in SOURCES]

    if compiler[0] == "cl":
        command = [
            "cl",
            "/nologo",
            "/O2",
            "/LD",
            *sources,
            f"/Fe:{output}",
        ]
    else:
        command = [
            *compiler,
            "-O2",
            "-shared",
            "-fPIC",
            "-o",
            str(output),
            *sources,
        ]

    if verbose:
        print(" ".join(command))
    result = subprocess.run(command, cwd=SOURCE_DIR)
    if result.returncode != 0:
        raise SystemExit(f"compilation failed with exit code {result.returncode}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    missing = [name for name in SOURCES if not (SOURCE_DIR / name).exists()]
    if missing:
        raise SystemExit(f"missing sources in {SOURCE_DIR}: {', '.join(missing)}")

    output = OUTPUT_DIR / library_name()
    compiler = find_compiler()
    print(f"building {output.name} for {platform.machine()} with {compiler[0]}")
    build(compiler, output, args.verbose)
    print(f"wrote {output} ({output.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
