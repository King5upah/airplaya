"""Freeze the receiver into a folder that runs without Python installed.

Shipping a mirroring app means shipping to people who do not have Python, PyAV
or libusb, so the receiver is frozen with PyInstaller and the desktop app runs
the resulting executable instead of `python -m airplaya`.

    python scripts/build_exe.py [--clean]

The output is `dist/airplaya/`, containing `airplaya.exe` and its libraries.
One folder rather than one file: a single-file build unpacks itself to a
temporary directory on every launch, which costs a second of startup and
confuses the antivirus heuristics that watch for exactly that behaviour.

What has to be carried explicitly:

* `playfair.dll`, the FairPlay key helper, which is loaded by path at runtime
  and so is invisible to the dependency scanner.
* `libusb-1.0.dll` out of `libusb_package`, for the cable path.
* PyAV's and sounddevice's own binaries, which their hooks handle, plus the
  codec plugins PyInstaller cannot see being imported.

ffmpeg is *not* bundled. It is only needed for recording clips, and a GPL
ffmpeg build carries its own source-offer obligation; the app says what is
missing if a clip is started without it.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "src" / "airplaya"
DIST = ROOT / "dist"
BUILD = ROOT / "build"

# Imported through a string or a plugin mechanism, so the scanner misses them.
HIDDEN_IMPORTS = [
    "airplaya.wired",
    "airplaya.wired.receiver",
    "airplaya.wired.usb",
    "usb.backend.libusb1",
    "sounddevice",
    "_sounddevice_data",
    "zeroconf",
    "zeroconf._utils.ipaddress",
    "zeroconf._handlers.answers",
]


def libusb_dll() -> Path | None:
    """The libusb binary that `libusb-package` ships, if it is installed."""
    try:
        import libusb_package
    except ImportError:
        return None
    base = Path(libusb_package.__file__).parent
    for candidate in base.rglob("libusb-1.0.dll"):
        return candidate
    return None


def build(clean: bool) -> int:
    playfair = PACKAGE / "crypto" / "playfair.dll"
    if not playfair.exists():
        print(
            "playfair.dll is missing. Build it first:\n"
            "  python scripts/build_playfair.py",
            file=sys.stderr,
        )
        return 1

    if clean:
        for directory in (DIST, BUILD):
            shutil.rmtree(directory, ignore_errors=True)

    # PyInstaller wants source:destination, and the destination is relative to
    # the bundle root.
    binaries = [f"{playfair}{__import__('os').pathsep}airplaya/crypto"]
    usb_dll = libusb_dll()
    if usb_dll is not None:
        binaries.append(f"{usb_dll}{__import__('os').pathsep}libusb_package")
    else:
        print(
            "libusb-package is not installed; the built app will not mirror "
            "over the cable",
            file=sys.stderr,
        )

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--name",
        "airplaya",
        "--console",
        # The receiver's log is its status channel and the app reads it from
        # the pipe, so a console build is what we want — the app starts it
        # without a window of its own.
        "--paths",
        str(ROOT / "src"),
    ]
    for module in HIDDEN_IMPORTS:
        command += ["--hidden-import", module]
    for binary in binaries:
        command += ["--add-binary", binary]
    command.append(str(PACKAGE / "__main__.py"))

    print(" ".join(command))
    result = subprocess.run(command, cwd=ROOT)
    if result.returncode != 0:
        return result.returncode

    target = DIST / "airplaya" / "airplaya.exe"
    if not target.exists():
        print(f"the build finished but {target} is missing", file=sys.stderr)
        return 1

    # A frozen build that cannot start is worse than no build, and the failure
    # is usually a missing hidden import, so prove it runs.
    check = subprocess.run([str(target), "--version"], capture_output=True)
    if check.returncode != 0:
        print(check.stderr.decode("utf-8", "replace"), file=sys.stderr)
        print("the frozen receiver would not start", file=sys.stderr)
        return 1

    size = sum(path.stat().st_size for path in (DIST / "airplaya").rglob("*") if path.is_file())
    print(f"\n{target}")
    print(f"{check.stdout.decode().strip()}  ({size / 1e6:.0f} MB in dist/airplaya)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--clean", action="store_true", help="delete build/ and dist/ first"
    )
    return build(parser.parse_args().clean)


if __name__ == "__main__":
    sys.exit(main())
