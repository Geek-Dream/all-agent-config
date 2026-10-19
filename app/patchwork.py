"""补丁快照与更新后自动迁移。

流程：
1. 更新前 snapshot()：下载当前版本官方包，保存被修改文件的官方原貌 + 你的修改版
2. 更新后 reapply()：
   - 文件未被更新改动 → 直接把补丁打回去
   - 文件被更新改动 → _port() 用 token 级 diff 把你的修改移植到新版本文件上
     （汉化替换、文案修改这类场景可自动跟上版本；移植失败则保守跳过并报告）
"""

from __future__ import annotations

import difflib
import json
import re
import shutil
import tarfile
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path

from . import categories
from .paths import relative_target
from .safefs import atomic_write_text, ensure_private_dir

WORK_ROOT = Path.home() / ".aac" / "patchwork"
PORT_SIZE_LIMIT = 8 * 1024 * 1024

TOKEN_RE = re.compile(
    r"[A-Za-z_$][\w$]*|\d+(?:\.\d+)?|[^\x00-\x7F]+|[^\w\s]|\s+",
    re.ASCII,
)


def _tokens(s: str) -> list[str]:
    return TOKEN_RE.findall(s)


def _char_offsets(tokens: list[str]) -> list[int]:
    offs, pos = [], 0
    for t in tokens:
        offs.append(pos)
        pos += len(t)
    return offs


def _edits_by_anchors(ot: list[str], pt: list[str], oo: list[int], old: str) -> list[dict]:
    """基于唯一公共 token 锚点找出 old→patched 的编辑区间，O(n)，适合大文件。

    在两个文件中都恰好出现一次的 token，其相对顺序几乎必然一致；
    相邻锚点之间的差异段即为编辑（替换 / 插入 / 删除）。
    """
    from collections import Counter

    co, cp = Counter(ot), Counter(pt)
    uo = {t: i for i, t in enumerate(ot) if co[t] == 1}
    pairs: list[tuple[int, int]] = []
    last = -1
    for j, t in enumerate(pt):
        i = uo.get(t)
        if i is not None and cp.get(t) == 1 and i > last:
            pairs.append((i, j))
            last = i
    if not pairs:
        return []

    edits: list[dict] = []
    o0 = p0 = 0
    all_pairs = pairs + [(len(ot), len(pt))]
    for idx, (i, j) in enumerate(all_pairs):
        nxt = ot[pairs[idx][0]] if idx < len(pairs) else None  # 下一个锚点 token
        if i > o0 or j > p0:
            so, sp = ot[o0:i], pt[p0:j]
            if so:
                edits.append({"start": oo[o0],
                              "end": oo[i] if i < len(oo) else len(old),
                              "repl": "".join(sp), "anchors": None, "next": nxt})
            else:  # 纯插入：以下一个锚点 token 为定位锚（多级回退）
                anchors: list[str] = []
                if i < len(ot):
                    a = ot[i]
                    seen = set()
                    for c in (a, a[:32], a[:16], a[:8]):
                        if c and c not in seen:
                            seen.add(c)
                            anchors.append(c)
                edits.append({"start": oo[o0] if o0 < len(oo) else len(old),
                              "end": oo[o0] if o0 < len(oo) else len(old),
                              "repl": "".join(sp), "anchors": anchors or None})
        o0, p0 = i + 1, j + 1
    return edits


def _core_span(span: str, repl: str, min_core: int = 8) -> tuple[str, str] | None:
    """去掉公共前后缀后的核心差异段；核心太短说明定位依据不足，返回 None。"""
    p = 0
    while p < min(len(span), len(repl)) and span[p] == repl[p]:
        p += 1
    s = 0
    while (s < min(len(span) - p, len(repl) - p)
           and span[len(span) - 1 - s] == repl[len(repl) - 1 - s]):
        s += 1
    core_o = span[p:len(span) - s if s else len(span)]
    core_r = repl[p:len(repl) - s if s else len(repl)]
    if len(core_o) >= min_core and core_o != core_r:
        return core_o, core_r
    return None


def _port(old: str, patched: str, new: str) -> str | None:
    """把 old→patched 的修改移植到 new 上；失败（定位不到）返回 None。"""
    if old == patched or old == new:
        return None
    ot, pt = _tokens(old), _tokens(patched)
    oo = _char_offsets(ot)
    edits = _edits_by_anchors(ot, pt, oo, old)
    if not edits:
        return None

    parts: list[str] = []
    cursor = 0
    applied: set[str] = set()
    for e in edits:
        if e.get("anchors"):
            k = -1
            for c in e["anchors"]:
                k = new.find(c, cursor)
                if k >= 0:
                    break
            if k < 0:
                return None
            parts.append(new[cursor:k])
            parts.append(e["repl"])
            cursor = k
            continue
        span = old[e["start"]:e["end"]]
        if not span:
            return None
        if span == e["repl"]:
            continue  # 恒等段（锚点稀疏产生的噪音），跳过
        k = new.find(span, cursor)
        target_span, target_repl = span, e["repl"]
        window_found = False
        na = -1
        if e.get("next"):
            na = new.find(e["next"], cursor)
        if k < 0:
            if span in applied:
                continue  # 多处相同编辑：该片段已被前面的编辑消费完
            # 结构变化（上游插入/重排）：在下一个唯一锚点限定的窗口内反向定位
            win_end = na if na >= 0 else len(new)
            k = new.rfind(span, cursor, win_end)
            if k >= 0:
                window_found = True
        if k < 0:
            # 上游在锚点与目标之间插入内容 / 边缘被截断：
            # 用公共前后缀核心定位（要求核心足够长且唯一）
            win_end = na if na >= 0 else len(new)
            core = _core_span(span, e["repl"])
            if core:
                k = new.find(core[0], cursor, win_end)
                if k >= 0 and new.find(core[0], k + 1, win_end) < 0:
                    target_span, target_repl = core
                    window_found = True
        if k < 0:
            # 修改目标在新版本中已不存在，保守判定移植失败
            return None
        applied.add(span)
        parts.append(new[cursor:k])
        parts.append(target_repl)
        cursor = k + len(target_span)
        if not target_repl or window_found:
            continue  # 删除操作、窗口定位不做多处替换（避免误伤）
        # minified JS 中片段常出现多次（如汉化多处相同文案）：后续出现一并替换
        while True:
            k2 = new.find(target_span, cursor)
            if k2 < 0:
                break
            parts.append(new[cursor:k2])
            parts.append(target_repl)
            cursor = k2 + len(target_span)
    parts.append(new[cursor:])
    result = "".join(parts)
    return result if result != new else None


# ---------------------------------------------------------------- 快照 / 重打

def work_dir(agent_id: str) -> Path:
    return WORK_ROOT / agent_id


def snapshot(agent: dict, patches: dict, log: list[str]) -> bool:
    """更新前保存补丁快照：官方原貌 + 修改版 + 元数据。"""
    pdir = categories.package_dir(agent)
    if not pdir:
        log.append("[补丁] 找不到安装目录，无法保存快照\n")
        return False
    pkg, version = patches.get("pkg"), patches.get("pkg_version")
    url = categories._tarball_url(pkg, version) if pkg and version else None
    if not url:
        log.append("[补丁] 无法获取官方包地址，跳过快照\n")
        return False
    work = work_dir(agent["id"])
    temp_work: Path | None = None
    try:
        with tempfile.TemporaryDirectory() as td:
            tgz = Path(td) / "pkg.tgz"
            urllib.request.urlretrieve(url, tgz)  # noqa: S310
            with tarfile.open(tgz, "r:gz") as tar:
                categories._safe_extract(tar, Path(td))
            official = Path(td) / "package"

            if work.is_symlink():
                raise ValueError("补丁快照目录是符号链接，拒绝覆盖")
            ensure_private_dir(WORK_ROOT)
            temp_work = Path(tempfile.mkdtemp(
                prefix=f".{agent['id']}-", dir=WORK_ROOT))
            (temp_work / "official").mkdir()
            (temp_work / "patched").mkdir()

            meta = {"pkg": pkg, "pkg_version": version,
                    "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "base": {}, "patched_hashes": {}}
            for rel in patches.get("modified", []):
                safe_rel = relative_target(pdir, rel)
                src_off = relative_target(official, rel)
                if safe_rel is None or src_off is None:
                    continue
                src_pat = safe_rel
                if not (src_off.is_file() and src_pat.is_file()):
                    continue
                dst_off = temp_work / "official" / rel
                dst_pat = temp_work / "patched" / rel
                dst_off.parent.mkdir(parents=True, exist_ok=True)
                dst_pat.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_off, dst_off)
                shutil.copy2(src_pat, dst_pat)
                meta["base"][rel] = categories._sha256(src_off)
                meta["patched_hashes"][rel] = categories._sha256(src_pat)
            for rel in patches.get("added", []):
                src_pat = relative_target(pdir, rel)
                if src_pat is None:
                    continue
                if not src_pat.is_file():
                    continue
                dst_pat = temp_work / "patched" / rel
                dst_pat.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_pat, dst_pat)
                meta["patched_hashes"][rel] = categories._sha256(src_pat)
            if not meta["patched_hashes"]:
                raise ValueError("没有可保存的补丁文件")
            atomic_write_text(
                temp_work / "meta.json",
                json.dumps(meta, ensure_ascii=False, indent=2),
            )
            old = work.with_name(f"{work.name}.old-{uuid.uuid4().hex}")
            if work.exists():
                work.rename(old)
            try:
                temp_work.rename(work)
                temp_work = None
            except Exception:
                if old.exists() and not work.exists():
                    old.rename(work)
                raise
            finally:
                if old.exists():
                    shutil.rmtree(old)
        log.append(f"[补丁] 已保存 {len(meta['patched_hashes'])} 个文件快照（基于 v{version}）\n")
        return True
    except Exception as e:  # noqa: BLE001
        log.append(f"[补丁] 快照失败：{e}\n")
        return False
    finally:
        if temp_work is not None and temp_work.exists():
            shutil.rmtree(temp_work, ignore_errors=True)


def _backup(p: Path, log: list[str]) -> None:
    if p.is_file():
        suffix = time.strftime(".bak-aac-%Y%m%d-%H%M%S")
        index = 0
        while True:
            bak = p.with_name(p.name + suffix + (f"-{index}" if index else ""))
            try:
                with bak.open("xb") as dst, p.open("rb") as src:
                    shutil.copyfileobj(src, dst)
                shutil.copystat(p, bak)
                log.append(f"        原文件已备份 → {bak.name}\n")
                return
            except FileExistsError:
                index += 1


def reapply(agent: dict, log: list[str]) -> dict:
    """更新后尝试把补丁打回/移植到新版文件。"""
    work = work_dir(agent["id"])
    meta_file = work / "meta.json"
    if not meta_file.is_file():
        result = {"status": "failed", "ok": 0, "ported": 0, "failed": 1}
        log.append("[补丁] 找不到更新前快照，无法重打\n")
        return result
    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    pdir = categories.package_dir(agent)
    if not pdir:
        result = {"status": "failed", "ok": 0, "ported": 0,
                  "failed": len(meta.get("patched_hashes", {})) or 1}
        log.append("[补丁] 更新后找不到安装目录，跳过重打\n")
        return result

    ok = ported = failed = 0
    for rel, psha in meta.get("patched_hashes", {}).items():
        target = relative_target(pdir, rel)
        patched_src = relative_target(work / "patched", rel)
        if target is None or patched_src is None or not patched_src.is_file():
            failed += 1
            log.append(f"  ❌ 快照路径不安全或文件缺失  {rel}\n")
            continue
        base_sha = meta.get("base", {}).get(rel)

        if base_sha is None:
            # 新增文件：目标不存在或内容不同则恢复
            if target.is_file() and categories._sha256(target) == psha:
                continue
            _backup(target, log)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(patched_src, target)
            ok += 1
            log.append(f"  ➕ 新增恢复  {rel}\n")
            continue

        if not target.is_file():
            shutil.copy2(patched_src, target)
            ok += 1
            log.append(f"  ✅ 重新打上  {rel}（文件曾被删除，已恢复）\n")
            continue

        cur_sha = categories._sha256(target)
        if cur_sha == psha:
            continue  # 补丁还在，无需处理
        if cur_sha == base_sha:
            # 更新未改动此文件 → 直接打回
            _backup(target, log)
            shutil.copy2(patched_src, target)
            ok += 1
            log.append(f"  ✅ 重新打上  {rel}\n")
            continue

        # 文件被新版本改变 → 尝试移植
        try:
            if target.stat().st_size > PORT_SIZE_LIMIT:
                failed += 1
                log.append(f"  ❌ 跳过（文件过大）{rel}\n")
                continue
            official = relative_target(work / "official", rel)
            if official is None or not official.is_file():
                failed += 1
                log.append(f"  ❌ 官方基线缺失  {rel}\n")
                continue
            old_text = official.read_text(errors="replace")
            pat_text = patched_src.read_text(errors="replace")
            new_text = target.read_text(errors="replace")
            ported_text = _port(old_text, pat_text, new_text)
            if ported_text is not None and ported_text != new_text:
                _backup(target, log)
                target.write_text(ported_text)
                ported += 1
                log.append(f"  🔄 自动移植  {rel}（修改已迁移到新版本）\n")
            else:
                failed += 1
                log.append(f"  ❌ 移植失败  {rel}（新版本改动较大，需手动重新打补丁）\n")
        except Exception as e:  # noqa: BLE001
            failed += 1
            log.append(f"  ❌ 移植出错  {rel}：{e}\n")

    log.append(f"\n[补丁] 重打完成：成功 {ok}，自动移植 {ported}，失败 {failed}\n")
    if failed:
        log.append("[补丁] 失败的文件已在快照中保留（~/.aac/patchwork），可手动处理后重新应用\n")
    status = "complete" if failed == 0 else ("partial" if ok or ported else "failed")
    return {"status": status, "ok": ok, "ported": ported, "failed": failed}
