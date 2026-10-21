"""WebDAV 客户端（零依赖，urllib 实现）。

支持坚果云 / Nextcloud / 群晖 等标准 WebDAV。
settings 存于 ~/.aac/settings.json，密码仅存本机。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from . import archive
from .safefs import atomic_write_text, ensure_private_dir

SETTINGS_FILE = Path.home() / ".aac" / "settings.json"
SYNC_STATE_FILE = Path.home() / ".aac" / "cloud-state.json"
MAX_DOWNLOAD_SIZE = archive.AAC_LIMITS.max_total_size

DEFAULT_SETTINGS: dict = {
    "webdav": {"url": "", "username": "", "password": "", "remote_dir": "/aac"},
    "auto_backup": {"enabled": False, "interval_hours": 24, "agents": []},
    "ccswitch": {"db": ""},
    "launch": {"terminal": "auto", "custom_terminal": "", "resume": False,
                "cwd": "", "modes": {}},
    "i18n": {"auto": True, "agents": {}},
}


def load_settings() -> dict:
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
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
    atomic_write_text(
        SETTINGS_FILE,
        json.dumps(settings, ensure_ascii=False, indent=2),
        mode=0o600,
    )
    return settings


def load_sync_state() -> dict:
    try:
        data = json.loads(SYNC_STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_sync_state(state: dict) -> None:
    atomic_write_text(
        SYNC_STATE_FILE,
        json.dumps(state, ensure_ascii=False, indent=2),
        mode=0o600,
    )


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

    def list_files(self, path: str) -> dict[str, dict]:
        """返回远端文件元数据（大小、ETag、最后修改时间）。"""
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
        files: dict[str, dict] = {}
        root = ET.fromstring(body)
        for resp_el in root:
            href = resp_el.findtext(".//{DAV:}href") or resp_el.findtext(".//href") or ""
            is_collection = resp_el.find(".//{DAV:}collection") is not None
            size_txt = resp_el.findtext(".//{DAV:}getcontentlength") or "0"
            etag = resp_el.findtext(".//{DAV:}getetag") or ""
            modified = resp_el.findtext(".//{DAV:}getlastmodified") or ""
            name = urllib.parse.unquote(href.rstrip("/")).rsplit("/", 1)[-1]
            if name and not is_collection:
                files[name] = {
                    "size": int(size_txt or 0),
                    "etag": etag.strip(),
                    "modified": modified.strip(),
                }
        return files

    def upload(self, local: Path, remote_dir: str, remote_name: str) -> None:
        self.ensure_dir(remote_dir)
        code, _ = self._req("PUT", f"{remote_dir.strip('/')}/{remote_name}",
                            local.read_bytes())
        if code not in (200, 201, 204):
            raise WebDAVError(f"上传失败，HTTP {code}")

    def download(
        self,
        remote_dir: str,
        remote_name: str,
        local: Path,
        max_size: int = MAX_DOWNLOAD_SIZE,
    ) -> None:
        if not safe_remote_name(remote_name):
            raise WebDAVError("远端文件名不安全")
        url = self.base + "/" + f"{remote_dir.strip('/')}/{remote_name}".lstrip("/")
        req = urllib.request.Request(url, method="GET")  # noqa: S310
        for key, value in self.headers.items():
            req.add_header(key, value)
        ensure_private_dir(local.parent)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{local.name}.", suffix=".download", dir=local.parent)
        tmp = Path(tmp_name)
        try:
            try:
                response = urllib.request.urlopen(req, timeout=self.timeout)  # noqa: S310
            except urllib.error.HTTPError as exc:
                raise WebDAVError(f"下载失败，HTTP {exc.code}") from exc
            except Exception as exc:  # noqa: BLE001
                raise WebDAVError(str(exc)) from exc
            with response:
                if response.status != 200:
                    raise WebDAVError(f"下载失败，HTTP {response.status}")
                try:
                    content_length = int(response.headers.get("Content-Length", "0"))
                except (TypeError, ValueError):
                    content_length = 0
                if content_length > max_size:
                    raise WebDAVError(
                        f"远端文件超过 {max_size // 1024 // 1024}MB 限制")
                total = 0
                with os.fdopen(fd, "wb") as fh:
                    fd = -1
                    while True:
                        chunk = response.read(1 << 20)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > max_size:
                            raise WebDAVError(
                                f"远端文件超过 {max_size // 1024 // 1024}MB 限制")
                        fh.write(chunk)
                    fh.flush()
                    os.fsync(fh.fileno())
            from . import bundle
            bundle.validate_aac(tmp)
            os.replace(tmp, local)
            try:
                local.chmod(0o600)
            except OSError:
                if os.name != "nt":
                    raise
        finally:
            if fd >= 0:
                os.close(fd)
            tmp.unlink(missing_ok=True)

    def delete(self, remote_dir: str, remote_name: str) -> None:
        self._req("DELETE", f"{remote_dir.strip('/')}/{remote_name}")


def get_client() -> WebDAV:
    s = load_settings()["webdav"]
    return WebDAV(s["url"], s["username"], s["password"])


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_remote_name(name: str) -> bool:
    return (
        bool(name)
        and name.endswith(".aac")
        and Path(name).name == name
        and "\\" not in name
        and ":" not in name
        and "\0" not in name
    )


def remote_fingerprint(meta: dict) -> str:
    payload = {
        "size": int(meta.get("size", 0)),
        "etag": str(meta.get("etag", "")),
        "modified": str(meta.get("modified", "")),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))
