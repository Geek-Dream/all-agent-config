import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import detect


class CurrentVersionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aac-detect-")
        self.addCleanup(self.temp.cleanup)
        self.prefix = Path(self.temp.name)

    def test_uses_binary_from_detected_prefix_when_not_on_path(self):
        binary = self.prefix / "bin" / "demo"
        binary.parent.mkdir()
        binary.write_text("program", encoding="utf-8")
        agent = {"command": "demo", "version_cmd": "demo --version", "sources": {}}
        installs = [{"prefix": str(self.prefix)}]

        with patch.object(detect, "resolve_command", return_value=None), \
                patch.object(detect, "_run", return_value="demo 1.2.3"):
            self.assertEqual(detect.current_version(agent, installs), "1.2.3")

    def test_falls_back_to_installed_npm_package_version(self):
        manifest = (
            self.prefix / "lib" / "node_modules" / "@demo" / "cli" / "package.json"
        )
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"version": "2.4.6"}), encoding="utf-8")
        agent = {
            "command": "demo",
            "version_cmd": "demo --version",
            "sources": {"npm": "@demo/cli"},
        }
        installs = [{"prefix": str(self.prefix)}]

        with patch.object(detect, "resolve_command", return_value=None), \
                patch.object(detect, "_run", return_value=""):
            self.assertEqual(detect.current_version(agent, installs), "2.4.6")


class InstallVersionTests(unittest.TestCase):
    def test_scan_reports_version_for_each_install_prefix(self):
        first = Path("/tmp/aac-pi-local")
        second = Path("/tmp/aac-pi-copy")
        installs = [
            {"prefix": str(first), "scope": "user", "primary": True, "needs_sudo": False},
            {"prefix": str(second), "scope": "user", "primary": False, "needs_sudo": False},
        ]
        agent = {
            "id": "demo",
            "name": "Demo",
            "command": "demo",
            "version_cmd": "demo --version",
            "sources": {"npm": "@demo/cli"},
        }

        def fake_run(cmd, timeout=20):
            return "2.0.0" if str(first) in cmd[0] else "2.1.0"

        with patch.object(detect, "detect_install_method", return_value="npm-global"), \
                patch.object(detect, "find_installs", return_value=installs), \
                patch.object(detect, "latest_version", return_value="2.1.0"), \
                patch.object(detect, "resolve_command", return_value=None), \
                patch.object(
                    detect,
                    "_command_candidates",
                    side_effect=lambda _agent, items, include_path=True: [
                        Path(items[0]["prefix"]) / "bin" / "demo"
                    ],
                ), \
                patch.object(detect, "_run", side_effect=fake_run):
            result = detect.scan_one(agent)

        self.assertEqual(result["current_version"], "2.0.0")
        self.assertTrue(result["updatable"])
        self.assertEqual(result["installs"][0]["current_version"], "2.0.0")
        self.assertTrue(result["installs"][0]["updatable"])
        self.assertEqual(result["installs"][1]["current_version"], "2.1.0")
        self.assertFalse(result["installs"][1]["updatable"])


if __name__ == "__main__":
    unittest.main()
