"""安装 / 更新 / 卸载执行器。

支持两种安装范围：
- user:   安装到 ~/.aac/npm（用户目录，无需 sudo，需要把 ~/.aac/npm/bin 加入 PATH）
- global: 安装到系统 npm prefix；prefix 不可写时通过 sudo -S 用用户提供的密码提权
"""

import asyncio
import os
import shutil
import subprocess
from pathlib import Path

USER_PREFIX = Path.home() / ".aac" / "npm"
IS_WINDOWS = os.name == "nt"


def npm_prefix_writable(command: str | None = None) -> bool:
    """全局安装是否需要提权：优先看该工具 bin 目录（若已安装）是否可写。"""
    if IS_WINDOWS:
        return True
    cmd_path = shutil.which(command) if command else None
    if cmd_path:
        return os.access(Path(cmd_path).parent, os.W_OK)
    if not shutil.which("npm"):
        return False
    try:
        p = subprocess.run(
            ["npm", "config", "get", "prefix"],
            capture_output=True, text=True, timeout=15,
        )
        prefix = Path(p.stdout.strip())
        bin_dir = prefix / ("Scripts" if IS_WINDOWS else "bin")
        return os.access(bin_dir, os.W_OK)
    except Exception:
        return False


def build_install_argv(agent: dict, scope: str) -> tuple[list[str], bool]:
    """返回 (argv, need_sudo)。"""
    npm_pkg = (agent.get("sources") or {})["npm"]  # 调用方需先确认 auto_install
    if scope == "user":
        return ["npm", "install", "-g", "--prefix", str(USER_PREFIX), npm_pkg], False
    need_sudo = not npm_prefix_writable(agent.get("command"))
    return ["npm", "install", "-g", npm_pkg], need_sudo


def build_update_argv(agent: dict, scope: str) -> tuple[list[str], bool]:
    npm_pkg = (agent.get("sources") or {})["npm"]
    if scope == "user":
        return ["npm", "install", "-g", "--prefix", str(USER_PREFIX), f"{npm_pkg}@latest"], False
    need_sudo = not npm_prefix_writable(agent.get("command"))
    return ["npm", "update", "-g", npm_pkg], need_sudo


def build_uninstall_argv(agent: dict, scope: str) -> tuple[list[str], bool]:
    npm_pkg = (agent.get("sources") or {})["npm"]
    if scope == "user":
        return ["npm", "uninstall", "-g", "--prefix", str(USER_PREFIX), npm_pkg], False
    need_sudo = not npm_prefix_writable(agent.get("command"))
    return ["npm", "uninstall", "-g", npm_pkg], need_sudo


ELEVATE_MARKER = "__AAC_ELEVATE__"


async def run(argv: list[str], log: list[str], sudo_password: str | None = None) -> int:
    """执行命令，输出实时追加到 log；sudo 时把密码喂给 stdin。"""
    if sudo_password == ELEVATE_MARKER and IS_WINDOWS:
        return await _run_elevated_windows(argv, log)
    use_sudo = bool(sudo_password)
    full = ["sudo", "-S", "-k", "-p", ""] + argv if use_sudo else argv
    log.append(f"$ {'sudo ' if use_sudo else ''}{' '.join(argv)}\n")
    proc = await asyncio.create_subprocess_exec(
        *full,
        stdin=asyncio.subprocess.PIPE if use_sudo else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    if use_sudo and proc.stdin:
        proc.stdin.write((sudo_password + "\n").encode())
        await proc.stdin.drain()
        proc.stdin.close()
    assert proc.stdout is not None
    async for line in proc.stdout:
        log.append(line.decode(errors="replace"))
    return await proc.wait()


async def _run_elevated_windows(argv: list[str], log: list[str]) -> int:
    """Windows 全局安装提权：PowerShell Start-Process -Verb RunAs（弹出 UAC 确认）。"""
    import json as _json
    import subprocess
    args = " ".join(_json.dumps(a) for a in argv[1:])
    ps = (f"Start-Process -Verb RunAs -Wait -FilePath {json_quote(argv[0])} "
          f"-ArgumentList {args}")
    log.append(f"$ (elevated) {argv[0]} {args}\n")
    proc = await asyncio.create_subprocess_exec(
        "powershell", "-NoProfile", "-NonInteractive", "-Command", ps,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    assert proc.stdout is not None
    async for line in proc.stdout:
        log.append(line.decode(errors="replace"))
    return await proc.wait()


def json_quote(s: str) -> str:
    import json as _json
    return _json.dumps(s)


def build_brew_argv(agent: dict, kind: str) -> list[str] | None:
    """brew 源安装/更新/卸载（仅 macOS/Linux 且装有 Homebrew）。"""
    brew = (agent.get("sources") or {}).get("brew")
    if not brew or IS_WINDOWS or not shutil.which("brew"):
        return None
    formula = brew.split("/")[-1]
    cmd = {"install": "install", "update": "upgrade", "uninstall": "uninstall"}.get(kind)
    return ["brew", cmd, formula] if cmd else None


def user_prefix_path_hint() -> str:
    bin_dir = USER_PREFIX / ("Scripts" if IS_WINDOWS else "bin")
    shell_rc = "~/.zshrc" if os.environ.get("SHELL", "").endswith("zsh") else "~/.bashrc"
    return (
        f"用户级安装完成。若终端找不到命令，请把 {bin_dir} 加入 PATH：\n"
        f"  echo 'export PATH=\"{bin_dir}:$PATH\"' >> {shell_rc} && source {shell_rc}"
    )
