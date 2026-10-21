"""安装源镜像：默认走国内镜像，可用 AAC_MIRROR=official 切回官方源。

npm  → registry.npmmirror.com
pip  → pypi.tuna.tsinghua.edu.cn
brew → mirrors.ustc.edu.cn（API / bottle / git remote）

只做两件事：
- flags(argv)：给 npm / pip 命令追加镜像参数（sudo 场景也能生效）；
- env_for(argv)：给 brew 等只能靠环境变量切换的工具注入镜像变量。
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

CN = {
    "label": "国内镜像",
    "npm": "https://registry.npmmirror.com",
    "pip": "https://pypi.tuna.tsinghua.edu.cn/simple",
    "brew_api": "https://mirrors.ustc.edu.cn/homebrew-bottles/api",
    "brew_bottle": "https://mirrors.ustc.edu.cn/homebrew-bottles",
    "brew_repo": "https://mirrors.ustc.edu.cn/brew.git",
    "brew_core": "https://mirrors.ustc.edu.cn/homebrew-core.git",
}

OFFICIAL = {
    "label": "官方源",
    "npm": "https://registry.npmjs.org",
    "pip": "https://pypi.org/simple",
    "brew_api": "https://formulae.brew.sh/api",
    "brew_bottle": "",
    "brew_repo": "https://github.com/Homebrew/brew.git",
    "brew_core": "https://github.com/Homebrew/homebrew-core.git",
}

_MODES = {"cn": CN, "official": OFFICIAL}


def mode() -> str:
    """当前镜像模式：cn（默认）或 official。"""
    raw = (os.environ.get("AAC_MIRROR") or "cn").strip().lower()
    if raw in ("official", "off", "none", "global"):
        return "official"
    return "cn"


def current() -> dict:
    data = dict(_MODES[mode()])
    data["mode"] = mode()
    return data


def info() -> dict:
    """给前端展示的简短信息。"""
    cur = current()
    return {"mode": cur["mode"], "label": cur["label"], "npm": cur["npm"], "pip": cur["pip"]}


def _cmd(argv: list[str]) -> str:
    if not argv:
        return ""
    return os.path.basename(argv[0]).lower().removesuffix(".cmd").removesuffix(".exe")


def _is_pip(argv: list[str]) -> bool:
    cmd = _cmd(argv)
    if cmd in ("pip", "pip3") or cmd.startswith("pip3."):
        return True
    return len(argv) >= 3 and cmd.startswith("python") and argv[1] == "-m" and argv[2] == "pip"


def flags(argv: list[str]) -> list[str]:
    """需要追加到命令行的镜像参数（默认国内源时非空）。"""
    if mode() != "cn":
        return []
    cmd = _cmd(argv)
    if cmd in ("npm", "npx", "pnpm"):
        return [f"--registry={CN['npm']}"]
    if _is_pip(argv):
        return ["-i", CN["pip"], "--trusted-host", urlparse(CN["pip"]).hostname or ""]
    return []


def with_flags(argv: list[str]) -> list[str]:
    extra = flags(argv)
    return list(argv) + extra if extra else list(argv)


def env_for(argv: list[str]) -> dict[str, str]:
    """需要注入的环境变量（brew 走环境变量；npm/pip 额外兜底一份）。"""
    if mode() != "cn":
        return {}
    cmd = _cmd(argv)
    if cmd == "brew":
        return {
            "HOMEBREW_API_DOMAIN": CN["brew_api"],
            "HOMEBREW_BOTTLE_DOMAIN": CN["brew_bottle"],
            "HOMEBREW_BREW_GIT_REMOTE": CN["brew_repo"],
            "HOMEBREW_CORE_GIT_REMOTE": CN["brew_core"],
            "HOMEBREW_NO_AUTO_UPDATE": "1",
        }
    if cmd in ("npm", "npx", "pnpm"):
        return {"npm_config_registry": CN["npm"]}
    if _is_pip(argv):
        return {
            "PIP_INDEX_URL": CN["pip"],
            "PIP_TRUSTED_HOST": urlparse(CN["pip"]).hostname or "",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        }
    return {}
