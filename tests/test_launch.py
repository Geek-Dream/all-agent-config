import json
import shlex
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml
from fastapi import HTTPException

from app import cloud, launch, main, registry


class RegistryLaunchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.agents = {agent["id"]: agent for agent in registry.load_agents()}

    def test_yaml_launch_blocks_are_declared_for_expected_agents(self):
        self.assertEqual(
            {agent_id for agent_id, agent in self.agents.items() if "launch" in agent},
            {"claude-code", "codex", "pi"},
        )
        parsed = yaml.safe_load(Path("app/agents.yaml").read_text(encoding="utf-8"))
        launches = {agent["id"]: agent["launch"] for agent in parsed["agents"]
                    if "launch" in agent}
        self.assertEqual(launches["codex"]["resume_args"], ["resume", "--last"])
        self.assertEqual(launches["claude-code"]["modes"]["sandbox"]["args"], [
            "--settings", '{"sandbox":{"enabled":true}}',
        ])
        self.assertFalse(launches["pi"]["modes"]["sandbox"]["supported"])
        self.assertFalse(launches["pi"]["modes"]["danger"]["supported"])

    def test_argv_for_each_supported_mode(self):
        for agent_id in ("claude-code", "codex", "pi"):
            agent = self.agents[agent_id]
            with mock.patch.object(launch.detect, "launch_target",
                                   return_value=Path(f"/bin/{agent_id}")):
                for mode_id, mode_spec in agent["launch"]["modes"].items():
                    if mode_spec.get("supported") is False:
                        continue
                    with self.subTest(agent=agent_id, mode=mode_id):
                        expected = [f"/bin/{agent_id}", *mode_spec.get("args", [])]
                        self.assertEqual(launch.build_agent_argv(agent, mode_id, False),
                                         expected)

    def test_unknown_and_unsupported_modes_raise_chinese_errors(self):
        pi = self.agents["pi"]
        with self.assertRaisesRegex(ValueError, "未知启动模式"):
            launch.build_agent_argv(pi, "missing", False)
        with self.assertRaisesRegex(ValueError, "Pi 无内置沙箱"):
            launch.build_agent_argv(pi, "sandbox", False)

    def test_codex_resume_subcommand_follows_global_flags(self):
        with mock.patch.object(launch.detect, "launch_target",
                               return_value=Path("/bin/codex")):
            self.assertEqual(
                launch.build_agent_argv(self.agents["codex"], "review", True),
                ["/bin/codex", "-s", "read-only", "resume", "--last"],
            )


class SettingsTests(unittest.TestCase):
    def test_launch_defaults_merge_with_saved_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings_file = Path(tmp) / "settings.json"
            settings_file.write_text(json.dumps({
                "launch": {"terminal": "kitty", "modes": {"codex": "review"}},
            }), encoding="utf-8")
            with mock.patch.object(cloud, "SETTINGS_FILE", settings_file):
                settings = cloud.load_settings()
        self.assertEqual(settings["launch"], {
            "terminal": "kitty", "custom_terminal": "", "resume": False,
            "cwd": "", "modes": {"codex": "review"},
        })


class TerminalResolutionTests(unittest.TestCase):
    def test_auto_uses_platform_default(self):
        with mock.patch.object(launch.sys, "platform", "darwin"):
            terminal = launch.resolve_terminal({"terminal": "auto"})
        self.assertEqual(terminal["id"], "terminal")
        self.assertEqual(terminal["kind"], "app")
        self.assertTrue(terminal["builtin"])

    def test_custom_app_and_executable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = root / "My Terminal.app"
            app.mkdir()
            executable = root / "my-terminal"
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            executable.chmod(0o755)
            app_terminal = launch.resolve_terminal({
                "terminal": "custom", "custom_terminal": str(app),
            })
            binary_terminal = launch.resolve_terminal({
                "terminal": "custom", "custom_terminal": str(executable),
            })
        self.assertEqual(app_terminal["kind"], "app")
        self.assertEqual(binary_terminal["kind"], "binary")
        self.assertEqual(binary_terminal["path"], str(executable))

    def test_custom_command_and_missing_custom(self):
        with mock.patch.object(launch.shutil, "which", return_value="/usr/bin/fake-term"):
            terminal = launch.resolve_terminal({
                "terminal": "custom", "custom_terminal": "fake-term",
            })
        self.assertEqual(terminal["path"], "/usr/bin/fake-term")
        with self.assertRaisesRegex(ValueError, "自定义终端"):
            launch.resolve_terminal({
                "terminal": "custom", "custom_terminal": "/missing/terminal",
            })


class QuotingTests(unittest.TestCase):
    def test_shell_and_applescript_quote_spaces_quotes_and_dollar(self):
        value = '/tmp/space "quote" $HOME'
        self.assertEqual(shlex.split(launch.shell_quote(value)), [value])
        self.assertEqual(launch.applescript_quote(value),
                         '/tmp/space \\"quote\\" $HOME')


class TerminalInvocationTests(unittest.TestCase):
    def setUp(self):
        self.cwd = Path("/tmp/project with spaces")
        self.argv = ["/opt/agent bin/agent", "--name", 'a "quote" $var']

    def test_darwin_terminal_and_iterm2_use_osascript(self):
        terminal = launch.terminal_invocation(
            {"id": "terminal", "name": "Terminal", "kind": "app",
             "path": "/System/Applications/Utilities/Terminal.app"},
            self.argv, self.cwd, platform="darwin",
        )
        self.assertEqual(terminal["kind"], "osascript")
        self.assertEqual(terminal["argv"][0], "osascript")
        self.assertIn("do script", terminal["script"])
        iterm = launch.terminal_invocation(
            {"id": "iterm2", "name": "iTerm2", "kind": "app",
             "path": "/Applications/iTerm.app"},
            self.argv, self.cwd, platform="darwin",
        )
        self.assertEqual(iterm["kind"], "osascript")
        self.assertIn("create window with default profile command", iterm["script"])

    def test_darwin_other_app_writes_private_command_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            tray_dir = Path(tmp) / "launch scripts"
            invocation = launch.terminal_invocation(
                {"id": "warp", "name": "Warp", "kind": "app",
                 "path": "/Applications/Warp.app"},
                self.argv, self.cwd, platform="darwin", tray_dir=tray_dir,
            )
            script_path = Path(invocation["script"])
            self.assertEqual(invocation["kind"], "open")
            self.assertEqual(invocation["argv"][:3], [
                "open", "-a", "/Applications/Warp.app",
            ])
            self.assertEqual(script_path.suffix, ".command")
            self.assertEqual(script_path.stat().st_mode & 0o777, 0o700)
            self.assertIn("exec", script_path.read_text(encoding="utf-8"))

    def test_windows_terminal_invocations(self):
        for terminal_id, path in (("cmd", "cmd.exe"), ("wt", "wt.exe"),
                                  ("powershell", "powershell.exe")):
            invocation = launch.terminal_invocation(
                {"id": terminal_id, "name": terminal_id, "kind": "binary",
                 "path": path}, self.argv, self.cwd, platform="win32",
            )
            with self.subTest(terminal=terminal_id):
                self.assertEqual(invocation["kind"], "direct")
                self.assertEqual(invocation["argv"][0], path)
                self.assertIn("/k" if terminal_id != "powershell" else "-NoExit",
                              invocation["argv"])
        wt = launch.terminal_invocation(
            {"id": "wt", "name": "Windows Terminal", "kind": "binary",
             "path": "wt.exe"}, self.argv, self.cwd, platform="win32",
        )
        self.assertEqual(wt["argv"][1:3], ["-d", str(self.cwd)])

    def test_linux_gnome_terminal_invocation(self):
        invocation = launch.terminal_invocation(
            {"id": "gnome-terminal", "name": "GNOME Terminal", "kind": "binary",
             "path": "/usr/bin/gnome-terminal"},
            self.argv, self.cwd, platform="linux",
        )
        self.assertEqual(invocation["kind"], "direct")
        self.assertEqual(invocation["argv"][:3], [
            "/usr/bin/gnome-terminal", "--working-directory", str(self.cwd),
        ])
        self.assertEqual(invocation["argv"][3], "--")

    def test_custom_binary_uses_direct_platform_invocation(self):
        custom = {"id": "custom", "name": "custom", "kind": "binary",
                  "path": "/usr/local/bin/my-terminal"}
        linux = launch.terminal_invocation(
            custom, self.argv, self.cwd, platform="linux",
        )
        macos = launch.terminal_invocation(
            custom, self.argv, self.cwd, platform="darwin",
        )
        self.assertEqual(linux["argv"], [
            "/usr/local/bin/my-terminal", "-e", *self.argv,
        ])
        self.assertEqual(macos["argv"], [
            "/usr/local/bin/my-terminal", "--", *self.argv,
        ])


class RunPlanTests(unittest.TestCase):
    def test_direct_spawn_reports_nonzero_exit_and_log_tail(self):
        class FailedProcess:
            def wait(self, timeout):
                return 7

        def fake_popen(command, *, stdout, **kwargs):
            stdout.write(b"terminal failed\n")
            return FailedProcess()

        plan = {
            "_invocation": {"argv": ["fake-terminal", "--", "agent"],
                            "kind": "direct"},
            "_platform": "linux",
            "terminal": "xterm",
            "cwd": "/tmp",
        }
        with mock.patch.object(launch.subprocess, "Popen", side_effect=fake_popen):
            result = launch.run_plan(plan)
        self.assertFalse(result["ok"])
        self.assertIn("退出码 7", result["error"])
        self.assertIn("terminal failed", result["error"])


class LaunchRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_options_include_launch_agents_and_saved_settings(self):
        with mock.patch.object(main.cloud, "load_settings", return_value={
                    "launch": {"terminal": "custom", "custom_terminal": "/tmp/term",
                               "resume": True, "cwd": "/tmp",
                               "modes": {"codex": "review"}},
                }), mock.patch.object(main.launch, "detect_terminals", return_value=[]), \
                mock.patch.object(main.launch, "default_terminal_id",
                                  return_value="terminal"), \
                mock.patch.object(main.detect, "launch_target",
                                  side_effect=lambda agent: Path("/bin") / agent["id"]):
            result = main.api_launch_options()
        self.assertEqual(set(result["agents"]), {"claude-code", "codex", "pi"})
        self.assertEqual(result["default_terminal"], "terminal")
        self.assertEqual(result["settings"]["modes"], {"codex": "review"})
        pi_modes = {mode["id"]: mode for mode in result["agents"]["pi"]["modes"]}
        self.assertFalse(pi_modes["sandbox"]["supported"])
        self.assertEqual(pi_modes["sandbox"]["reason"], "Pi 无内置沙箱")

    async def test_danger_mode_requires_explicit_ack(self):
        agent = registry.get_agent("claude-code")
        with mock.patch.object(main.registry, "get_agent", return_value=agent), \
                mock.patch.object(main.launch, "run_plan") as run_plan:
            with self.assertRaises(HTTPException) as caught:
                await main.api_launch("claude-code", main.LaunchReq(mode="danger"))
        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(caught.exception.detail, "危险模式需要显式确认")
        run_plan.assert_not_called()

    async def test_configured_launch_persists_only_after_success(self):
        agent = registry.get_agent("claude-code")
        with tempfile.TemporaryDirectory() as tmp:
            plan = {
                "terminal": "iterm2", "command": "iTerm2: claude --permission-mode plan",
                "argv": ["/bin/claude", "--permission-mode", "plan"], "cwd": tmp,
            }
            with mock.patch.object(main.registry, "get_agent", return_value=agent), \
                    mock.patch.object(main.cloud, "load_settings", return_value={
                        "launch": {"terminal": "auto", "custom_terminal": "",
                                   "resume": False, "cwd": "",
                                   "modes": {"codex": "sandbox"}},
                    }), mock.patch.object(main.launch, "plan_launch",
                                          return_value=plan) as plan_launch, \
                    mock.patch.object(main.launch, "run_plan",
                                      return_value={"ok": True, "error": None}), \
                    mock.patch.object(main.cloud, "save_settings") as save_settings:
                result = await main.api_launch("claude-code", main.LaunchReq(
                    mode="review", resume=True, terminal="iterm2", cwd=tmp,
                ))
        self.assertEqual(result, {
            "ok": True, "terminal": "iterm2", "command": plan["command"],
            "argv": plan["argv"], "cwd": tmp,
        })
        plan_launch.assert_called_once_with(
            agent, "review", True,
            {"terminal": "iterm2", "custom_terminal": "", "resume": False,
             "cwd": tmp, "modes": {"codex": "sandbox"}},
            tmp,
        )
        save_settings.assert_called_once_with({"launch": {
            "terminal": "iterm2", "custom_terminal": "", "resume": True,
            "cwd": tmp,
            "modes": {"codex": "sandbox", "claude-code": "review"},
        }})

    async def test_failed_launcher_returns_real_error_without_persisting(self):
        agent = registry.get_agent("claude-code")
        plan = {"terminal": "terminal", "command": "Terminal", "argv": ["claude"],
                "cwd": "/tmp"}
        with mock.patch.object(main.registry, "get_agent", return_value=agent), \
                mock.patch.object(main.cloud, "load_settings", return_value={"launch": {}}), \
                mock.patch.object(main.launch, "plan_launch", return_value=plan), \
                mock.patch.object(main.launch, "run_plan", return_value={
                    "ok": False, "error": "终端启动失败（退出码 3）",
                }), mock.patch.object(main.cloud, "save_settings") as save_settings:
            with self.assertRaises(HTTPException) as caught:
                await main.api_launch("claude-code")
        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(caught.exception.detail, "终端启动失败（退出码 3）")
        save_settings.assert_not_called()

    async def test_non_launch_agent_uses_legacy_path(self):
        agent = {"id": "legacy", "name": "Legacy", "command": "legacy"}
        with mock.patch.object(main.registry, "get_agent", return_value=agent), \
                mock.patch.object(main.detect, "launch_target",
                                  return_value=Path("/tmp/legacy")), \
                mock.patch.object(main, "_spawn") as spawn:
            result = await main.api_launch("legacy")
        self.assertEqual(result, {"ok": True, "legacy": True})
        spawn.assert_called_once_with(Path("/tmp/legacy"))


def test_windows_cmd_line_quotes_shell_metacharacters():
    """cmd.exe 不吃 CreateProcess 规则：含 & 的路径必须加引号，否则被当成命令分隔符。"""
    from app import launch

    line = launch._cmd_line([r"C:\Program Files\A&B\codex.exe", "--flag", "plain"])
    assert line.startswith('"C:\\Program Files\\A&B\\codex.exe"')
    assert line.endswith("--flag plain")
    # 带空格的普通路径同样要包起来，否则 cmd 会自己拆参数
    assert launch._cmd_line([r"C:\Users\a b\claude.exe"]) == r'"C:\Users\a b\claude.exe"'
    # 内嵌双引号按 cmd 规则重复
    assert launch._cmd_arg('a"b') == '"a""b"'
