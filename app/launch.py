"""Build and execute terminal-backed Agent launch plans."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from . import detect
from .safefs import ensure_private_dir


def _platform_name(value: str | None = None) -> str:
    return value or sys.platform


def default_terminal_id(platform: str | None = None) -> str:
    system = _platform_name(platform)
    if system == "darwin":
        return "terminal"
    if system == "win32":
        return "cmd"
    return "gnome-terminal"


def _which(*commands: str) -> str | None:
    for command in commands:
        found = shutil.which(command)
        if found:
            return found
    return None


def _terminal_entry(terminal_id: str, name: str, kind: str, path: str,
                    available: bool, builtin: bool = False) -> dict:
    return {"id": terminal_id, "name": name, "kind": kind, "path": path,
            "available": available, "builtin": builtin}


def detect_terminals() -> list[dict]:
    """Return available terminals plus the platform's built-in default."""
    system = _platform_name()
    if system == "darwin":
        terminal_app = Path("/Applications/Utilities/Terminal.app")
        system_terminal = Path("/System/Applications/Utilities/Terminal.app")
        default_path = terminal_app if terminal_app.exists() else system_terminal
        entries = [
            _terminal_entry("terminal", "Terminal", "app", str(default_path),
                            default_path.exists(), True),
        ]
        for terminal_id, name, path in (
            ("iterm2", "iTerm2", "/Applications/iTerm.app"),
            ("warp", "Warp", "/Applications/Warp.app"),
            ("ghostty", "Ghostty", "/Applications/Ghostty.app"),
        ):
            app_path = Path(path)
            entries.append(_terminal_entry(terminal_id, name, "app", str(app_path),
                                           app_path.exists()))
        for terminal_id, name, command in (
            ("kitty", "kitty", "kitty"),
            ("wezterm", "WezTerm", "wezterm"),
            ("alacritty", "Alacritty", "alacritty"),
        ):
            found = _which(command)
            entries.append(_terminal_entry(terminal_id, name, "binary",
                                           found or command, bool(found)))
        return entries

    if system == "win32":
        comspec = os.environ.get("COMSPEC", "cmd.exe")
        cmd_found = _which("cmd.exe", "cmd")
        cmd = cmd_found or comspec
        entries = [_terminal_entry("cmd", "Command Prompt", "binary", cmd,
                                   bool(cmd_found) or Path(cmd).exists(), True)]
        for terminal_id, name, commands in (
            ("wt", "Windows Terminal", ("wt.exe", "wt")),
            ("powershell", "PowerShell", ("powershell.exe", "powershell")),
        ):
            found = _which(*commands)
            entries.append(_terminal_entry(terminal_id, name, "binary",
                                           found or commands[0], bool(found)))
        return entries

    entries = []
    for terminal_id, name, command in (
        ("gnome-terminal", "GNOME Terminal", "gnome-terminal"),
        ("konsole", "Konsole", "konsole"),
        ("xterm", "xterm", "xterm"),
    ):
        found = _which(command)
        entries.append(_terminal_entry(terminal_id, name, "binary",
                                       found or command, bool(found)))
    return entries


def _resolve_terminal(settings_launch: dict, platform: str | None = None) -> dict:
    system = _platform_name(platform)
    terminal_id = settings_launch.get("terminal") or "auto"
    if terminal_id == "auto":
        terminal_id = default_terminal_id(system)
    if terminal_id == "custom":
        raw = str(settings_launch.get("custom_terminal") or "").strip()
        if not raw:
            raise ValueError("请填写自定义终端路径或命令")
        expanded = os.path.expandvars(os.path.expanduser(raw))
        candidate = Path(expanded)
        if expanded.lower().endswith(".app"):
            if not candidate.exists():
                raise ValueError(f"自定义终端应用不存在：{expanded}")
            return _terminal_entry("custom", candidate.stem, "app",
                                   str(candidate), True)
        is_windows_executable = os.name == "nt" and candidate.suffix.lower() in {
            ".exe", ".com", ".cmd", ".bat",
        }
        found = (str(candidate) if candidate.is_file()
                 and (os.access(candidate, os.X_OK) or is_windows_executable)
                 else shutil.which(expanded))
        if not found:
            raise ValueError(f"自定义终端不是可执行文件或命令：{raw}")
        return _terminal_entry("custom", Path(found).name, "binary", found, True)

    terminal = next((item for item in detect_terminals()
                     if item["id"] == terminal_id), None)
    if terminal is None:
        raise ValueError(f"不支持的终端：{terminal_id}")
    if not terminal["available"] and not terminal["builtin"]:
        raise ValueError(f"终端不可用：{terminal['name']}")
    return terminal


def resolve_terminal(settings_launch: dict) -> dict:
    """Resolve a configured ID to a platform terminal or executable custom path."""
    return _resolve_terminal(settings_launch)


def _mode_spec(agent: dict, mode: str) -> dict:
    launch_spec = agent.get("launch")
    if not isinstance(launch_spec, dict):
        raise ValueError("该工具没有可用的启动配置")
    modes = launch_spec.get("modes")
    mode_spec = modes.get(mode) if isinstance(modes, dict) else None
    if not isinstance(mode_spec, dict):
        raise ValueError(f"未知启动模式：{mode}")
    if mode_spec.get("supported") is False:
        raise ValueError(mode_spec.get("reason") or "该工具不支持此启动模式")
    return mode_spec


def build_agent_argv(agent: dict, mode: str, resume: bool) -> list[str]:
    """Build argv in registry order without passing arguments through a shell."""
    mode_spec = _mode_spec(agent, mode)
    target = detect.launch_target(agent)
    if target is None:
        raise ValueError("未找到可启动的程序，请先安装")
    mode_args = mode_spec.get("args", [])
    resume_args = (agent.get("launch") or {}).get("resume_args", []) if resume else []
    if not isinstance(mode_args, list) or not isinstance(resume_args, list):
        raise ValueError("启动参数配置无效")
    return [str(target), *(str(arg) for arg in mode_args),
            *(str(arg) for arg in resume_args)]


def shell_quote(value: str | Path) -> str:
    return shlex.quote(str(value))


def applescript_quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _ps_quote(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _cmd_arg(value: str | Path) -> str:
    """cmd.exe 视角的单个参数引号化。

    subprocess.list2cmdline 遵循 CreateProcess 规则，不是 cmd.exe 规则：
    含 `&` `|` `<` `>` `^` 的路径它不加引号，交给 cmd 就会被当成命令分隔符。
    """
    text = str(value)
    if not text:
        return '""'
    if any(ch in text for ch in ' \t&|<>^()!"'):
        return '"' + text.replace('"', '""') + '"'
    return text


def _cmd_line(argv: list[str]) -> str:
    """把 argv 拼成一段可以安全交给 `cmd /k` 执行的命令行。"""
    return " ".join(_cmd_arg(a) for a in argv)


def _mac_shell_line(argv: list[str], cwd: Path) -> str:
    return f"cd {shell_quote(cwd)} && exec " + " ".join(shell_quote(a) for a in argv)


def _write_command_script(argv: list[str], cwd: Path, tray_dir: Path | None) -> Path:
    directory = tray_dir or Path.home() / ".aac" / "launch"
    ensure_private_dir(directory)
    script_path = directory / f"launch-{uuid.uuid4().hex}.command"
    content = "#!/bin/sh\n" + f"cd {shell_quote(cwd)} || exit\n"
    content += "exec " + " ".join(shell_quote(arg) for arg in argv) + "\n"
    fd = os.open(script_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(content)
    script_path.chmod(0o700)
    return script_path


def terminal_invocation(terminal: dict, argv: list[str], cwd: Path, *,
                        platform: str | None = None,
                        tray_dir: Path | None = None) -> dict:
    """Build terminal argv; the app-fallback path writes one private .command."""
    system = _platform_name(platform)
    terminal_id = terminal["id"]
    path = str(terminal["path"])
    cwd = Path(cwd)
    script: str | None = None
    if terminal.get("kind") == "app" and system != "darwin":
        raise ValueError("当前平台不支持 macOS 应用包终端")

    if system == "darwin":
        if terminal.get("kind") == "app":
            if terminal_id == "terminal":
                line = _mac_shell_line(argv, cwd)
                first = f'tell application "Terminal" to do script "{applescript_quote(line)}"'
                second = 'tell application "Terminal" to activate'
                script = first + "\n" + second
                command = ["osascript", "-e", first, "-e", second]
                kind = "osascript"
            elif terminal_id == "iterm2":
                line = _mac_shell_line(argv, cwd)
                first = f'tell application "iTerm" to create window with default profile command "{applescript_quote(line)}"'
                script = first
                command = ["osascript", "-e", first]
                kind = "osascript"
            else:
                command_script = _write_command_script(argv, cwd, tray_dir)
                command = ["open", "-a", path, str(command_script)]
                script = str(command_script)
                kind = "open"
        elif terminal_id == "kitty":
            command = [path, "--directory", str(cwd), "--", *argv]
            kind = "direct"
        elif terminal_id == "wezterm":
            command = [path, "start", "--cwd", str(cwd), "--", *argv]
            kind = "direct"
        elif terminal_id == "alacritty":
            command = [path, "--working-directory", str(cwd), "-e", *argv]
            kind = "direct"
        elif terminal_id == "custom":
            command = [path, "--", *argv]
            kind = "direct"
        else:
            raise ValueError(f"不支持此终端：{terminal_id}")
    elif system == "win32":
        line = _cmd_line(argv)
        if terminal_id == "cmd":
            command = [path, "/k", line]
        elif terminal_id == "wt":
            command = [path, "-d", str(cwd), "cmd.exe", "/k", line]
        elif terminal_id == "powershell":
            invocation = "& " + " ".join(_ps_quote(arg) for arg in argv)
            ps_command = f"Set-Location -LiteralPath {_ps_quote(cwd)}; {invocation}"
            command = [path, "-NoExit", "-Command", ps_command]
        elif terminal_id == "custom":
            command = [path, *argv]
        else:
            raise ValueError(f"Windows 不支持此终端：{terminal_id}")
        kind = "direct"
    else:
        if terminal_id == "kitty":
            command = [path, "--directory", str(cwd), "--", *argv]
        elif terminal_id == "wezterm":
            command = [path, "start", "--cwd", str(cwd), "--", *argv]
        elif terminal_id == "alacritty":
            command = [path, "--working-directory", str(cwd), "-e", *argv]
        elif terminal_id == "gnome-terminal":
            command = [path, "--working-directory", str(cwd), "--", *argv]
        elif terminal_id == "konsole":
            command = [path, "--workdir", str(cwd), "-e", *argv]
        elif terminal_id == "xterm":
            line = _mac_shell_line(argv, cwd)
            command = [path, "-e", "sh", "-lc", line]
        elif terminal_id == "custom":
            command = [path, "-e", *argv]
        else:
            raise ValueError(f"不支持此终端：{terminal_id}")
        kind = "direct"

    if kind == "open":
        display = shlex.join(command)
    else:
        display = f"{terminal.get('name', terminal_id)}: {shlex.join(argv)}"
    return {"argv": command, "kind": kind, "display": display, "script": script}


def plan_launch(agent: dict, mode: str, resume: bool, settings_launch: dict,
                cwd: str | None, *, platform: str | None = None) -> dict:
    """Validate cwd and build a launch plan without starting any process."""
    system = _platform_name(platform)
    raw_cwd = cwd if cwd is not None else settings_launch.get("cwd") or str(Path.home())
    expanded_cwd = os.path.expandvars(os.path.expanduser(str(raw_cwd)))
    working_dir = Path(expanded_cwd).resolve()
    if not working_dir.exists() or not working_dir.is_dir():
        raise ValueError(f"工作目录不存在或不是文件夹：{raw_cwd}")

    argv = build_agent_argv(agent, mode, resume)
    launch_spec = agent.get("launch") or {}
    if launch_spec.get("needs_terminal", True):
        terminal = _resolve_terminal(settings_launch, system)
        invocation = terminal_invocation(terminal, argv, working_dir, platform=system)
    else:
        terminal = _terminal_entry("direct", "直接启动", "binary", argv[0], True)
        invocation = {"argv": argv, "kind": "direct", "display": shlex.join(argv),
                      "script": None}
    mode_spec = _mode_spec(agent, mode)
    return {
        "argv": argv,
        "target": argv[0],
        "mode": mode,
        "resume": bool(resume),
        "terminal": terminal["id"],
        "terminal_name": terminal["name"],
        "command": invocation["display"],
        "cwd": str(working_dir),
        "confirm_required": bool(mode_spec.get("confirm")),
        "_invocation": invocation,
        "_platform": system,
    }


def _tail_log(stream, limit: int = 4000) -> str:
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    stream.seek(max(0, size - limit))
    return stream.read().decode("utf-8", "replace").strip()


def run_plan(plan: dict) -> dict:
    """Run a launch plan and report launcher failures rather than assuming success."""
    invocation = plan.get("_invocation") or {}
    command = invocation.get("argv")
    if not isinstance(command, list) or not command:
        return {"ok": False, "error": "启动计划无效"}
    cwd = plan.get("cwd")
    try:
        if invocation.get("kind") in ("osascript", "open"):
            result = subprocess.run(command, cwd=cwd, capture_output=True,
                                    text=True, check=False)
            if result.returncode:
                output = (result.stderr or result.stdout or "").strip()
                detail = f"；{output[-2000:]}" if output else ""
                return {"ok": False,
                        "error": f"终端启动失败（退出码 {result.returncode}）{detail}"}
            return {"ok": True, "error": None}

        kwargs = {"cwd": cwd, "stdin": subprocess.DEVNULL}
        if plan.get("_platform") == "win32" and plan.get("terminal") in (
                "cmd", "powershell", "custom"):
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x10)
        with tempfile.TemporaryFile(mode="w+b") as log:
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                       **kwargs)
            try:
                returncode = process.wait(timeout=0.8)
            except subprocess.TimeoutExpired:
                return {"ok": True, "error": None}
            tail = _tail_log(log)
            detail = f"；输出：{tail}" if tail else "；无终端输出"
            return {"ok": returncode == 0,
                    "error": None if returncode == 0 else
                    f"终端已退出（退出码 {returncode}）{detail}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"启动失败：{exc}"}
