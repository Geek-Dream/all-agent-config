import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import footer


class FooterConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aac-footer-")
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.file = self.dir / "footer.json"
        for target, name, value in ((footer, "CONFIG_DIR", self.dir),
                                    (footer, "CONFIG_FILE", self.file)):
            mocked = patch.object(target, name, value)
            mocked.start()
            self.addCleanup(mocked.stop)

    def test_default_config_renders_three_lines_like_before(self):
        """默认模板 = 老 wl-setup 那个样子：目录 · 🌿分支 · 进度条 · 花费。"""
        lines = footer.render_lines()
        self.assertEqual(len(lines), 3)
        self.assertIn("🌿 main", lines[0])
        self.assertIn("▰▰▰▱▱▱▱▱▱▱ 26.4% 规划", lines[0])
        self.assertIn("≈¥0.42", lines[0])
        self.assertIn("deepseek-v4.1-flash", lines[1])
        self.assertIn("[high]", lines[1])
        self.assertIn("内存 62%", lines[1])
        self.assertIn("已用 tokens：88,528", lines[2])

    def test_context_bar_stages(self):
        """四档：规划 / 编码 / 拥挤 / 告急，未知时如实写 ? 未知。"""
        cases = {10: "规划", 55: "编码", 75: "拥挤", 95: "告急"}
        for pct, label in cases.items():
            with self.subTest(pct=pct):
                self.assertIn(label, footer.render_tag("context", {"ctx_pct": pct}))
        self.assertEqual(footer.render_tag("context", {"ctx_pct": None}),
                         "▱" * 10 + " ? 未知")

    def test_every_tag_renders_something_with_sample_stats(self):
        for tag in footer.TAG_ORDER:
            with self.subTest(tag=tag):
                self.assertTrue(footer.render_tag(tag, footer.sample_stats()).strip())

    def test_separator_switch_changes_only_the_joiner(self):
        base = {"lines": [["model", "cpu"]]}
        for key, glyph in footer.SEPARATORS.items():
            with self.subTest(separator=key):
                line = footer.render_lines({**base, "separator": key})[0]
                self.assertIn(f" {glyph} ", line)

    def test_unknown_tags_and_bad_shapes_are_dropped(self):
        cfg = footer.normalize({"lines": [["nope", "model", "model"], "junk", ["cpu"]],
                                "separator": "??", "enabled": "yes"})
        self.assertEqual(cfg["lines"], [["model"], ["cpu"]])
        self.assertEqual(cfg["separator"], "bar")
        self.assertTrue(cfg["enabled"])
        self.assertEqual(footer.normalize(None)["lines"], footer.DEFAULT_CONFIG["lines"])

    def test_missing_config_falls_back_to_default_template(self):
        self.assertFalse(self.file.exists())
        self.assertEqual(footer.load_config()["lines"], footer.DEFAULT_CONFIG["lines"])

    def test_corrupt_config_does_not_crash(self):
        self.file.write_text("{ not json", encoding="utf-8")
        self.assertEqual(footer.load_config()["separator"], "bar")
        self.assertTrue(footer.render_lines())

    def test_save_round_trip_and_atomic_write(self):
        saved = footer.save_config({"separator": "dot",
                                    "lines": [["model", "git", "cwd"]]})
        self.assertEqual(saved["separator"], "dot")
        on_disk = json.loads(self.file.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["lines"], [["model", "git", "cwd"]])
        self.assertFalse(self.file.with_suffix(".json.part").exists())

    def test_empty_tag_does_not_leave_dangling_separator(self):
        line = footer.render_lines({"lines": [["model", "session", "time"]]},
                                   {"model": "m", "session": "", "time": "10:00"})[0]
        self.assertEqual(line, "m │ 10:00")

    def test_per_agent_override_wins(self):
        footer.save_config({"separator": "bar", "lines": [["model"]],
                            "agents": {"claude": {"separator": "slash",
                                                  "lines": [["cpu", "mem"]]}}})
        self.assertEqual(footer.config_for("pi")["lines"], [["model"]])
        claude = footer.config_for("claude")
        self.assertEqual(claude["separator"], "slash")
        self.assertIn("CPU 37% / 内存 62%", footer.render_text(agent_id="claude"))

    def test_catalog_lists_tags_presets_and_preview(self):
        cat = footer.catalog()
        self.assertEqual(len(cat["tags"]), len(footer.TAG_ORDER))
        self.assertEqual({p["id"] for p in cat["presets"]}, set(footer.PRESETS))
        self.assertTrue(cat["preview"])
        self.assertFalse(cat["exists"])

    def test_three_line_limit_and_eight_tag_limit(self):
        cfg = footer.normalize({"lines": [[tag for tag in footer.TAG_ORDER],
                                          ["model"], ["cpu"], ["mem"]]})
        self.assertEqual(len(cfg["lines"]), footer.MAX_LINES)
        self.assertEqual(len(cfg["lines"][0]), footer.MAX_TAGS_PER_LINE)


if __name__ == "__main__":
    unittest.main()


class SlotTests(unittest.TestCase):
    """pi / claude / codex 各存一份：改一个不能动到别的。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aac-footer-slot-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        for name, value in {"CONFIG_FILE": self.home / ".aac" / "footer.json",
                            "CONFIG_DIR": self.home / ".aac"}.items():
            p = patch.object(footer, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_default_slot_writes_global(self):
        full = footer.load_config()
        saved = footer.save_slot(full, "default",
                                 {"separator": "slash", "lines": [["model"]]})
        self.assertEqual(saved["separator"], "slash")
        self.assertEqual(saved["lines"], [["model"]])

    def test_agent_slot_does_not_touch_global(self):
        full = footer.load_config()
        footer.save_slot(full, "default", {"separator": "bar", "lines": [["model", "cpu"]]})
        full = footer.load_config()
        saved = footer.save_slot(full, "pi", {"separator": "dot", "lines": [["cwd"]]})
        self.assertEqual(saved["lines"], [["model", "cpu"]])   # 通用没变
        self.assertEqual(saved["agents"]["pi"]["lines"], [["cwd"]])
        self.assertEqual(footer.slot_config(saved, "pi")["separator"], "dot")
        self.assertEqual(footer.slot_config(saved, "claude")["lines"], [["model", "cpu"]])

    def test_two_agents_keep_their_own_configs(self):
        full = footer.load_config()
        full = footer.save_slot(full, "pi", {"separator": "bar", "lines": [["model"]]})
        full = footer.save_slot(full, "claude", {"separator": "dot", "lines": [["cost"]]})
        self.assertEqual(footer.slot_config(full, "pi")["lines"], [["model"]])
        self.assertEqual(footer.slot_config(full, "claude")["lines"], [["cost"]])
        self.assertEqual(sorted(full["agents"]), ["claude", "pi"])

    def test_catalog_reports_slots_and_overrides(self):
        full = footer.load_config()
        footer.save_slot(full, "codex", {"separator": "bar", "lines": [["time"]]})
        cat = footer.catalog("codex")
        self.assertEqual(cat["slot"], "codex")
        self.assertEqual(cat["overrides"], ["codex"])
        self.assertEqual(cat["editing"]["lines"], [["time"]])
        self.assertIn("default", [s["id"] for s in cat["slots"]])

    def test_unknown_tags_and_shapes_are_dropped(self):
        full = footer.load_config()
        saved = footer.save_slot(full, "pi", {"separator": "nope",
                                              "lines": [["model", "evil", 5]]})
        self.assertEqual(saved["agents"]["pi"]["separator"], "bar")
        self.assertEqual(saved["agents"]["pi"]["lines"], [["model"]])


class HonestyTests(unittest.TestCase):
    """状态栏不许编数据：没有就是没有，不是 0、不是 "?"、不是假的 ¥。"""

    def test_missing_values_render_nothing(self):
        empty = {"tokens": None, "cache_hit": None, "cache_left_ms": None,
                 "speed": None, "mem": None, "cpu": None, "git": "", "session": ""}
        for tag in ("tokens", "cache", "cache_time", "speed", "mem", "cpu", "git", "session"):
            self.assertEqual(footer.render_tag(tag, empty), "", f"{tag} 该省略")

    def test_cost_is_marked_as_estimate(self):
        self.assertEqual(footer.render_tag("cost", {"cost": 5.27}), "≈¥5.27")
        self.assertEqual(footer.render_tag("cost", {"cost": 0}), "¥— 未计价")
        self.assertEqual(footer.render_tag("cost", {"cost": None}), "")

    def test_rate_is_configurable(self):
        self.assertAlmostEqual(footer.normalize({})["cny_per_usd"], footer.RATE_DEFAULT)
        self.assertAlmostEqual(footer.normalize({"cny_per_usd": 7.4})["cny_per_usd"], 7.4)
        # 离谱值回落到默认，不让用户把汇率写成 999
        self.assertAlmostEqual(footer.normalize({"cny_per_usd": 999})["cny_per_usd"], footer.RATE_DEFAULT)
        self.assertAlmostEqual(footer.normalize({"cny_per_usd": "abc"})["cny_per_usd"], footer.RATE_DEFAULT)

    def test_zero_usage_transcript_is_no_data(self):
        """第三方代理会回全 0 用量——那不是“花了 0 元”，是压根没数据。"""
        import json as _json
        with tempfile.TemporaryDirectory(prefix="aac-transcript-") as tmp:
            path = Path(tmp) / "s.jsonl"
            zero = {"type": "assistant", "timestamp": "2026-10-21T07:21:01.769Z",
                    "message": {"usage": {"input_tokens": 0, "output_tokens": 0,
                                          "cache_read_input_tokens": 0,
                                          "cache_creation_input_tokens": 0}}}
            path.write_text(_json.dumps(zero) + "\n", encoding="utf-8")
            self.assertIsNone(footer._last_usage_from_transcript(str(path)))

    def test_real_usage_transcript_gives_tokens(self):
        import json as _json
        with tempfile.TemporaryDirectory(prefix="aac-transcript-") as tmp:
            path = Path(tmp) / "s.jsonl"
            row = {"type": "assistant", "timestamp": "2026-10-21T07:21:01.769Z",
                   "message": {"usage": {"input_tokens": 100, "output_tokens": 900,
                                         "cache_read_input_tokens": 400,
                                         "cache_creation_input_tokens": 0}}}
            path.write_text(_json.dumps(row) + "\n", encoding="utf-8")
            got = footer._last_usage_from_transcript(str(path))
            self.assertEqual(got["tokens"], 1400)
            self.assertAlmostEqual(got["cache_hit"], 80.0)

    def test_claude_payload_uses_real_effort_level(self):
        payload = {"model": {"display_name": "gpt-5.6-sol"}, "effort": {"level": "high"},
                   "workspace": {"current_dir": "/tmp"}, "cost": {"total_cost_usd": 0}}
        stats = footer.stats_from_claude_payload(payload)
        self.assertEqual(stats["thinking"], "high")
        self.assertEqual(stats["model"], "gpt-5.6-sol")
        self.assertIsNone(stats["cost"] or None)   # 0 → 视为未计价
        self.assertIsNone(stats["tokens"])
