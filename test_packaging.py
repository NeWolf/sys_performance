"""Cross-platform regression tests for the macOS release compatibility gate."""
import importlib.util
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


if __name__ == "__main__":
    unittest.main()