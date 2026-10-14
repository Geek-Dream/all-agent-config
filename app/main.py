"""All Agent Config —— FastAPI 入口。

运行：python -m app.main        （默认 0.0.0.0:8600，自动打开浏览器）
"""

import asyncio
import uuid
import webbrowser
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import backup, bundle, categories as catmod, cloud, detect, installer, patchwork, registry

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="All Agent Config", version="0.1.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# 内存任务表：task_id -> {"status": "running"|"done"|"failed", "log": list[str]}
TASKS: dict[str, dict] = {}


class ActionReq(BaseModel):
    scope: str = "user"            # user | global
    sudo_password: str | None = None


def _sudo_arg(req: "ActionReq", need_sudo: bool) -> str | None:
    """返回传给安装器的提权参数；Windows 用 UAC 提权标记。"""
    if not need_sudo:
        return None
    if installer.IS_WINDOWS:
        return installer.ELEVATE_MARKER
    return req.sudo_password


def _new_task() -> str:
    tid = uuid.uuid4().hex[:8]
    TASKS[tid] = {"status": "running", "log": []}
    return tid


async def _run_task(tid: str, argv: list[str] | None, sudo_password: str | None,
                    tail_hint: str | None = None) -> None:
    log: list[str] = TASKS[tid]["log"]  # type: ignore[index]
    try:
        if argv is None:
            log.append("该工具暂无自动安装源，请按页面提示手动安装。\n")
            TASKS[tid]["status"] = "failed"
            return
        code = await installer.run(argv, log, sudo_password)
        if tail_hint:
            log.append("\n" + tail_hint + "\n")
        TASKS[tid]["status"] = "done" if code == 0 else "failed"
        log.append(f"\n[exit code: {code}]\n")
    except Exception as e:  # noqa: BLE001
        log.append(f"\n[error] {e}\n")
        TASKS[tid]["status"] = "failed"


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/tools")
async def api_tools() -> list[dict]:
    return await detect.scan_all(registry.load_agents())


@app.post("/api/install/{agent_id}")
async def api_install(agent_id: str, req: ActionReq) -> dict:
    agent = registry.get_agent(agent_id)
    if not agent:
        raise HTTPException(404, "unknown agent")
    if not (agent.get("sources") or {}).get("npm"):
        # 非 npm 源：尝试 brew
        tid = _new_task()
        asyncio.create_task(_run_task(tid, installer.build_brew_argv(agent, "install"), None))
        return {"task_id": tid}
    if req.scope == "global" and not installer.npm_prefix_writable(registry.get_agent(agent_id).get("command")) and not req.sudo_password:
        raise HTTPException(400, "global install requires sudo_password on this machine")
    argv, need_sudo = installer.build_install_argv(agent, req.scope)
    if need_sudo and not installer.IS_WINDOWS and not req.sudo_password:
        raise HTTPException(400, "sudo password required for global install")
    tid = _new_task()
    hint = installer.user_prefix_path_hint() if req.scope == "user" else None
    asyncio.create_task(_run_task(tid, argv, _sudo_arg(req, need_sudo), hint))
    return {"task_id": tid}


@app.post("/api/update/{agent_id}")
async def api_update(agent_id: str, req: ActionReq) -> dict:
    agent = registry.get_agent(agent_id)
    if not agent:
        raise HTTPException(404, "unknown agent")
    if not (agent.get("sources") or {}).get("npm"):
        tid = _new_task()
        asyncio.create_task(_run_task(tid, installer.build_brew_argv(agent, "update"), None))
        return {"task_id": tid}
    argv, need_sudo = installer.build_update_argv(agent, req.scope)
    if need_sudo and not installer.IS_WINDOWS and not req.sudo_password:
        raise HTTPException(400, "sudo password required")
    tid = _new_task()
    asyncio.create_task(_run_task(tid, argv, _sudo_arg(req, need_sudo)))
    return {"task_id": tid}


@app.post("/api/uninstall/{agent_id}")
async def api_uninstall(agent_id: str, req: ActionReq) -> dict:
    agent = registry.get_agent(agent_id)
    if not agent:
        raise HTTPException(404, "unknown agent")
    if not (agent.get("sources") or {}).get("npm"):
        tid = _new_task()
        asyncio.create_task(_run_task(tid, installer.build_brew_argv(agent, "uninstall"), None))
        return {"task_id": tid}
    argv, need_sudo = installer.build_uninstall_argv(agent, req.scope)
    if need_sudo and not installer.IS_WINDOWS and not req.sudo_password:
        raise HTTPException(400, "sudo password required")
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
            n = len((patches or {}).get("modified", [])) + len((patches or {}).get("added", []))
            has_patches = n > 0
            if has_patches:
                log.append(f"⚠️ 检测到 {n} 个程序修改文件，更新会先覆盖它们。正在保存补丁快照…\n")
                await asyncio.to_thread(patchwork.snapshot, agent, patches, log)
            argv, need_sudo = installer.build_update_argv(agent, req.scope)
            if need_sudo and not installer.IS_WINDOWS and not req.sudo_password:
                log.append("\n[error] 全局更新需要 sudo 密码\n")
                TASKS[tid]["status"] = "failed"
                return
            log.append("\n⬆️ 开始更新…\n")
            code = await installer.run(argv, log, _sudo_arg(req, need_sudo))
            if code != 0:
                TASKS[tid]["status"] = "failed"
                log.append(f"\n更新失败（exit {code}），补丁快照仍在 ~/.aac/patchwork 中\n")
                return
            if has_patches:
                log.append("\n🔧 正在把补丁打回新版本…\n")
                await asyncio.to_thread(patchwork.reapply, agent, log)
            TASKS[tid]["status"] = "done"
        except Exception as e:  # noqa: BLE001
            log.append(f"\n[error] {e}\n")
            TASKS[tid]["status"] = "failed"

    asyncio.create_task(job())
    return {"task_id": tid}


@app.get("/api/tasks/{task_id}")
def api_task(task_id: str) -> dict:
    task = TASKS.get(task_id)
    if not task:
        raise HTTPException(404, "unknown task")
    return {"status": task["status"], "log": "".join(task["log"])}


# ------------------------------------------------------------------ M4 云同步与自动备份

class SettingsReq(BaseModel):
    webdav: dict | None = None
    auto_backup: dict | None = None


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
    cloud.save_settings(patch)
    return {"saved": True}


@app.post("/api/cloud/test")
async def api_cloud_test() -> dict:
    try:
        return {"ok": True, "msg": await asyncio.to_thread(cloud.get_client().test)}
    except cloud.WebDAVError as e:
        return {"ok": False, "msg": str(e)}


def _cloud_job(tid: str, push: bool) -> None:
    log: list[str] = TASKS[tid]["log"]  # type: ignore[index]

    async def job() -> None:
        try:
            client = cloud.get_client()
            s = cloud.load_settings()["webdav"]
            remote_dir = s["remote_dir"].strip("/") or "aac"
            client.ensure_dir(remote_dir)
            remote = await asyncio.to_thread(client.list_files, remote_dir)
            local = {p.name: p.stat().st_size for p in bundle.EXPORTS_DIR.glob("*.aac")}
            if push:
                todo = [n for n, sz in local.items() if remote.get(n) != sz]
                if not todo:
                    log.append("远程已是最新，无需推送。\n")
                for n in todo:
                    await asyncio.to_thread(client.upload, bundle.EXPORTS_DIR / n,
                                            remote_dir, n)
                    log.append(f"⬆️ 已推送 {n}（{local[n]} 字节）\n")
            else:
                todo = [n for n, sz in remote.items() if local.get(n) != sz]
                if not todo:
                    log.append("本地已是最新，无需拉取。\n")
                for n in todo:
                    await asyncio.to_thread(client.download, remote_dir, n,
                                            bundle.EXPORTS_DIR / n)
                    log.append(f"⬇️ 已拉取 {n}（{remote[n]} 字节）\n")
            TASKS[tid]["status"] = "done"
        except Exception as e:  # noqa: BLE001
            log.append(f"\n[error] {e}\n")
            TASKS[tid]["status"] = "failed"

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
            TASKS[tid]["status"] = "done"
        except Exception as e:  # noqa: BLE001
            log.append(f"\n[error] {e}\n")
            TASKS[tid]["status"] = "failed"

    asyncio.create_task(job())
    return {"task_id": tid}


@app.on_event("startup")
async def _startup() -> None:
    asyncio.create_task(backup.auto_backup_loop())


# ------------------------------------------------------------------ M2 配置迁移

class ExportReq(BaseModel):
    categories: list[str]


class ImportApplyReq(BaseModel):
    filename: str
    categories: list[str]


@app.get("/api/agents/{agent_id}/detail")
async def api_agent_detail(agent_id: str) -> dict:
    agent = registry.get_agent(agent_id)
    if not agent:
        raise HTTPException(404, "unknown agent")
    return await asyncio.to_thread(catmod.agent_detail, agent)


@app.post("/api/export/{agent_id}")
async def api_export(agent_id: str, req: ExportReq) -> dict:
    try:
        out = await asyncio.to_thread(bundle.export_bundle, agent_id, req.categories)
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
async def api_import_preview(file: UploadFile) -> dict:
    bundle.IMPORTS_DIR.mkdir(parents=True, exist_ok=True)
    dest = bundle.IMPORTS_DIR / f"{uuid.uuid4().hex[:8]}-{Path(file.filename or 'import').name}"
    dest.write_bytes(await file.read())
    try:
        result = bundle.import_preview(dest)
    except Exception as e:  # noqa: BLE001
        dest.unlink(missing_ok=True)
        raise HTTPException(400, f"不是有效的 .aac 包: {e}")
    return {"filename": dest.name, **result}


@app.post("/api/import/apply")
async def api_import_apply(req: ImportApplyReq) -> dict:
    path = bundle.IMPORTS_DIR / Path(req.filename).name
    if not path.is_file():
        raise HTTPException(404, "uploaded file not found")
    tid = _new_task()
    log: list[str] = TASKS[tid]["log"]  # type: ignore[index]
    log.append(f"导入 {path.name}，分类：{', '.join(req.categories) or '（无）'}\n")

    async def _apply() -> None:
        try:
            await asyncio.to_thread(bundle.import_apply, path, req.categories, log)
            TASKS[tid]["status"] = "done"
        except Exception as e:  # noqa: BLE001
            log.append(f"\n[error] {e}\n")
            TASKS[tid]["status"] = "failed"

    asyncio.create_task(_apply())
    return {"task_id": tid}


def cli() -> None:
    import sys
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 8600
    url = f"http://localhost:{port}"
    print(f"All Agent Config → {url}")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    cli()
