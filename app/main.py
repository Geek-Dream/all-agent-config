"""All Agent Config —— FastAPI 入口。

运行：python -m app.main        （默认 0.0.0.0:8600，自动打开浏览器）
"""

import asyncio
import os
import shutil
import tarfile
import uuid
import webbrowser
import zipfile
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import backup, bundle, categories as catmod, cloud, configview, detect, env as envmod, installer, patchwork, registry

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="All Agent Config", version="0.1.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# 内存任务表：task_id -> {"status": "running"|"done"|"failed", "log": list[str]}
TASKS: dict[str, dict] = {}


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


@app.get("/api/env")
async def api_env() -> dict:
    """本机安装管理器（node/npm/pnpm/brew/scoop/winget/pipx）检测。"""
    return await asyncio.to_thread(envmod.check_env)


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
    """解 zip / tar / tar.gz / tgz 到 dest（防路径穿越）。"""
    name = pkg.name.lower()
    try:
        if zipfile.is_zipfile(pkg):
            with zipfile.ZipFile(pkg) as zf:
                for m in zf.namelist():
                    if m.startswith("/") or ".." in m.split("/"):
                        raise ValueError(f"压缩包含危险路径: {m}")
                zf.extractall(dest)
        elif tarfile.is_tarfile(pkg) or name.endswith((".tar.gz", ".tgz", ".tar")):
            with tarfile.open(pkg) as tf:
                for m in tf.getmembers():
                    if m.name.startswith("/") or ".." in m.name.split("/"):
                        raise ValueError(f"压缩包含危险路径: {m.name}")
                tf.extractall(dest)
        else:
            log.append("[error] 无法识别的压缩格式（支持 .zip / .tar.gz / .tgz / .tar）\n")
            return False
        return True
    except Exception as e:  # noqa: BLE001
        log.append(f"[error] 解压失败: {e}\n")
        return False


@app.post("/api/install-archive/{agent_id}")
async def api_install_archive(agent_id: str, file: UploadFile) -> dict:
    """压缩包识别安装：解压 → 在包内找该工具的命令二进制 → 落到 ~/.aac/apps/<id>/bin。"""
    agent = registry.get_agent(agent_id)
    if not agent:
        raise HTTPException(404, "unknown agent")
    data = await file.read()
    if len(data) > 1024 * 1024 * 1024:
        raise HTTPException(400, "压缩包超过 1GB，请手动安装")
    tid = _new_task()
    log: list[str] = TASKS[tid]["log"]  # type: ignore[index]

    async def job() -> None:
        try:
            dest = ARCHIVES_APPS / agent_id
            if dest.exists():
                shutil.rmtree(dest)
            dest.mkdir(parents=True)
            pkg = dest / (file.filename or "package.bin")
            pkg.write_bytes(data)
            log.append(f"📦 收到压缩包 {file.filename}（{len(data) // 1024} KB），开始解压…\n")
            if not await asyncio.to_thread(_extract_archive, pkg, dest, log):
                TASKS[tid]["status"] = "failed"
                return
            pkg.unlink(missing_ok=True)
            cmd = agent["command"]
            names = {cmd, f"{cmd}.exe", f"{cmd}.cmd", f"{cmd}.bat"}
            found = next((p for p in dest.rglob("*") if p.is_file() and p.name in names), None)
            if not found:
                exes = [p for p in dest.rglob("*") if p.is_file()
                        and (os.access(p, os.X_OK) or p.suffix.lower() == ".exe")
                        and not p.name.startswith(".")]
                if len(exes) == 1:
                    found = exes[0]
            if not found:
                top = ", ".join(p.name for p in sorted(dest.iterdir())[:10])
                log.append(f"[error] 包里没找到可执行文件 {cmd}。包内容：{top}\n")
                TASKS[tid]["status"] = "failed"
                return
            bin_dir = dest / "bin"
            bin_dir.mkdir(exist_ok=True)
            target = bin_dir / found.name
            if found.parent != bin_dir:
                shutil.move(str(found), target)
            if not installer.IS_WINDOWS:
                target.chmod(0o755)
            log.append(f"✅ 识别到可执行文件：{target}\n")
            log.append("安装完成。若终端找不到命令，可执行：\n"
                       f"  export PATH=\"{bin_dir}:$PATH\"\n")
            TASKS[tid]["status"] = "done"
        except Exception as e:  # noqa: BLE001
            log.append(f"\n[error] {e}\n")
            TASKS[tid]["status"] = "failed"

    asyncio.create_task(job())
    return {"task_id": tid}


@app.post("/api/install/{agent_id}")
async def api_install(agent_id: str, req: ActionReq) -> dict:
    agent = registry.get_agent(agent_id)
    if not agent:
        raise HTTPException(404, "unknown agent")
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
        asyncio.create_task(_run_task(tid, argv, None))
        return {"task_id": tid}
    if req.scope == "global" and not installer.npm_prefix_writable(registry.get_agent(agent_id).get("command")) and not req.sudo_password:
        raise HTTPException(400, "该工具安装在系统目录，需要 sudo 密码：请在弹窗中输入")
    argv, need_sudo = installer.build_install_argv(agent, req.scope)
    if need_sudo and not installer.IS_WINDOWS and not req.sudo_password:
        raise HTTPException(400, "该工具安装在系统目录，需要 sudo 密码：请在弹窗中输入")
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
        asyncio.create_task(_run_task(tid, argv, None))
        return {"task_id": tid}
    argv, need_sudo = installer.build_update_argv(agent, req.scope)
    if need_sudo and not installer.IS_WINDOWS and not req.sudo_password:
        raise HTTPException(400, "该工具安装在系统目录，需要 sudo 密码：请在弹窗中输入")
    tid = _new_task()
    asyncio.create_task(_run_task(tid, argv, _sudo_arg(req, need_sudo)))
    return {"task_id": tid}


@app.post("/api/uninstall/{agent_id}")
async def api_uninstall(agent_id: str, req: ActionReq) -> dict:
    agent = registry.get_agent(agent_id)
    if not agent:
        raise HTTPException(404, "unknown agent")
    if not (agent.get("sources") or {}).get("npm"):
        installs = detect.find_installs(agent["command"], agent.get("bin_paths"))
        prefix = req.prefix or (installs[0]["prefix"] if installs else None)
        apps_root = Path.home() / ".aac" / "apps"
        if prefix and str(prefix).startswith(str(apps_root)):
            # 压缩包安装（~/.aac/apps/<id>）：整目录删除，优先于任何管理器
            tid = _new_task()
            log2: list[str] = TASKS[tid]["log"]  # type: ignore[index]

            async def rmjob() -> None:
                log2.append(f"🗑 删除压缩包安装目录 {prefix} …\n")
                try:
                    await asyncio.to_thread(shutil.rmtree, prefix)
                    log2.append("已删除。\n")
                    TASKS[tid]["status"] = "done"
                except Exception as e:  # noqa: BLE001
                    log2.append(f"[error] {e}\n")
                    TASKS[tid]["status"] = "failed"

            asyncio.create_task(rmjob())
            return {"task_id": tid}
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
                TASKS[tid]["status"] = "done" if code == 0 else "failed"

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
    if req.prefix:
        # 卸载指定位置的副本（重复安装清理）：校验该前缀确实是探测到的安装位置之一
        known = {i["prefix"] for i in detect.find_installs(agent["command"], agent.get("bin_paths"))}
        if req.prefix not in known:
            raise HTTPException(400, f"未知的安装位置: {req.prefix}")
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
                # 更新后自动验证：重新与官方包比对，确认补丁是否完好
                log.append("\n🔍 验证补丁是否完好…\n")
                after = await asyncio.to_thread(catmod.detect_patches, agent)
                survived = (len((after or {}).get("modified", []))
                            + len((after or {}).get("added", [])))
                lost = n - survived
                if (after or {}).get("error"):
                    log.append(f"⚠️ 验证失败（{after['error']}），请打开配置详情手动确认补丁状态\n")
                elif lost <= 0:
                    log.append(f"✅ 补丁验证通过：{survived} 个修改文件全部完好\n")
                else:
                    log.append(f"⚠️ 补丁验证：{survived} 个已保留，{lost} 个未能移植到新版本"
                               "（快照在 ~/.aac/patchwork，可手动恢复）\n")
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
    conv_days: int | None = None      # 对话历史只打包最近 N 天（None = 全部）


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
        out = await asyncio.to_thread(bundle.export_bundle, agent_id, req.categories, req.conv_days)
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


# ------------------------------------------------------------------ 配置详情与开关

class ToggleReq(BaseModel):
    item_id: str
    enabled: bool


@app.get("/api/agents/{agent_id}/config")
async def api_agent_config(agent_id: str) -> dict:
    agent = registry.get_agent(agent_id)
    if not agent:
        raise HTTPException(404, "unknown agent")
    return await asyncio.to_thread(configview.list_config, agent)


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
