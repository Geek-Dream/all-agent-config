"""直链 / GitHub Release 下载安装（zip / dmg / exe / msi / tar）。

注册表里的下载源：
  sources.download: {macos-arm64: URL, windows-x64: URL, ...}
  sources.github:   {repo: owner/name, asset: "*.dmg", prerelease: false,
                     mirror: "https://镜像前缀/", setup: [[cmd, ...]]}
平台键由 platform_key() 生成；找不到对应平台时回落到 any。
"""

import fnmatch
import hashlib
import json
import os
import platform
import plistlib
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import archive
from .safefs import ensure_private_dir

APPS = Path.home() / ".aac" / "apps"
USER_APPS = Path.home() / "Applications"
DOWNLOAD_LIMIT = 2 * 1024 * 1024 * 1024
TIMEOUT_SECONDS = 60
USER_AGENT = "all-agent-config/0.2"


def platform_key(machine: str | None = None) -> str:
    system = platform.system()
    arch = (machine or platform.machine()).lower()
    if arch in ("arm64", "aarch64"):
        arch = "arm64"
    else:
        arch = "x64"
    if system == "Darwin":
        return f"macos-{arch}"
    if system == "Windows":
        return f"windows-{arch}"
    return f"linux-{arch}"


def _download_urls(agent: dict) -> dict:
    value = (agent.get("sources") or {}).get("download")
    return value if isinstance(value, dict) else {}


def source_for(agent: dict, machine: str | None = None) -> str | None:
    urls = _download_urls(agent)
    key = platform_key(machine)
    value = urls.get(key) or urls.get("any")
    return value if isinstance(value, str) and value.strip() else None


def github_spec(agent: dict) -> dict | None:
    value = (agent.get("sources") or {}).get("github")
    return value if isinstance(value, dict) and isinstance(value.get("repo"), str) else None


def available(agent: dict) -> bool:
    """注册表是否声明了下载机制（不保证当前平台有包）。"""
    return bool(_download_urls(agent) or github_spec(agent))


def platform_source_available(agent: dict) -> bool:
    """当前平台是否真的有可下载的包。"""
    return source_for(agent) is not None or github_spec(agent) is not None


def _apply_mirror(url: str, mirror: str | None) -> str:
    if not mirror:
        return url
    if "{url}" in mirror:
        return mirror.replace("{url}", url)
    return mirror.rstrip("/") + "/" + url


def _download(url: str, dest: Path, log: list[str], *,
              mirror: str | None = None, sha256: str | None = None) -> None:
    request = urllib.request.Request(
        _apply_mirror(url, mirror), headers={"User-Agent": USER_AGENT})
    written = 0
    digest = hashlib.sha256()
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        log.append(f"⬇️ 下载 {Path(urllib.parse.urlparse(url).path).name}"
                   f"（{total // 1024 // 1024 if total else '?'} MB）…\n")
        with open(dest, "wb") as fh:
            last = -1
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                written += len(chunk)
                if written > DOWNLOAD_LIMIT:
                    raise ValueError("下载体积超过 2 GB 限制")
                digest.update(chunk)
                fh.write(chunk)
                if total:
                    pct = written * 100 // total
                    if pct // 10 != last:
                        last = pct // 10
                        log.append(f"  {pct}%\n")
    if sha256:
        expected = sha256.split(":", 1)[-1].lower()
        if digest.hexdigest() != expected:
            raise ValueError("下载文件校验失败（SHA-256 不匹配）")
    log.append(f"✅ 下载完成（{written // 1024 // 1024} MB）\n")


def _github_asset(spec: dict, log: list[str]) -> tuple[str, str, str | None]:
    """返回 (文件名, 下载地址, sha256)。优先匹配平台 asset，其次任意 asset。"""
    repo = spec["repo"]
    include_prerelease = bool(spec.get("prerelease"))
    if include_prerelease:
        api = f"https://api.github.com/repos/{repo}/releases?per_page=20"
    else:
        api = f"https://api.github.com/repos/{repo}/releases/latest"
    request = urllib.request.Request(api, headers={
        "User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"无法读取 GitHub Release：{exc}") from exc
    releases = data if isinstance(data, list) else [data]
    patterns = spec.get("assets")
    if isinstance(patterns, dict):
        patterns = patterns.get(platform_key()) or spec.get("asset")
    if isinstance(patterns, str):
        patterns = [patterns]
    if not isinstance(patterns, list) or not patterns:
        patterns = ["*"]
    for release in releases:
        if not isinstance(release, dict) or release.get("draft"):
            continue
        for asset in release.get("assets") or []:
            name = asset.get("name") or ""
            if not name or name.endswith((".sha256", ".asc", ".sig")):
                continue
            if any(fnmatch.fnmatch(name, pattern) for pattern in patterns):
                digest = asset.get("digest")
                return name, asset["browser_download_url"], (
                    digest if isinstance(digest, str) else None)
    raise ValueError("该 Release 没有匹配当前平台的安装包")


def _source_zip(spec: dict, log: list[str]) -> tuple[str, str, None]:
    branch = spec.get("branch") or "master"
    name = f"{spec['repo'].split('/')[-1]}-{branch}.zip"
    url = f"https://github.com/{spec['repo']}/archive/refs/heads/{branch}.zip"
    log.append("ℹ️ 没有可用的平台安装包，改下载源码 zip…\n")
    return name, url, None


def _run_setup(spec: dict, root: Path, log: list[str]) -> None:
    for command in spec.get("setup") or []:
        if not isinstance(command, list) or not command:
            continue
        log.append(f"$ {' '.join(command)}\n")
        proc = subprocess.run(
            command, cwd=root, capture_output=True, text=True, timeout=1800)
        tail = ((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()[-20:]
        if tail:
            log.extend(line + "\n" for line in tail)
        if proc.returncode != 0:
            raise ValueError(f"安装命令失败（退出码 {proc.returncode}）：{' '.join(command)}")


def _swap_dir(stage: Path, dest: Path) -> None:
    old = dest.with_name(dest.name + ".old")
    if old.exists():
        shutil.rmtree(old, ignore_errors=True)
    if dest.exists():
        dest.rename(old)
    try:
        stage.rename(dest)
    except OSError:
        if old.exists() and not dest.exists():
            old.rename(dest)
        raise
    if old.exists():
        shutil.rmtree(old, ignore_errors=True)


def _place_app(app: Path, log: list[str]) -> Path:
    if platform.system() != "Darwin":
        raise ValueError("当前系统不能安装 .app")
    ensure_private_dir(USER_APPS)
    dest = USER_APPS / app.name
    stage = USER_APPS / f".{app.name}.new"
    if stage.exists():
        shutil.rmtree(stage)
    shutil.copytree(app, stage, symlinks=True)
    old = USER_APPS / f".{app.name}.old"
    if old.exists():
        shutil.rmtree(old, ignore_errors=True)
    if dest.exists():
        dest.rename(old)
    stage.rename(dest)
    if old.exists():
        shutil.rmtree(old, ignore_errors=True)
    log.append(f"✅ 已安装到 {dest}\n")
    return dest


def _find_command(root: Path, command: str) -> Path | None:
    names = {command, f"{command}.exe", f"{command}.cmd", f"{command}.bat"}
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name in names:
            return path
    return None


def _place_tree(root: Path, agent: dict, log: list[str]) -> Path:
    """把解压/选择的目录装到 ~/.aac/apps/<id>，并在 bin/ 下放命令入口。"""
    agent_id = agent["id"]
    command = agent["command"]
    ensure_private_dir(APPS)
    stage_root = Path(tempfile.mkdtemp(prefix=f".{agent_id}-", dir=APPS.parent))
    stage = stage_root / "payload"
    if root.is_dir():
        shutil.copytree(root, stage, symlinks=True)
    else:
        raise ValueError("安装内容不是目录")
    found = _find_command(stage, command)
    bin_dir = stage / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    if found is None:
        executables = [p for p in stage.rglob("*")
                       if p.is_file() and (os.access(p, os.X_OK)
                                           or p.suffix.lower() == ".exe")]
        if len(executables) == 1:
            found = executables[0]
    if found is None:
        shutil.rmtree(stage_root, ignore_errors=True)
        raise ValueError(f"目录里没找到可执行文件 {command}")
    if not installer_windows():
        try:
            found.chmod(0o755)
        except OSError:
            pass
    link_name = found.name if os.name == "nt" else command
    link = bin_dir / link_name
    if link.exists() or link.is_symlink():
        link.unlink()
    try:
        link.symlink_to(os.path.relpath(found, bin_dir))
    except OSError:
        shutil.copy2(found, link)
    dest = APPS / agent_id
    _swap_dir(stage, dest)
    shutil.rmtree(stage_root, ignore_errors=True)
    target = dest / "bin" / link_name
    log.append(f"✅ 已安装到 {dest}\n")
    return target


def installer_windows() -> bool:
    return os.name == "nt"


def _extract(path: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    archive.safe_extract(path, dest)


def _install_zip(path: Path, agent: dict, log: list[str]) -> Path:
    with tempfile.TemporaryDirectory(prefix=".aac-unzip-", dir=APPS.parent) as tmp:
        root = Path(tmp)
        _extract(path, root)
        apps = sorted(root.rglob("*.app"))
        if apps and platform.system() == "Darwin":
            return _place_app(apps[0], log)
        return _place_tree(root, agent, log)


def _install_tar(path: Path, agent: dict, log: list[str]) -> Path:
    with tempfile.TemporaryDirectory(prefix=".aac-untar-", dir=APPS.parent) as tmp:
        root = Path(tmp)
        _extract(path, root)
        apps = sorted(root.rglob("*.app"))
        if apps and platform.system() == "Darwin":
            return _place_app(apps[0], log)
        return _place_tree(root, agent, log)


def _install_dmg(path: Path, agent: dict, log: list[str]) -> Path:
    if platform.system() != "Darwin":
        raise ValueError("当前系统不能安装 .dmg")
    proc = subprocess.run(
        ["hdiutil", "attach", "-nobrowse", "-readonly", "-plist", str(path)],
        capture_output=True, timeout=180)
    if proc.returncode != 0:
        raise ValueError("挂载 DMG 失败：" + proc.stderr.decode(errors="replace")[-300:])
    try:
        info = plistlib.loads(proc.stdout)
        mount = next(
            (item["mount-point"] for item in info.get("system-entities", [])
             if item.get("mount-point")), None)
    except (ValueError, KeyError):
        mount = None
    if not mount:
        raise ValueError("找不到 DMG 挂载点")
    try:
        apps = sorted(Path(mount).glob("*.app"))
        if not apps:
            raise ValueError("DMG 里没有 .app")
        return _place_app(apps[0], log)
    finally:
        subprocess.run(["hdiutil", "detach", mount, "-quiet"],
                       capture_output=True, timeout=120)


def _install_exe(path: Path, agent: dict, log: list[str]) -> Path:
    if os.name != "nt":
        raise ValueError("当前系统不能运行 .exe 安装包")
    log.append("运行安装包（静默模式）…\n")
    proc = subprocess.run([str(path), "/S"], timeout=1800)
    if proc.returncode != 0:
        raise ValueError(f"安装包退出码 {proc.returncode}；请手动运行 {path}")
    from . import detect
    target = detect.desktop_path(agent)
    if target:
        log.append(f"✅ 已安装到 {target}\n")
        return target
    log.append("安装程序已结束；若未找到程序，请检查安装位置。\n")
    return path


def _install_msi(path: Path, log: list[str]) -> Path:
    if os.name != "nt":
        raise ValueError("当前系统不能运行 .msi 安装包")
    proc = subprocess.run(["msiexec", "/i", str(path), "/qn"], timeout=1800)
    if proc.returncode != 0:
        raise ValueError(f"MSI 安装失败（退出码 {proc.returncode}）")
    return path


def _install_file(path: Path, agent: dict, log: list[str]) -> Path:
    name = path.name.lower()
    if name.endswith(".dmg"):
        return _install_dmg(path, agent, log)
    if name.endswith((".zip",)):
        return _install_zip(path, agent, log)
    if name.endswith((".tar.gz", ".tgz", ".tar", ".tar.bz2", ".tar.xz")):
        return _install_tar(path, agent, log)
    if name.endswith(".exe"):
        return _install_exe(path, agent, log)
    if name.endswith(".msi"):
        return _install_msi(path, log)
    raise ValueError(f"暂不支持这种安装包：{path.name}")


def install_agent(agent: dict, log: list[str]) -> Path:
    """下载并安装；返回可启动的目标（桌面 .app 或 bin 命令）。"""
    ensure_private_dir(APPS)
    temp_root = Path(tempfile.mkdtemp(prefix=".aac-dl-", dir=APPS.parent))
    try:
        spec = github_spec(agent)
        url = source_for(agent)
        mirror = None
        sha256 = None
        if url:
            name = Path(urllib.parse.urlparse(url).path).name or f"{agent['id']}.bin"
        elif spec:
            mirror = spec.get("mirror") or os.environ.get("AAC_GITHUB_MIRROR")
            try:
                name, url, sha256 = _github_asset(spec, log)
            except ValueError as exc:
                if not spec.get("archive"):
                    raise
                log.append(f"ℹ️ {exc}\n")
                name, url, sha256 = _source_zip(spec, log)
        else:
            raise ValueError("该工具没有声明下载源")

        dest = temp_root / Path(name).name
        _download(url, dest, log, mirror=mirror, sha256=sha256)

        if spec and spec.get("archive") and dest.suffix.lower() == ".zip":
            with tempfile.TemporaryDirectory(prefix=".aac-src-", dir=temp_root) as tmp:
                root = Path(tmp)
                _extract(dest, root)
                dirs = [p for p in root.iterdir() if p.is_dir()]
                build_root = dirs[0] if len(dirs) == 1 else root
                _run_setup(spec, build_root, log)
                return _place_tree(build_root, agent, log)

        return _install_file(dest, agent, log)
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def install_folder(agent: dict, source: Path, log: list[str]) -> Path:
    """手动安装：把用户选择的文件夹（或 .app）复制进安装目录。"""
    try:
        src = source.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"目录不存在：{source}") from exc
    if not src.is_dir() or src.is_symlink():
        raise ValueError("只能选择文件夹")
    if src == Path.home().resolve() or src.parent == src:
        raise ValueError("不能选择主目录或根目录")
    ensure_private_dir(APPS)
    if src.suffix.lower() == ".app":
        return _place_app(src, log)
    with tempfile.TemporaryDirectory(prefix=".aac-folder-", dir=APPS.parent) as tmp:
        root = Path(tmp) / "payload"
        shutil.copytree(src, root, symlinks=True)
        apps = sorted(root.rglob("*.app"))
        if apps and platform.system() == "Darwin":
            return _place_app(apps[0], log)
        return _place_tree(root, agent, log)
