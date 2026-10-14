"""All Agent Config —— FastAPI 入口。

运行：python -m app.main        （默认 0.0.0.0:8600，自动打开浏览器）
"""

import asyncio
import uuid
import webbrowser
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import detect, installer, registry

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="All Agent Config", version="0.1.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# 内存任务表：task_id -> {"status": "running"|"done"|"failed", "log": str}
TASKS: dict[str, dict] = {}


class ActionReq(BaseModel):
    scope: str = "user"            # user | global
    sudo_password: str | None = None


def _new_task() -> str:
    tid = uuid.uuid4().hex[:8]
    TASKS[tid] = {"status": "running", "log": ""}
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
        tid = _new_task()
        asyncio.create_task(_run_task(tid, None, None))
        return {"task_id": tid}
    if req.scope == "global" and not installer.npm_prefix_writable() and not req.sudo_password:
        raise HTTPException(400, "global install requires sudo_password on this machine")
    argv, need_sudo = installer.build_install_argv(agent, req.scope)
    if need_sudo and not req.sudo_password:
        raise HTTPException(400, "sudo password required for global install")
    tid = _new_task()
    hint = installer.user_prefix_path_hint() if req.scope == "user" else None
    asyncio.create_task(_run_task(tid, argv, req.sudo_password if need_sudo else None, hint))
    return {"task_id": tid}


@app.post("/api/update/{agent_id}")
async def api_update(agent_id: str, req: ActionReq) -> dict:
    agent = registry.get_agent(agent_id)
    if not agent or not (agent.get("sources") or {}).get("npm"):
        raise HTTPException(404, "unknown or not auto-updatable agent")
    argv, need_sudo = installer.build_update_argv(agent, req.scope)
    if need_sudo and not req.sudo_password:
        raise HTTPException(400, "sudo password required")
    tid = _new_task()
    asyncio.create_task(_run_task(tid, argv, req.sudo_password if need_sudo else None))
    return {"task_id": tid}


@app.post("/api/uninstall/{agent_id}")
async def api_uninstall(agent_id: str, req: ActionReq) -> dict:
    agent = registry.get_agent(agent_id)
    if not agent or not (agent.get("sources") or {}).get("npm"):
        raise HTTPException(404, "unknown agent")
    argv, need_sudo = installer.build_uninstall_argv(agent, req.scope)
    if need_sudo and not req.sudo_password:
        raise HTTPException(400, "sudo password required")
    tid = _new_task()
    asyncio.create_task(_run_task(tid, argv, req.sudo_password if need_sudo else None))
    return {"task_id": tid}


@app.get("/api/tasks/{task_id}")
def api_task(task_id: str) -> dict:
    task = TASKS.get(task_id)
    if not task:
        raise HTTPException(404, "unknown task")
    return task


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
