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

# Kimi 桌面运行时自带的 npm 全局目录：服务进程的 PATH 指向它，
# 但往这里装/更新 = 把副本藏进别人家（还可能随宿主升级被清掉）。
KIMI_RUNTIME_MARKERS = ("kimi-desktop", "Kimi.app")


def is_kimi_runtime(prefix) -> bool:
    s = str(prefix)
    return any(m in s for m in KIMI_RUNTIME_MARKERS)


def system_prefix() -> Path:
    """系统级 npm 前缀兜底：优先机器上已有 node 环境的前缀。"""
    if IS_WINDOWS:
        return Path(os.environ.get("APPDATA", "")) / "npm"
    for p in (Path("/opt/homebrew"), Path("/usr/local")):
        if (p / "bin").is_dir():
            return p
    return Path("/usr/local")


def _global_prefix_for(command: str | None) -> Path:
    """全局安装/更新的目标前缀：跟随已装位置，但绝不写进 Kimi 运行时。"""
    prefix = install_prefix_of(command)
    if not prefix or is_kimi_runtime(prefix):
        prefix = system_prefix()
    return prefix


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
        return os.access(str(prefix) if IS_WINDOWS else str(prefix / "bin"), os.W_OK)
    except Exception:
        return False


def install_prefix_of(command: str | None) -> Path | None:
    """从已安装命令反推它的 npm 全局前缀（…/lib/node_modules 的上一级）。

    机器上常有多个 node/npm 环境（/usr/local、~/.local、各类 IDE 内置 npm），
    直接用 PATH 里的 npm 更新会打到错误的前缀上——命令在哪装的，就更新哪个前缀。
    """
    if not command:
        return None
    cmd = shutil.which(command)
    if not cmd:
        return None
    p = Path(cmd).resolve()
    for d in [p, *p.parents]:
        if d.name == "node_modules" and d.parent.name == "lib":
            return d.parent.parent          # …/lib/node_modules → prefix
        if d.name == "node_modules":        # Windows: prefix/node_modules
            return d.parent
    return None


def build_install_argv(agent: dict, scope: str) -> tuple[list[str], bool]:
    """返回 (argv, need_sudo)。"""
    npm_pkg = (agent.get("sources") or {})["npm"]  # 调用方需先确认 auto_install
    if scope == "user":
        return ["npm", "install", "-g", "--prefix", str(USER_PREFIX), npm_pkg], False
    prefix = _global_prefix_for(agent.get("command"))
    need_sudo = not prefix_writable(str(prefix))
    return ["npm", "install", "-g", "--prefix", str(prefix), npm_pkg], need_sudo


def build_update_argv(agent: dict, scope: str) -> tuple[list[str], bool]:
    """更新 = 原地升级到最新版。已安装的工具永远更新它所在的前缀，
    与页面上选的安装范围无关（scope 只影响全新安装的位置）。"""
    npm_pkg = (agent.get("sources") or {})["npm"]
    prefix = install_prefix_of(agent.get("command"))
    if prefix and not is_kimi_runtime(prefix):
        need_sudo = not prefix_writable(str(prefix))
        return ["npm", "install", "-g", "--prefix", str(prefix), f"{npm_pkg}@latest"], need_sudo
    if scope == "user":
        return ["npm", "install", "-g", "--prefix", str(USER_PREFIX), f"{npm_pkg}@latest"], False
    prefix = _global_prefix_for(agent.get("command"))
    need_sudo = not prefix_writable(str(prefix))
    return ["npm", "install", "-g", "--prefix", str(prefix), f"{npm_pkg}@latest"], need_sudo


def build_uninstall_argv(agent: dict, scope: str) -> tuple[list[str], bool]:
    npm_pkg = (agent.get("sources") or {})["npm"]
    prefix = install_prefix_of(agent.get("command"))
    if prefix and scope != "user":
        # 卸载跟随实际安装位置（Kimi 运行时里的副本也允许清掉——删除是安全的）
        need_sudo = not prefix_writable(str(prefix))
        return ["npm", "uninstall", "-g", "--prefix", str(prefix), npm_pkg], need_sudo
    if scope == "user":
        return ["npm", "uninstall", "-g", "--prefix", str(USER_PREFIX), npm_pkg], False
    need_sudo = not npm_prefix_writable(agent.get("command"))
    return ["npm", "uninstall", "-g", npm_pkg], need_sudo


def prefix_writable(prefix: str) -> bool:
    if IS_WINDOWS:
        return os.access(prefix, os.W_OK)   # Windows npm shim 直接在前缀根目录
    return os.access(Path(prefix) / "bin", os.W_OK)


def build_uninstall_argv_for_prefix(agent: dict, prefix: str) -> list[str]:
    """卸载指定前缀下的副本（重复安装清理）。"""
    npm_pkg = (agent.get("sources") or {})["npm"]
    return ["npm", "uninstall", "-g", "--prefix", prefix, npm_pkg]


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


def manager_available(manager: str) -> bool:
    return shutil.which(manager) is not None


def available_source(agent: dict) -> tuple[str, str] | None:
    """按优先级返回当前机器可用的 (安装管理器, 包名)。

    npm 源在没装 npm 但装了 pnpm 时自动改用 pnpm；brew/scoop/winget/pipx 需注册表显式声明。
    """
    src = agent.get("sources") or {}
    if src.get("npm"):
        if manager_available("npm"):
            return ("npm", src["npm"])
        if manager_available("pnpm"):
            return ("pnpm", src["npm"])
    for m in ("brew", "scoop", "winget", "pipx", "pnpm"):
        if src.get(m) and manager_available(m):
            return (m, src[m])
    return None


def missing_manager(agent: dict) -> str | None:
    """有声明的安装源但对应管理器没装时，返回缺的管理器名（用于引导用户装环境）。"""
    src = agent.get("sources") or {}
    if src.get("npm") and not (manager_available("npm") or manager_available("pnpm")):
        return "npm"
    for m in ("brew", "scoop", "winget", "pipx", "pnpm"):
        if src.get(m) and not manager_available(m):
            return m
    return None


def build_manager_argv(manager: str, pkg: str, kind: str) -> list[str] | None:
    """npm 以外的管理器安装命令（用户级，无需 sudo）。"""
    if manager == "pnpm":
        sub = {"install": ["add", "-g"], "update": ["update", "-g"],
               "uninstall": ["remove", "-g"]}.get(kind)
        return ["pnpm", *sub, pkg] if sub else None
    if manager == "scoop":
        sub = {"install": "install", "update": "update", "uninstall": "uninstall"}.get(kind)
        return ["scoop", sub, pkg] if sub else None
    if manager == "winget":
        sub = {"install": "install", "update": "upgrade", "uninstall": "uninstall"}.get(kind)
        return (["winget", sub, "-e", "--id", pkg,
                 "--accept-source-agreements", "--accept-package-agreements"] if sub else None)
    if manager == "pipx":
        sub = {"install": "install", "update": "upgrade", "uninstall": "uninstall"}.get(kind)
        return ["pipx", sub, pkg] if sub else None
    return None


def managed_remove_paths(agent: dict, prefix: str) -> list[Path] | None:
    """独立二进制卸载：只删注册表声明的程序文件，保留配置/会话/skills。

    安全校验：前缀必须在家目录下、且 prefix/bin/<command> 确实存在，
    不满足则返回 None（拒绝删除）。
    """
    if not agent.get("managed_files"):
        return None
    root = Path(prefix).expanduser()
    try:
        root.resolve().relative_to(Path.home())
    except ValueError:
        return None
    if not (root / "bin" / agent["command"]).is_file():
        return None
    return [root / rel for rel in agent["managed_files"]]


def remove_paths(paths: list[Path], log: list[str]) -> int:
    """删除受管文件（要求可写；不可写时调用方走 sudo rm）。"""
    for p in paths:
        if not p.exists():
            log.append(f"跳过（不存在）: {p}\n")
            continue
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        else:
            p.unlink()
        log.append(f"已删除: {p}\n")
    log.append("配置、会话与 skills 已保留。\n")
    return 0


def user_prefix_path_hint() -> str:
    bin_dir = USER_PREFIX if IS_WINDOWS else USER_PREFIX / "bin"
    shell_rc = "~/.zshrc" if os.environ.get("SHELL", "").endswith("zsh") else "~/.bashrc"
    return (
        f"用户级安装完成。若终端找不到命令，请把 {bin_dir} 加入 PATH：\n"
        f"  echo 'export PATH=\"{bin_dir}:$PATH\"' >> {shell_rc} && source {shell_rc}"
    )
