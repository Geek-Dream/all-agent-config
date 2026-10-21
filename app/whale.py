"""鲸鱼妹工作提示语（pi 扩展 aac-whale）的安装 / 卸载 / 状态。

资源随项目走（assets/pi-whale），装到 ~/.pi/pi-extensions/aac-whale，
再把自己注册进 ~/.pi/agent/settings.json 的 packages —— 这两处都在
Pi 的导出分类里，所以打包成 .aac 带到 Windows 也能原样恢复。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ASSET_DIR = PROJECT_ROOT / "assets" / "pi-whale"


def _target() -> Path:
    return Path.home() / ".pi" / "pi-extensions" / "aac-whale"


def _settings_file() -> Path:
    return Path.home() / ".pi" / "agent" / "settings.json"


def _load_settings() -> dict:
    try:
        data = json.loads(_settings_file().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 - 文件不存在/损坏都按空处理
        return {}


def _save_settings(data: dict) -> None:
    path = _settings_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_name(path.name + ".aac-bak")
        try:
            shutil.copy2(path, backup)
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


def _state_file() -> Path:
    return Path.home() / ".aac" / "whale-state.json"


def _load_state() -> dict:
    try:
        data = json.loads(_state_file().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _save_state(data: dict) -> None:
    path = _state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _whimsical_paths(packages: list) -> list[str]:
    found = []
    for item in packages:
        if isinstance(item, dict):
            for ext in item.get("extensions") or []:
                if "whimsical" in str(ext):
                    found.append(str(ext))
    return found


def _prune_whimsical(packages: list) -> dict:
    """把 whimsical 从 packages 里摘下来，返回 {source: [扩展路径]} 供恢复。"""
    saved: dict[str, list[str]] = {}
    for item in packages:
        if not isinstance(item, dict):
            continue
        exts = item.get("extensions")
        if not isinstance(exts, list):
            continue
        keep = [ext for ext in exts if "whimsical" not in str(ext)]
        if len(keep) != len(exts):
            saved[str(item.get("source") or "")] = [str(e) for e in exts if e not in keep]
            item["extensions"] = keep
    return saved


def _restore_whimsical(packages: list, saved: dict) -> bool:
    changed = False
    for item in packages:
        if not isinstance(item, dict):
            continue
        paths = saved.get(str(item.get("source") or ""))
        if not paths:
            continue
        exts = item.get("extensions")
        exts = list(exts) if isinstance(exts, list) else []
        for path in paths:
            if path not in exts:
                exts.append(path)
                changed = True
        item["extensions"] = exts
    return changed


def status() -> dict:
    target = _target()
    data = _load_settings()
    installed = (target / "extensions" / "whale.ts").is_file()
    registered = _registered(data, target)
    others = _whimsical_paths(data.get("packages") or [])
    return {
        "asset": str(ASSET_DIR),
        "target": str(target),
        "installed": installed,
        "registered": registered,
        "active": installed and registered,
        "conflicts": others,
    }


def install() -> dict:
    src = ASSET_DIR / "extensions" / "whale.ts"
    if not src.is_file():
        return {"ok": False, "message": f"资源缺失：{src}"}
    target = _target()
    (target / "extensions").mkdir(parents=True, exist_ok=True)
    shutil.copytree(ASSET_DIR, target, dirs_exist_ok=True)
    data = _load_settings()
    if not isinstance(data.get("packages"), list):
        data["packages"] = []
    changed = False
    if not _registered(data, target):
        data["packages"].append(str(target))
        changed = True
    # 两套梗已经并进 whale.ts 了；原来那个 whimsical 扩展必须关掉，
    # 否则两边都写同一条工作行，谁后加载谁赢，等于白合并。
    saved = _prune_whimsical(data["packages"])
    if saved:
        state = _load_state()
        state.setdefault("whimsical", {}).update(saved)
        _save_state(state)
        changed = True
    if changed:
        _save_settings(data)
    return {"ok": True, "message": "已装好，重启 pi 生效", "status": status()}


def uninstall() -> dict:
    target = _target()
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    data = _load_settings()
    packages = data.get("packages")
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
        # 把当初关掉的 whimsical 放回去，别把用户原来的梗弄丢
        saved = _load_state().get("whimsical") or {}
        if saved and _restore_whimsical(kept, saved):
            changed = True
            state = _load_state()
            state.pop("whimsical", None)
            _save_state(state)
        if changed:
            _save_settings(data)
    return {"ok": True, "message": "已卸下（重启 pi 生效）", "status": status()}


def _cli(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="鲸鱼妹工作提示语：安装 / 卸载")
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
        print(f"已安装: {'是' if st['installed'] else '否'} / 已注册: {'是' if st['registered'] else '否'}")
        if st["conflicts"]:
            print("⚠️ 还有 whimsical 开着，会和合并后的提示语抢同一行：" + ", ".join(st["conflicts"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())


__all__ = ["status", "install", "uninstall", "ASSET_DIR", "_target", "_settings_file"]
