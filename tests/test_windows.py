"""跨平台（Windows）路径逻辑测试。

本机不是 Windows，因此用一个「Windows 模拟环境」运行交叉逻辑：
把 bundle._os_name() 伪装成 Windows，并把 HOME / USERPROFILE / APPDATA /
LOCALAPPDATA 指向临时目录，从而验证：
  - 目录布局在 posix ↔ windows 之间的映射；
  - manifest 的 source_os / path_platform 标注；
  - 导入时 rel_home 的改写与配置文件正文里的绝对路径规范化。

只伪装 `_os_name` 而不是 sys.platform：后者会让 shutil.which 等库函数
走进 Windows 分支而崩溃。注意模拟环境里 HOME 仍是 posix 形式（本机是类
Unix），所以只断言「目录布局 / 相对路径 / 分隔符方向」相关行为，不断言
Windows 盘符或反斜杠 HOME 本身。
"""

import contextlib
import json
import os
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from app import bundle, categories, detect


@contextlib.contextmanager
def fake_home(home: Path, os_name: str | None = None):
    """把环境指向临时 home；os_name 用于伪装该端的操作系统。

    显式指定 os_name（而不是依赖宿主机）是为了让同一套测试在 macOS /
    Linux / Windows runner 上行为一致。
    """
    home.mkdir(parents=True, exist_ok=True)
    env = {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "APPDATA": str(home / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(home / "AppData" / "Local"),
    }
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, env))
        stack.enter_context(patch.object(bundle, "HOME", home))
        stack.enter_context(patch.object(bundle, "EXPORTS_DIR", home / ".aac" / "exports"))
        stack.enter_context(patch.object(bundle, "IMPORTS_DIR", home / ".aac" / "imports"))
        # 避免真的执行 `opencode --version` / 下载官方包比对，加速并保持离线稳定
        stack.enter_context(patch.object(categories, "detect_patches", lambda agent: None))
        stack.enter_context(patch.object(detect, "current_version",
                                         lambda agent, installs=None: None))
        if os_name:
            stack.enter_context(patch.object(bundle, "_os_name", lambda: os_name))
        yield home


class DeclPathTests(unittest.TestCase):
    def test_posix_and_windows_decl_forms(self):
        cases = {
            "~/.config/opencode": ".config/opencode",
            "%APPDATA%/opencode": "AppData/Roaming/opencode",
            "%LOCALAPPDATA%/opencode": "AppData/Local/opencode",
            "%USERPROFILE%/.codex": ".codex",
            "%USERPROFILE%\\.codex": ".codex",
            "%APPDATA%\\opencode": "AppData/Roaming/opencode",
        }
        for raw, expected in cases.items():
            self.assertEqual(bundle._decl_rel_home(raw), expected, raw)
        self.assertIsNone(bundle._decl_rel_home(""))
        self.assertIsNone(bundle._decl_rel_home(None))


class OsLabelTests(unittest.TestCase):
    def test_windows_simulation_reports_windows(self):
        home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, home, True)
        with fake_home(home, os_name="Windows"):
            self.assertEqual(bundle._os_name(), "Windows")
            self.assertEqual(bundle._os_key(bundle._os_name()), "windows")

    def test_os_key_folds_non_windows_to_posix(self):
        for name in ("macOS", "linux", "freebsd"):
            self.assertEqual(bundle._os_key(name), "posix", name)
        self.assertEqual(bundle._os_key("Windows"), "windows")


class MacToWindowsTests(unittest.TestCase):
    def setUp(self):
        self.mac = Path(tempfile.mkdtemp()).resolve()
        self.win = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.mac, True)
        self.addCleanup(shutil.rmtree, self.win, True)
        src = self.mac / ".config" / "opencode" / "opencode.json"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text(json.dumps({"model": "x", "dir": str(self.mac) + "/proj"}))

    def _export(self) -> tuple[Path, dict]:
        with fake_home(self.mac, os_name="macOS"):
            path = bundle.export_bundle("opencode", ["system"], target_os="Windows")
            with zipfile.ZipFile(path) as zf:
                return path, json.loads(zf.read("manifest.json"))

    def test_export_marks_path_platform_windows(self):
        _path, man = self._export()
        self.assertEqual(man["source_os"], "macOS")
        self.assertEqual(man["path_platform"], "Windows")
        rels = [e["rel_home"] for e in man["categories"]["system"]["entries"] if e.get("rel_home")]
        self.assertTrue(rels, "system 分类应有条目")
        self.assertTrue(all(r.startswith("AppData/Roaming/opencode/") for r in rels), rels)

    def test_import_lands_in_windows_appdata(self):
        path, _man = self._export()
        with fake_home(self.win, os_name="Windows"):
            log = []
            bundle.import_apply(path, ["system"], log)
            landed = self.win / "AppData" / "Roaming" / "opencode" / "opencode.json"
            self.assertTrue(landed.is_file(), "".join(log))
            self.assertFalse((self.win / ".config").exists())


class WindowsToMacTests(unittest.TestCase):
    """构造一个「Windows 导出的包」，在本机（类 Unix）导入。"""

    def setUp(self):
        self.mac = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.mac, True)
        self.src = self.mac / "win.aac"
        self._build(self.src)

    def _build(self, path: Path):
        agent = bundle.registry.get_agent("opencode")
        manifest = {
            "format": 2, "agent_id": "opencode", "agent_name": "OpenCode",
            "source_os": "Windows", "source_home": "C:\\Users\\wl",
            "path_platform": "Windows",
            "platform_dirs": agent.get("platform_dirs") or [],
            "categories": {"system": {"entries": [{
                "arc": "system/0000_opencode.json",
                "target": "C:\\Users\\wl\\AppData\\Roaming\\opencode\\opencode.json",
                "rel_home": "AppData/Roaming/opencode/opencode.json"}]}},
        }
        body = json.dumps({"model": "x", "dir": "C:\\Users\\wl\\proj"})
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("manifest.json", json.dumps(manifest))
            zf.writestr("system/0000_opencode.json", body)

    def test_import_rewrites_layout_and_content_separators(self):
        with fake_home(self.mac, os_name="macOS"):
            preview = bundle.import_preview(self.src)
            self.assertEqual(preview["conflicts"]["system"]["skip"], 0)
            log = []
            bundle.import_apply(self.src, ["system"], log)
            landed = self.mac / ".config" / "opencode" / "opencode.json"
        self.assertTrue(landed.is_file(), "".join(log))
        text = landed.read_text(encoding="utf-8")
        data = json.loads(text)
        # 源机 Windows home 已替换为本机，且路径尾部的反斜杠规范成正斜杠
        self.assertEqual(data["dir"], str(self.mac) + "/proj")
        self.assertNotIn("C:\\Users\\wl", text)
        self.assertIn("/proj", text)


class WindowsNativeRoundTripTests(unittest.TestCase):
    """在模拟 Windows 下导出再导入，布局应保持不变。"""

    def setUp(self):
        self.win = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.win, True)

    def test_round_trip_keeps_appdata_layout(self):
        with fake_home(self.win, os_name="Windows"):
            src = self.win / "AppData" / "Roaming" / "opencode" / "opencode.json"
            src.parent.mkdir(parents=True, exist_ok=True)
            src.write_text(json.dumps({"model": "x"}))

            path = bundle.export_bundle("opencode", ["system"])
            with zipfile.ZipFile(path) as zf:
                man = json.loads(zf.read("manifest.json"))
            self.assertEqual(man["source_os"], "Windows")
            self.assertEqual(man["path_platform"], "Windows")
            rels = [e["rel_home"] for e in man["categories"]["system"]["entries"]
                    if e.get("rel_home")]
            self.assertTrue(all(r.startswith("AppData/Roaming/opencode/") for r in rels), rels)

            src.unlink()
            log = []
            bundle.import_apply(path, ["system"], log)
            self.assertTrue(src.is_file(), "".join(log))
            self.assertEqual(json.loads(src.read_text())["model"], "x")


if __name__ == "__main__":
    unittest.main()
