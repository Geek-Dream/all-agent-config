import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import packages


class PackageReconcileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aac-pkg-")
        self.addCleanup(self.temp.cleanup)
        self.pi_home = Path(self.temp.name) / "pi-agent"
        self.pi_home.mkdir()
        self.npm_root = self.pi_home / "npm" / "node_modules"
        self.git_root = self.pi_home / "git"
        for target, name, value in (
            (packages, "PI_HOME", self.pi_home),
            (packages, "NPM_ROOT", self.npm_root),
            (packages, "GIT_ROOT", self.git_root),
        ):
            mocked = patch.object(target, name, value)
            mocked.start()
            self.addCleanup(mocked.stop)
        self.agent = {"id": "pi"}

    def write_settings(self, entries):
        (self.pi_home / "settings.json").write_text(
            json.dumps({"packages": entries}), encoding="utf-8")

    def test_only_remote_sources_are_tracked(self):
        self.write_settings([
            "npm:statusline-pi",
            {"source": "git:https://github.com/obra/superpowers",
             "skills": ["skills/x"]},
            "/Users/wl/.pi/pi-extensions/wl-setup",   # 本地目录，跟着包走
            {"source": "npm:pi-hooks", "extensions": ["lsp/lsp.ts"]},
        ])
        pkgs = packages.referenced_packages(self.agent)
        self.assertEqual([p["source"] for p in pkgs], [
            "npm:statusline-pi", "git:https://github.com/obra/superpowers",
            "npm:pi-hooks"])

    def test_git_source_maps_to_local_clone_dir(self):
        self.write_settings(["git:https://ghfast.top/https://github.com/mitsuhiko/agent-stuff"])
        pkg = packages.referenced_packages(self.agent)[0]
        self.assertEqual(packages.installed_path(pkg),
                         self.git_root / "ghfast.top/https:/github.com/mitsuhiko/agent-stuff")

    def test_reconcile_reports_missing_when_tools_absent(self):
        self.write_settings(["npm:statusline-pi"])
        log = []
        with patch.object(packages.shutil, "which", return_value=None):
            result = packages.reconcile(self.agent, log)
        self.assertEqual(result["missing"], ["npm:statusline-pi"])
        self.assertTrue(any("未检测到 pi" in note for note in result["notes"]))
        self.assertIn("pi install npm:statusline-pi", "".join(log))

    def test_reconcile_ok_when_package_already_present(self):
        self.write_settings(["npm:statusline-pi"])
        (self.npm_root / "statusline-pi").mkdir(parents=True)
        result = packages.reconcile(self.agent, [])
        self.assertEqual(result["missing"], [])
        self.assertEqual(result["ok"], ["npm:statusline-pi"])

    def test_pi_reconciler_counts_as_installed_after_success(self):
        self.write_settings(["npm:statusline-pi"])

        def fake_run(cmd, log, timeout):
            (self.npm_root / "statusline-pi").mkdir(parents=True, exist_ok=True)
            return 0, "installed"

        with patch.object(packages.shutil, "which", return_value="/usr/bin/pi"), \
             patch.object(packages, "_run", side_effect=fake_run):
            result = packages.reconcile(self.agent, [])
        self.assertEqual(result["installed"], ["npm:statusline-pi"])
        self.assertEqual(result["missing"], [])


if __name__ == "__main__":
    unittest.main()
