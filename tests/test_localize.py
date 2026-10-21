"""汉化补丁引擎测试：text（Pi 式 JS）与 binary（Codex 式原生包）两种驱动。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import localize


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    pkg = tmp_path / "pkg"
    (pkg / "dist" / "core").mkdir(parents=True)
    monkeypatch.setattr(localize, "find_package", lambda name: pkg if name else None)
    monkeypatch.setattr(localize, "MAPS_DIR", tmp_path / "maps")
    monkeypatch.setattr(localize, "BACKUP_ROOT", tmp_path / "backup")
    monkeypatch.setattr(localize, "WORK_ROOT", tmp_path / "work")
    (tmp_path / "maps").mkdir()
    return {"root": tmp_path, "pkg": pkg}


def _write_map(sandbox, mapping: dict, name: str = "test.json") -> str:
    path = sandbox["root"] / "maps" / name
    path.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
    return str(path)


def _text_agent(map_path: str) -> dict:
    return {
        "id": "pi",
        "name": "Pi",
        "i18n": {
            "kind": "text",
            "map": map_path,
            "marker": "Open settings menu",
            "locate": {"package": "@x/pi", "glob": "dist/**/*.js"},
        },
    }


def _binary_agent(map_path: str) -> dict:
    return {
        "id": "codex",
        "i18n": {
            "kind": "binary",
            "map": map_path,
            "locate": {"package": "@openai/codex", "glob": "node_modules/@openai/codex-*/vendor/*/bin/codex*"},
        },
    }


def test_text_apply_and_revert(sandbox):
    target = sandbox["pkg"] / "dist" / "core" / "slash-commands.js"
    target.write_text('const a = {description: "Start a new session"};\n'
                      'const b = {description:"Open settings menu"};\n', encoding="utf-8")
    mapping = {"Start a new session": "开始新会话", "Open settings menu": "打开设置菜单"}
    agent = _text_agent(_write_map(sandbox, mapping))

    assert localize.status(agent)["applied"] is False
    result = localize.apply(agent)
    assert result["ok"] and result["changed"] == 2
    text = target.read_text(encoding="utf-8")
    assert "开始新会话" in text and "打开设置菜单" in text
    assert localize.status(agent)["applied"] is True

    backup = localize.text_backup_path(agent, target)
    assert backup.exists()

    assert localize.revert(agent)["ok"] is True
    assert "Start a new session" in target.read_text(encoding="utf-8")
    assert localize.status(agent)["applied"] is False


def test_text_apply_is_idempotent(sandbox):
    target = sandbox["pkg"] / "dist" / "core" / "slash-commands.js"
    target.write_text('{description: "Start a new session"}\n'
                      '{description: "Open settings menu"}\n', encoding="utf-8")
    agent = _text_agent(_write_map(sandbox, {"Start a new session": "开始新会话",
                                             "Open settings menu": "打开设置菜单"}))
    assert localize.apply(agent)["changed"] == 2
    second = localize.apply(agent)
    assert second["changed"] == 0 and second["ok"] is False


def test_binary_apply_pads_and_keeps_length(sandbox, monkeypatch):
    binary = sandbox["pkg"] / "node_modules" / "@openai" / "codex-darwin-arm64" / "vendor" / "aarch64" / "bin" / "codex"
    binary.parent.mkdir(parents=True)
    original = b"....choose what model and reasoning effort to use...."
    binary.write_bytes(original)
    monkeypatch.setattr(localize.sys, "platform", "linux")  # 跳过 codesign
    agent = _binary_agent(_write_map(sandbox, {
        "choose what model and reasoning effort to use": "选择模型与推理强度",
    }))

    assert localize.status(agent)["applied"] is False
    result = localize.apply(agent)
    assert result["ok"] is True, result
    patched = binary.read_bytes()
    assert len(patched) == len(original)
    assert "选择模型与推理强度".encode() in patched
    assert b"choose what model" not in patched
    assert localize.binary_backup(binary).read_bytes() == original
    assert localize.status(agent)["applied"] is True

    assert localize.revert(agent)["ok"] is True
    assert binary.read_bytes() == original


def test_binary_too_long_is_skipped(sandbox, monkeypatch):
    binary = sandbox["pkg"] / "node_modules" / "@openai" / "codex-x64" / "vendor" / "x" / "bin" / "codex"
    binary.parent.mkdir(parents=True)
    original = b"....Copy...."
    binary.write_bytes(original)
    monkeypatch.setattr(localize.sys, "platform", "linux")
    agent = _binary_agent(_write_map(sandbox, {"Copy": "复制到剪贴板啦"}))

    result = localize.apply(agent)
    assert result["ok"] is False
    assert result["too_long"] == ["Copy"]
    assert binary.read_bytes() == original


def test_binary_multiple_occurrences(sandbox, monkeypatch):
    binary = sandbox["pkg"] / "node_modules" / "@openai" / "codex-x64" / "vendor" / "x" / "bin" / "codex"
    binary.parent.mkdir(parents=True)
    original = b"AAAA Create AGENTS.md BAAA Create AGENTS.md C"
    binary.write_bytes(original)
    monkeypatch.setattr(localize.sys, "platform", "linux")
    agent = _binary_agent(_write_map(sandbox, {"Create AGENTS.md": "创建 AGENTS.md"}))

    result = localize.apply(agent)
    assert result["replaced"] == 2
    patched = binary.read_bytes()
    assert patched.count("创建 AGENTS.md".encode()) == 2
    assert len(patched) == len(original)


def test_status_without_map(sandbox):
    agent = _text_agent(str(sandbox["root"] / "maps" / "nope.json"))
    st = localize.status(agent)
    assert st["available"] is False and "词表缺失" in st["message"]


def test_ensure_respects_auto_flag(sandbox):
    target = sandbox["pkg"] / "dist" / "core" / "slash-commands.js"
    target.write_text('{description: "Start a new session"}'
                      '{description: "Open settings menu"}', encoding="utf-8")
    agent = _text_agent(_write_map(sandbox, {"Start a new session": "开始新会话",
                                             "Open settings menu": "打开设置菜单"}))
    agent["i18n"]["auto"] = False
    assert localize.ensure(agent) is None
    assert "Start a new session" in target.read_text(encoding="utf-8")


def test_ensure_applies_when_pending(sandbox):
    target = sandbox["pkg"] / "dist" / "core" / "slash-commands.js"
    target.write_text('{description: "Start a new session"}'
                      '{description: "Open settings menu"}', encoding="utf-8")
    agent = _text_agent(_write_map(sandbox, {"Start a new session": "开始新会话",
                                             "Open settings menu": "打开设置菜单"}))
    result = localize.ensure(agent)
    assert result and result["ok"] is True
    assert localize.ensure(agent) is None  # 已汉化不再重复


def test_catalog_lists_only_i18n_agents(sandbox):
    plain = {"id": "claude-code", "name": "Claude Code"}
    agent = _text_agent(_write_map(sandbox, {"Start a new session": "开始新会话"}))
    items = localize.catalog([plain, agent])
    assert [i["id"] for i in items] == ["pi"]
    assert items[0]["keys"] == 1


def test_windows_elevation_uses_powershell_copy(sandbox, monkeypatch):
    """Windows 提权分支必须是 PowerShell 命令：那边没有 cp/chmod，也不支持 &&。"""
    binary = (sandbox["pkg"] / "node_modules" / "@openai" / "codex-win32-x64"
              / "vendor" / "x86_64-pc-windows-msvc" / "bin" / "codex.exe")
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"....choose what model and reasoning effort to use....")
    monkeypatch.setattr(localize.sys, "platform", "win32")
    monkeypatch.setattr(localize.os, "access", lambda *a, **k: False)  # 强制走提权分支
    monkeypatch.setattr(localize, "_verify_version", lambda path: "")
    captured: dict = {}

    def fake_elevated(commands=None, ps_commands=None):
        captured["commands"] = commands or []
        captured["ps"] = ps_commands or []
        return 0, ""

    monkeypatch.setattr(localize, "_elevated", fake_elevated)
    agent = _binary_agent(_write_map(sandbox, {
        "choose what model and reasoning effort to use": "选择模型与推理强度",
    }))
    result = localize.install_binary(agent)
    assert result["ok"] is True, result
    assert captured["ps"], "Windows 必须给出 PowerShell 步骤"
    assert all("Copy-Item -LiteralPath" in step for step in captured["ps"])
    assert not any("chmod" in part or "cp" == part for part in sum(captured["commands"], []))
    assert "&&" not in " ".join(captured["ps"])
