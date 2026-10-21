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


def status() -> dict:
    target = _target()
    data = _load_settings()
    installed = (target / "extensions" / "whale.ts").is_file()
    registered = _registered(data, target)
    others = []
    for item in data.get("packages") or []:
        if isinstance(item, dict):
            for ext in item.get("extensions") or []:
                if "whimsical" in str(ext):
                    others.append(str(ext))
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
    if not _registered(data, target):
        data["packages"].append(str(target))
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
        if len(kept) != len(packages):
            data["packages"] = kept
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
            print("⚠️ 同时启用的其它提示语扩展：" + ", ".join(st["conflicts"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())


__all__ = ["status", "install", "uninstall", "ASSET_DIR", "_target", "_settings_file"]
