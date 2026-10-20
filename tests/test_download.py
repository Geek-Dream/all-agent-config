import io
import json
import os
import platform
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from fastapi import HTTPException

from app import detect, download, main


class FakeResponse:
    def __init__(self, data: bytes, headers: dict | None = None):
        self._data = data
        self._pos = 0
        self.headers = headers or {}

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            chunk = self._data[self._pos:]
            self._pos = len(self._data)
            return chunk
        chunk = self._data[self._pos:self._pos + size]
        self._pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def os_key() -> str:
    return {"Darwin": "macos", "Windows": "windows"}.get(platform.system(), "linux")


class SourceTests(unittest.TestCase):
    def test_platform_key_arch(self):
        prefix = {"Darwin": "macos", "Windows": "windows"}.get(platform.system(), "linux")
        self.assertEqual(download.platform_key("arm64"), f"{prefix}-arm64")
        self.assertEqual(download.platform_key("x86_64"), f"{prefix}-x64")

    def test_source_for_current_platform(self):
        key = download.platform_key()
        agent = {"sources": {"download": {key: "https://x/a.dmg"}}}
        self.assertEqual(download.source_for(agent), "https://x/a.dmg")
        self.assertIsNone(download.source_for({"sources": {}}))
        self.assertEqual(
            download.source_for({"sources": {"download": {"any": "https://x/b"}}}),
            "https://x/b")

    def test_available(self):
        self.assertTrue(download.available({"sources": {"download": {"any": "u"}}}))
        self.assertTrue(download.available({"sources": {"github": {"repo": "a/b"}}}))
        self.assertFalse(download.available({"sources": {"manual": "x"}}))

    def test_mirror(self):
        self.assertEqual(
            download._apply_mirror("https://a/b", "https://m/"), "https://m/https://a/b")
        self.assertEqual(
            download._apply_mirror("https://a/b", "https://m/{url}"), "https://m/https://a/b")


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_download_and_checksum(self):
        log = []
        dest = self.root / "a.bin"
        with mock.patch.object(download.urllib.request, "urlopen",
                               return_value=FakeResponse(b"hello", {"Content-Length": "5"})):
            download._download("https://x/a.bin", dest, log)
        self.assertEqual(dest.read_bytes(), b"hello")

    def test_download_rejects_bad_checksum(self):
        log = []
        with mock.patch.object(download.urllib.request, "urlopen",
                               return_value=FakeResponse(b"hello")):
            with self.assertRaises(ValueError):
                download._download("https://x/a.bin", self.root / "a.bin", log,
                                   sha256="0" * 64)

    def test_github_asset_selection(self):
        payload = {"draft": False, "assets": [
            {"name": "tool-linux.tar.gz", "browser_download_url": "https://x/l"},
            {"name": "tool-macos-arm64.zip", "browser_download_url": "https://x/m",
             "digest": "sha256:" + "a" * 64},
        ]}
        spec = {"repo": "o/r", "assets": {"macos-arm64": "*macos-arm64*"}}
        with mock.patch.object(download, "platform_key", return_value="macos-arm64"), \
                mock.patch.object(download.urllib.request, "urlopen",
                                  return_value=FakeResponse(json.dumps(payload).encode())):
            name, url, digest = download._github_asset(spec, [])
        self.assertEqual(name, "tool-macos-arm64.zip")
        self.assertEqual(url, "https://x/m")
        self.assertEqual(digest, "sha256:" + "a" * 64)

    def test_github_asset_missing(self):
        payload = {"draft": False, "assets": []}
        with mock.patch.object(download.urllib.request, "urlopen",
                               return_value=FakeResponse(json.dumps(payload).encode())):
            with self.assertRaises(ValueError):
                download._github_asset({"repo": "o/r", "asset": "*.dmg"}, [])

    def test_source_zip_fallback(self):
        name, url, _ = download._source_zip({"repo": "o/r", "branch": "main"}, [])
        self.assertEqual(name, "r-main.zip")
        self.assertEqual(url, "https://github.com/o/r/archive/refs/heads/main.zip")


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.apps = self.home / ".aac" / "apps"
        self.user_apps = self.home / "Applications"
        self.apps.parent.mkdir(parents=True)
        for target, name, value in (
            (download, "APPS", self.apps),
            (download, "USER_APPS", self.user_apps),
        ):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.agent = {"id": "deepseek-harness", "name": "DSH",
                      "command": "dsh", "sources": {}}

    def test_install_folder_creates_bin_link(self):
        src = self.root / "src"
        src.mkdir()
        exe = src / "dsh"
        exe.write_text("#!/bin/sh\n", encoding="utf-8")
        exe.chmod(0o755)
        target = download.install_folder(self.agent, src, [])
        self.assertTrue(target.is_file())
        self.assertEqual(target.parent.name, "bin")
        if os.name != "nt":
            self.assertTrue(os.access(target, os.X_OK))
            self.assertTrue(target.is_symlink())

    def test_install_folder_rejects_home(self):
        with mock.patch.object(Path, "home", return_value=self.home):
            with self.assertRaises(ValueError):
                download.install_folder(self.agent, self.home, [])

    def test_install_folder_missing(self):
        with self.assertRaises(ValueError):
            download.install_folder(self.agent, self.root / "nope", [])

    @unittest.skipUnless(platform.system() == "Darwin", "macOS only")
    def test_install_app_bundle(self):
        app = self.root / "DeepSeek Harness.app"
        (app / "Contents" / "MacOS").mkdir(parents=True)
        target = download.install_folder(self.agent, app, [])
        self.assertEqual(target, self.user_apps / "DeepSeek Harness.app")

    @unittest.skipUnless(platform.system() == "Darwin", "macOS only")
    def test_install_zip_finds_app(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as zf:
            zf.writestr("Payload.app/Contents/Info.plist", "x")
        archive_path = self.root / "payload.zip"
        archive_path.write_bytes(stream.getvalue())
        target = download._install_zip(archive_path, self.agent, [])
        self.assertEqual(target.name, "Payload.app")


class DesktopDetectTests(unittest.TestCase):
    def test_desktop_path_reads_declared_location(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / "Demo.app"
            app.mkdir()
            agent = {"id": "demo", "command": "demo",
                     "desktop": {os_key(): [str(app)]}}
            self.assertEqual(detect.desktop_path(agent), app)
            self.assertEqual(detect.launch_target(agent), app)

    def test_desktop_path_missing(self):
        agent = {"id": "demo", "command": "demo",
                 "desktop": {os_key(): ["/nonexistent/Demo.app"]}}
        self.assertIsNone(detect.desktop_path(agent))


class RouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_launch_unknown_agent(self):
        with mock.patch.object(main.registry, "get_agent", return_value=None):
            with self.assertRaises(HTTPException) as ctx:
                await main.api_launch("nope")
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_launch_spawns_target(self):
        agent = {"id": "demo", "name": "Demo", "command": "demo", "sources": {}}
        with mock.patch.object(main.registry, "get_agent", return_value=agent), \
                mock.patch.object(main.detect, "launch_target",
                                  return_value=Path("/tmp/demo")), \
                mock.patch.object(main, "_spawn") as spawn:
            result = await main.api_launch("demo")
        self.assertTrue(result["ok"])
        spawn.assert_called_once_with(Path("/tmp/demo"))

    async def test_folder_install_unknown_agent(self):
        with mock.patch.object(main.registry, "get_agent", return_value=None):
            with self.assertRaises(HTTPException) as ctx:
                await main.api_install_folder("nope", main.FolderReq(path="/tmp"))
        self.assertEqual(ctx.exception.status_code, 404)
