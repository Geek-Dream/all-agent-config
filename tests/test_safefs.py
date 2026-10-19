import os
import stat
import tempfile
import unittest
from pathlib import Path

from app.safefs import atomic_write_text, ensure_private_dir


class SafeFilesystemTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aac-safefs-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    @unittest.skipIf(os.name == "nt", "POSIX permission bits are not enforced on Windows")
    def test_private_directory_and_atomic_file_modes(self):
        target = self.root / "state" / "settings.json"
        atomic_write_text(target, '{"ok": true}')
        self.assertEqual(stat.S_IMODE(target.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(target.read_text(encoding="utf-8"), '{"ok": true}')
        self.assertEqual(list(target.parent.glob(f".{target.name}.*")), [])

    @unittest.skipIf(os.name == "nt", "symlink creation requires extra privileges on Windows")
    def test_symlinked_parent_is_rejected(self):
        real = self.root / "real"
        real.mkdir()
        linked = self.root / "linked"
        linked.symlink_to(real, target_is_directory=True)
        with self.assertRaises(OSError):
            ensure_private_dir(linked)
        self.assertFalse((real / "created").exists())


if __name__ == "__main__":
    unittest.main()
