import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from app import bundle


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aac-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.package = self.home / ".demo" / "package"
        self.package.mkdir(parents=True)
        self.settings = self.home / "settings.json"
        self.agent = {
            "id": "demo",
            "name": "Demo",
            "command": "demo",
            "sources": {"npm": "demo-pkg"},
            "config_dirs": [str(self.home / ".demo")],
            "categories": {
                "system": [str(self.settings)],
                "hooks": [str(self.settings)],
            },
        }
        for target, name, value in (
            (bundle, "HOME", self.home),
            (bundle, "EXPORTS_DIR", self.home / "exports"),
        ):
            mocked = patch.object(target, name, value)
            mocked.start()
            self.addCleanup(mocked.stop)
        for target, name, value in (
            (bundle.registry, "get_agent", self.agent),
            (bundle.categories, "package_dir", self.package),
            (bundle.categories, "installed_pkg_version", "1.0"),
        ):
            mocked = patch.object(target, name, return_value=value)
            mocked.start()
            self.addCleanup(mocked.stop)

    def archive(self, entries, category="system", source_home="/source/home"):
        path = self.root / "import.aac"
        manifest = {
            "format": 2, "agent_id": "demo", "agent_name": "Demo",
            "source_home": source_home,
            "categories": {category: {"entries": entries}},
        }
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("manifest.json", json.dumps(manifest))
            for index, entry in enumerate(entries):
                zf.writestr(entry["arc"], f"payload {index}")
        return path

    def test_relative_home_rejects_traversal_absolute_and_drive_paths(self):
        roots = bundle.allowed_import_roots(self.agent)
        for relative in ("../outside.txt", r"..\outside.txt", str(self.root / "outside.txt"),
                         "C:/outside.txt", "C:outside.txt", r"\\server\share\file", "", "."):
            with self.subTest(relative=relative):
                entry = {"rel_home": relative, "target": str(self.home / "fallback.txt")}
                self.assertIsNone(bundle._resolve_target(entry, roots))

    def test_legacy_target_requires_absolute_path_inside_home(self):
        roots = bundle.allowed_import_roots(self.agent)
        for target in (str(self.root / "outside.txt"), str(self.home), "relative.txt", "", "C:/outside.txt", r"\\server\share\file"):
            with self.subTest(target=target):
                self.assertIsNone(bundle._resolve_target({"target": target}, roots))
        self.assertEqual(bundle._resolve_target({"target": str(self.settings)}, roots),
                         self.home / "settings.json")

    def test_home_parent_and_file_symlinks_cannot_escape(self):
        roots = bundle.allowed_import_roots(self.agent)
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_bytes(b"keep")
        (self.home / "parent").symlink_to(outside, target_is_directory=True)
        (self.home / "file.txt").symlink_to(outside / "keep.txt")
        for relative in ("parent/new.txt", "file.txt"):
            with self.subTest(relative=relative):
                self.assertIsNone(bundle._resolve_target({"rel_home": relative}, roots))

    def test_only_registry_approved_targets_can_be_imported(self):
        roots = bundle.allowed_import_roots(self.agent)
        self.assertEqual(
            bundle._resolve_target({"rel_home": ".demo/settings.txt"}, roots),
            self.home / ".demo" / "settings.txt",
        )
        self.assertEqual(
            bundle._resolve_target({"rel_home": "settings.json"}, roots),
            self.settings,
        )
        for relative in (".zshrc", ".ssh/authorized_keys", "unrelated.txt"):
            with self.subTest(relative=relative):
                self.assertIsNone(bundle._resolve_target({"rel_home": relative}, roots))

    def test_invalid_home_target_is_skipped_but_safe_sibling_imports(self):
        entries = [
            {"arc": "system/bad", "rel_home": "../outside.txt"},
            {"arc": "system/good", "rel_home": ".demo/settings.txt"},
        ]
        path = self.archive(entries)
        preview = bundle.import_preview(path)
        self.assertEqual(preview["conflicts"]["system"], {"overwrite": 0, "new": 1, "skip": 1})
        log = []
        bundle.import_apply(path, ["system"], log)
        self.assertFalse((self.root / "outside.txt").exists())
        self.assertEqual((self.home / ".demo" / "settings.txt").read_text(encoding="utf-8"), "payload 1")
        self.assertTrue(any("跳过" in line for line in log))

    def test_patch_preview_and_apply_confine_entries_to_package(self):
        outside = self.home / ".demo" / "outside.txt"
        outside.write_bytes(b"keep")
        entries = [
            {"arc": "patches/bad", "rel": "../outside.txt"},
            {"arc": "patches/good", "rel": "lib/index.js"},
        ]
        path = self.archive(entries, "patches")
        preview = bundle.import_preview(path)
        self.assertEqual(preview["conflicts"]["patches"], {"overwrite": 0, "new": 1, "skip": 1})
        bundle.import_apply(path, ["patches"], [])
        self.assertEqual(outside.read_bytes(), b"keep")
        self.assertEqual((self.package / "lib" / "index.js").read_text(encoding="utf-8"), "payload 1")

    def test_patch_symlinks_are_skipped_in_preview_and_apply(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.package / "linked").symlink_to(outside, target_is_directory=True)
        path = self.archive([{"arc": "patches/one", "rel": "linked/index.js"}], "patches")
        self.assertEqual(bundle.import_preview(path)["conflicts"]["patches"],
                         {"overwrite": 0, "new": 0, "skip": 1})
        bundle.import_apply(path, ["patches"], [])
        self.assertFalse((outside / "index.js").exists())

    def test_missing_package_preview_counts_only_skipped_patches(self):
        path = self.archive([{"arc": "patches/one", "rel": "index.js"}], "patches")
        with patch.object(bundle.categories, "package_dir", return_value=None):
            self.assertEqual(bundle.import_preview(path)["conflicts"]["patches"],
                             {"overwrite": 0, "new": 0, "skip": 1})
            bundle.import_apply(path, ["patches"], [])
        self.assertFalse((self.package / "index.js").exists())

    def test_safe_export_round_trip_merges_json_and_backs_up_existing_file(self):
        target = self.home / "settings.json"
        source_home = r"C:\Users\source"
        target.write_text(json.dumps({"hooks": {"path": source_home + r"\bin"}, "unrelated": "keep"}), encoding="utf-8")
        detail = {"categories": {"hooks": {"files": [{"path": str(target), "size": target.stat().st_size,
                                                     "json_keys": ["hooks"]}]}}, "patches": None}
        with patch.object(bundle.categories, "agent_detail", return_value=detail):
            path = bundle.export_bundle("demo", ["hooks"])
        target.write_text(json.dumps({"unrelated": "local", "hooks": {"old": True}}), encoding="utf-8")
        self.assertEqual(bundle.import_preview(path)["conflicts"]["hooks"],
                         {"overwrite": 1, "new": 0, "skip": 0})
        bundle.import_apply(path, ["hooks"], [])
        result = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(result["unrelated"], "local")
        self.assertEqual(result["hooks"], {"path": source_home + r"\bin"})
        backups = list(self.home.glob("settings.json.bak-aac-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text(encoding="utf-8"))["hooks"], {"old": True})

    def test_backup_symlink_cannot_redirect_import_writes(self):
        target = self.home / ".demo" / "settings.txt"
        target.write_bytes(b"original")
        outside = self.root / "outside.txt"
        outside.write_bytes(b"keep")
        target.with_name("settings.txt.bak-aac-fixed").symlink_to(outside)
        path = self.archive([{"arc": "system/one", "rel_home": ".demo/settings.txt"}])
        with patch.object(bundle.time, "strftime", return_value=".bak-aac-fixed"):
            bundle.import_apply(path, ["system"], [])
        self.assertEqual(outside.read_bytes(), b"keep")
        self.assertEqual(target.read_bytes(), b"payload 0")
        backups = [p for p in target.parent.glob("settings.txt.bak-aac-*")
                   if not p.is_symlink()]
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), b"original")

    def test_import_remaps_windows_home_inside_json(self):
        source_home = r"C:\Users\source"
        path = self.archive([{"arc": "system/one", "rel_home": "settings.json"}], source_home=source_home)
        with zipfile.ZipFile(path) as zf:
            manifest = zf.read("manifest.json")
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("manifest.json", manifest)
            zf.writestr("system/one", json.dumps({"path": source_home + r"\bin"}))
        bundle.import_apply(path, ["system"], [])
        self.assertEqual(json.loads((self.home / "settings.json").read_text(encoding="utf-8"))["path"],
                         str(self.home) + "/bin")

    def test_platform_dir_layout_is_rewritten_across_os(self):
        dirs = [{"posix": "~/.config/opencode", "windows": "%APPDATA%/opencode"}]
        self.assertEqual(
            bundle._decl_rel_home("%APPDATA%/opencode"), "AppData/Roaming/opencode")
        self.assertEqual(
            bundle._map_platform_rel(dirs, ".config/opencode/opencode.json", "macOS", "Windows"),
            "AppData/Roaming/opencode/opencode.json")
        self.assertEqual(
            bundle._map_platform_rel(dirs, "AppData/Roaming/opencode/opencode.json", "Windows", "macOS"),
            ".config/opencode/opencode.json")
        self.assertEqual(
            bundle._map_platform_rel(dirs, ".claude/settings.json", "macOS", "Windows"),
            ".claude/settings.json")

    def test_export_target_os_writes_windows_relative_paths(self):
        detail = {"categories": {"system": {"files": [
            {"path": str(self.home / ".config" / "opencode" / "opencode.json"), "size": 3}]}},
            "patches": None}
        (self.home / ".config" / "opencode").mkdir(parents=True)
        (self.home / ".config" / "opencode" / "opencode.json").write_text("{}")
        agent = dict(self.agent, platform_dirs=[
            {"posix": "~/.config/opencode", "windows": "%APPDATA%/opencode"}])
        with patch.object(bundle.registry, "get_agent", return_value=agent), \
                patch.object(bundle.categories, "agent_detail", return_value=detail):
            path = bundle.export_bundle("demo", ["system"], target_os="Windows")
        with zipfile.ZipFile(path) as zf:
            manifest = json.loads(zf.read("manifest.json"))
        self.assertEqual(manifest["path_platform"], "Windows")
        self.assertEqual(manifest["categories"]["system"]["entries"][0]["rel_home"],
                         "AppData/Roaming/opencode/opencode.json")
