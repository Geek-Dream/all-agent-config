import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import patchwork


class PatchworkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aac-patchwork-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_backup_symlink_cannot_redirect_copy(self):
        target = self.root / "index.js"
        target.write_text("patched", encoding="utf-8")
        outside = self.root / "outside.js"
        outside.write_text("outside", encoding="utf-8")
        backup = target.with_name(target.name + ".bak-aac-fixed")
        backup.symlink_to(outside)
        with patch.object(patchwork.time, "strftime", return_value=".bak-aac-fixed"):
            patchwork._backup(target, [])
        self.assertEqual(outside.read_text(encoding="utf-8"), "outside")
        backups = [p for p in self.root.glob("index.js.bak-aac-*") if not p.is_symlink()]
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), "patched")

    def test_reapply_returns_partial_when_only_some_files_succeed(self):
        work = self.root / "work"
        patched = work / "patched"
        package = self.root / "package"
        patched.mkdir(parents=True)
        package.mkdir()
        source = patched / "restored.js"
        source.write_text("patched", encoding="utf-8")
        meta = {
            "patched_hashes": {
                "restored.js": patchwork.categories._sha256(source),
                "missing.js": "missing",
            },
            "base": {},
        }
        (work / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        with patch.object(patchwork, "work_dir", return_value=work), \
                patch.object(patchwork.categories, "package_dir", return_value=package):
            result = patchwork.reapply({"id": "demo"}, [])
        self.assertEqual(
            result,
            {"status": "partial", "ok": 1, "ported": 0, "failed": 1},
        )
        self.assertEqual((package / "restored.js").read_text(encoding="utf-8"), "patched")


if __name__ == "__main__":
    unittest.main()
