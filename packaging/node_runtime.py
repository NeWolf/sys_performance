"""Validate explicitly supplied official Node distributions; never discover via PATH."""
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import tempfile


PLATFORMS = {"Darwin": "darwin", "Windows": "win32", "Linux": "linux"}
ARCHES = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64", "amd64": "x64", "x64": "x64"}
NODE_PROBE = "console.log(JSON.stringify({version:process.version,platform:process.platform,arch:process.arch}))"
CORE_SMOKE = """import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {readFileSync} from 'node:fs';
import {basename} from 'node:path';
assert.equal(createHash('sha256').update('abc').digest('hex'),
  'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad');
assert.equal(typeof fetch, 'function');
assert.equal(typeof FormData, 'function');
assert.equal(typeof Blob, 'function');
assert.equal(typeof readFileSync, 'function');
assert.equal(basename('/a/b'), 'b');
console.log('joyspace-node-core-ok');
"""


def isolated_env(folder):
    """No user Node options, library injection, credentials, proxies or PATH."""
    env = {key: value for key, value in os.environ.items()
           if key.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP"}}
    env.update(PATH="", HOME=str(folder), USERPROFILE=str(folder))
    return env


def check_license(license_path):
    if not license_path.is_file():
        raise RuntimeError(f"Node LICENSE missing: {license_path}")
    text = license_path.read_text(encoding="utf-8")
    if "Node.js" not in text or "Permission is hereby granted, free of charge" not in text:
        raise RuntimeError(f"Invalid Node LICENSE: {license_path}")


def check_identity(text, system, machine=None):
    try:
        info = json.loads(text)
        version = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", info["version"])
        expected_arch = ARCHES[(machine or platform.machine()).lower()]
        if not version or tuple(map(int, version.groups())) < (22, 7, 0):
            raise ValueError("Node >=22.7.0 required for ESM syntax detection")
        if info["platform"] != PLATFORMS[system] or info["arch"] != expected_arch:
            raise ValueError(f"Node platform/arch mismatch: {info}")
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid Node runtime identity: {exc}") from exc
    return info


def check_dependencies(text, system):
    """Fail closed for non-system or unresolved shared libraries."""
    dependencies = []
    if system == "Darwin":
        for line in text.splitlines():
            if " (compatibility version " in line:
                dependency = line.strip().split(" (", 1)[0]
                if not dependency.startswith(("/usr/lib/", "/System/Library/")) or ".." in Path(dependency).parts:
                    raise RuntimeError(f"Non-system Node dependency: {dependency}")
                dependencies.append(dependency)
    elif system == "Linux":
        allowed = re.compile(r"(?:lib(?:c|m|dl|rt|pthread|gcc_s|stdc\+\+|atomic)\.so(?:\.\d+)*|ld-linux[^/]*\.so(?:\.\d+)*)")
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            if re.fullmatch(r"linux-vdso\.so\.\d+\s+\(0x[0-9a-fA-F]+\)", line):
                continue
            if "=>" in line:
                name, resolved = line.split("=>", 1)
                name = name.strip()
                path = resolved.strip().split()[0]
            else:
                path = line.split()[0]
                name = Path(path).name
            if (not allowed.fullmatch(name)
                    or not path.startswith(("/lib/", "/lib64/", "/usr/lib/", "/usr/lib64/"))
                    or ".." in Path(path).parts or Path(path).name != name):
                raise RuntimeError(f"Non-system or unresolved Node dependency: {line}")
            dependencies.append(path)
    if system in ("Darwin", "Linux") and not dependencies:
        raise RuntimeError("No auditable Node system dependencies found")


def verify_node(node, license_path, system, folder=None, core_smoke=False):
    node = Path(node).resolve()
    check_license(Path(license_path))
    if not node.is_file() or not node.stat().st_size:
        raise RuntimeError(f"Node executable missing: {node}")
    if system != "Windows" and not os.access(node, os.X_OK):
        raise RuntimeError(f"Node is not executable: {node}")
    folder = Path(folder or node.parent).resolve()
    env = isolated_env(folder)
    if system in ("Darwin", "Linux"):
        command = ["/usr/bin/otool", "-L", str(node)] if system == "Darwin" else ["/usr/bin/ldd", str(node)]
        audit = subprocess.run(command, check=True, capture_output=True, text=True,
                               timeout=20, env=env, cwd=folder)
        check_dependencies(audit.stdout, system)
    result = subprocess.run([str(node), "-e", NODE_PROBE], check=True,
                            capture_output=True, text=True, timeout=20, env=env, cwd=folder)
    info = check_identity(result.stdout, system)
    if core_smoke:
        # Match the skill's .js entrypoints, not Node's separate eval parser.
        # An empty package boundary prevents inherited module-type settings.
        with tempfile.TemporaryDirectory(prefix="node-core-smoke-", dir=folder) as temporary:
            smoke_root = Path(temporary)
            (smoke_root / "package.json").write_text("{}", encoding="utf-8")
            script = smoke_root / "core-smoke.js"
            script.write_text(CORE_SMOKE, encoding="utf-8")
            label = f"Node core smoke ({info['version']} {info['platform']}/{info['arch']}, {node})"

            def output_text(value):
                if isinstance(value, bytes):
                    return value.decode("utf-8", errors="replace")
                return value or "(empty)"

            try:
                result = subprocess.run([str(node), str(script)], check=True,
                                        capture_output=True, text=True, timeout=20, env=env, cwd=folder)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                status = f"exit {exc.returncode}" if isinstance(exc, subprocess.CalledProcessError) else "timeout after 20s"
                raise RuntimeError(
                    f"{label} failed: {status}\nstdout:\n{output_text(exc.stdout)}"
                    f"\nstderr:\n{output_text(exc.stderr)}") from exc
            if result.stdout.strip() != "joyspace-node-core-ok":
                raise RuntimeError(
                    f"{label} failed: unexpected output\nstdout:\n{output_text(result.stdout)}"
                    f"\nstderr:\n{output_text(result.stderr)}")
    return info


def official_node_files(directory, system):
    if not directory:
        raise RuntimeError("Provide --node-dist-dir or JDPERF_NODE_DIST_DIR (official Node distribution root)")
    root = Path(directory).expanduser().resolve()
    node = root / ("node.exe" if system == "Windows" else "bin/node")
    license_path = root / "LICENSE"
    # Disallow redirecting an apparently official directory to Homebrew/system Node.
    if node.is_symlink() or license_path.is_symlink():
        raise RuntimeError("Official Node executable and LICENSE must not be symlinks")
    if not node.resolve().is_relative_to(root):
        raise RuntimeError("Node executable escapes distribution directory")
    verify_node(node, license_path, system)
    return node, license_path


def verify_skill(root):
    skill = Path(root) / "joyspace_assets/joyspace-kit"
    required = (
        "SKILL.md",
        "skills/shared-auth.mjs",
        "skills/hioffice-auth/scripts/hioffice-auth.mjs",
        "skills/joyspace-read-doc/scripts/read_joyspace_doc.js",
        "skills/markdown-to-joyspace/scripts/import_markdown_doc.js",
    )
    if any(not (skill / name).is_file() for name in required):
        raise RuntimeError(f"JoySpace skill resources missing: {skill}")
    for path in skill.rglob("*"):
        if path.is_symlink():
            raise RuntimeError(f"JoySpace skill resources must not contain symlinks: {path}")
    return skill


def verify_joyspace_payload(root, folder, system):
    verify_skill(root)
    runtime = Path(root) / "joyspace_assets/node"
    node = runtime / ("node.exe" if system == "Windows" else "node")
    return verify_node(node, runtime / "LICENSE", system, folder, core_smoke=True)