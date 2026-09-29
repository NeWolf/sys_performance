"""Cross-platform regression tests for the macOS release compatibility gate."""
import importlib.util
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "macos_compat", Path(__file__).parent / "packaging/macos_compat.py")
compat = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(compat)
NODE_SPEC = importlib.util.spec_from_file_location(
    "node_runtime", Path(__file__).parent / "packaging/node_runtime.py")
node_runtime = importlib.util.module_from_spec(NODE_SPEC)
NODE_SPEC.loader.exec_module(node_runtime)


def modern(version="14.0", target="1"):
    return (f"Load command 1\n cmd LC_BUILD_VERSION\n cmdsize 32\n"
            f" platform {target}\n minos {version}\n sdk 15.5\n")


class MacOSCompatibilityTests(unittest.TestCase):
    def test_supported_minimums_and_newer_sdk(self):
        for version in ("10.9", "11.0", "13.6", "14.0", "14.0.0"):
            with self.subTest(version=version):
                compat.check_load_commands(modern(version), "Python")
        compat.check_load_commands(modern(target="macos"), "Python")
        compat.check_load_commands(
            "Load command 2\n cmd LC_VERSION_MIN_MACOSX\n version 11.0\n sdk 15.5\n",
            "extension.so")

    def test_newer_dependencies_rejected(self):
        for version in ("14.0.1", "14.8", "15.0", "26.0"):
            with self.subTest(version=version):
                with self.assertRaisesRegex(RuntimeError, "requires macOS"):
                    compat.check_load_commands(modern(version), "dependency.dylib")
        with self.assertRaisesRegex(RuntimeError, "requires macOS"):
            compat.check_load_commands(
                "Load command 2\n cmd LC_VERSION_MIN_MACOSX\n version 15.0\n",
                "extension.so")

    def test_missing_invalid_or_non_macos_metadata_rejected(self):
        for text in ("", "sdk 15.0", modern(target="2"), modern("invalid"),
                     "Load command 1\n cmd LC_BUILD_VERSION\n platform 1\n"):
            with self.subTest(text=text):
                with self.assertRaises(RuntimeError):
                    compat.check_load_commands(text, "broken.so")

    def test_plist_baseline_is_fixed(self):
        self.assertEqual(compat.MACOS_MIN_VERSION, "14.0")
        compat.verify_macos_metadata({"LSMinimumSystemVersion": "14.0"})
        for value in (None, "14.8", "15.7.9", "13.0"):
            with self.subTest(value=value):
                with self.assertRaises(RuntimeError):
                    compat.verify_macos_metadata({"LSMinimumSystemVersion": value})

    def test_scans_executable_and_nested_dependencies_not_android(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            internal = root / "_internal"
            internal.mkdir()
            (root / "SysMonitor").write_bytes(b"\xcf\xfa\xed\xfe" + b"binary")
            (internal / "Python").write_bytes(b"\xca\xfe\xba\xbe" + b"binary")
            (internal / "extension.so").write_bytes(b"\xcf\xfa\xed\xfe" + b"binary")
            (internal / "joyspace_assets/node").mkdir(parents=True)
            (internal / "joyspace_assets/node/node").write_bytes(b"\xcf\xfa\xed\xfe" + b"node")
            (internal / "sysmonitor").write_bytes(b"\x7fELFandroid")
            (internal / "asset.js").write_text("plain asset", encoding="utf-8")
            with patch.object(compat.platform, "machine", return_value="arm64"), \
                    patch.object(compat.subprocess, "run") as run:
                run.return_value = subprocess.CompletedProcess([], 0, stdout=modern())
                self.assertEqual(compat.verify_macos_binaries(root), 4)
                self.assertEqual(run.call_count, 8)
                commands = [call.args[0] for call in run.call_args_list]
                self.assertEqual(sum(command[0] == "lipo" for command in commands), 4)
                self.assertTrue(any(str(internal / "joyspace_assets/node/node") in command for command in commands))
                self.assertTrue(all("arm64" in command for command in commands))
                self.assertFalse(any(str(internal / "sysmonitor") in c for c in commands))
                run.side_effect = subprocess.CalledProcessError(1, ["lipo"])
                with self.assertRaises(subprocess.CalledProcessError):
                    compat.verify_macos_binaries(root)
                run.side_effect = None
                run.return_value = subprocess.CompletedProcess([], 0, stdout=modern("15.0"))
                with self.assertRaisesRegex(RuntimeError, "requires macOS"):
                    compat.verify_macos_binaries(root)

    def test_empty_payload_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(RuntimeError, "No Mach-O"):
                compat.verify_macos_binaries(Path(temporary))

    def test_version_comparison_is_numeric(self):
        self.assertGreater(compat.version_tuple("14.10"), compat.version_tuple("14.8"))
        self.assertEqual(compat.version_tuple("14.0"), compat.version_tuple("14.0.0"))


class DeviceResourcePackagingTests(unittest.TestCase):
    def load_build(self):
        spec = importlib.util.spec_from_file_location(
            "sysmonitor_build", Path(__file__).parent / "packaging/build.py")
        build = importlib.util.module_from_spec(spec)
        with patch.dict("sys.modules", {"macos_compat": compat, "node_runtime": node_runtime}):
            spec.loader.exec_module(build)
        return build

    def test_device_script_bundled_next_to_controller_on_all_platforms(self):
        build = self.load_build()
        self.assertTrue((build.ROOT / "device_top_capture.sh").is_file())
        for system in ("Darwin", "Windows", "Linux"):
            with self.subTest(system=system), tempfile.TemporaryDirectory() as temporary:
                with patch.object(build.sys, "argv", ["build.py", "--skip-frontend", "--output-dir", temporary]), \
                        patch.object(build.platform, "system", return_value=system), \
                        patch.object(build, "build_icon", return_value=Path(temporary) / "icon"), \
                        patch.object(Path, "is_file", return_value=True), \
                        patch.object(build, "verify_skill", return_value=build.ROOT / "joyspace_assets/joyspace-kit"), \
                        patch.object(build, "official_node_files", return_value=(Path(temporary) / ("node.exe" if system == "Windows" else "node"), Path(temporary) / "LICENSE")), \
                        patch.dict(build.os.environ, {}, clear=False), \
                        patch.object(build, "run", side_effect=RuntimeError("stop before real build")) as run:
                    with self.assertRaisesRegex(RuntimeError, "stop before real build"):
                        build.main()
                args = run.call_args.args
                self.assertIn("PyInstaller", args)
                resources = [args[i + 1] for i, value in enumerate(args) if value == "--add-data"]
                self.assertIn(str(build.ROOT / "device_top_capture.sh") + ":.", resources)
                self.assertIn(str(build.ROOT / "joyspace_assets/joyspace-kit") + ":joyspace_assets/joyspace-kit", resources)
                self.assertIn(str(Path(temporary) / "LICENSE") + ":joyspace_assets/node", resources)
                self.assertIn(str(Path(temporary) / ("node.exe" if system == "Windows" else "node")) + ":joyspace_assets/node", resources)
                self.assertNotIn("--add-binary", args)
                self.assertEqual(args[args.index("--hidden-import") + 1], "perf_joyspace")

    def test_missing_device_script_fails_before_build(self):
        build = self.load_build()
        with patch.object(build.sys, "argv", ["build.py", "--skip-frontend"]), \
                patch.object(build.platform, "system", return_value="Linux"), \
                patch.object(Path, "is_file", lambda path: path.name != "device_top_capture.sh"), \
                patch.object(build, "run") as run, patch("sys.stderr", new_callable=io.StringIO) as error:
            with self.assertRaises(SystemExit) as raised:
                build.main()
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("Missing resource: device_top_capture.sh", error.getvalue())
        run.assert_not_called()


    def test_native_packages_use_jdperf_brand(self):
        import plistlib
        build = self.load_build()
        for system in ("Darwin", "Windows", "Linux"):
            with self.subTest(system=system), tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary)
                icon = output / {"Darwin": "JDPerf.icns", "Windows": "JDPerf.ico", "Linux": "JDPerf.png"}[system]
                icon.write_bytes(b"test icon")

                def fake_run(*args):
                    if "PyInstaller" in args:
                        self.assertEqual(args[args.index("--name") + 1], "JDPerf")
                        payload = args[args.index("--distpath") + 1] / "JDPerf"
                        payload.mkdir(parents=True)
                        (payload / ("JDPerf.exe" if system == "Windows" else "JDPerf")).write_bytes(b"server")
                    elif args[0] in ("hdiutil", "dpkg-deb"):
                        Path(args[-1]).write_bytes(b"package")
                    elif args[0] == "ISCC":
                        installer = Path(args[1])
                        stem = next(line.split("=", 1)[1] for line in installer.read_text().splitlines()
                                    if line.startswith("OutputBaseFilename="))
                        (installer.parent / (stem + ".exe")).write_bytes(b"installer")

                with patch.object(build.sys, "argv", ["build.py", "--skip-frontend", "--output-dir", temporary]), \
                        patch.object(build.platform, "system", return_value=system), \
                        patch.object(build, "build_icon", return_value=icon), \
                        patch.object(Path, "is_file", return_value=True), \
                        patch.object(build, "verify_skill", return_value=build.ROOT / "joyspace_assets/joyspace-kit"), \
                        patch.object(build, "official_node_files", return_value=(Path(temporary) / ("node.exe" if system == "Windows" else "node"), Path(temporary) / "LICENSE")), \
                        patch.dict(build.os.environ, {}, clear=False), \
                        patch.object(build, "verify_macos_binaries"), \
                        patch.object(build.shutil, "which", side_effect=lambda name: name), \
                        patch.object(build.subprocess, "check_output", return_value="amd64\n"), \
                        patch.object(build, "run", side_effect=fake_run), \
                        patch("sys.stdout", new_callable=io.StringIO):
                    build.main()
                folder = next(output.glob("JDPerf-*/"))
                manifest = (folder / "SHA256SUMS").read_text()
                self.assertIn("JDPerf-", manifest)
                self.assertNotIn("SysMonitor-", manifest)
                if system == "Darwin":
                    contents = folder / "image/JDPerf.app/Contents"
                    info = plistlib.loads((contents / "Info.plist").read_bytes())
                    for key in ("CFBundleName", "CFBundleDisplayName", "CFBundleExecutable"):
                        self.assertEqual(info[key], "JDPerf")
                    self.assertEqual(info["CFBundleIconFile"], "JDPerf.icns")
                    self.assertEqual((contents / "Resources/JDPerf.icns").read_bytes(), b"test icon")
                    self.assertIn('server/JDPerf"', (contents / "Resources/Start.command").read_text())
                    self.assertTrue((contents / "MacOS/JDPerf").is_file())
                elif system == "Windows":
                    config = (folder / "installer.iss").read_text()
                    for text in ("AppName=JDPerf", "AppId=SysMonitor", "Programs\\JDPerf",
                                 "UninstallDisplayIcon={app}\\JDPerf.exe", "{userdesktop}\\JDPerf"):
                        self.assertIn(text, config)
                    self.assertIn("JDPerf.exe", (folder / "portable/JDPerf/Start.cmd").read_text())
                else:
                    package = folder / "deb-root"
                    self.assertIn("Package: jdperf", (package / "DEBIAN/control").read_text())
                    desktop = (package / "usr/share/applications/jdperf.desktop").read_text()
                    for text in ("Name=JDPerf", "Exec=/opt/jdperf/JDPerf", "Icon=jdperf"):
                        self.assertIn(text, desktop)
                    self.assertEqual((package / "usr/share/icons/hicolor/256x256/apps/jdperf.png").read_bytes(), b"test icon")

    def test_native_icons_use_shared_jd_logo_without_stretching(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow is an optional packaging dependency")
        build = self.load_build()
        with tempfile.TemporaryDirectory() as temporary:
            for system, suffix in (("Darwin", ".icns"), ("Windows", ".ico"), ("Linux", ".png")):
                with self.subTest(system=system):
                    icon = build.build_icon(Path(temporary), system)
                    self.assertEqual(icon.name, "JDPerf" + suffix)
                    with Image.open(icon) as image:
                        self.assertEqual(image.width, image.height)
                        self.assertIsNotNone(image.convert("RGBA").getbbox())


class NodeRuntimePackagingTests(unittest.TestCase):
    def test_runtime_identity_minimum_platform_and_arch(self):
        import json
        for system, target in node_runtime.PLATFORMS.items():
            for machine, arch in node_runtime.ARCHES.items():
                with self.subTest(system=system, machine=machine):
                    node_runtime.check_identity(json.dumps(dict(version="v22.7.0", platform=target, arch=arch)), system, machine)
        for info in ({}, [], {"version": "v22.6.0"},
                     dict(version="v24.0.0-rc.1", platform="linux", arch="x64"),
                     dict(version="v24.14.0", platform="darwin", arch="x64"),
                     dict(version="v24.14.0", platform="linux", arch="arm64")):
            with self.subTest(info=info), self.assertRaises(RuntimeError):
                node_runtime.check_identity(json.dumps(info), "Linux", "x86_64")

    def test_dependency_allowlists(self):
        mac = "node:\n\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0, current version 1.0.0)\n"
        node_runtime.check_dependencies(mac, "Darwin")
        for prefix in ("/opt/homebrew/lib/", "/usr/local/lib/", "@rpath/", "/usr/lib/../local/lib/"):
            with self.subTest(prefix=prefix), self.assertRaises(RuntimeError):
                node_runtime.check_dependencies(mac.replace("/usr/lib/", prefix), "Darwin")
        linux = "linux-vdso.so.1 (0x123)\nlibc.so.6 => /lib/x86_64-linux-gnu/libc.so.6 (0x123)\n/lib64/ld-linux-x86-64.so.2 (0x123)"
        node_runtime.check_dependencies(linux, "Linux")
        for text in ("", "statically linked", "libc.so.6 => not found", "libssl.so.3 => /lib/libssl.so.3 (0x123)",
                     "libc.so.6 => /opt/custom/libc.so.6 (0x123)"):
            with self.subTest(text=text), self.assertRaises(RuntimeError):
                node_runtime.check_dependencies(text, "Linux")

    def test_explicit_distribution_only_and_license_required(self):
        with patch.object(node_runtime.subprocess, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "node-dist-dir"):
                node_runtime.official_node_files(None, "Linux")
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                with self.assertRaisesRegex(RuntimeError, "LICENSE missing"):
                    node_runtime.official_node_files(root, "Linux")
                license_path = root / "LICENSE"
                license_path.write_text("not a license", encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "Invalid Node LICENSE"):
                    node_runtime.check_license(license_path)
            run.assert_not_called()

    def test_official_layouts(self):
        for system in node_runtime.PLATFORMS:
            with self.subTest(system=system), tempfile.TemporaryDirectory() as temporary:
                with patch.object(node_runtime, "verify_node") as verify:
                    node, license_path = node_runtime.official_node_files(temporary, system)
                    self.assertEqual(node, Path(temporary).resolve() / ("node.exe" if system == "Windows" else "bin/node"))
                    verify.assert_called_once_with(node, license_path, system)

    def test_payload_smoke_is_offline_and_path_isolated(self):
        import json
        for system in node_runtime.PLATFORMS:
            with self.subTest(system=system), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                node = root / ("node.exe" if system == "Windows" else "node")
                node.write_bytes(b"mock-node")
                node.chmod(0o755)
                license_path = root / "LICENSE"
                license_path.write_text("Node.js\nPermission is hereby granted, free of charge", encoding="utf-8")
                info = dict(version="v24.14.0", platform=node_runtime.PLATFORMS[system], arch="x64")
                results = [subprocess.CompletedProcess([], 0, stdout=json.dumps(info)),
                           subprocess.CompletedProcess([], 0, stdout="joyspace-node-core-ok\n")]
                if system != "Windows":
                    audit = "node:\n\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0)" if system == "Darwin" else "libc.so.6 => /lib/libc.so.6 (0x123)"
                    results.insert(0, subprocess.CompletedProcess([], 0, stdout=audit))
                with patch.object(node_runtime.platform, "machine", return_value="x86_64"), \
                        patch.dict(node_runtime.os.environ, {"NODE_OPTIONS": "--require=bad", "PATH": "/bad", "SECRET": "unused", "LD_PRELOAD": "bad"}), \
                        patch.object(node_runtime.subprocess, "run", side_effect=results) as run:
                    self.assertEqual(node_runtime.verify_node(node, license_path, system, root, True), info)
                for call in run.call_args_list:
                    self.assertEqual(call.kwargs["env"]["PATH"], "")
                    self.assertEqual(call.kwargs["env"]["HOME"], str(root.resolve()))
                    for key in ("NODE_OPTIONS", "SECRET", "LD_PRELOAD"):
                        self.assertNotIn(key, call.kwargs["env"])
                    self.assertEqual(call.kwargs["timeout"], 20)
                self.assertEqual(run.call_args.args[0], [str(node.resolve()), "-e", node_runtime.CORE_SMOKE])

    def test_verifier_checks_runtime_before_server_smoke(self):
        spec = importlib.util.spec_from_file_location(
            "sysmonitor_verify", Path(__file__).parent / "packaging/verify_build.py")
        verify = importlib.util.module_from_spec(spec)
        from types import ModuleType
        with patch.dict("sys.modules", {"macos_compat": compat, "node_runtime": node_runtime,
                                        "httpx": ModuleType("httpx")}):
            spec.loader.exec_module(verify)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            collector = root / "_internal/frontend/src/sysmonitor_test/sysmonitor"
            collector.parent.mkdir(parents=True)
            collector.write_bytes(b"collector")
            (root / "JDPerf").write_bytes(b"mock server")
            with patch.object(verify, "verify_joyspace_payload", side_effect=RuntimeError("missing runtime")) as runtime, \
                    patch.object(verify, "smoke") as smoke:
                with self.assertRaisesRegex(RuntimeError, "missing runtime"):
                    verify.verify_payload(root, root, "Linux")
                runtime.assert_called_once_with(root / "_internal", root, "Linux")
                smoke.assert_not_called()

    def test_build_passes_explicit_parameter_and_environment(self):
        build = DeviceResourcePackagingTests().load_build()
        for argv, expected in ((["--node-dist-dir", "/explicit-node"], "/explicit-node"), ([], "/env-node")):
            with self.subTest(argv=argv), patch.object(build.sys, "argv", ["build.py", "--skip-frontend", *argv]), \
                    patch.dict(build.os.environ, {"JDPERF_NODE_DIST_DIR": "/env-node"}), \
                    patch.object(Path, "is_file", return_value=True), \
                    patch.object(build, "verify_skill"), \
                    patch.object(build, "official_node_files", side_effect=RuntimeError("preflight stop")) as files, \
                    patch.object(build, "run") as run:
                with self.assertRaisesRegex(RuntimeError, "preflight stop"):
                    build.main()
                self.assertEqual(files.call_args.args[0], Path(expected))
                run.assert_not_called()

    def test_skill_and_payload_locations(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(RuntimeError, "skill resources missing"):
                node_runtime.verify_skill(root)
            skill = root / "joyspace_assets/joyspace-kit"
            required = ("SKILL.md", "skills/shared-auth.mjs",
                        "skills/hioffice-auth/scripts/hioffice-auth.mjs",
                        "skills/joyspace-read-doc/scripts/read_joyspace_doc.js",
                     "skills/markdown-to-joyspace/scripts/import_markdown_doc.js")
            for name in required:
                target = skill / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("mock resource", encoding="utf-8")
            self.assertEqual(node_runtime.verify_skill(Path(__file__).parent),
                             Path(__file__).parent / "joyspace_assets/joyspace-kit")
            with patch.object(node_runtime, "verify_node") as verify:
                node_runtime.verify_joyspace_payload(root, root, "Windows")
                runtime = root / "joyspace_assets/node"
                verify.assert_called_once_with(runtime / "node.exe", runtime / "LICENSE", "Windows", root, core_smoke=True)


if __name__ == "__main__":
    unittest.main()