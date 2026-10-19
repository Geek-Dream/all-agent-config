import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from app import cloud


class FakeResponse:
    def __init__(self, body: bytes, *, status: int = 200, headers: dict | None = None):
        self.body = body
        self.status = status
        self.headers = headers or {}
        self.offset = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self.body) - self.offset
        chunk = self.body[self.offset:self.offset + size]
        self.offset += len(chunk)
        return chunk


class WebDAVDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aac-cloud-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.local = self.root / "backup.aac"
        self.local.write_bytes(b"original")
        self.client = cloud.WebDAV("https://dav.example.test", "user", "pass")

    def test_invalid_download_does_not_replace_local_file(self):
        response = FakeResponse(b"not a zip")
        with patch.object(cloud.urllib.request, "urlopen", return_value=response):
            with self.assertRaises(Exception):
                self.client.download("aac", "backup.aac", self.local)
        self.assertEqual(self.local.read_bytes(), b"original")
        self.assertEqual(list(self.root.glob(".backup.aac.*.download")), [])

    def test_oversized_download_is_rejected_before_replacement(self):
        response = FakeResponse(b"12345", headers={"Content-Length": "5"})
        with patch.object(cloud.urllib.request, "urlopen", return_value=response):
            with self.assertRaises(cloud.WebDAVError):
                self.client.download("aac", "backup.aac", self.local, max_size=4)
        self.assertEqual(self.local.read_bytes(), b"original")

    def test_valid_download_is_atomically_replaced(self):
        archive = self.root / "valid.aac"
        manifest = {"format": 2, "agent_id": "claude-code", "categories": {}}
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("manifest.json", json.dumps(manifest))
        response = FakeResponse(archive.read_bytes())
        with patch.object(cloud.urllib.request, "urlopen", return_value=response):
            self.client.download("aac", "backup.aac", self.local)
        with zipfile.ZipFile(self.local) as zf:
            self.assertEqual(json.loads(zf.read("manifest.json"))["format"], 2)

    def test_same_size_remote_change_is_visible_in_fingerprint(self):
        before = {"size": 100, "etag": '"old"', "modified": "Sun, 01 Jan 2026 00:00:00 GMT"}
        after = {"size": 100, "etag": '"new"', "modified": "Sun, 01 Jan 2026 00:00:00 GMT"}
        self.assertNotEqual(cloud.remote_fingerprint(before), cloud.remote_fingerprint(after))


if __name__ == "__main__":
    unittest.main()
