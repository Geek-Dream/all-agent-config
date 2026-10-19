import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from app import bundle, crypto


class CryptoTests(unittest.TestCase):
    def test_round_trip_and_wrong_password(self):
        blob = b"hello world" * 100
        sealed = crypto.encrypt(blob, "correct horse")
        self.assertTrue(crypto.is_encrypted(sealed))
        self.assertEqual(crypto.decrypt(sealed, "correct horse"), blob)
        with self.assertRaises(ValueError):
            crypto.decrypt(sealed, "wrong")
        with self.assertRaises(ValueError):
            crypto.decrypt(b"not encrypted", "x")

    def test_empty_password_rejected(self):
        with self.assertRaises(ValueError):
            crypto.encrypt(b"data", "")


class EncryptedBundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aac-crypt-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.detail = {"categories": {}, "patches": None, "agent_version": "1.0"}
        for target, name, value in ((bundle, "HOME", self.home),
                                    (bundle, "EXPORTS_DIR", self.home / "exports"),
                                    (bundle, "IMPORTS_DIR", self.home / "imports")):
            mocked = patch.object(target, name, value)
            mocked.start()
            self.addCleanup(mocked.stop)

    def export(self, password):
        with patch.object(bundle.categories, "agent_detail", return_value=self.detail):
            return bundle.export_bundle("claude-code", [], password=password)

    def test_encrypted_bundle_requires_password(self):
        path = self.export("s3cret")
        self.assertTrue(crypto.is_encrypted(path.read_bytes()))
        with self.assertRaises(ValueError) as ctx:
            bundle.import_preview(path)
        self.assertIn("ENCRYPTED", str(ctx.exception))
        with self.assertRaises(ValueError):
            bundle.import_preview(path, "nope")
        preview = bundle.import_preview(path, "s3cret")
        self.assertEqual(preview["manifest"]["agent_id"], "claude-code")

    def test_plain_bundle_has_no_magic(self):
        path = self.export(None)
        self.assertFalse(crypto.is_encrypted(path.read_bytes()))
        with zipfile.ZipFile(path) as zf:
            self.assertIn("manifest.json", zf.namelist())
        self.assertEqual(bundle.import_preview(path)["manifest"]["agent_id"], "claude-code")


if __name__ == "__main__":
    unittest.main()
