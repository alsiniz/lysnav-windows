"""Build the Windows x64 Microsoft Store submission package."""

import argparse
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
from zipfile import ZipFile

from build_windows import ROOT, BUILD, RELEASE, check_runtime, copy_licenses, freeze_app, generate_app_icon


def find_makeappx():
    sdk = Path(r"C:\Program Files (x86)\Windows Kits\10\bin")
    candidates = [path for path in sdk.glob("10.*/x64/makeappx.exe") if path.is_file()]
    if candidates:
        return max(candidates, key=lambda path: tuple(map(int, path.parents[1].name.split("."))))
    found = shutil.which("MakeAppx.exe")
    return Path(found) if found else None


def package_manifest(version):
    parts = version.split(".")
    if (not re.fullmatch(r"[1-9][0-9]*\.[0-9]+\.[0-9]+", version)
            or any(int(part) > 65535 for part in parts)):
        raise ValueError("MSIX requires a stable major.minor.patch version with parts at most 65535")
    template = (ROOT / "packaging/msix/AppxManifest.xml").read_text(encoding="utf-8")
    # The last component is reserved for the Store.
    return template.replace('Version="1.1.0.0"', f'Version="{version}.0"')


def render_logos(directory):
    from PySide6.QtCore import Qt, QRectF
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    directory.mkdir()
    renderer = QSvgRenderer(str(ROOT / "assets/lysnav-logo.svg"))
    if not renderer.isValid():
        raise RuntimeError("Lysnav logo could not be read")
    for name, size in (("StoreLogo", 50), ("Square44x44Logo", 44), ("Square150x150Logo", 150)):
        image = QImage(size, size, QImage.Format.Format_ARGB32)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        renderer.render(painter, QRectF(0, 0, size, size))
        painter.end()
        if not image.save(str(directory / f"{name}.png")):
            raise RuntimeError(f"Could not write {name}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--makeappx", type=Path, help="Path to the Windows SDK's MakeAppx.exe")
    args = parser.parse_args()
    if sys.platform != "win32" or platform.machine().upper() not in {"AMD64", "X86_64"} or sys.maxsize <= 2**32:
        parser.error("Build on Windows with x64 Python")
    makeappx = args.makeappx or find_makeappx()
    if makeappx is None or not makeappx.is_file():
        parser.error("Install the Windows SDK or specify the tool with --makeappx")
    sys.path.insert(0, str(ROOT))
    from version import VERSION

    generate_app_icon()
    stage = BUILD / "msix"
    freeze_app("Lysnav", "--onedir", stage, entry_point="store_launcher.py")
    bundle = stage / "Lysnav"
    check_runtime(bundle / "Lysnav.exe")
    copy_licenses(bundle, "requirements-msix.txt")
    (bundle / "AppxManifest.xml").write_text(package_manifest(VERSION), encoding="utf-8")
    render_logos(bundle / "StoreAssets")
    release = RELEASE / "msix"
    release.mkdir(parents=True, exist_ok=True)
    package = release / f"Lysnav-Windows-{VERSION}-x64.msix"
    subprocess.run([str(makeappx.resolve()), "pack", "/d", str(bundle), "/p", str(package), "/o"],
                   check=True, cwd=ROOT)
    with ZipFile(package) as archive:
        files = set(archive.namelist())
        required = {"AppxManifest.xml", "AppxBlockMap.xml", "Lysnav.exe", "StoreAssets/StoreLogo.png",
                    "licenses/Lysnav-LICENSE.txt", "licenses/versions.txt"}
        if not required <= files:
            raise RuntimeError(f"MSIX is missing files: {sorted(required - files)}")
    print(package)


if __name__ == "__main__":
    main()
