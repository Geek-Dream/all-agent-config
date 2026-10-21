import json
import tempfile
import unittest
from pathlib import Path

from app import checks


class HookAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aac-checks-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.cfg = self.home / ".demo"
        (self.cfg / "hooks").mkdir(parents=True)
        self.agent = {"id": "demo", "config_dirs": [str(self.cfg)]}

    def write(self, rel, text):
        path = self.cfg / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_macos_only_command_and_bash_shebang_are_reported(self):
        self.write("hooks/on-save.sh", "#!/bin/zsh\nosascript -e 'display dialog 1'\n")
        issues = checks.audit_hooks(self.agent, source_os="macOS",
                                    local_home=str(self.home))
        reasons = {i["issue"] for i in issues}
        self.assertIn("依赖 bash/zsh", reasons)
        self.assertIn("macOS 专有命令", reasons)
        for item in issues:
            self.assertTrue(item["file"].endswith("on-save.sh"))
            self.assertTrue(item["advice"])
        self.assertEqual(issues[0]["line"], 1)

    def test_path_to_another_machine_is_reported(self):
        self.write("hooks/copy.sh", "cp /Users/someone-else/data.json .\n")
        issues = checks.audit_hooks(self.agent, local_home=str(self.home))
        self.assertTrue(any("别的机器" in i["issue"] for i in issues))

    def test_path_to_current_user_is_not_reported(self):
        current = Path.home().name
        self.write("hooks/copy.sh", f"cp /Users/{current}/data.json .\n")
        issues = checks.audit_hooks(self.agent, local_home=str(Path.home()))
        self.assertEqual(issues, [])

    def test_commands_inside_json_hooks_are_reported(self):
        (self.cfg / "settings.json").write_text(json.dumps({
            "hooks": {"PreToolUse": [{"matcher": "*",
                                      "hooks": [{"type": "command", "command": "/bin/zsh -lc 'echo hi'"}]}]}
        }), encoding="utf-8")
        issues = checks.audit_hooks(self.agent, local_home=str(self.home))
        self.assertTrue(any("依赖 bash/zsh" in i["issue"] for i in issues))

    def test_clean_script_produces_nothing(self):
        self.write("hooks/ok.sh", "#!/usr/bin/env python3\nprint('hi')\n")
        self.assertEqual(checks.audit_hooks(self.agent, local_home=str(self.home)), [])

    def test_format_warnings_caps_the_list(self):
        issues = [{"file": f"/tmp/{i}.sh", "line": i, "command": "osascript",
                   "issue": "macOS 专有命令", "advice": "换掉"} for i in range(20)]
        lines = checks.format_warnings(issues, limit=3)
        text = "".join(lines)
        self.assertIn("还有 17 条", text)
        self.assertEqual(text.count("⚠"), 3)


if __name__ == "__main__":
    unittest.main()
