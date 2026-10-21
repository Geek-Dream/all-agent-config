"""状态栏扩展（aac-footer）的安装 / 卸载 / 状态。

资源随项目走（assets/pi-footer），装到 ~/.pi/pi-extensions/aac-footer，
再注册进 ~/.pi/agent/settings.json 的 packages。

装的时候要把**抢 footer 的另外两个扩展**关掉，否则谁后加载谁赢：
  - wl-setup 的 rmbFooter（~/.pi/agent/wl-setup.json）
  - statusline-pi（~/.pi/agent/statusline-pi.json）
关之前把原值记进 ~/.aac/footer-state.json，卸载时原样还回去 ——
用户原来的配置不能因为试用一次新状态栏就丢。
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ASSET_DIR = PROJECT_ROOT / "assets" / "pi-footer"


def _target() -> Path:
    return Path.home() / ".pi" / "pi-extensions" / "aac-footer"


def _agent_dir() -> Path:
    return Path.home() / ".pi" / "agent"


def _settings_file() -> Path:
    return _agent_dir() / "settings.json"


def _wl_setup_file() -> Path:
    return _agent_dir() / "wl-setup.json"


def _statusline_file() -> Path:
    return _agent_dir() / "statusline-pi.json"


def _state_file() -> Path:
    return Path.home() / ".aac" / "footer-state.json"


def _claude_settings_file() -> Path:
    return Path.home() / ".claude" / "settings.json"


def _bin_dir() -> Path:
    return Path.home() / ".aac" / "bin"


def _render_script() -> Path:
    """claude 的 statusLine 只接受一条命令，所以给一个包装脚本。

    不写死 python 路径的话，`.aac` 包换到别的电脑就废了；写死也不行 ——
    所以在安装时按当前解释器生成，换机器重装即重新生成。
    """
    name = "aac-footer.cmd" if sys.platform == "win32" else "aac-footer"
    return _bin_dir() / name


def _script_body(project_root: Path) -> str:
    python = Path(sys.executable).as_posix()
    root = project_root.as_posix()
    if sys.platform == "win32":
        return ("@echo off\r\n"
                f'set "PYTHONPATH={root}"\r\n'
                f'"{python}" -m app.footer --stdin --agent claude\r\n')
    return ("#!/bin/sh\n"
            f'PYTHONPATH="{root}" exec "{python}" -m app.footer --stdin --agent claude\n')


def claude_status() -> dict:
    cfg = _load_json(_claude_settings_file())
    line = cfg.get("statusLine") if isinstance(cfg.get("statusLine"), dict) else {}
    command = str(line.get("command") or "")
    script = _render_script()
    return {
        "settings": str(_claude_settings_file()),
        "script": str(script),
        "script_exists": script.is_file(),
        "installed": bool(command) and str(script) in command,
        "installed_for_other": bool(command) and str(script) not in command,
        "current": command,
    }


def claude_install(project_root: Path | None = None) -> dict:
    """把状态栏挂到 Claude Code 的 statusLine 上（可回滚）。"""
    root = project_root or PROJECT_ROOT
    script = _render_script()
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(_script_body(root), encoding="utf-8")
    if sys.platform != "win32":
        script.chmod(0o755)

    path = _claude_settings_file()
    cfg = _load_json(path)
    state = _load_json(_state_file())
    previous = cfg.get("statusLine")
    if previous and not (isinstance(previous, dict) and str(script) in str(previous.get("command") or "")):
        # 用户原来配过别人的状态栏，记下来，卸载时还给他
        state.setdefault("claude_statusline", previous)
        _save_json(_state_file(), state)
    cfg["statusLine"] = {"type": "command", "command": str(script)}
    _save_json(path, cfg)

    # 顺手给 claude 写一套「少显示没数据的段」的默认模板（已有覆盖就不动）
    from . import footer

    config = footer.load_config()
    if "claude" not in (config.get("agents") or {}):
        config.setdefault("agents", {})["claude"] = {
            "separator": "bar",
            "lines": [["model", "thinking", "git", "cost"], ["mem", "cache_time", "time"]],
        }
        footer.save_config(config)
    return {"ok": True, "message": "已挂到 Claude Code 状态栏，重启 claude 生效",
            "status": claude_status()}


def claude_uninstall() -> dict:
    path = _claude_settings_file()
    cfg = _load_json(path)
    state = _load_json(_state_file())
    previous = state.pop("claude_statusline", None)
    if previous:
        cfg["statusLine"] = previous
    else:
        cfg.pop("statusLine", None)
    _save_json(path, cfg)
    _save_json(_state_file(), state)
    try:
        _render_script().unlink()
    except OSError:
        pass
    return {"ok": True, "message": "已从 Claude Code 卸下状态栏", "status": claude_status()}


def _load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 - 不存在 / 损坏都当空
        return {}


def _save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            shutil.copy2(path, path.with_name(path.name + ".aac-bak"))
        except OSError:
            pass
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _registered(data: dict, target: Path) -> bool:
    want = target.expanduser().resolve()
    for item in data.get("packages") or []:
        if isinstance(item, str):
            try:
                if Path(item).expanduser().resolve() == want:
                    return True
            except (OSError, RuntimeError):
                continue
    return False


def _has_conflict(path: Path, key: str) -> bool:
    return bool(_load_json(path).get(key))


def status() -> dict:
    target = _target()
    data = _load_json(_settings_file())
    installed = (target / "extensions" / "footer.ts").is_file()
    registered = _registered(data, target)
    conflicts = []
    if _has_conflict(_wl_setup_file(), "rmbFooter"):
        conflicts.append("wl-setup 的 rmbFooter")
    if _has_conflict(_statusline_file(), "enabled"):
        conflicts.append("statusline-pi")
    return {
        "asset": str(ASSET_DIR),
        "target": str(target),
        "config": str(Path.home() / ".aac" / "footer.json"),
        "installed": installed,
        "registered": registered,
        "active": installed and registered,
        "conflicts": conflicts,
    }


def install() -> dict:
    src = ASSET_DIR / "extensions" / "footer.ts"
    if not src.is_file():
        return {"ok": False, "message": f"资源缺失：{src}"}
    target = _target()
    (target / "extensions").mkdir(parents=True, exist_ok=True)
    shutil.copytree(ASSET_DIR, target, dirs_exist_ok=True)

    data = _load_json(_settings_file())
    if not isinstance(data.get("packages"), list):
        data["packages"] = []
    changed = False
    if not _registered(data, target):
        data["packages"].append(str(target))
        changed = True

    state = _load_json(_state_file())
    saved = state.setdefault("disabled", {})
    for path, key, label in ((_wl_setup_file(), "rmbFooter", "wl-setup"),
                             (_statusline_file(), "enabled", "statusline-pi")):
        cfg = _load_json(path)
        if cfg.get(key):
            saved.setdefault(label, {"path": str(path), "key": key, "value": cfg[key]})
            cfg[key] = False
            _save_json(path, cfg)
            changed = True
    if changed:
        _save_json(_settings_file(), data)
        _save_json(_state_file(), state)
    return {"ok": True, "message": "已装好，重启 pi 生效", "status": status()}


def uninstall() -> dict:
    target = _target()
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)

    data = _load_json(_settings_file())
    packages = data.get("packages")
    changed = False
    if isinstance(packages, list):
        want = target.expanduser().resolve()
        kept = []
        for item in packages:
            if isinstance(item, str):
                try:
                    if Path(item).expanduser().resolve() == want:
                        continue
                except (OSError, RuntimeError):
                    pass
            kept.append(item)
        changed = len(kept) != len(packages)
        data["packages"] = kept

    # 把当初关掉的 rmbFooter / statusline-pi 还回去
    state = _load_json(_state_file())
    for label, info in (state.get("disabled") or {}).items():
        if not isinstance(info, dict):
            continue
        path = Path(str(info.get("path") or ""))
        key = str(info.get("key") or "")
        if not key or not path.name:
            continue
        cfg = _load_json(path)
        if key in cfg or info.get("value"):
            cfg[key] = info.get("value", True)
            _save_json(path, cfg)
            changed = True
    if state.get("disabled"):
        state.pop("disabled", None)
        _save_json(_state_file(), state)

    if changed:
        _save_json(_settings_file(), data)
    return {"ok": True, "message": "已卸下（重启 pi 生效）", "status": status()}


def _cli(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="状态栏扩展：安装 / 卸载")
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--uninstall", action="store_true")
    args = parser.parse_args(argv)
    if args.install:
        print(install())
    elif args.uninstall:
        print(uninstall())
    else:
        st = status()
        print(f"资源: {st['asset']}")
        print(f"目标: {st['target']}")
        print(f"配置: {st['config']}")
        print(f"已安装: {'是' if st['installed'] else '否'} / 已注册: {'是' if st['registered'] else '否'}")
        if st["conflicts"]:
            print("⚠️ 还有别的东西在抢状态栏：" + ", ".join(st["conflicts"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())


__all__ = ["status", "install", "uninstall", "claude_status", "claude_install",
           "claude_uninstall", "ASSET_DIR", "_target", "_settings_file"]
