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
            (internal / "sysmonitor").write_bytes(b"\x7fELFandroid")
            (internal / "asset.js").write_text("plain asset", encoding="utf-8")
            with patch.object(compat.platform, "machine", return_value="arm64"), \
                    patch.object(compat.subprocess, "run") as run:
                run.return_value = subprocess.CompletedProcess([], 0, stdout=modern())
                self.assertEqual(compat.verify_macos_binaries(root), 3)
                self.assertEqual(run.call_count, 6)
                commands = [call.args[0] for call in run.call_args_list]
                self.assertEqual(sum(command[0] == "lipo" for command in commands), 3)
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
        with patch.dict("sys.modules", {"macos_compat": compat}):
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
                        patch.dict(build.os.environ, {}, clear=False), \
                        patch.object(build, "run", side_effect=RuntimeError("stop before real build")) as run:
                    with self.assertRaisesRegex(RuntimeError, "stop before real build"):
                        build.main()
                args = run.call_args.args
                self.assertIn("PyInstaller", args)
                resources = [args[i + 1] for i, value in enumerate(args) if value == "--add-data"]
                self.assertIn(str(build.ROOT / "device_top_capture.sh") + ":.", resources)

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


if __name__ == "__main__":
    unittest.main()