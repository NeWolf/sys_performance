"""Build on the target OS: python packaging/build.py (requires PyInstaller)."""
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

ROOT = Path(__file__).resolve().parent.parent
VERSION = "0.1.1"


def run(*args):
    subprocess.run([str(arg) for arg in args], cwd=ROOT, check=True)


def write(path, text, executable=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if executable:
        path.chmod(0o755)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-frontend", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "release",
                        help="Parent directory for build outputs (default: release)")
    args = parser.parse_args()
    system = platform.system()
    if system not in ("Darwin", "Windows", "Linux"):
        parser.error("Unsupported operating system")
    if not args.skip_frontend:
        npm = shutil.which("npm")
        if not npm:
            parser.error("Install Node.js and npm first")
        run(npm, "--prefix", "frontend", "ci")
        run(npm, "--prefix", "frontend", "run", "build")
        run(npm, "--prefix", "frontend", "run", "lint")
    for resource in ("frontend/dist/index.html", "frontend/src/sysmonitor_test/sysmonitor"):
        if not (ROOT / resource).is_file():
            parser.error("Missing resource: " + resource)
    arch = platform.machine().lower()
    label = {"Darwin": "macos", "Windows": "windows", "Linux": "linux"}[system]
    stem = f"SysMonitor-{VERSION}-{label}-{arch}"
    output = args.output_dir.resolve() / (stem + "-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    output.mkdir(parents=True, exist_ok=False)
    run(sys.executable, "-m", "PyInstaller", "--noconfirm", "--onedir",
        "--name", "SysMonitor", "--distpath", output / "portable",
        "--workpath", output / "work", "--specpath", output,
        "--add-data", str(ROOT / "frontend/dist") + ":frontend/dist",
        "--add-data", str(ROOT / "frontend/src/sysmonitor_test/sysmonitor") + ":frontend/src/sysmonitor_test",
        ROOT / "main.py")
    payload = output / "portable" / "SysMonitor"
    artifacts = []
    if system == "Darwin":
        stage = output / "image"
        app = stage / "SysMonitor.app"
        contents = app / "Contents"
        resources = contents / "Resources"
        shutil.copytree(payload, resources / "server")
        write(contents / "MacOS" / "SysMonitor",
              (ROOT / "packaging/macos-launcher.sh").read_text(), True)
        write(resources / "Start.command", '''#!/bin/sh
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export PATH="$HOME/Library/Android/sdk/platform-tools:/opt/homebrew/bin:/usr/local/bin:$PATH"
printf '%s\\n' 'SysMonitor: Control-C stops the local server, not Android collection.'
exec "$HERE/server/SysMonitor" --open-browser
''', True)
        with (contents / "Info.plist").open("wb") as handle:
            plistlib.dump({"CFBundleExecutable": "SysMonitor", "CFBundleName": "SysMonitor",
                          "CFBundleIdentifier": "local.sysmonitor.desktop", "CFBundlePackageType": "APPL",
                          "CFBundleShortVersionString": VERSION, "CFBundleVersion": VERSION,
                          "LSMinimumSystemVersion": platform.mac_ver()[0]}, handle)
        (stage / "Applications").symlink_to("/Applications", target_is_directory=True)
        dmg = output / (stem + ".dmg")
        run("hdiutil", "create", "-volname", "SysMonitor", "-srcfolder", stage,
            "-format", "UDZO", dmg)
        artifacts.append(dmg)
    elif system == "Windows":
        write(payload / "Start.cmd", '@echo off\r\n"%~dp0SysMonitor.exe" --open-browser\r\npause\r\n')
        installer = output / "installer.iss"
        write(installer, f'''[Setup]
AppName=SysMonitor
AppVersion={VERSION}
DefaultDirName={{localappdata}}\\Programs\\SysMonitor
PrivilegesRequired=lowest
OutputDir={output}
OutputBaseFilename={stem}-setup
Compression=lzma2
SolidCompression=yes
UninstallDisplayIcon={{app}}\\SysMonitor.exe
[Files]
Source: "{payload}\\*"; DestDir: "{{app}}"; Flags: ignoreversion recursesubdirs createallsubdirs
[Icons]
Name: "{{userprograms}}\\SysMonitor"; Filename: "{{app}}\\SysMonitor.exe"; Parameters: "--open-browser"
Name: "{{userdesktop}}\\SysMonitor"; Filename: "{{app}}\\SysMonitor.exe"; Parameters: "--open-browser"
''')
        compiler = shutil.which("ISCC")
        if compiler:
            run(compiler, installer)
            artifacts.append(output / (stem + "-setup.exe"))
        else:
            print("Inno Setup not found: portable ZIP only; compile installer.iss with ISCC for installer.")
        artifacts.append(Path(shutil.make_archive(str(output / stem), "zip", payload.parent, payload.name)))
    else:
        write(payload / "start.sh", '#!/bin/sh\nHERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\nexec "$HERE/SysMonitor" --open-browser\n', True)
        artifacts.append(Path(shutil.make_archive(str(output / stem), "gztar", payload.parent, payload.name)))
        if shutil.which("dpkg-deb"):
            package = output / "deb-root"
            shutil.copytree(payload, package / "opt/sysmonitor")
            deb_arch = subprocess.check_output(["dpkg", "--print-architecture"], text=True).strip()
            write(package / "DEBIAN/control", f"Package: sysmonitor\nVersion: {VERSION}\nArchitecture: {deb_arch}\nMaintainer: SysMonitor\nDepends: libc6\nDescription: Local Android performance log analysis\n")
            write(package / "usr/share/applications/sysmonitor.desktop", "[Desktop Entry]\nType=Application\nName=SysMonitor\nExec=/opt/sysmonitor/SysMonitor --open-browser\nTerminal=true\nCategories=Development;\n")
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