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


def npm_prefix_writable() -> bool:
    """系统 npm 全局目录是否可直接写入（不可写则需要 sudo / 提权）。"""
    if IS_WINDOWS or not shutil.which("npm"):
        return True
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
    need_sudo = not npm_prefix_writable()
    return ["npm", "install", "-g", npm_pkg], need_sudo


def build_update_argv(agent: dict, scope: str) -> tuple[list[str], bool]:
    npm_pkg = (agent.get("sources") or {})["npm"]
    if scope == "user":
        return ["npm", "install", "-g", "--prefix", str(USER_PREFIX), f"{npm_pkg}@latest"], False
    need_sudo = not npm_prefix_writable()
    return ["npm", "update", "-g", npm_pkg], need_sudo


def build_uninstall_argv(agent: dict, scope: str) -> tuple[list[str], bool]:
    npm_pkg = (agent.get("sources") or {})["npm"]
    if scope == "user":
        return ["npm", "uninstall", "-g", "--prefix", str(USER_PREFIX), npm_pkg], False
    need_sudo = not npm_prefix_writable()
    return ["npm", "uninstall", "-g", npm_pkg], need_sudo


async def run(argv: list[str], log: list[str], sudo_password: str | None = None) -> int:
    """执行命令，输出实时追加到 log；sudo 时把密码喂给 stdin。"""
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


def user_prefix_path_hint() -> str:
    bin_dir = USER_PREFIX / ("Scripts" if IS_WINDOWS else "bin")
    shell_rc = "~/.zshrc" if os.environ.get("SHELL", "").endswith("zsh") else "~/.bashrc"
    return (
        f"用户级安装完成。若终端找不到命令，请把 {bin_dir} 加入 PATH：\n"
        f"  echo 'export PATH=\"{bin_dir}:$PATH\"' >> {shell_rc} && source {shell_rc}"
    )
