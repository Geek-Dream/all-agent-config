import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from app import engines, env


class VersionTests(unittest.TestCase):
    def test_parse_version(self):
        self.assertEqual(engines.parse_version("22.19.0"), (22, 19, 0))
        self.assertEqual(engines.parse_version("v22.19"), (22, 19, 0))
        self.assertEqual(engines.parse_version("22"), (22, 0, 0))
        self.assertIsNone(engines.parse_version("nope"))

    def test_caret_or(self):
        rng = "^22.19.0 || >=24.0.0"
        self.assertTrue(engines.satisfies("22.19.0", rng))
        self.assertFalse(engines.satisfies("22.18.0", rng))
        self.assertTrue(engines.satisfies("24.1.0", rng))

    def test_comparators_and_and(self):
        self.assertFalse(engines.satisfies("22.11.0", ">=22.19.0"))
        self.assertTrue(engines.satisfies("22.19.0", ">=22.19.0 <23"))
        self.assertFalse(engines.satisfies("23.0.0", ">=22.19.0 <23"))
        self.assertTrue(engines.satisfies("18.0.0", ">=16"))

    def test_tilde_and_zero_major(self):
        self.assertTrue(engines.satisfies("22.1.5", "~22.1.0"))
        self.assertFalse(engines.satisfies("22.2.0", "~22.1.0"))
        self.assertTrue(engines.satisfies("0.2.1", "^0.2.0"))
        self.assertFalse(engines.satisfies("0.3.0", "^0.2.0"))

    def test_partial_and_wildcard(self):
        self.assertTrue(engines.satisfies("22.9.0", "22"))
        self.assertFalse(engines.satisfies("23.0.0", "22"))
        self.assertTrue(engines.satisfies("1.2.3", "*"))

    def test_unparseable_is_unknown(self):
        self.assertIsNone(engines.satisfies("22.1.0", "not-a-range"))
        self.assertIsNone(engines.satisfies(None, ">=22"))


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name) / "engines.json"
        patcher = mock.patch.object(engines, "CACHE_PATH", self.cache)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_save_and_load(self):
        engines.save_cache({"pkg": {"range": ">=22", "at": time.time()}})
        self.assertEqual(engines.load_cache()["pkg"]["range"], ">=22")

    def test_corrupt_cache_is_empty(self):
        self.cache.write_text("{", encoding="utf-8")
        self.assertEqual(engines.load_cache(), {})

    def test_refresh_keeps_old_value_on_failure(self):
        agents = [{"id": "x", "sources": {"npm": "@scope/x"}}]
        engines.save_cache({"@scope/x": {"range": ">=18", "at": 0}})
        with mock.patch.object(engines, "_fetch_range", return_value=None):
            engines.refresh(agents)
        self.assertEqual(engines.load_cache()["@scope/x"]["range"], ">=18")

    def test_refresh_updates_stale_entry(self):
        agents = [{"id": "x", "sources": {"npm": "@scope/x"}}]
        engines.save_cache({"@scope/x": {"range": ">=18", "at": 0}})
        with mock.patch.object(engines, "_fetch_range", return_value=">=22.19"):
            engines.refresh(agents)
        entry = engines.load_cache()["@scope/x"]
        self.assertEqual(entry["range"], ">=22.19")
        self.assertGreater(entry["at"], 0)

    def test_check_flags_old_node(self):
        agents = [{"id": "x", "sources": {"npm": "@scope/x"}}]
        engines.save_cache({"@scope/x": {"range": ">=22.19", "at": time.time()}})
        with mock.patch.object(engines, "node_version", return_value="22.11.0"):
            result = engines.check(agents[0])
        self.assertEqual(result, {"required": ">=22.19", "have": "22.11.0", "ok": False})

    def test_check_without_cache(self):
        with mock.patch.object(engines, "node_version", return_value="22.19.0"):
            self.assertIsNone(engines.check({"sources": {"npm": "@nope/none"}}))


class EnvTests(unittest.TestCase):
    def test_env_includes_python_git_system(self):
        data = env.check_env()
        for key in ("git", "python", "system"):
            self.assertIn(key, data)
        self.assertTrue(data["python"]["installed"])
        self.assertFalse(data["python"]["auto_install"])
        self.assertTrue(data["system"]["installed"])
        self.assertIn(data["git"]["label"], ("Git",))

    def test_git_install_hint(self):
        argv, hint = env.install_argv("git")
        self.assertTrue(argv is not None or hint)
