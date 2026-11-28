"""Build a Windows x64 installer and portable builds."""

import argparse
from collections import deque
import importlib.metadata as metadata
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
APP_ICON = BUILD / "lysnav.ico"
DIST = ROOT / "dist"
RELEASE = ROOT / "release"
APP = DIST / "Lysnav"
PORTABLE_EXE = RELEASE / "portable-exe"
BUNDLE_DOCUMENTS = ("LICENSE", "THIRD_PARTY_NOTICES.md", "README.md")


def generate_app_icon():
    """Rasterize the canonical SVG into a multi-resolution Windows ICO."""
    from struct import pack
    from PySide6.QtCore import QSize
    from PySide6.QtGui import QGuiApplication, QIcon, QImage

    app = QGuiApplication.instance() or QGuiApplication([])
    icon = QIcon(str(ROOT / "assets" / "lysnav-logo.svg"))
    sizes = (16, 20, 24, 32, 40, 48, 64, 128, 256)
    entries = []
    images = []
    offset = 6 + 16 * len(sizes)
    for size in sizes:
        image = icon.pixmap(QSize(size, size)).toImage().convertToFormat(QImage.Format.Format_ARGB32)
        if image.isNull() or image.width() != size or image.height() != size:
            raise RuntimeError(f"Unable to render Lysnav logo at {size}px")
        raw = bytes(image.constBits())
        stride = image.bytesPerLine()
        # ICO DIBs are bottom-up BGRA with a padded one-bit transparency mask.
        pixels = b"".join(raw[y * stride:y * stride + size * 4]
                          for y in reversed(range(size)))
        mask_stride = ((size + 31) // 32) * 4
        mask = bytearray(mask_stride * size)
        for y in range(size):
            for x in range(size):
                if pixels[(y * size + x) * 4 + 3] == 0:
                    mask[y * mask_stride + x // 8] |= 0x80 >> (x % 8)
        dib = pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0,
                   len(pixels), 0, 0, 0, 0) + pixels + mask
        entries.append(pack("<BBBBHHII", size % 256, size % 256, 0, 0,
                            1, 32, len(dib), offset))
        images.append(dib)
        offset += len(dib)
    APP_ICON.parent.mkdir(exist_ok=True)
    APP_ICON.write_bytes(pack("<HHH", 0, 1, len(sizes)) + b"".join(entries + images))
    # Verify that Qt can read the generated native resource with the right colours.
    rendered = QIcon(str(APP_ICON)).pixmap(64, 64).toImage()
    if (rendered.isNull() or rendered.pixelColor(32, 20).name() != "#fe8019"
            or rendered.pixelColor(32, 42).name() != "#ebdbb2"):
        raise RuntimeError("Generated Lysnav ICO does not match the canonical logo")


def find_iscc():
    found = shutil.which("ISCC.exe")
    if found:
        return Path(found)
    for base in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        if os.environ.get(base):
            parent = Path(os.environ[base])
            for candidate in (parent / "Inno Setup 6", parent / "Programs" / "Inno Setup 6"):
                if (compiler := candidate / "ISCC.exe").is_file():
                    return compiler
    return None


def copy_licenses(app_dir=APP, requirements_file="requirements.txt"):
    output = app_dir / "licenses"
    shutil.copytree(ROOT / "packaging" / "qt-licenses", output / "Qt", dirs_exist_ok=True)
    shutil.copy2(ROOT / "LICENSE", output / "Lysnav-LICENSE.txt")
    shutil.copy2(ROOT / "THIRD_PARTY_NOTICES.md", output / "THIRD_PARTY_NOTICES.md")
    pending = deque()
    pending.extend(Requirement(line) for line in (ROOT / "requirements.txt").read_text().splitlines())
    if requirements_file != "requirements.txt":
        pending.extend(Requirement(line) for line in (ROOT / requirements_file).read_text().splitlines()
                       if line and not line.startswith(("#", "-r ")))
    installed = {}
    while pending:
        requirement = pending.popleft()
        name = canonicalize_name(requirement.name)
        if name in installed or (requirement.marker and not requirement.marker.evaluate()):
            continue
        package = metadata.distribution(name)
        installed[name] = package.version
        for file in package.files or []:
            info_dir = next((part for part in file.parts if part.endswith(".dist-info")), None)
            if not info_dir:
                continue
            relative = Path(*file.parts[file.parts.index(info_dir) + 1:])
            if relative.parts[0] == "licenses" or relative.name.upper().startswith(
                ("LICENSE", "COPYING", "NOTICE")
            ):
                target = output / name / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(file.locate(), target)
        pending.extend(Requirement(entry) for entry in package.requires or [])
    for filename in ("LICENSE.txt", "LICENSE"):
        source = Path(sys.base_prefix) / filename
        if source.is_file():
            shutil.copy2(source, output / "Python-LICENSE.txt")
            break
    (output / "versions.txt").write_text(
        f"Python=={platform.python_version()}\n"
        + "".join(f"{name}=={version}\n" for name, version in sorted(installed.items())),
        encoding="utf-8",
    )


def freeze_app(name, mode, output, *, entry_point="app.py"):
    extra_data = ["--add-data", f"{APP / 'licenses'}:licenses"] if mode == "--onefile" else []
    subprocess.run(
        [
            sys.executable, "-m", "PyInstaller",
            "--noconfirm", "--clean", "--windowed", mode, "--noupx",
            "--name", name, "--icon", str(APP_ICON),
            "--distpath", str(output), "--workpath", str(BUILD / "pyinstaller"),
            "--specpath", str(BUILD),
            "--paths", str(ROOT),
            "--add-data", f"{ROOT / 'assets'}:assets",
            *extra_data,
            "--copy-metadata", "PySide6-Essentials", "--copy-metadata", "dirigera",
            str(ROOT / entry_point),
        ],
        check=True,
        cwd=ROOT,
    )


def build_app():
    freeze_app("Lysnav", "--onedir", DIST)
    copy_licenses()
    for filename in BUNDLE_DOCUMENTS:
        shutil.copy2(ROOT / filename, APP / filename)


def check_runtime(executable):
    subprocess.run(
        [str(executable), "--check-runtime"],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        check=True,
        timeout=60,
    )


def verify_standalone_bundle(executable):
    from PyInstaller.archive.readers import CArchiveReader

    archive = CArchiveReader(str(executable))
    names = set(archive.toc)
    names.update(archive.open_embedded_archive("PYZ.pyz").toc)
    if (executable.parent / "_internal").is_dir():
        names.update(str(path.relative_to(executable.parent / "_internal"))
                     for path in (executable.parent / "_internal").rglob("*"))
    unexpected = sorted(name for name in names if name.startswith(
        ("store_launcher", "store_license", "winrt.", "winrt\\", "winrt/", "winrt_runtime")
    ) or name == "winrt")
    if unexpected:
        raise RuntimeError(f"Store dependencies in standalone build: {unexpected}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iscc", type=Path, help="Path to the Inno Setup 6 compiler")
    parser.add_argument("--package-only", action="store_true", help="Use the existing app bundle")
    parser.add_argument("--portable-exe", action="store_true", help="Also build a standalone portable executable")
    args = parser.parse_args()
    if sys.platform != "win32" or platform.machine().upper() not in {"AMD64", "X86_64"} or sys.maxsize <= 2**32:
        parser.error("Build on Windows with x64 Python")
    iscc = args.iscc or find_iscc()
    if iscc is None or not iscc.is_file():
        parser.error("Install Inno Setup 6 or specify the compiler with --iscc")

    sys.path.insert(0, str(ROOT))
    from version import VERSION

    generate_app_icon()
    portable_name = f"Lysnav-Windows-{VERSION}-x64-Portable"
    portable_executable = PORTABLE_EXE / f"{portable_name}.exe"
    if not args.package_only:
        BUILD.mkdir(exist_ok=True)
        build_app()
        if args.portable_exe:
            freeze_app(portable_name, "--onefile", PORTABLE_EXE)
            shutil.copytree(APP / "licenses", PORTABLE_EXE / "licenses", dirs_exist_ok=True)
            for filename in BUNDLE_DOCUMENTS:
                shutil.copy2(APP / filename, PORTABLE_EXE / filename)
    elif not (APP / "Lysnav.exe").is_file():
        parser.error("Run a full build before using --package-only")
    elif args.portable_exe and not portable_executable.is_file():
        parser.error("Run a full build with --portable-exe first")

    check_runtime(APP / "Lysnav.exe")
    verify_standalone_bundle(APP / "Lysnav.exe")
    if args.portable_exe:
        check_runtime(portable_executable)
        verify_standalone_bundle(portable_executable)
    RELEASE.mkdir(exist_ok=True)
    subprocess.run(
        [str(iscc.resolve()), f"/DAppVersion={VERSION}", str(ROOT / "packaging" / "lysnav.iss")],
        check=True,
        cwd=ROOT,
    )
    shutil.make_archive(
        str(RELEASE / portable_name),
        "zip",
        root_dir=DIST,
        base_dir=APP.name,
    )
    for artifact in sorted(RELEASE.glob(f"Lysnav-Windows-{VERSION}-x64-*")):
        print(artifact)
    if args.portable_exe:
        print(portable_executable)


if __name__ == "__main__":
    main()
