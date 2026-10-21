"""状态栏扩展安装 / 卸载：必须能关掉抢 footer 的邻居，卸载时原样还回去。"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import footer_ext


class _Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aac-footer-ext-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.agent = self.home / ".pi" / "agent"
        self.agent.mkdir(parents=True)
        patches = {
            "_target": lambda: self.home / ".pi" / "pi-extensions" / "aac-footer",
            "_agent_dir": lambda: self.agent,
            "_settings_file": lambda: self.agent / "settings.json",
            "_wl_setup_file": lambda: self.agent / "wl-setup.json",
            "_statusline_file": lambda: self.agent / "statusline-pi.json",
            "_state_file": lambda: self.home / ".aac" / "footer-state.json",
            "_claude_settings_file": lambda: self.home / ".claude" / "settings.json",
            "_bin_dir": lambda: self.home / ".aac" / "bin",
            "_render_script": lambda: self.home / ".aac" / "bin"
            / ("aac-footer.cmd" if footer_ext.sys.platform == "win32" else "aac-footer"),
        }
        for name, value in patches.items():
            mocked = patch.object(footer_ext, name, value)
            mocked.start()
            self.addCleanup(mocked.stop)
        self._write(self.agent / "settings.json", {"packages": ["npm:pi-web-access"]})
        self._write(self.agent / "wl-setup.json", {"zhPatch": True, "rmbFooter": True})
        self._write(self.agent / "statusline-pi.json", {"version": 1, "enabled": True})

    def _write(self, path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def _read(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))


class FooterExtTests(_Base):

    def test_install_registers_package_and_installs_extension(self):
        result = footer_ext.install()
        self.assertTrue(result["ok"], result)
        target = footer_ext._target()
        self.assertTrue((target / "extensions" / "footer.ts").is_file())
        self.assertIn(str(target), self._read(self.agent / "settings.json")["packages"])

    def test_install_disables_the_two_footer_hogs(self):
        footer_ext.install()
        self.assertFalse(self._read(self.agent / "wl-setup.json")["rmbFooter"])
        self.assertFalse(self._read(self.agent / "statusline-pi.json")["enabled"])
        # 其它设置不能被顺手改掉
        self.assertTrue(self._read(self.agent / "wl-setup.json")["zhPatch"])

    def test_status_reports_conflicts_before_install(self):
        st = footer_ext.status()
        self.assertFalse(st["installed"])
        self.assertEqual(len(st["conflicts"]), 2)

    def test_uninstall_restores_neighbours(self):
        footer_ext.install()
        result = footer_ext.uninstall()
        self.assertTrue(result["ok"], result)
        self.assertFalse(footer_ext._target().exists())
        self.assertTrue(self._read(self.agent / "wl-setup.json")["rmbFooter"])
        self.assertTrue(self._read(self.agent / "statusline-pi.json")["enabled"])
        self.assertNotIn(str(footer_ext._target()), self._read(self.agent / "settings.json")["packages"])

    def test_uninstall_without_state_keeps_neighbours_untouched(self):
        self._write(self.agent / "wl-setup.json", {"rmbFooter": False})
        result = footer_ext.uninstall()
        self.assertTrue(result["ok"], result)
        self.assertFalse(self._read(self.agent / "wl-setup.json")["rmbFooter"])

    def test_install_is_idempotent(self):
        footer_ext.install()
        footer_ext.install()
        packages = self._read(self.agent / "settings.json")["packages"]
        self.assertEqual(packages.count(str(footer_ext._target())), 1)

    def test_missing_asset_reports_error(self):
        with patch.object(footer_ext, "ASSET_DIR", self.home / "nope"):
            result = footer_ext.install()
        self.assertFalse(result["ok"])
        self.assertIn("资源缺失", result["message"])


class ClaudeStatuslineTests(_Base):
    """claude 的 statusLine 也是配置文件 + 一条命令，同样要能装能卸。"""

    def setUp(self):
        FooterExtTests.setUp(self)
        self._write(self.home / ".claude" / "settings.json", {"env": {"X": "1"}})
        mocked = patch.object(footer_ext, "_state_file",
                              lambda: self.home / ".aac" / "footer-state.json")
        mocked.start()
        self.addCleanup(mocked.stop)

    def test_install_writes_script_and_points_statusline_at_it(self):
        result = footer_ext.claude_install(self.home)
        self.assertTrue(result["ok"], result)
        script = footer_ext._render_script()
        self.assertTrue(script.is_file())
        self.assertIn("app.footer --stdin", script.read_text(encoding="utf-8"))
        cfg = self._read(self.home / ".claude" / "settings.json")
        self.assertEqual(cfg["statusLine"]["type"], "command")
        self.assertIn(str(script), cfg["statusLine"]["command"])
        self.assertEqual(cfg["env"]["X"], "1", "别的设置不能被改掉")
        self.assertTrue(footer_ext.claude_status()["installed"])

    def test_install_keeps_previous_statusline_for_rollback(self):
        self._write(self.home / ".claude" / "settings.json",
                    {"statusLine": {"type": "command", "command": "my-own-status"}})
        footer_ext.claude_install(self.home)
        footer_ext.claude_uninstall()
        cfg = self._read(self.home / ".claude" / "settings.json")
        self.assertEqual(cfg["statusLine"]["command"], "my-own-status")

    def test_uninstall_removes_key_when_there_was_none(self):
        footer_ext.claude_install(self.home)
        footer_ext.claude_uninstall()
        cfg = self._read(self.home / ".claude" / "settings.json")
        self.assertNotIn("statusLine", cfg)
        self.assertFalse(footer_ext._render_script().exists())

    def test_install_sets_claude_default_template_once(self):
        from app import footer

        with patch.object(footer, "CONFIG_FILE", self.home / ".aac" / "footer.json"), \
             patch.object(footer, "CONFIG_DIR", self.home / ".aac"), \
             patch.object(footer, "config_for", footer.config_for):
            footer_ext.claude_install(self.home)
            cfg = json.loads((self.home / ".aac" / "footer.json").read_text(encoding="utf-8"))
            lines = cfg["agents"]["claude"]["lines"]
            self.assertEqual(lines[0][0], "model")
            # 已有覆盖不覆盖
            cfg["agents"]["claude"]["lines"] = [["cpu"]]
            (self.home / ".aac" / "footer.json").write_text(json.dumps(cfg), encoding="utf-8")
            footer_ext.claude_install(self.home)
            again = json.loads((self.home / ".aac" / "footer.json").read_text(encoding="utf-8"))
            self.assertEqual(again["agents"]["claude"]["lines"], [["cpu"]])


if __name__ == "__main__":
    unittest.main()
