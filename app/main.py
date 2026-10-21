"""All Agent Config —— FastAPI 入口。

运行：python -m app.main        （默认 127.0.0.1:8600，仅本机访问，自动打开浏览器）
"""

import asyncio
import os
import platform
import shutil
import subprocess
import tempfile
import time
import uuid
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from urllib.parse import urlsplit

from . import archive, backup, bundle, categories as catmod, ccswitch, cloud, configview, detect, download, engines, env as envmod, footer, footer_ext, installer, launch, localize, meta, patchwork, providers, registry, whale
from .paths import relative_target
from .safefs import ensure_private_dir

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

_OPEN_URL: str | None = None


async def _open_browser_later(url: str) -> None:
    """等服务真正开始监听后再开浏览器，避免首屏「无法连接」。"""
    await asyncio.sleep(0.6)
    try:
        await asyncio.to_thread(webbrowser.open, url)
    except Exception:  # noqa: BLE001
        pass


@asynccontextmanager
async def _lifespan(_: FastAPI):
    asyncio.create_task(backup.auto_backup_loop())
    if _OPEN_URL:
        asyncio.create_task(_open_browser_later(_OPEN_URL))
    yield


app = FastAPI(title="All Agent Config", version="0.2.0", lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# 内存任务表：task_id -> {"status": "running"|"done"|"failed", "log": list[str]}
TASKS: dict[str, dict] = {}
TASK_TTL_SECONDS = 60 * 60
TASK_MAX_COUNT = 200
IMPORT_UPLOAD_LIMIT = 256 * 1024 * 1024
ARCHIVE_UPLOAD_LIMIT = 1024 * 1024 * 1024
IMPORT_TTL_SECONDS = 24 * 60 * 60
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
STATE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _host_parts(value: str | None) -> tuple[str | None, int | None]:
    if not value or any(c in value for c in ("/", "\\", "@")):
        return None, None
    try:
        parsed = urlsplit(f"//{value}")
        return parsed.hostname, parsed.port
    except ValueError:
        return None, None


def _local_request(host: str | None, origin: str | None) -> bool:
    hostname, host_port = _host_parts(host)
    if hostname not in LOOPBACK_HOSTS:
        return False
    if origin is None:
        return True
    try:
        parsed = urlsplit(origin)
        origin_port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https") or parsed.hostname not in LOOPBACK_HOSTS:
        return False
    return parsed.hostname == hostname and origin_port == (host_port or 80)


@app.middleware("http")
async def _local_api_guard(request: Request, call_next):
    host = request.headers.get("host")
    if not _local_request(host, request.headers.get("origin")):
        return JSONResponse({"detail": "拒绝非本机或跨源请求"}, status_code=403)
    if request.method in STATE_METHODS and request.headers.get("x-aac-request") != "1":
        return JSONResponse({"detail": "缺少本机 API 请求标记"}, status_code=403)
    return await call_next(request)


class ActionReq(BaseModel):
    scope: str = "user"            # user | global
    sudo_password: str | None = None
    prefix: str | None = None      # 指定卸载某个安装位置（重复安装场景）


def _sudo_arg(req: "ActionReq", need_sudo: bool) -> str | None:
    """返回传给安装器的提权参数；Windows 用 UAC 提权标记。"""
    if not need_sudo:
        return None
    if installer.IS_WINDOWS:
        return installer.ELEVATE_MARKER
    return req.sudo_password


def _detected_update_prefix(agent: dict, requested: str | None = None) -> str | None:
    """更新必须跟随已探测到的主安装，不能按新装 scope 落到别的前缀。"""
    installs = detect.find_installs(
        agent["command"], agent.get("bin_paths"), detect.desktop_path(agent))
    known = {str(item["prefix"]) for item in installs if item.get("prefix")}
    if requested and requested not in known:
        raise HTTPException(400, f"未知的安装位置: {requested}")
    if requested:
        return requested
    return installs[0]["prefix"] if installs else None


def _new_task() -> str:
    _prune_tasks()
    tid = uuid.uuid4().hex[:8]
    TASKS[tid] = {"status": "running", "log": [], "created_at": time.monotonic()}
    return tid


def _finish_task(tid: str, status: str) -> None:
    task = TASKS.get(tid)
    if task is None:
        return
    task["status"] = status
    task["finished_at"] = time.monotonic()


def _spawn(target: Path) -> None:
    """后台启动程序：不阻塞服务，关闭终端也不影响。"""
    kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
              "stderr": subprocess.DEVNULL}
    if platform.system() == "Darwin" and target.suffix.lower() == ".app":
        subprocess.Popen(["open", str(target)], **kwargs)
        return
    if os.name == "nt":
        kwargs["creationflags"] = (subprocess.DETACHED_PROCESS
                                   | subprocess.CREATE_NEW_PROCESS_GROUP)
        kwargs["close_fds"] = True
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([str(target)], **kwargs)


def _start_download(agent: dict) -> dict:
    tid = _new_task()
    log: list[str] = TASKS[tid]["log"]  # type: ignore[index]

    async def job() -> None:
        try:
            target = await asyncio.to_thread(download.install_agent, agent, log)
            log.append(f"✅ 安装完成：{target}\n")
            try:
                _spawn(target)
                log.append(f"🚀 已启动：{target}\n")
            except OSError as exc:
                log.append(f"安装完成，但自动启动失败：{exc}\n")
            _finish_task(tid, "done")
        except Exception as e:  # noqa: BLE001
            log.append(f"\n[error] {e}\n")
            _finish_task(tid, "failed")

    asyncio.create_task(job())
    return {"task_id": tid}


def _prune_tasks() -> None:
    now = time.monotonic()
    for tid, task in list(TASKS.items()):
        finished = task.get("finished_at")
        if task.get("status") != "running" and finished is not None:
            if now - finished > TASK_TTL_SECONDS:
                TASKS.pop(tid, None)
    overflow = len(TASKS) - TASK_MAX_COUNT
    if overflow <= 0:
        return
    completed = sorted(
        ((task.get("finished_at", task.get("created_at", 0)), tid)
         for tid, task in TASKS.items() if task.get("status") != "running"),
        key=lambda item: item[0],
    )
    for _, tid in completed[:overflow]:
        TASKS.pop(tid, None)


async def _run_task(tid: str, argv: list[str] | None, sudo_password: str | None,
                    tail_hint: str | None = None, agent_id: str | None = None) -> None:
    log: list[str] = TASKS[tid]["log"]  # type: ignore[index]
    try:
        if argv is None:
            log.append("该工具暂无自动安装源，请按页面提示手动安装。\n")
            _finish_task(tid, "failed")
            return
        code = await installer.run(argv, log, sudo_password)
        if tail_hint:
            log.append("\n" + tail_hint + "\n")
        if code == 0 and agent_id:
            await _auto_localize(agent_id, log)
        _finish_task(tid, "done" if code == 0 else "failed")
        log.append(f"\n[exit code: {code}]\n")
    except Exception as e:  # noqa: BLE001
        log.append(f"\n[error] {e}\n")
        _finish_task(tid, "failed")


def _i18n_prefs() -> tuple[bool, dict]:
    """安全读取汉化设置。

    设置来自前端 JSON，形状可能是任何东西（`{"agents": []}` 之类），
    这里一律降级成默认值——汉化是附加功能，绝不能因此把安装或启动弄成失败。
    """
    try:
        raw = cloud.load_settings().get("i18n")
    except Exception:  # noqa: BLE001
        return True, {}
    if not isinstance(raw, dict):
        return True, {}
    auto = raw.get("auto", True)
    agents = raw.get("agents")
    return (auto if isinstance(auto, bool) else True,
            agents if isinstance(agents, dict) else {})


async def _auto_localize(agent_id: str, log: list[str]) -> None:
    """安装/更新成功后自动重打汉化补丁（升级会覆盖掉旧补丁）。"""
    try:
        auto, agent_prefs = _i18n_prefs()
        if not auto or agent_prefs.get(agent_id, True) is False:
            return
        agent = registry.get_agent(agent_id)
        if not agent or not agent.get("i18n"):
            return
        result = await asyncio.to_thread(localize.ensure, agent)
        if result:
            log.append("\n[汉化] " + str(result.get("message", "")) + "\n")
    except Exception as e:  # noqa: BLE001 - 汉化失败不影响安装结果
        log.append(f"\n[汉化] 跳过：{e}\n")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/tools")
async def api_tools() -> list[dict]:
    agents = registry.load_agents()
    tools = await detect.scan_all(agents)
    for tool in tools:
        agent = registry.get_agent(tool.get("id", ""))
        tool["provider_supported"] = bool(agent and providers.supported(agent))
        tool["requires"] = engines.check(agent) if agent else None
        tool["launchable"] = bool(agent and detect.launch_target(agent))
    return tools


@app.get("/api/env")
async def api_env() -> dict:
    """本机安装管理器（node/npm/pnpm/git/brew/scoop/winget/pipx）+ Python/系统信息。"""
    data = await asyncio.to_thread(envmod.check_env)
    agents = registry.load_agents()
    if engines.stale(agents):
        asyncio.create_task(asyncio.to_thread(engines.refresh, agents))
    return data


@app.post("/api/env/install/{name}")
async def api_env_install(name: str) -> dict:
    if name not in envmod.MANAGERS:
        raise HTTPException(404, "unknown manager")
    argv, hint = envmod.install_argv(name)
    if argv is None:
        raise HTTPException(400, hint or f"请手动安装 {name}")
    tid = _new_task()
    asyncio.create_task(_run_task(tid, argv, None, hint))
    return {"task_id": tid}


ARCHIVES_APPS = Path.home() / ".aac" / "apps"


def _extract_archive(pkg: Path, dest: Path, log: list[str]) -> bool:
    """解 zip / tar / tar.gz / tgz 到 dest；完整预检路径、链接和特殊文件。"""
    try:
        archive.safe_extract(pkg, dest, archive.DEFAULT_LIMITS)
        return True
    except Exception as e:  # noqa: BLE001
        log.append(f"[error] 解压失败: {e}\n")
        return False


async def _save_upload(file: UploadFile, dest: Path,
                       limit: int) -> int:
    ensure_private_dir(dest.parent)
    total = 0
    try:
        with dest.open("xb") as out:
            while True:
                chunk = await file.read(1 << 20)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise HTTPException(
                        413, f"上传文件超过 {limit // 1024 // 1024}MB 限制")
                out.write(chunk)
        return total
    except Exception:
        dest.unlink(missing_ok=True)
        raise


def _cleanup_imports() -> None:
    imports = bundle.IMPORTS_DIR
    ensure_private_dir(imports)
    now = time.time()
    entries: list[tuple[float, Path]] = []
    for path in imports.iterdir():
        try:
            st = path.lstat()
        except OSError:
            continue
        if path.is_dir() and not path.is_symlink():
            continue
        entries.append((st.st_mtime, path))
    for mtime, path in entries:
        if now - mtime > IMPORT_TTL_SECONDS:
            path.unlink(missing_ok=True)
    remaining = sorted((entry for entry in entries if entry[1].exists()), key=lambda e: e[0])
    for _, path in remaining[:-100]:
        path.unlink(missing_ok=True)


def _import_path(filename: str) -> Path:
    path = bundle.IMPORTS_DIR / Path(filename).name
    if path.is_symlink() or not path.is_file():
        raise HTTPException(404, "uploaded file not found")
    try:
        if path.resolve().parent != bundle.IMPORTS_DIR.resolve():
            raise HTTPException(404, "uploaded file not found")
    except OSError as e:
        raise HTTPException(404, "uploaded file not found") from e
    return path


@app.post("/api/install-archive/{agent_id}")
async def api_install_archive(agent_id: str, file: UploadFile) -> dict:
    """压缩包识别安装：解压 → 在包内找该工具的命令二进制 → 落到 ~/.aac/apps/<id>/bin。"""
    agent = registry.get_agent(agent_id)
    if not agent:
        raise HTTPException(404, "unknown agent")
    ensure_private_dir(ARCHIVES_APPS.parent)
    fd, upload_name = tempfile.mkstemp(
        prefix=".aac-archive-", suffix=".bin", dir=ARCHIVES_APPS.parent)
    os.close(fd)
    upload_path = Path(upload_name)
    upload_path.unlink()
    try:
        size = await _save_upload(file, upload_path, ARCHIVE_UPLOAD_LIMIT)
    except Exception:
        upload_path.unlink(missing_ok=True)
        raise
    tid = _new_task()
    log: list[str] = TASKS[tid]["log"]  # type: ignore[index]

    async def job() -> None:
        try:
            ensure_private_dir(ARCHIVES_APPS)
            with tempfile.TemporaryDirectory(prefix=f".{agent_id}-", dir=ARCHIVES_APPS.parent) as tmp:
                stage = Path(tmp)
                extracted = stage / "payload"
                extracted.mkdir()
                log.append(f"📦 收到压缩包 {file.filename}（{size // 1024} KB），开始解压…\n")
                if not await asyncio.to_thread(
                        _extract_archive, upload_path, extracted, log):
                    _finish_task(tid, "failed")
                    return
                cmd = agent["command"]
                names = {cmd, f"{cmd}.exe", f"{cmd}.cmd", f"{cmd}.bat"}
                found = next((p for p in extracted.rglob("*")
                              if p.is_file() and p.name in names), None)
                if not found:
                    exes = [p for p in extracted.rglob("*") if p.is_file()
                            and (os.access(p, os.X_OK) or p.suffix.lower() == ".exe")
                            and not p.name.startswith(".")]
                    if len(exes) == 1:
                        found = exes[0]
                if not found:
                    top = ", ".join(p.name for p in sorted(extracted.iterdir())[:10])
                    log.append(f"[error] 包里没找到可执行文件 {cmd}。包内容：{top}\n")
                    _finish_task(tid, "failed")
                    return
                bin_dir = extracted / "bin"
                bin_dir.mkdir(exist_ok=True)
                target = bin_dir / found.name
                if found != target:
                    shutil.move(str(found), target)
                if not installer.IS_WINDOWS:
                    target.chmod(0o755)
                dest = ARCHIVES_APPS / agent_id
                if dest.is_symlink() or (dest.exists() and not dest.is_dir()):
                    raise ValueError("已有安装目录是符号链接或非目录，拒绝替换")
                old = dest.with_name(dest.name + f".old-{uuid.uuid4().hex}")
                if dest.exists():
                    dest.rename(old)
                try:
                    extracted.rename(dest)
                except Exception:
                    if old.exists() and not dest.exists():
                        old.rename(dest)
                    raise
                if old.exists():
                    shutil.rmtree(old)
                target = dest / "bin" / found.name
                log.append(f"✅ 识别到可执行文件：{target}\n")
                log.append("安装完成。若终端找不到命令，可执行：\n"
                           f"  export PATH=\"{dest / 'bin'}:$PATH\"\n")
                _finish_task(tid, "done")
        except Exception as e:  # noqa: BLE001
            log.append(f"\n[error] {e}\n")
            _finish_task(tid, "failed")
        finally:
            upload_path.unlink(missing_ok=True)

    asyncio.create_task(job())
    return {"task_id": tid}


def _require_node(agent: dict, action: str) -> None:
    """安装/更新前校验 Node 下限：取 npm 上最新版的 engines.node 与本机比较。"""
    problem = engines.check(agent, refresh_cache=True)
    if problem and problem["ok"] is False:
        have = problem["have"] or "未安装"
        raise HTTPException(
            400,
            f"ENV_VERSION:node:{problem['required']}:{have}:"
            f"{action} {agent['name']} 需要 Node {problem['required']}，当前 {have}",
        )


@app.post("/api/install/{agent_id}")
async def api_install(agent_id: str, req: ActionReq) -> dict:
    agent = registry.get_agent(agent_id)
    if not agent:
        raise HTTPException(404, "unknown agent")
    if download.available(agent):
        if not download.platform_source_available(agent):
            raise HTTPException(
                400,
                f"当前平台（{download.platform_key()}）暂无安装包："
                "可点「访问官网」手动下载，或用「压缩包安装」")
        return _start_download(agent)
    _require_node(agent, "安装")
    src = installer.available_source(agent)
    if not src:
        miss = installer.missing_manager(agent)
        if miss:
            raise HTTPException(400, f"ENV_MISSING:{miss}:安装 {agent['name']} 需要 {miss}，但本机未安装")
        raise HTTPException(400, "该工具暂无自动安装源：可点「访问官网」手动安装，或用顶部「压缩包安装」")
    mgr, pkg = src
    if mgr != "npm":
        argv = (installer.build_brew_argv(agent, "install") if mgr == "brew"
                else installer.build_manager_argv(mgr, pkg, "install"))
        tid = _new_task()
        asyncio.create_task(_run_task(tid, argv, None, agent_id=agent_id))
        return {"task_id": tid}
    if req.scope == "global" and not installer.npm_prefix_writable(registry.get_agent(agent_id).get("command")) and not req.sudo_password:
        raise HTTPException(400, "该工具安装在系统目录，需要 sudo 密码：请在弹窗中输入")
    argv, need_sudo = installer.build_install_argv(agent, req.scope)
    if need_sudo and not installer.IS_WINDOWS and not req.sudo_password:
        raise HTTPException(400, "该工具安装在系统目录，需要 sudo 密码：请在弹窗中输入")
    tid = _new_task()
    hint = installer.user_prefix_path_hint() if req.scope == "user" else None
    asyncio.create_task(_run_task(tid, argv, _sudo_arg(req, need_sudo), hint, agent_id=agent_id))
    return {"task_id": tid}


class FolderReq(BaseModel):
    path: str


class LaunchReq(BaseModel):
    mode: str = "normal"
    resume: bool = False
    terminal: str | None = None
    custom_terminal: str | None = None
    cwd: str | None = None
    confirm_ack: bool = False


@app.get("/api/launch/options")
def api_launch_options() -> dict:
    settings = cloud.load_settings()
    launch_settings = settings.get("launch") or {}
    launch_agents: dict = {}
    for agent in registry.load_agents():
        launch_spec = agent.get("launch")
        if not isinstance(launch_spec, dict):
            continue
        target = detect.launch_target(agent)
        modes = []
        for mode_id, mode_spec in (launch_spec.get("modes") or {}).items():
            modes.append({
                "id": mode_id,
                "name": mode_spec.get("name", mode_id),
                "args": mode_spec.get("args", []),
                "supported": mode_spec.get("supported") is not False,
                "confirm": bool(mode_spec.get("confirm")),
                "warning": mode_spec.get("warning"),
                "note": mode_spec.get("note"),
                "reason": mode_spec.get("reason"),
            })
        launch_agents[agent["id"]] = {
            "launchable": target is not None,
            "target": str(target) if target else None,
            "modes": modes,
            "resume": bool(launch_spec.get("resume_args")),
        }
    return {
        "terminals": launch.detect_terminals(),
        "default_terminal": launch.default_terminal_id(),
        "agents": launch_agents,
        "settings": {
            "terminal": launch_settings.get("terminal", "auto"),
            "custom_terminal": launch_settings.get("custom_terminal", ""),
            "resume": launch_settings.get("resume", False),
            "modes": launch_settings.get("modes", {}),
            "cwd": launch_settings.get("cwd", ""),
        },
    }


@app.post("/api/launch/{agent_id}")
async def api_launch(agent_id: str, req: LaunchReq | None = None) -> dict:
    req = req or LaunchReq()
    agent = registry.get_agent(agent_id)
    if not agent:
        raise HTTPException(404, "未找到该工具")
    launch_spec = agent.get("launch")
    if not isinstance(launch_spec, dict):
        target = detect.launch_target(agent)
        if target is None:
            raise HTTPException(400, "未找到可启动的程序，请先安装")
        try:
            _spawn(target)
        except OSError as e:
            raise HTTPException(400, f"启动失败：{e}") from e
        return {"ok": True, "legacy": True}

    modes = launch_spec.get("modes") or {}
    mode_spec = modes.get(req.mode) if isinstance(modes, dict) else None
    if not isinstance(mode_spec, dict):
        raise HTTPException(400, f"未知启动模式：{req.mode}")
    if mode_spec.get("supported") is False:
        raise HTTPException(400, mode_spec.get("reason") or "该工具不支持此启动模式")
    if mode_spec.get("confirm") and not req.confirm_ack:
        raise HTTPException(400, "危险模式需要显式确认")

    # 启动前保汉化（升级后补丁会丢，这里兜底；失败不影响启动）
    await _ensure_i18n(agent)

    settings = cloud.load_settings()
    launch_settings = dict(settings.get("launch") or {})
    if req.terminal is not None:
        launch_settings["terminal"] = req.terminal
    if req.custom_terminal is not None:
        launch_settings["custom_terminal"] = req.custom_terminal.strip()
    if req.cwd is not None:
        launch_settings["cwd"] = req.cwd
    try:
        plan = launch.plan_launch(agent, req.mode, req.resume, launch_settings,
                                  req.cwd)
    except (OSError, ValueError) as e:
        raise HTTPException(400, str(e)) from e
    result = await asyncio.to_thread(launch.run_plan, plan)
    if not result.get("ok"):
        raise HTTPException(400, result.get("error") or "启动失败")

    last_modes = launch_settings.get("modes")
    last_modes = dict(last_modes) if isinstance(last_modes, dict) else {}
    last_modes[agent_id] = req.mode
    cloud.save_settings({"launch": {
        "terminal": launch_settings.get("terminal") or plan["terminal"],
        "custom_terminal": launch_settings.get("custom_terminal", ""),
        "resume": req.resume,
        "cwd": plan["cwd"],
        "modes": last_modes,
    }})
    return {"ok": True, "terminal": plan["terminal"],
            "command": plan["command"], "argv": plan["argv"], "cwd": plan["cwd"]}


@app.post("/api/install-folder/{agent_id}")
async def api_install_folder(agent_id: str, req: FolderReq) -> dict:
    """手动安装：复制用户选择的文件夹（或 .app）到安装目录。"""
    agent = registry.get_agent(agent_id)
    if not agent:
        raise HTTPException(404, "unknown agent")
    tid = _new_task()
    log: list[str] = TASKS[tid]["log"]  # type: ignore[index]

    async def job() -> None:
        try:
            target = await asyncio.to_thread(
                download.install_folder, agent, Path(req.path), log)
            log.append(f"✅ 安装完成：{target}\n")
            _finish_task(tid, "done")
        except Exception as e:  # noqa: BLE001
            log.append(f"\n[error] {e}\n")
            _finish_task(tid, "failed")

    asyncio.create_task(job())
    return {"task_id": tid}


@app.post("/api/update/{agent_id}")
async def api_update(agent_id: str, req: ActionReq) -> dict:
    agent = registry.get_agent(agent_id)
    if not agent:
        raise HTTPException(404, "unknown agent")
    if download.available(agent):
        if not download.platform_source_available(agent):
            raise HTTPException(
                400,
                f"当前平台（{download.platform_key()}）暂无安装包："
                "可点「访问官网」手动下载，或用「压缩包安装」")
        return _start_download(agent)
    _require_node(agent, "更新")
    if not (agent.get("sources") or {}).get("npm"):
        src = installer.available_source(agent)
        if not src:
            miss = installer.missing_manager(agent)
            if miss:
                raise HTTPException(400, f"ENV_MISSING:{miss}:更新 {agent['name']} 需要 {miss}，但本机未安装")
            raise HTTPException(400, "该工具暂无自动更新方式")
        mgr, pkg = src
        argv = (installer.build_brew_argv(agent, "update") if mgr == "brew"
                else installer.build_manager_argv(mgr, pkg, "update"))
        tid = _new_task()
        asyncio.create_task(_run_task(tid, argv, None, agent_id=agent_id))
        return {"task_id": tid}
    target_prefix = _detected_update_prefix(agent, req.prefix)
    argv, need_sudo = installer.build_update_argv(agent, req.scope, target_prefix)
    if need_sudo and not installer.IS_WINDOWS and not req.sudo_password:
        raise HTTPException(400, "该工具安装在系统目录，需要 sudo 密码：请在弹窗中输入")
    tid = _new_task()
    asyncio.create_task(_run_task(tid, argv, _sudo_arg(req, need_sudo), agent_id=agent_id))
    return {"task_id": tid}


@app.post("/api/uninstall/{agent_id}")
async def api_uninstall(agent_id: str, req: ActionReq) -> dict:
    agent = registry.get_agent(agent_id)
    if not agent:
        raise HTTPException(404, "unknown agent")
    archive_prefix = Path.home() / ".aac" / "apps" / agent_id
    archive_installs = detect.find_installs(agent["command"], agent.get("bin_paths"))
    archive_known = {str(i["prefix"]) for i in archive_installs if i.get("prefix")}
    if req.prefix and req.prefix not in archive_known:
        raise HTTPException(400, f"未知的安装位置: {req.prefix}")
    archive_prefix_arg = req.prefix or (archive_installs[0]["prefix"] if archive_installs else None)
    desktop = detect.desktop_path(agent)
    if desktop is not None and desktop.suffix.lower() == ".app":
        try:
            desktop.resolve().relative_to(Path.home().resolve())
        except ValueError:
            raise HTTPException(400, "该程序安装在系统目录，请手动拖到废纸篓") from None
        tid = _new_task()
        log: list[str] = TASKS[tid]["log"]  # type: ignore[index]

        async def rmdir() -> None:
            log.append(f"🗑 删除 {desktop} …\n")
            try:
                await asyncio.to_thread(shutil.rmtree, desktop)
                parent = desktop.parent
                if (parent.name == agent_id and parent.parent.name == "apps"
                        and parent.parent.parent == Path.home() / ".aac"
                        and parent.is_dir() and not any(parent.iterdir())):
                    parent.rmdir()
                log.append("已删除。\n")
                _finish_task(tid, "done")
            except Exception as e:  # noqa: BLE001
                log.append(f"[error] {e}\n")
                _finish_task(tid, "failed")

        asyncio.create_task(rmdir())
        return {"task_id": tid}
    if archive_prefix_arg and Path(archive_prefix_arg) == archive_prefix:
        if (Path(archive_prefix_arg).parent != archive_prefix.parent
                or archive_prefix.parent.is_symlink()
                or Path(archive_prefix_arg).is_symlink()
                or not Path(archive_prefix_arg).is_dir()):
            raise HTTPException(400, "压缩包安装位置不是安全目录")
        tid = _new_task()
        log: list[str] = TASKS[tid]["log"]  # type: ignore[index]

        async def rmjob() -> None:
            log.append(f"🗑 删除压缩包安装目录 {archive_prefix_arg} …\n")
            try:
                if (Path(archive_prefix_arg).parent != archive_prefix.parent
                        or archive_prefix.parent.is_symlink()
                        or Path(archive_prefix_arg).is_symlink()
                        or not Path(archive_prefix_arg).is_dir()):
                    raise ValueError("压缩包安装位置在删除前发生变化")
                await asyncio.to_thread(shutil.rmtree, archive_prefix_arg)
                log.append("已删除。\n")
                _finish_task(tid, "done")
            except Exception as e:  # noqa: BLE001
                log.append(f"[error] {e}\n")
                _finish_task(tid, "failed")

        asyncio.create_task(rmjob())
        return {"task_id": tid}
    if not (agent.get("sources") or {}).get("npm"):
        prefix = archive_prefix_arg
        if agent.get("managed_files"):
            # 独立二进制（如 Kimi Code）：只删程序文件，保留配置/会话/skills
            paths = installer.managed_remove_paths(agent, prefix) if prefix else None
            if not paths:
                raise HTTPException(400, "未找到可卸载的程序文件（或安装位置校验失败）")
            import os as _os
            writable = all(_os.access(p.parent, _os.W_OK) for p in paths if p.exists())
            if not writable and not installer.IS_WINDOWS and not req.sudo_password:
                raise HTTPException(400, "该安装位置需要 sudo 密码：请在弹窗中输入")
            tid = _new_task()
            log: list[str] = TASKS[tid]["log"]  # type: ignore[index]

            async def job() -> None:
                if writable or installer.IS_WINDOWS:
                    log.append("🗑 卸载程序文件（保留配置/会话/skills）…\n")
                    code = await asyncio.to_thread(installer.remove_paths, paths, log)
                else:
                    code = await installer.run(
                        ["rm", "-rf", *[str(p) for p in paths]], log, req.sudo_password)
                    log.append("配置、会话与 skills 已保留。\n")
                _finish_task(tid, "done" if code == 0 else "failed")

            asyncio.create_task(job())
            return {"task_id": tid}
        tid = _new_task()
        src = installer.available_source(agent)
        if src:
            mgr, pkg = src
            argv = (installer.build_brew_argv(agent, "uninstall") if mgr == "brew"
                    else installer.build_manager_argv(mgr, pkg, "uninstall"))
            asyncio.create_task(_run_task(tid, argv, None))
            return {"task_id": tid}
        raise HTTPException(400, "该工具暂无自动卸载方式：请按说明手动删除")
    npm_pkg = (agent.get("sources") or {}).get("npm")
    installs = detect.find_installs(
        agent["command"], agent.get("bin_paths"), detect.desktop_path(agent))
    known = {str(i["prefix"]) for i in installs if i.get("prefix")}
    if req.prefix and req.prefix not in known:
        raise HTTPException(400, f"未知的安装位置: {req.prefix}")
    cand = req.prefix or (installs[0]["prefix"] if installs else None)
    script_prefix = agent.get("script_prefix")
    if cand and installer.is_script_layout(
            cand, agent["command"], npm_pkg, script_prefix):
        # 脚本安装布局（如 ~/.opencode）：npm uninstall 会「假成功」，直接删程序文件
        tid = _new_task()
        log3: list[str] = TASKS[tid]["log"]  # type: ignore[index]

        async def sjob() -> None:
            code = await asyncio.to_thread(
                installer.remove_script_prefix,
                cand, agent["command"], npm_pkg, log3, script_prefix)
            _finish_task(tid, "done" if code == 0 else "failed")

        asyncio.create_task(sjob())
        return {"task_id": tid}
    if req.prefix:
        # 卸载指定位置的副本（重复安装清理）：前面已校验该前缀确实是探测到的位置
        argv = installer.build_uninstall_argv_for_prefix(agent, req.prefix)
        need_sudo = not installer.IS_WINDOWS and not installer.prefix_writable(req.prefix)
        if need_sudo and not req.sudo_password:
            raise HTTPException(400, "该安装位置在系统目录，需要 sudo 密码：请在弹窗中输入")
        tid = _new_task()
        asyncio.create_task(_run_task(tid, argv, _sudo_arg(req, need_sudo)))
        return {"task_id": tid}
    argv, need_sudo = installer.build_uninstall_argv(agent, req.scope)
    if need_sudo and not installer.IS_WINDOWS and not req.sudo_password:
        raise HTTPException(400, "该工具安装在系统目录，需要 sudo 密码：请在弹窗中输入")
    tid = _new_task()
    asyncio.create_task(_run_task(tid, argv, _sudo_arg(req, need_sudo)))
    return {"task_id": tid}


@app.post("/api/safe-update/{agent_id}")
async def api_safe_update(agent_id: str, req: ActionReq) -> dict:
    """带补丁保护的更新：快照 → 更新 → 自动重打/移植补丁。"""
    agent = registry.get_agent(agent_id)
    if not agent or not (agent.get("sources") or {}).get("npm"):
        raise HTTPException(404, "unknown or not auto-updatable agent")
    tid = _new_task()
    log: list[str] = TASKS[tid]["log"]  # type: ignore[index]

    async def job() -> None:
        try:
            patches = await asyncio.to_thread(catmod.detect_patches, agent)
            if not isinstance(patches, dict):
                log.append("\n[error] 无法检测程序修改，拒绝执行未受保护的更新\n")
                _finish_task(tid, "failed")
                return
            if patches.get("error"):
                log.append(f"\n[error] 补丁检测失败：{patches['error']}\n"
                           "为避免覆盖修改，已取消更新\n")
                _finish_task(tid, "failed")
                return
            n = len(patches.get("modified", [])) + len(patches.get("added", []))
            has_patches = n > 0
            if has_patches:
                log.append(f"⚠️ 检测到 {n} 个程序修改文件，更新会先覆盖它们。正在保存补丁快照…\n")
                saved = await asyncio.to_thread(
                    patchwork.snapshot, agent, patches, log)
                if not saved:
                    log.append("\n[error] 补丁快照失败，已取消更新\n")
                    _finish_task(tid, "failed")
                    return
            target_prefix = _detected_update_prefix(agent, req.prefix)
            argv, need_sudo = installer.build_update_argv(
                agent, req.scope, target_prefix)
            if need_sudo and not installer.IS_WINDOWS and not req.sudo_password:
                log.append("\n[error] 全局更新需要 sudo 密码\n")
                _finish_task(tid, "failed")
                return
            log.append("\n⬆️ 开始更新…\n")
            code = await installer.run(argv, log, _sudo_arg(req, need_sudo))
            if code != 0:
                _finish_task(tid, "failed")
                log.append(f"\n更新失败（exit {code}），补丁快照仍在 ~/.aac/patchwork 中\n")
                return
            if has_patches:
                log.append("\n🔧 正在把补丁打回新版本…\n")
                reapply_result = await asyncio.to_thread(
                    patchwork.reapply, agent, log)
                # 更新后自动验证：重新与官方包比对，确认补丁是否完好
                log.append("\n🔍 验证补丁是否完好…\n")
                after = await asyncio.to_thread(catmod.detect_patches, agent)
                complete = reapply_result.get("status") == "complete"
                if not isinstance(after, dict) or after.get("error"):
                    complete = False
                    detail = (after or {}).get("error") if isinstance(after, dict) else "无法检测"
                    log.append(f"⚠️ 验证失败（{detail}），请打开配置详情手动确认补丁状态\n")
                    survived = reapply_result.get("ok", 0) + reapply_result.get("ported", 0)
                else:
                    survived = (len(after.get("modified", []))
                                + len(after.get("added", [])))
                    lost = max(0, n - survived)
                    if lost:
                        complete = False
                if not complete:
                    if reapply_result.get("failed"):
                        log.append(
                            f"⚠️ 补丁重打不完整：成功 {reapply_result.get('ok', 0)}，"
                            f"移植 {reapply_result.get('ported', 0)}，"
                            f"失败 {reapply_result.get('failed', 0)}"
                            "（快照在 ~/.aac/patchwork，可手动恢复）\n")
                    _finish_task(tid, "partial")
                else:
                    log.append(f"✅ 补丁验证通过：{survived} 个修改文件全部完好\n")
                    _finish_task(tid, "done")
                return
            _finish_task(tid, "done")
        except Exception as e:  # noqa: BLE001
            log.append(f"\n[error] {e}\n")
            _finish_task(tid, "failed")

    asyncio.create_task(job())
    return {"task_id": tid}


@app.get("/api/tasks/{task_id}")
def api_task(task_id: str) -> dict:
    _prune_tasks()
    task = TASKS.get(task_id)
    if not task:
        raise HTTPException(404, "unknown task")
    return {"status": task["status"], "log": "".join(task["log"])}


# ------------------------------------------------------------------ M4 云同步与自动备份

class SettingsReq(BaseModel):
    webdav: dict | None = None
    auto_backup: dict | None = None
    ccswitch: dict | None = None
    launch: dict | None = None
    i18n: dict | None = None
    export: dict | None = None


@app.get("/api/settings")
def api_settings() -> dict:
    s = cloud.load_settings()
    if s["webdav"].get("password"):
        s["webdav"]["password_set"] = True
        s["webdav"]["password"] = ""  # 不回传明文
    return s


@app.post("/api/settings")
def api_save_settings(req: SettingsReq) -> dict:
    patch: dict = {}
    if req.webdav is not None:
        wd = dict(req.webdav)
        if not wd.get("password") and wd.get("password_set"):
            wd["password"] = cloud.load_settings()["webdav"].get("password", "")
        wd.pop("password_set", None)
        patch["webdav"] = wd
    if req.auto_backup is not None:
        patch["auto_backup"] = req.auto_backup
    if req.ccswitch is not None:
        patch["ccswitch"] = req.ccswitch
    if req.launch is not None:
        patch["launch"] = req.launch
    if req.i18n is not None:
        patch["i18n"] = req.i18n
    if req.export is not None:
        cfg = dict(cloud.load_settings().get("export") or {})
        cfg.update(req.export)
        cfg["session_max_mb"] = bundle.clamp_session_mb(cfg.get("session_max_mb"))
        patch["export"] = cfg
    cloud.save_settings(patch)
    return {"saved": True}


class FooterReq(BaseModel):
    config: dict | None = None
    agent_id: str | None = None


@app.get("/api/footer")
async def api_footer(agent_id: str | None = None) -> dict:
    return await asyncio.to_thread(footer.catalog, agent_id)


@app.post("/api/footer")
async def api_footer_save(req: FooterReq) -> dict:
    saved = await asyncio.to_thread(footer.save_config, req.config or {})
    return {"saved": True, "config": saved,
            "preview": footer.render_lines(saved)}


@app.post("/api/footer/preview")
async def api_footer_preview(req: FooterReq) -> dict:
    cfg = footer.normalize(req.config or {})
    return {"config": cfg, "preview": footer.render_lines(cfg)}


async def _ensure_i18n(agent: dict) -> None:
    """启动前自动补汉化；任何异常都吞掉，不影响启动。"""
    try:
        auto, agent_prefs = _i18n_prefs()
        if not auto or agent_prefs.get(agent["id"], True) is False:
            return
        if not agent.get("i18n"):
            return
        await asyncio.to_thread(localize.ensure, agent)
    except Exception:  # noqa: BLE001
        pass


@app.get("/api/i18n")
def api_i18n() -> dict:
    """汉化总览：每个声明了 i18n 的工具当前状态。"""
    auto, enabled_map = _i18n_prefs()
    items = localize.catalog(registry.load_agents())
    for item in items:
        item["enabled"] = enabled_map.get(item["id"], True) is not False
    return {"auto": auto, "agents": items}


class I18nReq(BaseModel):
    enabled: bool | None = None
    auto: bool | None = None
    action: str | None = None   # apply | revert


@app.post("/api/i18n/{agent_id}")
async def api_i18n_agent(agent_id: str, req: I18nReq) -> dict:
    agent = registry.get_agent(agent_id)
    if not agent:
        raise HTTPException(404, "未找到该工具")
    if not agent.get("i18n"):
        raise HTTPException(400, "该工具暂不支持汉化")
    auto, enabled_map = _i18n_prefs()
    enabled_map = dict(enabled_map)
    if req.enabled is not None:
        enabled_map[agent_id] = req.enabled
    patch: dict = {"agents": enabled_map, "auto": req.auto if req.auto is not None else auto}
    cloud.save_settings({"i18n": patch})

    result = None
    if req.action == "apply":
        result = await asyncio.to_thread(localize.apply, agent)
    elif req.action == "revert":
        result = await asyncio.to_thread(localize.revert, agent)
    elif req.action not in (None, ""):
        raise HTTPException(400, f"未知操作：{req.action}")
    return {"saved": True, "result": result,
            "status": await asyncio.to_thread(localize.status, agent)}


class WhaleReq(BaseModel):
    action: str = "status"   # install | uninstall | status


@app.get("/api/whale")
def api_whale_status() -> dict:
    return whale.status()


@app.post("/api/whale")
async def api_whale(req: WhaleReq) -> dict:
    if req.action == "install":
        return await asyncio.to_thread(whale.install)
    if req.action == "uninstall":
        return await asyncio.to_thread(whale.uninstall)
    if req.action == "status":
        return {"ok": True, "status": await asyncio.to_thread(whale.status)}
    raise HTTPException(400, f"未知操作：{req.action}")


@app.get("/api/footer/ext")
def api_footer_ext_status() -> dict:
    return footer_ext.status()


@app.get("/api/footer/ext/claude")
def api_footer_claude_status() -> dict:
    return footer_ext.claude_status()


@app.post("/api/footer/ext/claude")
async def api_footer_claude(req: WhaleReq) -> dict:
    if req.action == "install":
        return await asyncio.to_thread(footer_ext.claude_install)
    if req.action == "uninstall":
        return await asyncio.to_thread(footer_ext.claude_uninstall)
    return {"ok": True, "status": await asyncio.to_thread(footer_ext.claude_status)}


@app.post("/api/footer/ext")
async def api_footer_ext(req: WhaleReq) -> dict:
    if req.action == "install":
        return await asyncio.to_thread(footer_ext.install)
    if req.action == "uninstall":
        return await asyncio.to_thread(footer_ext.uninstall)
    if req.action == "status":
        return {"ok": True, "status": await asyncio.to_thread(footer_ext.status)}
    raise HTTPException(400, f"未知操作：{req.action}")


@app.post("/api/cloud/test")
async def api_cloud_test() -> dict:
    try:
        return {"ok": True, "msg": await asyncio.to_thread(cloud.get_client().test)}
    except cloud.WebDAVError as e:
        return {"ok": False, "msg": str(e)}


def _cloud_pull_decision(
    previous: dict,
    local_info: dict | None,
    remote_fingerprint: str,
) -> str:
    remote_changed = previous.get("remote_fingerprint") != remote_fingerprint
    if local_info is None:
        return "pull" if remote_changed else "unchanged"
    previous_local_hash = previous.get("local_sha256")
    local_changed = (
        not previous_local_hash
        or previous_local_hash != local_info["sha256"]
    )
    if local_changed and remote_changed:
        return "conflict"
    if local_changed:
        return "local"
    return "pull" if remote_changed else "unchanged"


def _cloud_job(tid: str, push: bool) -> None:
    log: list[str] = TASKS[tid]["log"]  # type: ignore[index]

    async def job() -> None:
        try:
            client = cloud.get_client()
            s = cloud.load_settings()["webdav"]
            remote_dir = s["remote_dir"].strip("/") or "aac"
            client.ensure_dir(remote_dir)
            remote = await asyncio.to_thread(client.list_files, remote_dir)
            local: dict[str, dict] = {}
            if bundle.EXPORTS_DIR.is_dir():
                for path in bundle.EXPORTS_DIR.glob("*.aac"):
                    if not path.is_file() or path.is_symlink():
                        continue
                    if not cloud.safe_remote_name(path.name):
                        continue
                    local[path.name] = {
                        "path": path,
                        "size": path.stat().st_size,
                        "sha256": await asyncio.to_thread(cloud.sha256_file, path),
                    }
            state = cloud.load_sync_state()
            section = state.setdefault(remote_dir, {})
            if push:
                todo = []
                for name, info in local.items():
                    previous = section.get(name) or {}
                    meta = remote.get(name)
                    unchanged = (
                        previous.get("local_sha256") == info["sha256"]
                        and isinstance(meta, dict)
                        and previous.get("remote_fingerprint")
                        == cloud.remote_fingerprint(meta)
                    )
                    if not unchanged:
                        todo.append(name)
                if not todo:
                    log.append("远程已是最新，无需推送。\n")
                for n in todo:
                    await asyncio.to_thread(client.upload, local[n]["path"], remote_dir, n)
                    log.append(f"⬆️ 已推送 {n}（{local[n]['size']} 字节）\n")
                remote = await asyncio.to_thread(client.list_files, remote_dir)
                for name, info in local.items():
                    meta = remote.get(name)
                    if isinstance(meta, dict):
                        section[name] = {
                            "local_sha256": info["sha256"],
                            "remote_fingerprint": cloud.remote_fingerprint(meta),
                            "remote_size": int(meta.get("size", 0)),
                            "synced_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                        }
            else:
                pulled = 0
                skipped = 0
                for name, meta in remote.items():
                    if not cloud.safe_remote_name(name) or not isinstance(meta, dict):
                        log.append(f"[跳过] 不安全的远端文件名：{name}\n")
                        skipped += 1
                        continue
                    previous = section.get(name) or {}
                    local_info = local.get(name)
                    remote_fp = cloud.remote_fingerprint(meta)
                    decision = _cloud_pull_decision(previous, local_info, remote_fp)
                    if decision == "conflict":
                        log.append(
                            f"[冲突] {name}：本地和远端都有未同步修改，已保留本地文件；"
                            "请先备份或处理该文件后再同步\n")
                        skipped += 1
                        continue
                    if decision == "local":
                        log.append(f"[跳过] {name}：本地有未同步修改，远端未变化\n")
                        skipped += 1
                        continue
                    if decision == "unchanged":
                        continue
                    target = bundle.EXPORTS_DIR / name
                    await asyncio.to_thread(client.download, remote_dir, name, target)
                    digest = await asyncio.to_thread(cloud.sha256_file, target)
                    section[name] = {
                        "local_sha256": digest,
                        "remote_fingerprint": remote_fp,
                        "remote_size": int(meta.get("size", 0)),
                        "synced_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    }
                    pulled += 1
                    log.append(f"⬇️ 已拉取 {name}（{meta.get('size', 0)} 字节）\n")
                if pulled == 0:
                    if skipped:
                        log.append(f"未拉取任何文件，跳过 {skipped} 个冲突或不安全项。\n")
                    else:
                        log.append("本地已是最新，无需拉取。\n")
            await asyncio.to_thread(cloud.save_sync_state, state)
            _finish_task(tid, "done")
        except Exception as e:  # noqa: BLE001
            log.append(f"\n[error] {e}\n")
            _finish_task(tid, "failed")

    asyncio.create_task(job())


@app.post("/api/cloud/push")
async def api_cloud_push() -> dict:
    tid = _new_task()
    TASKS[tid]["log"].append("推送本地导出包到 WebDAV…\n")
    _cloud_job(tid, push=True)
    return {"task_id": tid}


@app.post("/api/cloud/pull")
async def api_cloud_pull() -> dict:
    tid = _new_task()
    TASKS[tid]["log"].append("从 WebDAV 拉取导出包…\n")
    _cloud_job(tid, push=False)
    return {"task_id": tid}


@app.post("/api/backup-now")
async def api_backup_now() -> dict:
    tid = _new_task()
    log: list[str] = TASKS[tid]["log"]  # type: ignore[index]
    log.append("开始一轮增量备份…\n")

    async def job() -> None:
        try:
            result = await asyncio.to_thread(backup.run_backup_cycle, log)
            changed = {k: v for k, v in result.items() if v.startswith("exported")}
            log.append(f"\n完成：{len(changed)} 个工具有变化已导出，"
                       f"{len(result) - len(changed)} 个无变化。\n")
            _finish_task(tid, "done")
        except Exception as e:  # noqa: BLE001
            log.append(f"\n[error] {e}\n")
            _finish_task(tid, "failed")

    asyncio.create_task(job())
    return {"task_id": tid}


# ------------------------------------------------------------------ M2 配置迁移

class ExportReq(BaseModel):
    categories: list[str]
    conv_days: int | None = None      # 对话历史只打包最近 N 天（None = 全部）
    target_os: str | None = None      # 目标平台路径布局：None/macOS/Windows/linux
    password: str | None = None       # 可选：整包加密密码
    session_max_mb: int | None = None    # 单个会话文件上限，None = 用设置里的


class ImportApplyReq(BaseModel):
    filename: str
    categories: list[str]
    password: str | None = None


@app.get("/api/agents/{agent_id}/detail")
async def api_agent_detail(agent_id: str) -> dict:
    agent = registry.get_agent(agent_id)
    if not agent:
        raise HTTPException(404, "unknown agent")
    detail = await asyncio.to_thread(catmod.agent_detail, agent)
    detail["providers"] = await asyncio.to_thread(providers.detail, agent)
    return detail


@app.post("/api/export/{agent_id}")
async def api_export(agent_id: str, req: ExportReq) -> dict:
    export_cfg = cloud.load_settings().get("export")
    export_cfg = export_cfg if isinstance(export_cfg, dict) else {}
    session_max_mb = req.session_max_mb
    if session_max_mb is None:
        session_max_mb = bundle.clamp_session_mb(export_cfg.get("session_max_mb"))
    try:
        out = await asyncio.to_thread(bundle.export_bundle, agent_id, req.categories,
                                      req.conv_days, req.target_os, req.password,
                                      session_max_mb)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"file": out.name, "size": out.stat().st_size}


@app.get("/api/exports/{name}")
def api_download(name: str) -> FileResponse:
    p = bundle.EXPORTS_DIR / Path(name).name
    if not p.is_file():
        raise HTTPException(404, "file not found")
    return FileResponse(p, filename=p.name)


@app.post("/api/import/preview")
async def api_import_preview(file: UploadFile, password: str = Form("")) -> dict:
    await asyncio.to_thread(_cleanup_imports)
    dest = bundle.IMPORTS_DIR / f"{uuid.uuid4().hex[:8]}-{Path(file.filename or 'import').name}"
    try:
        await _save_upload(file, dest, IMPORT_UPLOAD_LIMIT)
        result = await asyncio.to_thread(bundle.import_preview, dest, password or None)
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise
    except ValueError as e:
        dest.unlink(missing_ok=True)
        msg = str(e)
        if msg.startswith("ENCRYPTED:"):
            raise HTTPException(401, "NEED_PASSWORD")
        if "密码" in msg:
            raise HTTPException(401, "PASSWORD_ERROR")
        raise HTTPException(400, f"不是有效的 .aac 包: {e}")
    except Exception as e:  # noqa: BLE001
        dest.unlink(missing_ok=True)
        raise HTTPException(400, f"不是有效的 .aac 包: {e}")
    return {"filename": dest.name, **result}


# ------------------------------------------------------------------ 配置详情与开关

class ToggleReq(BaseModel):
    item_id: str
    enabled: bool


@app.get("/api/agents/{agent_id}/config")
async def api_agent_config(agent_id: str) -> dict:
    agent = registry.get_agent(agent_id)
    if not agent:
        raise HTTPException(404, "unknown agent")
    cfg = await asyncio.to_thread(configview.list_config, agent)
    m = meta.get_agent(agent_id)                       # 挂上用户标签/备注
    for g in cfg["groups"]:
        for it in g["items"]:
            mm = m.get(it["id"])
            if mm:
                it["tags"] = mm.get("tags", [])
                it["note"] = mm.get("note", "")
    return cfg


class MetaReq(BaseModel):
    item_id: str
    tags: list[str] | None = None
    note: str | None = None


@app.post("/api/agents/{agent_id}/meta")
def api_agent_meta(agent_id: str, req: MetaReq) -> dict:
    agent = registry.get_agent(agent_id)
    if not agent:
        raise HTTPException(404, "unknown agent")
    return meta.set_item(agent_id, req.item_id, req.tags, req.note)


@app.post("/api/agents/{agent_id}/toggle")
def api_agent_toggle(agent_id: str, req: ToggleReq) -> dict:
    agent = registry.get_agent(agent_id)
    if not agent:
        raise HTTPException(404, "unknown agent")
    try:
        msg = configview.toggle(req.item_id, req.enabled)
    except (ValueError, FileNotFoundError, PermissionError) as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "msg": msg}


# ------------------------------------------------------------------ 供应商配置

class ProviderReq(BaseModel):
    id: str | None = None
    name: str
    base_url: str = ""
    api_key: str | None = None
    model: str = ""
    small_model: str = ""
    extra: dict | None = None


def _provider_agent(agent_id: str) -> dict:
    agent = registry.get_agent(agent_id)
    if not agent or not providers.supported(agent):
        raise HTTPException(404, "该工具暂不支持供应商管理")
    return agent


@app.get("/api/agents/{agent_id}/providers")
async def api_providers(agent_id: str) -> dict:
    agent = _provider_agent(agent_id)
    return await asyncio.to_thread(providers.detail, agent)


@app.post("/api/agents/{agent_id}/providers")
async def api_provider_save(agent_id: str, req: ProviderReq) -> dict:
    _provider_agent(agent_id)
    try:
        return await asyncio.to_thread(providers.save, agent_id, req.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/agents/{agent_id}/providers/{provider_id}")
async def api_provider_delete(agent_id: str, provider_id: str) -> dict:
    _provider_agent(agent_id)
    try:
        await asyncio.to_thread(providers.delete, agent_id, provider_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/agents/{agent_id}/providers/{provider_id}/activate")
async def api_provider_activate(agent_id: str, provider_id: str) -> dict:
    _provider_agent(agent_id)
    try:
        return await asyncio.to_thread(providers.activate, agent_id, provider_id)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/agents/{agent_id}/providers/import")
async def api_provider_import(agent_id: str) -> dict:
    _provider_agent(agent_id)
    try:
        return await asyncio.to_thread(providers.import_current, agent_id)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/agents/{agent_id}/providers/{provider_id}/key")
async def api_provider_key(agent_id: str, provider_id: str) -> dict:
    _provider_agent(agent_id)
    try:
        return {"api_key": await asyncio.to_thread(providers.reveal, agent_id, provider_id)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/agents/{agent_id}/providers/{provider_id}/test")
async def api_provider_test(agent_id: str, provider_id: str) -> dict:
    _provider_agent(agent_id)
    try:
        return await asyncio.to_thread(providers.probe, agent_id, provider_id)
    except ValueError as e:
        raise HTTPException(400, str(e))


class ModelsReq(BaseModel):
    id: str | None = None
    base_url: str = ""
    api_key: str | None = None


@app.post("/api/agents/{agent_id}/providers/models")
async def api_provider_models(agent_id: str, req: ModelsReq) -> dict:
    _provider_agent(agent_id)
    try:
        return await asyncio.to_thread(
            providers.fetch_models, agent_id, req.id, req.base_url, req.api_key)
    except ValueError as e:
        raise HTTPException(400, str(e))


class CCSwitchImportReq(BaseModel):
    db: str | None = None
    selected: list[dict] | None = None


@app.get("/api/ccswitch/scan")
async def api_ccswitch_scan(db: str | None = None) -> dict:
    return await asyncio.to_thread(ccswitch.scan, db)


@app.post("/api/ccswitch/import")
async def api_ccswitch_import(req: CCSwitchImportReq) -> dict:
    try:
        return await asyncio.to_thread(ccswitch.import_selected, req.db, req.selected or [])
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/import/apply")
async def api_import_apply(req: ImportApplyReq) -> dict:
    path = _import_path(req.filename)
    tid = _new_task()
    log: list[str] = TASKS[tid]["log"]  # type: ignore[index]
    log.append(f"导入 {path.name}，分类：{', '.join(req.categories) or '（无）'}\n")

    async def _apply() -> None:
        try:
            await asyncio.to_thread(bundle.import_apply, path, req.categories, log, req.password)
            path.unlink(missing_ok=True)
            _finish_task(tid, "done")
        except Exception as e:  # noqa: BLE001
            log.append(f"\n[error] {e}\n")
            _finish_task(tid, "failed")

    asyncio.create_task(_apply())
    return {"task_id": tid}


def cli() -> None:
    import argparse
    global _OPEN_URL
    parser = argparse.ArgumentParser(prog="aac", description="All Agent Config")
    parser.add_argument("--port", type=int, default=8600)
    parser.add_argument("--no-browser", action="store_true",
                        help="启动后不自动打开浏览器")
    args = parser.parse_args()
    url = f"http://127.0.0.1:{args.port}"
    _OPEN_URL = None if args.no_browser else url
    print(f"All Agent Config: {url}")
    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    cli()
