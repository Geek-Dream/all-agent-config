import json
import os
import tempfile
import tomllib
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from app import bundle, providers


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aac-prov-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        env = patch.dict(os.environ, {"HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        store = patch.object(providers, "STORE", self.home / ".aac" / "providers.json")
        store.start()
        self.addCleanup(store.stop)

    def write(self, rel: str, text: str) -> Path:
        path = self.home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_save_masks_key_and_persists_private(self):
        item = providers.save("claude-code", {
            "name": "Kimi", "base_url": "https://api.kimi.com",
            "api_key": "sk-secret-123456", "model": "kimi-k2"})
        self.assertTrue(item["api_key_set"])
        self.assertEqual(item["api_key_hint"], "…3456")
        self.assertNotIn("api_key", item)
        stored = json.loads(providers.STORE.read_text(encoding="utf-8"))
        raw = stored["agents"]["claude-code"][0]
        self.assertEqual(raw["api_key"], "sk-secret-123456")
        if os.name != "nt":   # Windows 不强制执行 POSIX 权限位
            self.assertEqual(providers.STORE.stat().st_mode & 0o777, 0o600)

    def test_save_keeps_existing_key_when_omitted(self):
        first = providers.save("claude-code", {
            "name": "Kimi", "base_url": "https://a", "api_key": "sk-keep-1234"})
        providers.save("claude-code", {"id": first["id"], "name": "Kimi",
                                       "base_url": "https://b"})
        stored = json.loads(providers.STORE.read_text(encoding="utf-8"))
        raw = stored["agents"]["claude-code"][0]
        self.assertEqual(raw["api_key"], "sk-keep-1234")
        self.assertEqual(raw["base_url"], "https://b")

    def test_rejects_bad_url_and_auth_field(self):
        for payload in (
            {"name": "x", "base_url": "ftp://nope"},
            {"name": "x", "extra": {"auth_field": "ANTHROPIC_NOPE"}},
            {"name": "  "},
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                providers.save("claude-code", payload)

    def test_claude_activate_writes_env_and_preserves_other_settings(self):
        settings = self.write(".claude/settings.json", json.dumps(
            {"hooks": {"Stop": [{"x": 1}]}, "env": {"KEEP": "me"}}))
        item = providers.save("claude-code", {
            "name": "中转", "base_url": "https://relay.example",
            "api_key": "sk-abc12345", "model": "claude-x"})
        result = providers.activate("claude-code", item["id"])
        self.assertEqual(result["current"], item["id"])
        data = json.loads(settings.read_text(encoding="utf-8"))
        self.assertEqual(data["env"]["ANTHROPIC_BASE_URL"], "https://relay.example")
        self.assertEqual(data["env"]["ANTHROPIC_AUTH_TOKEN"], "sk-abc12345")
        self.assertEqual(data["env"]["ANTHROPIC_MODEL"], "claude-x")
        self.assertEqual(data["env"]["KEEP"], "me")
        self.assertEqual(data["hooks"], {"Stop": [{"x": 1}]})
        self.assertEqual(len(list(self.home.glob(".claude/settings.json.bak-aac-*"))), 1)
        self.assertEqual(providers.detail(
            providers.registry.get_agent("claude-code"))["current"], item["id"])

    def test_codex_activate_writes_provider_table(self):
        config = self.write(".codex/config.toml",
                            'model = "old"\n\n[mcp_servers.x]\ncommand = "y"\n')
        item = providers.save("codex", {
            "name": "DeepSeek", "base_url": "https://api.deepseek.com",
            "api_key": "sk-ds-12345678", "model": "deepseek-chat"})
        providers.activate("codex", item["id"])
        text = config.read_text(encoding="utf-8")
        data = tomllib.loads(text)
        slug = providers._slug(item["id"])
        self.assertEqual(data["model_provider"], slug)
        self.assertEqual(data["model"], "deepseek-chat")
        self.assertEqual(data["model_providers"][slug]["base_url"], "https://api.deepseek.com")
        self.assertEqual(data["model_providers"][slug]["experimental_bearer_token"], "sk-ds-12345678")
        self.assertEqual(data["mcp_servers"]["x"]["command"], "y")

    def test_opencode_activate_adds_coexist_provider(self):
        config = self.write(".config/opencode/opencode.json", json.dumps({"mcp": {}}))
        item = providers.save("opencode", {
            "name": "Kimi", "base_url": "https://api.kimi.com",
            "api_key": "sk-kimi-1234", "model": "kimi-k2"})
        providers.activate("opencode", item["id"])
        data = json.loads(config.read_text(encoding="utf-8"))
        slug = providers._slug(item["id"])
        self.assertEqual(data["provider"][slug]["options"]["baseURL"], "https://api.kimi.com")
        self.assertEqual(data["provider"][slug]["options"]["apiKey"], "sk-kimi-1234")
        self.assertEqual(data["model"], f"{slug}/kimi-k2")
        self.assertEqual(data["mcp"], {})

    def test_unsupported_agent_and_unknown_provider_raise(self):
        self.assertFalse(providers.supported(providers.registry.get_agent("auggie")))
        self.assertRaises(ValueError, providers.activate, "claude-code", "missing")
        self.assertRaises(ValueError, providers.activate, "auggie", "x")

    def test_import_current_claude_reads_existing_config(self):
        self.write(".claude/settings.json", json.dumps({"env": {
            "ANTHROPIC_BASE_URL": "https://euzhi.vip",
            "ANTHROPIC_AUTH_TOKEN": "sk-live-abcdef123456",
            "ANTHROPIC_MODEL": "gpt-5.6-sol"}}))
        item = providers.import_current("claude-code")
        self.assertEqual(item["base_url"], "https://euzhi.vip")
        self.assertEqual(item["model"], "gpt-5.6-sol")
        self.assertTrue(item["api_key_set"])
        self.assertNotIn("api_key", item)
        detail = providers.detail(providers.registry.get_agent("claude-code"))
        self.assertEqual(detail["current"], item["id"])
        stored = json.loads(providers.STORE.read_text(encoding="utf-8"))
        raw = stored["agents"]["claude-code"][0]
        self.assertEqual(raw["api_key"], "sk-live-abcdef123456")
        self.assertEqual(raw["extra"]["auth_field"], "ANTHROPIC_AUTH_TOKEN")

    def test_import_current_is_idempotent_for_same_base_url(self):
        self.write(".claude/settings.json", json.dumps({"env": {
            "ANTHROPIC_BASE_URL": "https://same.example", "ANTHROPIC_AUTH_TOKEN": "sk-1"}}))
        first = providers.import_current("claude-code")
        second = providers.import_current("claude-code")
        self.assertEqual(first["id"], second["id"])
        stored = json.loads(providers.STORE.read_text(encoding="utf-8"))
        self.assertEqual(len(stored["agents"]["claude-code"]), 1)

    def test_import_current_codex_and_opencode(self):
        self.write(".codex/config.toml",
                   'model = "deepseek-chat"\nmodel_provider = "deepseek"\n\n'
                   '[model_providers.deepseek]\nname = "DeepSeek"\n'
                   'base_url = "https://api.deepseek.com"\nwire_api = "chat"\n'
                   'experimental_bearer_token = "sk-ds-1234"\n')
        codex = providers.import_current("codex")
        self.assertEqual(codex["base_url"], "https://api.deepseek.com")
        self.assertEqual(codex["name"], "DeepSeek")
        self.assertEqual(codex["model"], "deepseek-chat")

        self.write(".config/opencode/opencode.json", json.dumps({
            "model": "kimi/kimi-k2",
            "provider": {"kimi": {"npm": "@ai-sdk/openai-compatible",
                                  "options": {"baseURL": "https://api.kimi.com",
                                              "apiKey": "sk-kimi-9"}}}}))
        oc = providers.import_current("opencode")
        self.assertEqual(oc["base_url"], "https://api.kimi.com")
        self.assertEqual(oc["model"], "kimi-k2")
        self.assertTrue(oc["api_key_set"])

    def test_detail_auto_absorbs_current_config(self):
        self.write(".claude/settings.json", json.dumps({"env": {
            "ANTHROPIC_BASE_URL": "https://auto.example",
            "ANTHROPIC_AUTH_TOKEN": "sk-auto-123456", "ANTHROPIC_MODEL": "m1"}}))
        detail = providers.detail(providers.registry.get_agent("claude-code"))
        self.assertEqual(len(detail["items"]), 1)
        self.assertEqual(detail["items"][0]["base_url"], "https://auto.example")
        self.assertEqual(detail["current"], detail["items"][0]["id"])

    def test_detail_does_not_overwrite_edited_fields(self):
        self.write(".claude/settings.json", json.dumps({"env": {
            "ANTHROPIC_BASE_URL": "https://euzhi.vip",
            "ANTHROPIC_AUTH_TOKEN": "sk-live-123456", "ANTHROPIC_MODEL": "old-model"}}))
        detail = providers.detail(providers.registry.get_agent("claude-code"))
        pid = detail["items"][0]["id"]
        providers.save("claude-code", {"id": pid, "name": "改名了",
                                       "base_url": "https://euzhi.vip",
                                       "model": "new-model", "small_model": ""})
        again = providers.detail(providers.registry.get_agent("claude-code"))
        item = next(i for i in again["items"] if i["id"] == pid)
        self.assertEqual(item["name"], "改名了")
        self.assertEqual(item["model"], "new-model")

    def test_detail_lists_all_native_providers(self):
        self.write(".pi/agent/settings.json", json.dumps(
            {"defaultProvider": "b", "defaultModel": "m2"}))
        self.write(".pi/agent/models.json", json.dumps({"providers": {
            "a": {"baseUrl": "https://a.example", "api": "openai-completions", "apiKey": "sk-a"},
            "b": {"baseUrl": "https://b.example", "api": "openai-responses", "apiKey": "sk-b"},
            "c": {"baseUrl": "https://c.example", "api": "openai-completions", "apiKey": "sk-c"},
        }}))
        detail = providers.detail(providers.registry.get_agent("pi"))
        self.assertEqual(len(detail["items"]), 3)
        cur = next(i for i in detail["items"] if i["id"] == detail["current"])
        self.assertEqual(cur["base_url"], "https://b.example")

    def test_pi_import_and_activate(self):
        self.write(".pi/agent/settings.json", json.dumps(
            {"defaultProvider": "relay", "defaultModel": "m"}))
        self.write(".pi/agent/models.json", json.dumps({"providers": {"relay": {
            "baseUrl": "https://pi.example", "api": "openai-completions",
            "apiKey": "sk-pi-12345678", "models": {"m": {}}}}}))
        item = providers.import_current("pi")
        self.assertEqual(item["base_url"], "https://pi.example")
        self.assertEqual(item["model"], "m")
        self.assertEqual(item["extra"]["pi_api"], "openai-completions")

        new = providers.save("pi", {"name": "Other", "base_url": "https://other.example",
                                    "api_key": "sk-other-9999", "model": "gpt",
                                    "extra": {"pi_api": "openai-responses"}})
        providers.activate("pi", new["id"])
        slug = providers._slug(new["id"])
        models = json.loads((self.home / ".pi/agent/models.json").read_text())
        self.assertEqual(models["providers"][slug]["baseUrl"], "https://other.example")
        self.assertEqual(models["providers"][slug]["api"], "openai-responses")
        settings = json.loads((self.home / ".pi/agent/settings.json").read_text())
        self.assertEqual(settings["defaultProvider"], slug)
        self.assertEqual(settings["defaultModel"], "gpt")

    def test_import_current_raises_when_nothing_configured(self):
        self.assertRaises(ValueError, providers.import_current, "claude-code")
        self.assertRaises(ValueError, providers.import_current, "auggie")

    def test_reveal_returns_plaintext_key(self):
        pid = self._probe_id(base_url="https://reveal.example")
        self.assertEqual(providers.reveal("claude-code", pid), "sk-12345678")
        listed = json.dumps(providers.detail(providers.registry.get_agent("claude-code")))
        self.assertNotIn("sk-12345678", listed)

    def test_reveal_unknown_provider(self):
        self.assertRaises(ValueError, providers.reveal, "claude-code", "nope")

    def _probe_id(self, agent="claude-code", base_url="https://relay.example"):
        return providers.save(agent, {"name": "P", "base_url": base_url,
                                      "api_key": "sk-12345678"})["id"]

    @staticmethod
    def _resp(status):
        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        Resp.status = status
        return Resp()

    def test_probe_success_uses_anthropic_path_and_header(self):
        pid = self._probe_id()
        seen = {}

        def fake(req, timeout=None):
            seen["url"] = req.full_url
            seen["headers"] = {k.lower(): v for k, v in req.header_items()}
            return self._resp(200)

        with patch.object(providers.urllib.request, "urlopen", fake):
            out = providers.probe("claude-code", pid)
        self.assertTrue(out["ok"])
        self.assertEqual(out["url"], "https://relay.example/v1/models")
        self.assertEqual(seen["headers"]["x-api-key"], "sk-12345678")
        self.assertIsNone(seen["headers"].get("authorization"))

    def test_probe_openai_compatible_uses_bearer(self):
        pid = self._probe_id("codex", "https://api.deepseek.com")
        seen = {}

        def fake(req, timeout=None):
            seen["url"] = req.full_url
            seen["headers"] = {k.lower(): v for k, v in req.header_items()}
            return self._resp(200)

        with patch.object(providers.urllib.request, "urlopen", fake):
            out = providers.probe("codex", pid)
        self.assertEqual(out["url"], "https://api.deepseek.com/models")
        self.assertEqual(seen["headers"]["authorization"], "Bearer sk-12345678")

    def test_probe_http_and_network_errors(self):
        import urllib.error

        pid = self._probe_id()
        err = urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)
        with patch.object(providers.urllib.request, "urlopen", side_effect=err):
            out = providers.probe("claude-code", pid)
        self.assertFalse(out["ok"])
        self.assertEqual(out["status"], 401)
        with patch.object(providers.urllib.request, "urlopen",
                          side_effect=urllib.error.URLError("boom")):
            out = providers.probe("claude-code", pid)
        self.assertFalse(out["ok"])
        self.assertIsNone(out["status"])
        self.assertIn("无法连接", out["detail"])

    def test_probe_requires_base_url(self):
        item = providers.save("claude-code", {"name": "NoUrl"})
        self.assertRaises(ValueError, providers.probe, "claude-code", item["id"])
        self.assertRaises(ValueError, providers.probe, "claude-code", "missing")

    @staticmethod
    def _json_resp(payload, status=200):
        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps(payload).encode()

        Resp.status = status
        return Resp()

    def test_fetch_models_parses_openai_and_gemini_shapes(self):
        pid = self._probe_id("codex", "https://api.deepseek.com")
        seen = {}

        def fake(req, timeout=None, payload=None):
            seen["url"] = req.full_url
            return self._json_resp(payload)

        with patch.object(providers.urllib.request, "urlopen",
                          lambda r, timeout=None: fake(r, payload={
                              "data": [{"id": "deepseek-chat"}, {"id": "deepseek-reasoner"}]})):
            out = providers.fetch_models("codex", pid)
        self.assertEqual(out["models"], ["deepseek-chat", "deepseek-reasoner"])
        self.assertEqual(seen["url"], "https://api.deepseek.com/models")

        with patch.object(providers.urllib.request, "urlopen",
                          lambda r, timeout=None: fake(r, payload={
                              "models": [{"name": "models/gemini-pro"}, "gemini-flash"]})):
            out = providers.fetch_models("codex", pid)
        self.assertEqual(out["models"], ["models/gemini-pro", "gemini-flash"])

    def test_fetch_models_explicit_key_and_errors(self):
        seen = {}

        def fake(req, timeout=None):
            seen["url"] = req.full_url
            seen["headers"] = {k.lower(): v for k, v in req.header_items()}
            return self._json_resp({"data": [{"id": "m1"}]})

        with patch.object(providers.urllib.request, "urlopen", fake):
            out = providers.fetch_models("claude-code", None, "https://relay.example", "sk-new")
        self.assertEqual(out["models"], ["m1"])
        self.assertEqual(seen["url"], "https://relay.example/v1/models")
        self.assertEqual(seen["headers"]["x-api-key"], "sk-new")

        self.assertRaises(ValueError, providers.fetch_models, "claude-code", None, "")

        import urllib.error
        err = urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)
        with patch.object(providers.urllib.request, "urlopen", side_effect=err):
            out = providers.fetch_models("claude-code", None, "https://relay.example", "sk")
        self.assertEqual(out["models"], [])
        self.assertIn("401", out["detail"])

    def test_export_and_import_round_trip(self):
        providers.save("claude-code", {"name": "A", "base_url": "https://a",
                                       "api_key": "sk-a-123456"})
        payload = providers.export_payload("claude-code")
        self.assertEqual(len(payload["items"]), 1)
        providers.STORE.unlink()
        added = providers.import_payload("claude-code", payload)
        self.assertEqual(added, 1)
        items = providers.detail(providers.registry.get_agent("claude-code"))["items"]
        self.assertEqual(items[0]["name"], "A")

    def test_bundle_exports_and_imports_providers(self):
        providers.save("claude-code", {"name": "A", "base_url": "https://a",
                                       "api_key": "sk-a-123456"})
        empty_detail = {"categories": {}, "patches": None, "agent_version": "1.0"}
        with patch.object(bundle, "HOME", self.home), \
                patch.object(bundle, "EXPORTS_DIR", self.home / "exports"), \
                patch.object(bundle.categories, "agent_detail", return_value=empty_detail):
            path = bundle.export_bundle("claude-code", ["providers"])
            with zipfile.ZipFile(path) as zf:
                manifest = json.loads(zf.read("manifest.json"))
            self.assertEqual(manifest["providers"]["count"], 1)
            providers.STORE.unlink()
            self.assertIn("providers", bundle.import_preview(path)["conflicts"])
            log = []
            bundle.import_apply(path, ["providers"], log)
        items = providers.detail(providers.registry.get_agent("claude-code"))["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["name"], "A")
        self.assertTrue(any("providers" in line for line in log))


if __name__ == "__main__":
    unittest.main()
