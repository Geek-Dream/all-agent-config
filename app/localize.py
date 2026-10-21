"""汉化补丁引擎：text（JS 分发文本替换）与 binary（原生二进制等字节替换）。

两类目标：
- text   —— Pi 这类 JS 工具：改 dist 里 `description: "EN"`，备份原文，升级后重打。
- binary —— Codex 这类 Rust 原生包：TUI 文案是编译进二进制的 `&str`（长度写死在机器码里），
           只能逐字节等长替换（中文不足补空格），改完要做 ad-hoc 重签名。

文件布局：
  data/i18n/<map>.json        项目内置词表（{英文: 中文}，`_` 开头的键忽略）
  ~/.aac/i18n-build/<id>/     待写入的补丁副本（binary）
  ~/.aac/i18n-backup/<id>/    text 型原文备份
  <binary>.aac-orig           binary 型原文备份（与二进制同目录）

提权：binary 通常装在 root 所有的目录（/usr/local），写入走系统授权：
  macOS   osascript ... with administrator privileges（弹系统密码框）
  Linux   pkexec sh -c ...
  Windows powershell Start-Process -Verb RunAs
"""

from __future__ import annotations

import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MAPS_DIR = PROJECT_ROOT / "data" / "i18n"
WORK_ROOT = Path.home() / ".aac" / "i18n"
BACKUP_ROOT = WORK_ROOT / "backup"
BACKUP_SUFFIX = ".aac-orig"

PLATFORM_PKG = {
    ("darwin", "arm64"): "@openai/codex-darwin-arm64",
    ("darwin", "x86_64"): "@openai/codex-darwin-x64",
    ("linux", "x86_64"): "@openai/codex-linux-x64",
    ("linux", "aarch64"): "@openai/codex-linux-arm64",
    ("win32", "x86_64"): "@openai/codex-win32-x64",
    ("win32", "arm64"): "@openai/codex-win32-arm64",
}


# ---------------------------------------------------------------- 基础工具

def _npm_roots() -> list[Path]:
    home = Path.home()
    raw = [
        "/usr/local/lib/node_modules",
        str(home / ".npm-global/lib/node_modules"),
        str(home / ".local/lib/node_modules"),
        str(home / ".nvm/versions/node/*/lib/node_modules"),
        "/opt/homebrew/lib/node_modules",
        "/usr/lib/node_modules",
    ]
    if os.name == "nt" and os.environ.get("APPDATA"):
        raw.append(str(Path(os.environ["APPDATA"]) / "npm" / "node_modules"))
    roots: list[Path] = []
    for entry in raw:
        if "*" in entry:
            parent = Path(entry.split("*")[0]).parent
            roots.extend(sorted(p for p in parent.glob(Path(entry).name) if p.is_dir()))
        else:
            p = Path(entry).expanduser()
            if p.is_dir():
                roots.append(p)
    try:
        out = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True, timeout=20)
        if out.returncode == 0 and out.stdout.strip():
            p = Path(out.stdout.strip())
            if p.is_dir() and p not in roots:
                roots.append(p)
    except Exception:  # noqa: BLE001 - npm 不一定存在
        pass
    return roots


def find_package(package: str) -> Path | None:
    for root in _npm_roots():
        cand = root / package
        if cand.is_dir():
            return cand
    return None


def load_map(agent: dict) -> dict[str, str]:
    spec = agent.get("i18n") or {}
    name = spec.get("map") or ""
    path = Path(name) if Path(name).is_absolute() else MAPS_DIR / name
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 词表缺失按空处理
        return {}
    return {k: v for k, v in raw.items() if isinstance(v, str) and not k.startswith("_")}


def _spec(agent: dict) -> dict | None:
    spec = agent.get("i18n")
    return spec if isinstance(spec, dict) else None


def _locate_spec(agent: dict) -> dict:
    return (_spec(agent) or {}).get("locate") or {}


# ---------------------------------------------------------------- 目标定位

def locate_binary(agent: dict) -> Path | None:
    loc = _locate_spec(agent)
    pkg = find_package(loc.get("package") or "")
    if pkg is None:
        return None
    pattern = loc.get("glob") or ""
    exe = "codex.exe" if os.name == "nt" else "codex"
    for cand in sorted(pkg.glob(pattern)):
        if cand.is_file() and (cand.name in (exe, "codex", "codex.exe") or exe in cand.name):
            return cand
    for cand in sorted(pkg.glob(pattern)):
        if cand.is_file():
            return cand
    return None


def locate_text_files(agent: dict) -> list[Path]:
    loc = _locate_spec(agent)
    pkg = find_package(loc.get("package") or "")
    if pkg is None:
        return []
    return [p for p in sorted(pkg.glob(loc.get("glob") or "dist/**/*.js")) if p.is_file()]


def binary_backup(binary: Path) -> Path:
    return binary.with_name(binary.name + BACKUP_SUFFIX)


def text_backup_path(agent: dict, target: Path) -> Path:
    return BACKUP_ROOT / agent["id"] / target.name


# ---------------------------------------------------------------- 替换逻辑

def replace_equal_length(buf: bytearray, en: bytes, zh: bytes) -> int:
    """等字节替换：中文不足补空格，超长则抛 ValueError。返回替换处数。"""
    if len(zh) > len(en):
        raise ValueError(f"译文超长 {len(zh)}>{len(en)}")
    padded = zh + b" " * (len(en) - len(zh))
    pos, hits = 0, 0
    while True:
        idx = buf.find(en, pos)
        if idx < 0:
            return hits
        buf[idx:idx + len(en)] = padded
        pos = idx + len(en)
        hits += 1


def _text_variants(en: str) -> list[str]:
    return [f'description: "{en}"', f'description:"{en}"']


def patch_text(text: str, mapping: dict[str, str], pattern_only: bool = True) -> tuple[str, int, list[str]]:
    """替换 JS 文本里的 description 字段；返回 (新文本, 处数, 超长英文列表)。"""
    replaced, too_long = 0, []
    for en, zh in mapping.items():
        if len(zh.encode()) > len(en.encode()) and pattern_only:
            # 文本型没有字节约束，这里只记录信息，不阻止
            too_long.append(en)
        for variant in _text_variants(en):
            if variant in text:
                text = text.replace(variant, variant.replace(f'"{en}"', f'"{zh}"'))
                replaced += 1
    return text, replaced, too_long


# ---------------------------------------------------------------- 状态

def _binary_state(binary: Path, mapping: dict[str, str]) -> dict:
    blob = binary.read_bytes()
    present = [en for en in mapping if en.encode() in blob]
    zh_present = [zh for zh in mapping.values() if zh.encode() in blob]
    return {
        "pending": len(present),
        "translated": len(zh_present),
        "missing": [],
    }


def status(agent: dict) -> dict:
    spec = _spec(agent)
    if spec is None:
        return {"available": False, "kind": None, "applied": False, "message": "未配置汉化"}
    kind = spec.get("kind")
    mapping = load_map(agent)
    base = {"available": False, "kind": kind, "applied": False,
            "revertible": False, "keys": len(mapping), "message": ""}
    if not mapping:
        return {**base, "message": "词表缺失"}

    if kind == "text":
        files = locate_text_files(agent)
        if not files:
            return {**base, "message": "未找到安装目录"}
        marker = spec.get("marker") or ""
        unpatched = [f for f in files if marker and marker in f.read_text(encoding="utf-8", errors="ignore")]
        backup = BACKUP_ROOT / agent["id"]
        return {**base, "available": True, "target": str(files[0].parent.parent),
                "applied": not unpatched, "files": len(files),
                "revertible": backup.is_dir(),
                "message": "已汉化" if not unpatched else f"待汉化（{len(unpatched)} 个文件）"}

    if kind == "binary":
        binary = locate_binary(agent)
        if binary is None:
            return {**base, "message": "未找到安装目录"}
        try:
            state = _binary_state(binary, mapping)
        except OSError as e:
            return {**base, "available": False, "message": f"读取失败：{e}"}
        applied = state["pending"] == 0
        writable = os.access(binary, os.W_OK)
        return {**base, "available": True, "target": str(binary), "applied": applied,
                "revertible": binary_backup(binary).exists(),
                "writable": writable, "needs_elevation": not writable,
                "pending": state["pending"], "translated": state["translated"],
                "message": "已汉化" if applied else f"待汉化（{state['pending']} 条英文残留）"}

    return {**base, "message": f"未知类型：{kind}"}


# ---------------------------------------------------------------- text 型应用

def apply_text(agent: dict) -> dict:
    spec = _spec(agent) or {}
    mapping = load_map(agent)
    files = locate_text_files(agent)
    if not files:
        return {"ok": False, "changed": 0, "message": "未找到安装目录"}
    marker = spec.get("marker") or ""
    changed, touched = 0, 0
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if marker and marker not in text:
            continue
        backup = text_backup_path(agent, path)
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup)
        new_text, hits, _ = patch_text(text, mapping)
        if hits:
            path.write_text(new_text, encoding="utf-8")
            changed += hits
            touched += 1
    return {"ok": changed > 0, "changed": changed, "files": touched,
            "message": f"汉化 {changed} 处" if changed else "无需汉化（可能已是最新）"}


def revert_text(agent: dict) -> dict:
    root = BACKUP_ROOT / agent["id"]
    if not root.is_dir():
        return {"ok": False, "message": "没有备份可还原"}
    restored = 0
    for backup in root.glob("*.js"):
        for target in locate_text_files(agent):
            if target.name == backup.name:
                shutil.copy2(backup, target)
                restored += 1
    return {"ok": restored > 0, "restored": restored,
            "message": f"已还原 {restored} 个文件" if restored else "没有匹配的备份文件"}


# ---------------------------------------------------------------- binary 型应用

def load_risky(agent: dict) -> dict:
    """「改了就会把程序搞坏」的词：放进 <map>.risky.json，补丁时跳过。

    二进制里同一段英文可能既是界面文案、又是 Bun/WebKit 内建脚本里的标识符。
    后者一改，整个内建模块语法错乱（实测 claude 的 Reject 会让 --version 直接报
    Invalid character）。所以这类词不猜，直接关进禁改名单。
    """
    spec = _spec(agent) or {}
    name = spec.get("map")
    if not name:
        return {}
    path = MAPS_DIR / (name[:-5] + ".risky.json" if name.endswith(".json") else name + ".risky.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 没有名单 / 名单坏了都当没有
        return {}
    return data if isinstance(data, dict) else {}


def build_patched_binary(agent: dict) -> dict:
    mapping = load_map(agent)
    risky = load_risky(agent)
    mapping = {k: v for k, v in mapping.items() if k not in risky}
    binary = locate_binary(agent)
    if binary is None:
        return {"ok": False, "message": "未找到二进制"}
    buf = bytearray(binary.read_bytes())
    replaced, too_long, missing = 0, [], []
    for en, zh in mapping.items():
        nb, zb = en.encode(), zh.encode()
        if nb not in buf:
            missing.append(en)
            continue
        if len(zb) > len(nb):
            too_long.append(en)
            continue
        replaced += replace_equal_length(buf, nb, zb)
    out_dir = WORK_ROOT / agent["id"]
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / (binary.name + ".patched")
    out.write_bytes(buf)
    out.chmod(0o755)
    return {"ok": replaced > 0, "replaced": replaced, "missing": missing,
            "too_long": too_long, "risky": list(risky), "patched": out, "binary": binary}


def _ps_quote(value) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _elevated(commands: list[list[str]] | None = None,
              ps_commands: list[str] | None = None) -> tuple[int, str]:
    """用系统提权执行文件操作，返回 (returncode, 输出)。

    按平台各用各的原生写法：macOS / Linux 走 POSIX 命令，Windows 走 PowerShell cmdlet——
    那边没有 cp / chmod，PowerShell 5 也不支持 `&&`，硬套 POSIX 命令必然失败。
    """
    commands = commands or []
    if os.environ.get("AAC_NO_ELEVATION", "").lower() in ("1", "true", "yes"):
        # 测试和无人值守场景：绝不弹授权框（会一直挂在那里等点击）
        return 1, "已禁用提权（AAC_NO_ELEVATION）"
    if sys.platform == "darwin":
        shell_cmd = " && ".join(shlex.join([str(a) for a in c]) for c in commands)
        script = (f'do shell script {json.dumps(shell_cmd)} '
                  f'with prompt "需要管理员权限：安装汉化补丁" with administrator privileges')
        proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    elif sys.platform.startswith("linux"):
        shell_cmd = " && ".join(shlex.join([str(a) for a in c]) for c in commands)
        proc = subprocess.run(["pkexec", "sh", "-c", shell_cmd], capture_output=True, text=True)
    elif sys.platform == "win32":
        script = "\r\n".join(ps_commands or [])
        if not script:
            return 1, "没有可执行的提权命令"
        WORK_ROOT.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix="elev-", suffix=".ps1", dir=str(WORK_ROOT))
        os.close(fd)
        tmp = Path(tmp_name)
        try:
            # utf-8-sig：Windows PowerShell 5 无 BOM 会按 ANSI 读，中文路径会乱码
            tmp.write_text(script + "\r\n", encoding="utf-8-sig")
            launcher = ("Start-Process powershell -Verb RunAs -Wait -ArgumentList "
                        f"'-NoProfile','-ExecutionPolicy','Bypass','-File',{_ps_quote(tmp)}")
            proc = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                                   "-Command", launcher], capture_output=True, text=True)
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
    else:
        return 1, "当前平台不支持自动提权"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def install_binary(agent: dict, require_elevation: bool = True) -> dict:
    built = build_patched_binary(agent)
    if not built.get("ok"):
        return {"ok": False, "message": built.get("message") or "没有可替换的文案",
                "missing": built.get("missing") or [], "too_long": built.get("too_long") or []}
    binary: Path = built["binary"]
    patched: Path = built["patched"]
    backup = binary_backup(binary)
    risky_note = f"（跳过 {len(built.get('risky') or [])} 个禁改词）" if built.get("risky") else ""

    # 上机前先在临时目录里跑一遍：宁可汉化失败，不可把系统目录里的程序弄坏。
    # 原版自己都跑不起来（测试用的假文件、异常安装）就没法自检，只能跳过。
    can_check, _orig = binary_health(binary)
    check_note = "" if can_check else "（原程序跑不起来，跳过自检）"
    if can_check:
        if sys.platform == "darwin":
            _codesign(patched)
        healthy, detail = binary_health(patched)
        if not healthy:
            patched.unlink(missing_ok=True)
            return {"ok": False, "needs_elevation": False,
                    "message": f"词表有问题，补丁没装下去：{detail}。请把出错词报给开发者。",
                    "risky": built.get("risky") or []}

    if os.access(binary, os.W_OK) and os.access(binary.parent, os.W_OK):
        if not backup.exists():
            shutil.copy2(binary, backup)
        tmp = binary.with_name(binary.name + ".tmp")
        shutil.copy2(patched, tmp)
        os.replace(tmp, binary)
        sign = _codesign(binary) if sys.platform == "darwin" else (0, "")
        ok, detail = (True, "跳过") if not can_check else binary_health(binary)
        if not ok:
            if backup.exists():
                shutil.copy2(backup, binary)
                if sys.platform == "darwin":
                    _codesign(binary)
            return {"ok": False, "needs_elevation": False, "rolled_back": True,
                    "message": f"补丁后自检失败（{detail}），已自动还原英文"}
        return {"ok": True, "replaced": built["replaced"], "missing": built["missing"],
                "risky": built.get("risky") or [],
                "message": f"汉化 {built['replaced']} 处{risky_note}{check_note}"
                           + ("" if not can_check else f"；自检通过 {detail}")
                           + ("" if sign[0] == 0 else "（重签失败）"),
                "needs_elevation": False}

    if not require_elevation:
        return {"ok": False, "needs_elevation": True,
                "message": "安装目录需要管理员权限：请从界面点「应用汉化」授权，或用 sudo 运行"}

    steps: list[list[str]] = []
    ps_steps: list[str] = []
    if sys.platform == "win32":
        if not backup.exists():
            ps_steps.append(f"Copy-Item -LiteralPath {_ps_quote(binary)} "
                            f"-Destination {_ps_quote(backup)} -Force")
        ps_steps.append(f"Copy-Item -LiteralPath {_ps_quote(patched)} "
                        f"-Destination {_ps_quote(binary)} -Force")
    else:
        if not backup.exists():
            steps.append(["cp", "-p", str(binary), str(backup)])
        steps.append(["cp", "-p", str(patched), str(binary)])
        steps.append(["chmod", "755", str(binary)])
        if sys.platform == "darwin":
            steps.append(["codesign", "--force", "--sign", "-", str(binary)])
    code, out = _elevated(steps, ps_steps)
    if code != 0:
        return {"ok": False, "needs_elevation": True, "message": f"授权失败：{out.strip()[:200]}"}
    ok, detail = (True, "跳过") if not can_check else binary_health(binary)
    if not ok:
        try:
            revert_binary(agent, require_elevation=True)
        except Exception:  # noqa: BLE001 还原都失败也要把话说清楚
            pass
        return {"ok": False, "needs_elevation": True, "rolled_back": True,
                "message": f"补丁后自检失败（{detail}），已自动还原英文"}
    return {"ok": True, "replaced": built["replaced"], "missing": built["missing"],
            "risky": built.get("risky") or [],
            "message": f"汉化 {built['replaced']} 处{risky_note}{check_note}"
                       + ("" if not can_check else f"；自检通过 {detail}"),
            "needs_elevation": True}


def revert_binary(agent: dict, require_elevation: bool = True) -> dict:
    binary = locate_binary(agent)
    if binary is None:
        return {"ok": False, "message": "未找到二进制"}
    backup = binary_backup(binary)
    if not backup.exists():
        return {"ok": False, "message": "没有备份可还原"}
    if os.access(binary, os.W_OK) and os.access(binary.parent, os.W_OK):
        tmp = binary.with_name(binary.name + ".tmp")
        shutil.copy2(backup, tmp)
        os.replace(tmp, binary)
        if sys.platform == "darwin":
            _codesign(binary)
        return {"ok": True, "message": "已还原英文原版"}
    if not require_elevation:
        return {"ok": False, "needs_elevation": True, "message": "还原需要管理员权限"}
    if sys.platform == "win32":
        steps: list[list[str]] = []
        ps_steps = [f"Copy-Item -LiteralPath {_ps_quote(backup)} "
                    f"-Destination {_ps_quote(binary)} -Force"]
    else:
        steps = [["cp", "-p", str(backup), str(binary)]]
        ps_steps = []
        if sys.platform == "darwin":
            steps.append(["codesign", "--force", "--sign", "-", str(binary)])
    code, out = _elevated(steps, ps_steps)
    return {"ok": code == 0, "needs_elevation": True,
            "message": "已还原英文原版" if code == 0 else f"还原失败：{out.strip()[:200]}"}


def _codesign(binary: Path) -> tuple[int, str]:
    proc = subprocess.run(["codesign", "--force", "--sign", "-", str(binary)],
                          capture_output=True, text=True)
    return proc.returncode, proc.stderr.strip()


def _verify_version(binary: Path) -> str:
    try:
        proc = subprocess.run([str(binary), "--version"], capture_output=True, text=True, timeout=60)
        line = (proc.stdout or proc.stderr).strip().splitlines()
        return line[0] if line else ""
    except Exception:  # noqa: BLE001
        return ""


_BAD_MARKERS = ("Unexpected error", "Invalid character", "SyntaxError", "Syntax error",
                "panic:", "segmentation fault", "Illegal instruction", "Trace/breakpoint")


def binary_health(binary: Path) -> tuple[bool, str]:
    """打完补丁必须跑一遍：能启动、退出码 0、不报内建语法错。

    只看版本号第一行是不够的 —— 实测打坏的 claude 先打印 ``2.1.304``，
    紧跟着才报 ``Invalid character``。
    """
    try:
        proc = subprocess.run([str(binary), "--version"], capture_output=True,
                              text=True, timeout=120)
    except Exception as exc:  # noqa: BLE001
        return False, f"跑不起来：{exc}"
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    head = output.splitlines()[0] if output else ""
    for marker in _BAD_MARKERS:
        if marker in output:
            return False, f"自检报错：{marker}"
    if proc.returncode != 0:
        return False, f"自检退出码 {proc.returncode}：{head[:80]}"
    if not head:
        return False, "自检没输出"
    return True, head


# ---------------------------------------------------------------- 统一入口

def apply(agent: dict, require_elevation: bool = True) -> dict:
    spec = _spec(agent)
    if spec is None:
        return {"ok": False, "message": "未配置汉化"}
    kind = spec.get("kind")
    if kind == "text":
        return apply_text(agent)
    if kind == "binary":
        return install_binary(agent, require_elevation=require_elevation)
    return {"ok": False, "message": f"未知类型：{kind}"}


def revert(agent: dict, require_elevation: bool = True) -> dict:
    spec = _spec(agent)
    if spec is None:
        return {"ok": False, "message": "未配置汉化"}
    kind = spec.get("kind")
    if kind == "text":
        return revert_text(agent)
    if kind == "binary":
        return revert_binary(agent, require_elevation=require_elevation)
    return {"ok": False, "message": f"未知类型：{kind}"}


def ensure(agent: dict) -> dict | None:
    """启动/更新后调用：未汉化则自动补打，失败不抛异常。"""
    spec = _spec(agent)
    if spec is None or spec.get("auto") is False:
        return None
    try:
        st = status(agent)
        if not st.get("available") or st.get("applied"):
            return None
        result = apply(agent)
        return {"agent": agent.get("id"), **result}
    except Exception as e:  # noqa: BLE001 - 汉化失败不能影响主流程
        return {"agent": agent.get("id"), "ok": False, "message": str(e)}


def catalog(agents: list[dict]) -> list[dict]:
    items = []
    for agent in agents:
        if _spec(agent) is None:
            continue
        st = status(agent)
        items.append({
            "id": agent["id"],
            "name": agent.get("name", agent["id"]),
            "kind": st.get("kind"),
            "available": st.get("available", False),
            "applied": st.get("applied", False),
            "revertible": st.get("revertible", False),
            "needs_elevation": st.get("needs_elevation", False),
            "keys": st.get("keys", 0),
            "pending": st.get("pending"),
            "target": st.get("target"),
            "message": st.get("message", ""),
        })
    return items


# ---------------------------------------------------------------- CLI

def _cli(argv: list[str] | None = None) -> int:
    import argparse

    from . import registry

    parser = argparse.ArgumentParser(description="汉化补丁：查看状态 / 应用 / 还原")
    parser.add_argument("agent", nargs="?", help="工具 id，如 codex / pi")
    parser.add_argument("--status", action="store_true", help="只看状态（默认）")
    parser.add_argument("--apply", action="store_true", help="打补丁")
    parser.add_argument("--revert", action="store_true", help="还原英文")
    args = parser.parse_args(argv)

    agents = [a for a in registry.load_agents() if a.get("i18n")]
    if args.agent:
        agents = [a for a in agents if a["id"] == args.agent]
        if not agents:
            print(f"未找到支持汉化的工具：{args.agent}")
            return 2
    for agent in agents:
        st = status(agent)
        print(f"[{agent['id']}] {st.get('message')}  target={st.get('target')} keys={st.get('keys')}")
        if args.apply:
            print("  ", apply(agent))
        elif args.revert:
            print("  ", revert(agent))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
