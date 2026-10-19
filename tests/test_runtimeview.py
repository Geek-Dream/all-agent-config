import json
import tempfile
import unittest
from pathlib import Path

from app import configview, runtimeview


class OpenCodeRuntimeViewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aac-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "opencode.json"
        self.auth = self.root / "auth.json"
        self.agent = {
            "id": "opencode",
            "name": "OpenCode",
            "runtime": {
                "type": "opencode",
                "config_paths": [str(self.config)],
                "auth_paths": [str(self.auth)],
            },
        }

    def write(self, path, data):
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_provider_model_and_credentials_are_visible_without_secrets(self):
        provider_secret = "provider-secret-value"
        auth_secret = "auth-secret-value"
        self.write(self.config, {
            "model": "kimi/kimi-k2.6",
            "provider": {
                "deepseek": {
                    "options": {
                        "apiKey": provider_secret,
                        "baseURL": "https://user:pass@api.example.test/v1?token=hidden",
                    },
                    "models": {"deepseek-v4-pro": {}, "deepseek-v4-flash": {}},
                },
                "kimi": {
                    "options": {
                        "apiKey": "{env:KIMI_API_KEY}",
                        "baseURL": "https://kimi.example.test/v1",
                    },
                    "models": {"kimi-k2.6": {}},
                },
            },
        })
        self.write(self.auth, {
            "alibaba": {"type": "api", "key": auth_secret},
        })

        items = runtimeview.items_for_agent(self.agent)
        rendered = json.dumps(items, ensure_ascii=False)

        self.assertIn("kimi/kimi-k2.6", rendered)
        self.assertIn("https://api.example.test/v1", rendered)
        self.assertIn("当前使用", rendered)
        self.assertIn("API Key 已配置（值已隐藏）", rendered)
        self.assertIn("环境变量 KIMI_API_KEY", rendered)
        self.assertIn("alibaba", rendered)
        self.assertNotIn(provider_secret, rendered)
        self.assertNotIn(auth_secret, rendered)
        self.assertNotIn("user:pass", rendered)
        self.assertNotIn("token=hidden", rendered)

    def test_unknown_runtime_and_missing_files_are_safe(self):
        self.assertEqual(
            runtimeview.items_for_agent({"runtime": {"type": "unknown"}}),
            [],
        )
        items = runtimeview.items_for_agent(self.agent)
        self.assertEqual(items[0]["name"], "OpenCode 配置")
        self.assertFalse(items[0]["enabled"])
        self.assertEqual(items[-1]["name"], "API 凭据")
        self.assertFalse(items[-1]["enabled"])

    def test_config_detail_exposes_read_only_runtime_group(self):
        self.write(self.config, {"model": "kimi/kimi-k2.6", "provider": {}})
        self.write(self.auth, {"alibaba": {"type": "api", "key": "hidden"}})
        detail = configview.list_config(self.agent)
        group = next(g for g in detail["groups"] if g["key"] == "runtime")
        self.assertEqual(group["label"], "运行配置")
        self.assertTrue(all(not item["toggleable"] for item in group["items"]))


if __name__ == "__main__":
    unittest.main()
