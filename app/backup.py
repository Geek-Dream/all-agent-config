"""定时增量自动备份。

按设置的间隔扫描所选工具：配置分类（不含对话历史）有变化就自动导出 .aac 包。
变化指纹 = 文件路径 + 大小 + mtime，轻量、无需下载官方包。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from pathlib import Path

from . import bundle, categories, cloud, registry
from .bundle import EXPORTS_DIR
from .safefs import atomic_write_text

STATE_FILE = Path.home() / ".aac" / "backup-state.json"

DEFAULT_CATS = ["system", "hooks", "skills", "mcp", "theme", "custom", "patches"]
_BACKUP_LOCK = threading.Lock()


class BackupBusyError(RuntimeError):
    pass


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    atomic_write_text(
        STATE_FILE,
        json.dumps(state, ensure_ascii=False, indent=2),
        mode=0o600,
    )


def fingerprint(agent: dict) -> str:
    """配置分类（不含对话）的文件路径+大小+mtime 指纹。"""
    h = hashlib.sha256()
    cats = categories.scan_categories(agent)
    for cat in DEFAULT_CATS:
        if cat == "patches":
            continue  # 补丁检测需要下载官方包，自动备份改用包内文件 mtime
        for f in cats.get(cat, {}).get("files", []):
            p = Path(f["path"])
            try:
                st = p.stat()
                h.update(f"{p}|{st.st_size}|{st.st_mtime_ns}".encode())
            except OSError:
                h.update(f"{p}|gone".encode())
    # 程序包修改过的文件（存在 patchwork 快照时纳入）
    work = Path.home() / ".aac" / "patchwork" / agent["id"] / "meta.json"
    if work.is_file():
        h.update(work.read_bytes())
    return h.hexdigest()


def run_backup_cycle(log: list[str] | None = None) -> dict:
    """执行一轮增量备份，返回 {agent_id: 'exported'|'unchanged'|'error:...'}。"""
    if not _BACKUP_LOCK.acquire(blocking=False):
        raise BackupBusyError("已有备份任务正在运行")
    try:
        settings = cloud.load_settings()["auto_backup"]
        result: dict[str, str] = {}
        state = _load_state()
        for agent_id in settings.get("agents", []):
            agent = registry.get_agent(agent_id)
            if not agent:
                continue
            try:
                fp = fingerprint(agent)
                if state.get(agent_id) == fp:
                    result[agent_id] = "unchanged"
                    continue
                out = bundle.export_bundle(agent_id, DEFAULT_CATS)
                state[agent_id] = fp
                result[agent_id] = f"exported → {out.name}"
                if log is not None:
                    log.append(
                        f"[{time.strftime('%H:%M:%S')}] {agent['name']}: "
                        f"有变化，已导出 {out.name}\n")
            except Exception as e:  # noqa: BLE001
                result[agent_id] = f"error: {e}"
                if log is not None:
                    log.append(
                        f"[{time.strftime('%H:%M:%S')}] {agent['name']}: "
                        f"备份失败 {e}\n")
        _save_state(state)
        return result
    finally:
        _BACKUP_LOCK.release()


async def auto_backup_loop() -> None:
    """后台循环：按间隔执行本地增量备份。"""
    while True:
        try:
            interval = max(1, int(
                cloud.load_settings()["auto_backup"].get("interval_hours", 24)))
            await asyncio.sleep(interval * 3600)
            settings = cloud.load_settings()["auto_backup"]
            if not settings.get("enabled"):
                continue
            log: list[str] = []
            await asyncio.to_thread(run_backup_cycle, log)
            if log:
                log_path = Path.home() / ".aac" / "backup.log"
                log_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    log_path.parent.chmod(0o700)
                except OSError:
                    pass
                with open(log_path, "a") as f:
                    f.write(f"=== {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
                    f.writelines(log)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass  # 后台循环不抛
