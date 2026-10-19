import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path, PureWindowsPath
from unittest.mock import patch

from app import ccswitch, providers


def _make_db(path: Path, rows: list) -> None:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE providers (app_type TEXT, name TEXT, is_current INT, "
                "settings_config TEXT, sort_index INT)")
    con.executemany(
        "INSERT INTO providers (app_type,name,is_current,settings_config,sort_index) "
        "VALUES (?,?,?,?,?)", rows)
    con.commit()
    con.close()


class CCSwitchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aac-ccs-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        env = patch.dict(os.environ, {"HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        store = patch.object(providers, "STORE", self.home / ".aac" / "providers.json")
        store.start()
        self.addCleanup(store.stop)
        self.db = self.home / "cc-switch.db"

    def test_scan_maps_all_app_types(self):
        _make_db(self.db, [
            ("claude", "亮神", 1, json.dumps({"env": {
                "ANTHROPIC_BASE_URL": "https://a.example", "ANTHROPIC_AUTH_TOKEN": "sk-a",
                "ANTHROPIC_MODEL": "m1"}}), 0),
            ("codex", "DeepSeek", 0, json.dumps({
                "auth": {"OPENAI_API_KEY": "sk-c"},
                "config": 'model = "gpt-5"\nmodel_provider = "ds"\n\n'
                          '[model_providers.ds]\nbase_url = "https://b.example/v1"\n'
                          'wire_api = "responses"\n'}), 1),
            ("openclaw", "自用100", 0, json.dumps({
                "baseUrl": "https://c.example", "apiKey": "sk-o", "api": "openai-completions",
                "models": [{"id": "mc"}]}), 2),
            ("gemini", "Google", 0, json.dumps({"env": {}, "config": {}}), 3),
        ])
        out = ccswitch.scan(str(self.db))
        self.assertTrue(out["exists"])
        self.assertIsNone(out["error"])
        by = {(e["app_type"], e["name"]): e for e in out["entries"]}
        self.assertEqual(by[("claude", "亮神")]["base_url"], "https://a.example")
        self.assertTrue(by[("claude", "亮神")]["current"])
        self.assertTrue(by[("claude", "亮神")]["supported"])
        self.assertEqual(by[("codex", "DeepSeek")]["base_url"], "https://b.example/v1")
        self.assertEqual(by[("codex", "DeepSeek")]["model"], "gpt-5")
        self.assertEqual(by[("openclaw", "自用100")]["agent_id"], "openclaw")
        self.assertTrue(by[("openclaw", "自用100")]["supported"])
        self.assertFalse(by[("gemini", "Google")]["supported"])

    def test_import_selected_writes_store_only(self):
        _make_db(self.db, [
            ("claude", "亮神", 1, json.dumps({"env": {
                "ANTHROPIC_BASE_URL": "https://a.example", "ANTHROPIC_AUTH_TOKEN": "sk-a"}}), 0),
            ("claude", "河图", 0, json.dumps({"env": {
                "ANTHROPIC_BASE_URL": "https://b.example", "ANTHROPIC_AUTH_TOKEN": "sk-b"}}), 1),
        ])
        before = self.db.stat().st_mtime_ns
        out = ccswitch.import_selected(str(self.db), [{"app_type": "claude", "name": "河图"}])
        self.assertEqual(out["imported"], 1)
        self.assertEqual(self.db.stat().st_mtime_ns, before)   # cc-switch 未被改动
        items = json.loads(providers.STORE.read_text())["agents"]["claude-code"]
        self.assertEqual([i["name"] for i in items], ["河图"])
        self.assertEqual(items[0]["base_url"], "https://b.example")

    def test_import_skips_unsupported(self):
        _make_db(self.db, [("gemini", "Google", 0, json.dumps({"env": {}}), 0)])
        out = ccswitch.import_selected(str(self.db), [{"app_type": "gemini", "name": "Google"}])
        self.assertEqual(out["imported"], 0)

    def test_missing_db(self):
        out = ccswitch.scan(str(self.home / "nope.db"))
        self.assertFalse(out["exists"])

    def test_sqlite_uri_is_windows_safe(self):
        win = PureWindowsPath(r"C:\Users\John Doe\.cc-switch\cc-switch.db")
        self.assertEqual(
            ccswitch._sqlite_uri(win),
            "file:C:/Users/John%20Doe/.cc-switch/cc-switch.db?mode=ro")
        posix = Path("/Users/wl/.cc-switch/cc-switch.db")
        self.assertEqual(
            ccswitch._sqlite_uri(posix),
            "file:/Users/wl/.cc-switch/cc-switch.db?mode=ro")


class OpenClawAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aac-oc-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        env = patch.dict(os.environ, {"HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        store = patch.object(providers, "STORE", self.home / ".aac" / "providers.json")
        store.start()
        self.addCleanup(store.stop)

    def test_scan_and_activate(self):
        path = self.home / ".openclaw" / "openclaw.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "models": {"providers": {
                "mimo": {"baseUrl": "https://m.example", "apiKey": "sk-m",
                         "api": "openai-completions", "models": [{"id": "mimo-v2"}]}}},
            "agents": {"defaults": {"model": {"primary": "mimo/mimo-v2"}}},
        }))
        agent = providers.registry.get_agent("openclaw")
        self.assertTrue(providers.supported(agent))
        detail = providers.detail(agent)
        self.assertEqual(len(detail["items"]), 1)
        self.assertEqual(detail["current"], detail["items"][0]["id"])
        self.assertEqual(detail["items"][0]["base_url"], "https://m.example")

        new = providers.save("openclaw", {"name": "kimi", "base_url": "https://k.example",
                                          "api_key": "sk-k", "model": "k2",
                                          "extra": {"openclaw_api": "openai-responses"}})
        providers.activate("openclaw", new["id"])
        data = json.loads(path.read_text())
        slug = providers._slug(new["id"])
        self.assertEqual(data["models"]["providers"][slug]["baseUrl"], "https://k.example")
        self.assertEqual(data["agents"]["defaults"]["model"]["primary"], f"{slug}/k2")


if __name__ == "__main__":
    unittest.main()
