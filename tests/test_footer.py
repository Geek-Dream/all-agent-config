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
        lines = footer.render_lines()
        self.assertEqual(len(lines), 3)
        self.assertIn("deepseek-v4.1-flash", lines[0])
        self.assertIn("[high]", lines[0])
        self.assertIn("内存 62%", lines[1])
        self.assertIn("已用 tokens：88,528", lines[2])

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
