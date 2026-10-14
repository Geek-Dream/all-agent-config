"""运行环境（node/npm/pnpm/brew/scoop/winget/pipx）检测与一键安装。

启动时前端拉一次 /api/env 展示环境状态；安装工具缺少对应管理器时，
前端弹窗引导：能自动装的给一键安装（任务日志可见），不能自动装的给官方安装说明。
"""

import shutil
import subprocess

from .installer import IS_WINDOWS

MANAGERS = ["node", "npm", "pnpm", "brew", "scoop", "winget", "pipx"]

_MANAGER_LABELS = {
    "node": "Node.js", "npm": "npm", "pnpm": "pnpm", "brew": "Homebrew",
    "scoop": "Scoop", "winget": "winget", "pipx": "pipx",
}


def _version(cmd: str) -> str | None:
    try:
        p = subprocess.run([cmd, "--version"], capture_output=True, text=True, timeout=15)
        line = ((p.stdout or "") + (p.stderr or "")).strip().splitlines()
        return line[0].strip() if line else None
    except Exception:
        return None


def check_env() -> dict:
    """各管理器安装状态 + 版本 + 是否支持一键安装。"""
    out = {}
    for m in MANAGERS:
        path = shutil.which(m)
        argv, hint = install_argv(m)
        out[m] = {
            "label": _MANAGER_LABELS.get(m, m),
            "installed": bool(path),
            "version": _version(path) if path else None,
            "path": path,
            "auto_install": argv is not None,
            "manual_hint": hint,
        }
    return out


def install_argv(name: str) -> tuple[list[str] | None, str | None]:
    """返回 (自动安装命令, 附加提示)；无法自动安装时返回 (None, 手动安装说明)。"""
    if name in ("node", "npm"):
        if IS_WINDOWS and shutil.which("winget"):
            return (["winget", "install", "-e", "--id", "OpenJS.NodeJS.LTS",
                     "--accept-source-agreements", "--accept-package-agreements"],
                    "安装完成后请重开终端（或重启本工具）再操作")
        if shutil.which("brew"):
            return ["brew", "install", "node"], None
        return None, ("请安装 Node.js（自带 npm）：https://nodejs.org/ 下载 LTS 版；"
                      "macOS 也可以先装 Homebrew 再 brew install node")
    if name == "pnpm":
        if shutil.which("npm"):
            return ["npm", "install", "-g", "pnpm"], None
        if shutil.which("brew"):
            return ["brew", "install", "pnpm"], None
        return None, "pnpm 依赖 Node.js：请先安装 https://nodejs.org/ ，再 npm install -g pnpm"
    if name == "pipx":
        if shutil.which("brew"):
            return ["brew", "install", "pipx"], "安装后执行 pipx ensurepath 把 ~/.local/bin 加入 PATH"
        py = shutil.which("python3") or shutil.which("python")
        if py:
            return ([py, "-m", "pip", "install", "--user", "pipx"],
                    "安装后执行 pipx ensurepath（或手动把 python 用户 bin 目录加入 PATH）")
        return None, "pipx 依赖 Python：请先安装 https://www.python.org/downloads/"
    if name == "brew":
        return None, ('macOS 终端执行：/bin/bash -c "$(curl -fsSL '
                      'https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"')
    if name == "scoop":
        return None, "Windows PowerShell 执行：irm get.scoop.sh | iex"
    if name == "winget":
        return None, "winget 由微软「应用安装程序」提供：请从 Microsoft Store 安装「应用安装程序」"
    return None, f"请手动安装 {name}"
