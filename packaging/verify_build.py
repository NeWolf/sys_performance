"""Verify release checksums and smoke-test the extracted portable application."""
import argparse
import hashlib
from pathlib import Path
import platform
import re
import shutil
import socket
import subprocess
import tarfile
import tempfile
import time
import zipfile

import httpx


def smoke(command, folder):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with (folder / "server.log").open("w+", encoding="utf-8") as log:
        process = subprocess.Popen(
            [*command, "--port", str(port), "--database", str(folder / "smoke.sqlite3")],
            cwd=folder, stdout=log, stderr=subprocess.STDOUT)
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5, trust_env=False) as client:
                deadline = time.monotonic() + 60
                while True:
                    if process.poll() is not None:
                        raise RuntimeError("Packaged server exited before becoming ready")
                    try:
                        response = client.get("/")
                        break
                    except httpx.ConnectError:
                        if time.monotonic() >= deadline:
                            raise RuntimeError("Packaged server startup timed out")
                        time.sleep(0.2)
                response.raise_for_status()
                assets = re.findall(r'(?:src|href)="(/assets/[^\"]+)"', response.text)
                if not assets:
                    raise RuntimeError("Frontend assets missing from packaged homepage")
                for asset in assets:
                    client.get(asset).raise_for_status()
                if client.get("/api/sessions").status_code != 403:
                    raise RuntimeError("Session-token protection is not active")
                token = client.get("/api/token")
                token.raise_for_status()
                client.headers["X-Session-Token"] = token.json()["token"]
                sessions = client.get("/api/sessions")
                sessions.raise_for_status()
                if sessions.json() != []:
                    raise RuntimeError("Smoke test database was not empty")
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
            log.seek(0)
            print(log.read())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--publish-dir", required=True, type=Path)
    args = parser.parse_args()
    system = platform.system()
    if system not in ("Windows", "Linux"):
        parser.error("Release verification supports Windows and Linux")
    manifests = list(args.output_dir.glob("*/SHA256SUMS"))
    if len(manifests) != 1:
        raise RuntimeError("Expected exactly one build in the output directory")
    manifest = manifests[0]
    artifacts = []
    for entry in manifest.read_text(encoding="utf-8").splitlines():
        digest, name = entry.split("  ", 1)
        if not re.fullmatch(r"[0-9a-f]{64}", digest) or Path(name).name != name:
            raise RuntimeError("Invalid checksum manifest entry")
        artifact = manifest.parent / name
        with artifact.open("rb") as handle:
            actual = hashlib.file_digest(handle, "sha256").hexdigest()
        if actual != digest:
            raise RuntimeError(f"Checksum mismatch: {name}")
        artifacts.append(artifact)
    suffixes = (".zip", "-setup.exe") if system == "Windows" else (".tar.gz", ".deb")
    if len(artifacts) != 2 or any(sum(p.name.endswith(s) for p in artifacts) != 1 for s in suffixes):
        raise RuntimeError("Both portable archive and native installer are required")
    archive = next(p for p in artifacts if p.name.endswith(suffixes[0]))
    with tempfile.TemporaryDirectory(prefix="sysmonitor smoke ") as temporary:
        folder = Path(temporary)
        if system == "Windows":
            with zipfile.ZipFile(archive) as bundle:
                bundle.extractall(folder)
        else:
            with tarfile.open(archive) as bundle:
                bundle.extractall(folder, filter="data")
        payload = folder / "SysMonitor"
        resource = payload / "_internal/frontend/src/sysmonitor_test/sysmonitor"
        if not resource.is_file() or not resource.stat().st_size:
            raise RuntimeError("Android collector missing from portable archive")
        executable = payload / ("SysMonitor.exe" if system == "Windows" else "SysMonitor")
        smoke([str(executable)], folder)
    args.publish_dir.mkdir(parents=True, exist_ok=False)
    for artifact in [*artifacts, manifest]:
        shutil.copy2(artifact, args.publish_dir / artifact.name)
    print(f"Verified artifacts staged in {args.publish_dir}")


if __name__ == "__main__":
    main()