"""WebDAV 客户端（零依赖，urllib 实现）。

支持坚果云 / Nextcloud / 群晖 等标准 WebDAV。
settings 存于 ~/.aac/settings.json，密码仅存本机。
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

SETTINGS_FILE = Path.home() / ".aac" / "settings.json"

DEFAULT_SETTINGS: dict = {
    "webdav": {"url": "", "username": "", "password": "", "remote_dir": "/aac"},
    "auto_backup": {"enabled": False, "interval_hours": 24, "agents": []},
}


def load_settings() -> dict:
    try:
        data = json.loads(SETTINGS_FILE.read_text())
    except Exception:
        data = {}
    merged = json.loads(json.dumps(DEFAULT_SETTINGS))
    for k, v in (data or {}).items():
        if isinstance(v, dict) and k in merged:
            merged[k].update(v)
        else:
            merged[k] = v
    return merged


def save_settings(patch: dict) -> dict:
    settings = load_settings()
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(settings.get(k), dict):
            settings[k].update(v)
        else:
            settings[k] = v
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(settings, ensure_ascii=False, indent=2))
    return settings


class WebDAVError(Exception):
    pass


class WebDAV:
    def __init__(self, url: str, username: str, password: str, timeout: int = 30):
        if not url:
            raise WebDAVError("未配置 WebDAV 地址")
        self.base = url.rstrip("/")
        self.timeout = timeout
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        self.headers = {"Authorization": f"Basic {token}"}

    def _req(self, method: str, path: str, data: bytes | None = None) -> tuple[int, bytes]:
        url = self.base + "/" + path.lstrip("/")
        req = urllib.request.Request(url, data=data, method=method)  # noqa: S310
        for k, v in self.headers.items():
            req.add_header(k, v)
        if data is not None:
            req.add_header("Content-Type", "application/octet-stream")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read() or b""
        except Exception as e:  # noqa: BLE001
            raise WebDAVError(str(e)) from e

    def test(self) -> str:
        """连接测试：尝试 PROPFIND 根路径。"""
        code, _ = self._req("PROPFIND", "", None)
        if code in (200, 207, 404, 301, 302):
            return "ok"
        raise WebDAVError(f"连接失败，HTTP {code}（请检查地址与账号密码）")

    def ensure_dir(self, path: str) -> None:
        code, _ = self._req("MKCOL", path)
        if code not in (201, 405):
            raise WebDAVError(f"创建远程目录失败，HTTP {code}")

    def list_files(self, path: str) -> dict[str, int]:
        """返回 {文件名: 大小}（Depth:1 的非集合资源）。"""
        req_headers = dict(self.headers)
        req_headers["Depth"] = "1"
        url = self.base + "/" + path.strip("/")
        req = urllib.request.Request(url, method="PROPFIND", data=b"")  # noqa: S310
        for k, v in req_headers.items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
                body = resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return {}
            raise WebDAVError(f"列出远程文件失败，HTTP {e.code}") from e
        files: dict[str, int] = {}
        root = ET.fromstring(body)
        for resp_el in root:
            href = resp_el.findtext(".//{DAV:}href") or resp_el.findtext(".//href") or ""
            is_collection = resp_el.find(".//{DAV:}collection") is not None
            size_txt = resp_el.findtext(".//{DAV:}getcontentlength") or "0"
            name = urllib.parse.unquote(href.rstrip("/")).rsplit("/", 1)[-1]
            if name and not is_collection:
                files[name] = int(size_txt or 0)
        return files

    def upload(self, local: Path, remote_dir: str, remote_name: str) -> None:
        self.ensure_dir(remote_dir)
        code, _ = self._req("PUT", f"{remote_dir.strip('/')}/{remote_name}",
                            local.read_bytes())
        if code not in (200, 201, 204):
            raise WebDAVError(f"上传失败，HTTP {code}")

    def download(self, remote_dir: str, remote_name: str, local: Path) -> None:
        code, body = self._req("GET", f"{remote_dir.strip('/')}/{remote_name}")
        if code != 200:
            raise WebDAVError(f"下载失败，HTTP {code}")
        local.write_bytes(body)

    def delete(self, remote_dir: str, remote_name: str) -> None:
        self._req("DELETE", f"{remote_dir.strip('/')}/{remote_name}")


def get_client() -> WebDAV:
    s = load_settings()["webdav"]
    return WebDAV(s["url"], s["username"], s["password"])
