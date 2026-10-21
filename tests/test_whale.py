"""鲸鱼妹提示语扩展（aac-whale）安装器测试：全部在临时 home 里跑。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import whale


@pytest.fixture()
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".pi" / "agent").mkdir(parents=True)
    settings = home / ".pi" / "agent" / "settings.json"
    settings.write_text(json.dumps({"packages": ["npm:foo"]}), encoding="utf-8")
    monkeypatch.setattr(whale, "_target", lambda: home / ".pi" / "pi-extensions" / "aac-whale")
    monkeypatch.setattr(whale, "_settings_file", lambda: settings)
    monkeypatch.setattr(whale, "_state_file", lambda: home / ".aac" / "whale-state.json")
    return {"home": home, "settings": settings}


def test_install_copies_asset_and_registers(fake_home):
    result = whale.install()
    assert result["ok"] is True
    target = fake_home["home"] / ".pi" / "pi-extensions" / "aac-whale"
    assert (target / "extensions" / "whale.ts").is_file()
    assert (target / "package.json").is_file()

    data = json.loads(fake_home["settings"].read_text(encoding="utf-8"))
    assert str(target) in data["packages"]
    assert data["packages"][0] == "npm:foo"       # 原有条目不被清掉
    assert fake_home["settings"].with_name("settings.json.aac-bak").exists()

    st = whale.status()
    assert st["installed"] and st["registered"] and st["active"]


def test_install_is_idempotent(fake_home):
    whale.install()
    whale.install()
    data = json.loads(fake_home["settings"].read_text(encoding="utf-8"))
    target = fake_home["home"] / ".pi" / "pi-extensions" / "aac-whale"
    assert data["packages"].count(str(target)) == 1


def test_uninstall_removes_dir_and_entry(fake_home):
    whale.install()
    result = whale.uninstall()
    assert result["ok"] is True
    assert not (fake_home["home"] / ".pi" / "pi-extensions" / "aac-whale").exists()
    data = json.loads(fake_home["settings"].read_text(encoding="utf-8"))
    assert data["packages"] == ["npm:foo"]
    assert whale.status()["active"] is False


def test_status_reports_conflicting_extension(fake_home):
    data = json.loads(fake_home["settings"].read_text(encoding="utf-8"))
    data["packages"].append({"source": "git:x", "extensions": ["extensions/whimsical.ts"]})
    fake_home["settings"].write_text(json.dumps(data), encoding="utf-8")
    assert whale.status()["conflicts"] == ["extensions/whimsical.ts"]


def test_install_fails_when_asset_missing(fake_home, monkeypatch, tmp_path):
    monkeypatch.setattr(whale, "ASSET_DIR", tmp_path / "nope")
    result = whale.install()
    assert result["ok"] is False and "资源缺失" in result["message"]


def test_asset_has_messages():
    text = (whale.ASSET_DIR / "extensions" / "whale.ts").read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if ln.strip().startswith('"') and ln.rstrip().endswith('",')]
    assert len(lines) >= 100            # 够多才不重复
    assert "setWorkingMessage" in text  # 接口没写错
    assert text.count('"') % 2 == 0


def _settings_with_whimsical(fake_home) -> None:
    settings = fake_home["settings"]
    data = json.loads(settings.read_text(encoding="utf-8"))
    data["packages"].append({
        "source": "git:https://example.invalid/agent-stuff",
        "extensions": ["extensions/whimsical.ts"],
        "skills": [],
    })
    settings.write_text(json.dumps(data), encoding="utf-8")


def test_install_disables_whimsical_and_remembers_it(fake_home):
    """梗已经合并进 whale.ts，install 必须把 whimsical 关掉，不然等于白合并。"""
    _settings_with_whimsical(fake_home)
    assert whale.status()["conflicts"] == ["extensions/whimsical.ts"]

    whale.install()

    data = json.loads(fake_home["settings"].read_text(encoding="utf-8"))
    entry = next(item for item in data["packages"]
                 if isinstance(item, dict) and "agent-stuff" in str(item.get("source")))
    assert entry["extensions"] == []
    assert entry["skills"] == []          # 其它资源不动
    assert whale.status()["conflicts"] == []

    state = json.loads((fake_home["home"] / ".aac" / "whale-state.json").read_text(encoding="utf-8"))
    assert state["whimsical"]["git:https://example.invalid/agent-stuff"] == ["extensions/whimsical.ts"]


def test_uninstall_restores_whimsical(fake_home):
    _settings_with_whimsical(fake_home)
    whale.install()
    whale.uninstall()

    data = json.loads(fake_home["settings"].read_text(encoding="utf-8"))
    entry = next(item for item in data["packages"]
                 if isinstance(item, dict) and "agent-stuff" in str(item.get("source")))
    assert entry["extensions"] == ["extensions/whimsical.ts"]
    assert whale.status()["conflicts"] == ["extensions/whimsical.ts"]
