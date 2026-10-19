import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import installer


class ScriptLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aac-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        home_patch = patch.object(Path, "home", return_value=self.home)
        home_patch.start()
        self.addCleanup(home_patch.stop)

    def layout(self, prefix):
        package = prefix / "node_modules" / "@demo" / "cli"
        package.mkdir(parents=True)
        (package / "index.js").write_text("program", encoding="utf-8")
        (prefix / "bin").mkdir()
        (prefix / "bin" / "demo").write_text("shim", encoding="utf-8")
        return prefix

    def test_rejects_parent_traversal_outside_home(self):
        self.layout(self.root / "outside")
        self.assertFalse(installer.is_script_layout(
            str(self.home / ".." / "outside"), "demo", "@demo/cli"))

    def test_rejects_home_itself_as_script_prefix(self):
        self.layout(self.home)
        self.assertFalse(installer.is_script_layout(str(self.home), "demo", "@demo/cli"))

    def test_rejects_symlinked_prefix_outside_home(self):
        self.layout(self.root / "outside")
        prefix = self.home / "linked"
        prefix.symlink_to(self.root / "outside", target_is_directory=True)
        self.assertFalse(installer.is_script_layout(str(prefix), "demo", "@demo/cli"))

    def test_rejects_escaping_package_and_bin_directories(self):
        outside = self.layout(self.root / "outside")
        for directory in ("node_modules", "bin"):
            with self.subTest(directory=directory):
                prefix = self.layout(self.home / directory)
                shutil.rmtree(prefix / directory)
                (prefix / directory).symlink_to(outside / directory, target_is_directory=True)
                self.assertFalse(installer.is_script_layout(str(prefix), "demo", "@demo/cli"))

    def test_direct_removal_revalidates_prefix_before_deleting(self):
        outside = self.layout(self.root / "outside")
        log = []
        result = installer.remove_script_prefix(str(outside), "demo", "@demo/cli", log)
        self.assertNotEqual(result, 0)
        self.assertTrue((outside / "node_modules" / "@demo" / "cli" / "index.js").is_file())
        self.assertTrue((outside / "bin" / "demo").is_file())

    def test_valid_script_removal_preserves_config_and_sessions(self):
        prefix = self.layout(self.home / ".demo")
        (prefix / "settings.json").write_text("config", encoding="utf-8")
        (prefix / "sessions").mkdir()
        (prefix / "sessions" / "one.json").write_text("session", encoding="utf-8")
        self.assertEqual(installer.remove_script_prefix(
            str(prefix), "demo", "@demo/cli", []), 0)
        self.assertFalse((prefix / "node_modules" / "@demo" / "cli").exists())
        self.assertFalse((prefix / "bin" / "demo").exists())
        self.assertEqual((prefix / "settings.json").read_text(encoding="utf-8"), "config")
        self.assertEqual((prefix / "sessions" / "one.json").read_text(encoding="utf-8"), "session")

    def test_reports_failed_package_deletion(self):
        prefix = self.layout(self.home / ".demo")
        def denied(path, ignore_errors=False):
            if not ignore_errors:
                raise PermissionError("denied")

        with patch.object(installer.shutil, "rmtree", side_effect=denied):
            result = installer.remove_script_prefix(str(prefix), "demo", "@demo/cli", [])
        self.assertNotEqual(result, 0)
        self.assertTrue((prefix / "bin" / "demo").is_file())

    def test_native_script_layout_uses_declared_prefix_without_npm_package(self):
        prefix = self.home / ".opencode"
        (prefix / "bin").mkdir(parents=True)
        (prefix / "bin" / "opencode").write_text("binary", encoding="utf-8")
        package = prefix / "node_modules" / "@opencode-ai" / "plugin"
        package.mkdir(parents=True)
        (package / "index.js").write_text("plugin", encoding="utf-8")
        (prefix / "package.json").write_text(
            '{"dependencies":{"@opencode-ai/plugin":"1.0.0"}}', encoding="utf-8")
        self.assertTrue(installer.is_script_layout(
            str(prefix), "opencode", "opencode-ai", "~/.opencode"))

    def test_native_script_removal_preserves_config(self):
        prefix = self.home / ".opencode"
        (prefix / "bin").mkdir(parents=True)
        (prefix / "bin" / "opencode").write_text("binary", encoding="utf-8")
        package = prefix / "node_modules" / "@opencode-ai" / "plugin"
        package.mkdir(parents=True)
        (package / "index.js").write_text("plugin", encoding="utf-8")
        (prefix / "package.json").write_text("{}", encoding="utf-8")
        (prefix / "opencode.json").write_text("keep", encoding="utf-8")
        self.assertEqual(installer.remove_script_prefix(
            str(prefix), "opencode", "opencode-ai", [], "~/.opencode"), 0)
        self.assertFalse((prefix / "bin" / "opencode").exists())
        self.assertFalse((prefix / "node_modules").exists())
        self.assertFalse((prefix / "package.json").exists())
        self.assertEqual((prefix / "opencode.json").read_text(encoding="utf-8"), "keep")


class UpdateArgvTests(unittest.TestCase):
    def test_update_uses_detected_prefix_when_command_is_not_on_path(self):
        agent = {"command": "demo", "sources": {"npm": "@demo/cli"}}
        with patch.object(installer, "install_prefix_of", return_value=None), \
                patch.object(installer, "prefix_writable", return_value=True):
            argv, need_sudo = installer.build_update_argv(
                agent, "user", "/Users/wl/.local")
        self.assertEqual(
            argv,
            ["npm", "install", "-g", "--prefix", "/Users/wl/.local",
             "@demo/cli@latest"],
        )
        self.assertFalse(need_sudo)
