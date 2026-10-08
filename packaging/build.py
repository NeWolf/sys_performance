"""Build on the target OS: python packaging/build.py (requires PyInstaller and Pillow)."""
import argparse
from datetime import datetime
import hashlib
import os
from pathlib import Path
import platform
import plistlib
import shutil
import subprocess
import sys

from macos_compat import MACOS_MIN_VERSION, verify_macos_binaries
from node_runtime import official_node_files, verify_skill

ROOT = Path(__file__).resolve().parent.parent
VERSION = "1.3.1"


def run(*args):
    subprocess.run([str(arg) for arg in args], cwd=ROOT, check=True)


def write(path, text, executable=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if executable:
        path.chmod(0o755)


def build_icon(output, system):
    """Convert the shared PNG to the native icon format without distorting it."""
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:
        raise RuntimeError("Icon conversion requires Pillow: python -m pip install 'Pillow>=10.4,<12'") from exc

    size = 1024 if system == "Darwin" else 256
    suffix = {"Darwin": ".icns", "Windows": ".ico", "Linux": ".png"}[system]
    icon = output / ("JDPerf" + suffix)
    with Image.open(ROOT / "JD_logo.png") as source:
        fitted = ImageOps.contain(source.convert("RGBA"), (size, size), Image.Resampling.LANCZOS)
        canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        canvas.paste(fitted, ((size - fitted.width) // 2, (size - fitted.height) // 2))
        if system == "Windows":
            canvas.save(icon, sizes=[(n, n) for n in (16, 24, 32, 48, 64, 128, 256)])
        else:
            canvas.save(icon)
    return icon


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-frontend", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "release",
                        help="Parent directory for build outputs (default: release)")
    parser.add_argument("--node-dist-dir", type=Path, default=os.environ.get("JDPERF_NODE_DIST_DIR"),
                        help="Official Node distribution root containing LICENSE and bin/node (or node.exe)")
    args = parser.parse_args()
    system = platform.system()
    if system not in ("Darwin", "Windows", "Linux"):
        parser.error("Unsupported operating system")
    if system == "Darwin":
        os.environ["MACOSX_DEPLOYMENT_TARGET"] = MACOS_MIN_VERSION
    if not args.skip_frontend:
        npm = shutil.which("npm")
        if not npm:
            parser.error("Install Node.js and npm first")
        run(npm, "--prefix", "frontend", "ci")
        run(npm, "--prefix", "frontend", "run", "build")
        run(npm, "--prefix", "frontend", "run", "lint")
    for resource in ("frontend/dist/index.html", "frontend/dist/JD_logo.png", "frontend/src/sysmonitor_test/sysmonitor",
                     "report_assets/report.css", "report_assets/report.js",
                     "report_assets/vendor/echarts.min.js", "report_assets/vendor/ECHARTS-LICENSE",
                     "report_assets/vendor/ECHARTS-NOTICE", "JD_logo.png", "device_top_capture.sh"):
        if not (ROOT / resource).is_file():
            parser.error("Missing resource: " + resource)
    skill = verify_skill(ROOT)
    node, node_license = official_node_files(args.node_dist_dir, system)
    arch = platform.machine().lower()
    label = {"Darwin": "macos", "Windows": "windows", "Linux": "linux"}[system]
    stem = f"JDPerf-{VERSION}-{label}-{arch}"
    output = args.output_dir.resolve() / (stem + "-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    output.mkdir(parents=True, exist_ok=False)
    icon = build_icon(output, system)
    icon_args = ["--icon", icon] if system == "Windows" else []
    run(sys.executable, "-m", "PyInstaller", "--noconfirm", "--onedir", *icon_args,
        "--name", "JDPerf", "--distpath", output / "portable",
        "--workpath", output / "work", "--specpath", output,
        "--add-data", str(ROOT / "frontend/dist") + ":frontend/dist",
        "--add-data", str(ROOT / "frontend/src/sysmonitor_test/sysmonitor") + ":frontend/src/sysmonitor_test",
        "--add-data", str(ROOT / "report_assets") + ":report_assets",
        "--add-data", str(ROOT / "device_top_capture.sh") + ":.",
        "--add-data", str(skill) + ":joyspace_assets/joyspace-kit",
        # Keep Node out of PyInstaller binary dependency discovery and signing.
        "--add-data", str(node) + ":joyspace_assets/node",
        "--add-data", str(node_license) + ":joyspace_assets/node",
        "--hidden-import", "perf_joyspace",
        ROOT / "main.py")
    payload = output / "portable" / "JDPerf"
    artifacts = []
    if system == "Darwin":
        verify_macos_binaries(payload)
        stage = output / "image"
        app = stage / "JDPerf.app"
        contents = app / "Contents"
        resources = contents / "Resources"
        shutil.copytree(payload, resources / "server")
        shutil.copy2(icon, resources / icon.name)
        write(contents / "MacOS" / "JDPerf",
              (ROOT / "packaging/macos-launcher.sh").read_text(), True)
        write(resources / "Start.command", '''#!/bin/sh
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export PATH="$HOME/Library/Android/sdk/platform-tools:/opt/homebrew/bin:/usr/local/bin:$PATH"
printf '%s\\n' 'JDPerf: Control-C stops the local server, not Android collection.'
exec "$HERE/server/JDPerf" --open-browser
''', True)
        with (contents / "Info.plist").open("wb") as handle:
            plistlib.dump({"CFBundleExecutable": "JDPerf", "CFBundleName": "JDPerf",
                          "CFBundleDisplayName": "JDPerf",
                          "CFBundleIdentifier": "local.sysmonitor.desktop", "CFBundlePackageType": "APPL",
                          "CFBundleIconFile": icon.name,
                          "CFBundleShortVersionString": VERSION, "CFBundleVersion": VERSION,
                          "LSMinimumSystemVersion": MACOS_MIN_VERSION}, handle)
        (stage / "Applications").symlink_to("/Applications", target_is_directory=True)
        dmg = output / (stem + ".dmg")
        run("hdiutil", "create", "-volname", "JDPerf", "-srcfolder", stage,
            "-format", "UDZO", dmg)
        artifacts.append(dmg)
    elif system == "Windows":
        write(payload / "Start.cmd", '@echo off\r\n"%~dp0JDPerf.exe" --open-browser\r\npause\r\n')
        installer = output / "installer.iss"
        write(installer, f'''[Setup]
AppId=SysMonitor
AppName=JDPerf
AppVersion={VERSION}
DefaultDirName={{localappdata}}\\Programs\\JDPerf
PrivilegesRequired=lowest
OutputDir={output}
OutputBaseFilename={stem}-setup
Compression=lzma2
SolidCompression=yes
SetupIconFile={icon}
UninstallDisplayIcon={{app}}\\JDPerf.exe
[Files]
Source: "{payload}\\*"; DestDir: "{{app}}"; Flags: ignoreversion recursesubdirs createallsubdirs
[Icons]
Name: "{{userprograms}}\\JDPerf"; Filename: "{{app}}\\JDPerf.exe"; Parameters: "--open-browser"
Name: "{{userdesktop}}\\JDPerf"; Filename: "{{app}}\\JDPerf.exe"; Parameters: "--open-browser"
''')
        compiler = shutil.which("ISCC")
        if compiler:
            run(compiler, installer)
            artifacts.append(output / (stem + "-setup.exe"))
        else:
            print("Inno Setup not found: portable ZIP only; compile installer.iss with ISCC for installer.")
        artifacts.append(Path(shutil.make_archive(str(output / stem), "zip", payload.parent, payload.name)))
    else:
        shutil.copy2(icon, payload / "jdperf.png")
        write(payload / "start.sh", '#!/bin/sh\nHERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\nexec "$HERE/JDPerf" --open-browser\n', True)
        artifacts.append(Path(shutil.make_archive(str(output / stem), "gztar", payload.parent, payload.name)))
        if shutil.which("dpkg-deb"):
            package = output / "deb-root"
            shutil.copytree(payload, package / "opt/jdperf")
            deb_arch = subprocess.check_output(["dpkg", "--print-architecture"], text=True).strip()
            write(package / "DEBIAN/control", f"Package: jdperf\nVersion: {VERSION}\nArchitecture: {deb_arch}\nMaintainer: JDPerf\nDepends: libc6\nDescription: Local Android performance log analysis\n")
            icon_dir = package / "usr/share/icons/hicolor/256x256/apps"
            icon_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(icon, icon_dir / "jdperf.png")
            write(package / "usr/share/applications/jdperf.desktop", "[Desktop Entry]\nType=Application\nName=JDPerf\nExec=/opt/jdperf/JDPerf --open-browser\nIcon=jdperf\nTerminal=true\nCategories=Development;\n")
            deb = output / (stem + ".deb")
            run("dpkg-deb", "--build", "--root-owner-group", package, deb)
            artifacts.append(deb)
    write(output / "SHA256SUMS", "".join(
        hashlib.sha256(path.read_bytes()).hexdigest() + "  " + path.name + "\n" for path in artifacts))
    print("Build complete:", output)
    for path in artifacts:
        print(path)


if __name__ == "__main__":
    main()