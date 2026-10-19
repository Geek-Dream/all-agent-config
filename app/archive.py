"""Bounded, path-safe extraction for ZIP and TAR archives."""

from __future__ import annotations

import shutil
import stat
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from .paths import relative_target


@dataclass(frozen=True)
class ArchiveLimits:
    max_members: int
    max_total_size: int
    max_ratio: int = 200
    max_path_length: int = 4096
    max_path_depth: int = 64


DEFAULT_LIMITS = ArchiveLimits(
    max_members=20_000,
    max_total_size=2 * 1024 * 1024 * 1024,
)
AAC_LIMITS = ArchiveLimits(
    max_members=20_000,
    max_total_size=512 * 1024 * 1024,
    max_ratio=250,
)


def _validate_name(name: str, *, is_dir: bool, limits: ArchiveLimits) -> None:
    if (not isinstance(name, str) or not name or len(name) > limits.max_path_length
            or "\\" in name or ":" in name or "\0" in name):
        raise ValueError(f"压缩包含无效路径: {name!r}")
    pure = PurePosixPath(name)
    if (pure.is_absolute() or PureWindowsPath(name).drive or ".." in pure.parts
            or len(pure.parts) > limits.max_path_depth
            or (str(pure) in ("", ".") and not is_dir)):
        raise ValueError(f"压缩包含危险路径: {name}")


def _check_limits(count: int, total_size: int, compressed_size: int,
                  limits: ArchiveLimits) -> None:
    if count > limits.max_members:
        raise ValueError(f"压缩包成员过多（{count} > {limits.max_members}）")
    if total_size > limits.max_total_size:
        raise ValueError(
            f"压缩包解压后过大（{total_size} > {limits.max_total_size} 字节）")
    if total_size > 16 * 1024 * 1024:
        ratio = total_size / max(compressed_size, 1)
        if ratio > limits.max_ratio:
            raise ValueError(
                f"压缩比异常（{ratio:.0f}:1 > {limits.max_ratio}:1），拒绝解压")


def inspect_zip(path: Path, limits: ArchiveLimits = DEFAULT_LIMITS) -> None:
    with zipfile.ZipFile(path) as zf:
        total = 0
        for info in zf.infolist():
            _validate_name(info.filename, is_dir=info.is_dir(), limits=limits)
            total += info.file_size
        _check_limits(len(zf.infolist()), total, path.stat().st_size, limits)


def inspect_tar(tar: tarfile.TarFile, limits: ArchiveLimits = DEFAULT_LIMITS) -> None:
    members = tar.getmembers()
    total = 0
    for member in members:
        _validate_name(member.name, is_dir=member.isdir(), limits=limits)
        if not (member.isdir() or member.isfile()):
            raise ValueError(f"压缩包含链接或特殊文件: {member.name}")
        total += member.size
    try:
        compressed_size = Path(tar.name).stat().st_size
    except (OSError, TypeError):
        compressed_size = 1
    _check_limits(len(members), total, compressed_size, limits)


def extract_zip(path: Path, dest: Path, limits: ArchiveLimits = DEFAULT_LIMITS) -> None:
    with zipfile.ZipFile(path) as zf:
        infos = zf.infolist()
        total = sum(info.file_size for info in infos)
        _check_limits(len(infos), total, path.stat().st_size, limits)
        planned: list[tuple[zipfile.ZipInfo, Path | None]] = []
        seen: set[Path] = set()
        for info in infos:
            is_dir = info.is_dir()
            _validate_name(info.filename, is_dir=is_dir, limits=limits)
            target = relative_target(dest, info.filename, allow_root=is_dir)
            mode = (info.external_attr >> 16) & 0o170000
            if target is None or mode == stat.S_IFLNK:
                raise ValueError(f"压缩包含危险路径或链接: {info.filename}")
            if mode and mode not in (stat.S_IFREG, stat.S_IFDIR):
                raise ValueError(f"压缩包含特殊文件: {info.filename}")
            if target in seen:
                raise ValueError(f"压缩包含重复路径: {info.filename}")
            seen.add(target)
            planned.append((info, None if is_dir else target))

        dirs = [relative_target(dest, info.filename, allow_root=True)
                for info, _ in planned if info.is_dir()]
        for directory in dirs:
            if directory is not None:
                directory.mkdir(parents=True, exist_ok=True)

        written = 0
        for info, target in planned:
            if target is None:
                continue
            resolved = relative_target(dest, info.filename)
            if resolved is None:
                raise ValueError(f"目标路径在解压前发生变化: {info.filename}")
            resolved.parent.mkdir(parents=True, exist_ok=True)
            if resolved.exists() and resolved.is_symlink():
                raise ValueError(f"目标包含符号链接: {resolved}")
            with zf.open(info) as src, open(resolved, "wb") as dst:
                while True:
                    chunk = src.read(1 << 20)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > total or written > limits.max_total_size:
                        raise ValueError("压缩包实际解压体积超过限制")
                    dst.write(chunk)


def extract_tar(tar: tarfile.TarFile, dest: Path,
                limits: ArchiveLimits = DEFAULT_LIMITS) -> None:
    members = tar.getmembers()
    total = sum(member.size for member in members)
    try:
        compressed_size = Path(tar.name).stat().st_size
    except (OSError, TypeError):
        compressed_size = 1
    _check_limits(len(members), total, compressed_size, limits)

    planned: list[tuple[tarfile.TarInfo, Path | None]] = []
    seen: set[Path] = set()
    for member in members:
        is_dir = member.isdir()
        _validate_name(member.name, is_dir=is_dir, limits=limits)
        target = relative_target(dest, member.name, allow_root=is_dir)
        if target is None or not (is_dir or member.isfile()):
            raise ValueError(f"压缩包含危险路径、链接或特殊文件: {member.name}")
        if target in seen:
            raise ValueError(f"压缩包含重复路径: {member.name}")
        seen.add(target)
        planned.append((member, None if is_dir else target))

    for member, _ in planned:
        if member.isdir():
            directory = relative_target(dest, member.name, allow_root=True)
            if directory is not None:
                directory.mkdir(parents=True, exist_ok=True)
                try:
                    directory.chmod((member.mode & 0o777) or 0o755)
                except OSError:
                    pass

    written = 0
    for member, target in planned:
        if target is None:
            continue
        resolved = relative_target(dest, member.name)
        src = tar.extractfile(member)
        if resolved is None or src is None:
            raise ValueError(f"无法安全解压成员: {member.name}")
        resolved.parent.mkdir(parents=True, exist_ok=True)
        if resolved.exists() and resolved.is_symlink():
            raise ValueError(f"目标包含符号链接: {resolved}")
        with src, open(resolved, "wb") as dst:
            while True:
                chunk = src.read(1 << 20)
                if not chunk:
                    break
                written += len(chunk)
                if written > total or written > limits.max_total_size:
                    raise ValueError("压缩包实际解压体积超过限制")
                dst.write(chunk)
        try:
            resolved.chmod((member.mode & 0o777) or 0o644)
        except OSError:
            pass


def safe_extract(path: Path, dest: Path,
                 limits: ArchiveLimits = DEFAULT_LIMITS) -> None:
    """Detect and extract a ZIP/TAR archive after validating the whole archive."""
    if zipfile.is_zipfile(path):
        extract_zip(path, dest, limits)
        return
    if tarfile.is_tarfile(path):
        with tarfile.open(path) as tar:
            extract_tar(tar, dest, limits)
        return
    raise ValueError("无法识别的压缩格式（支持 .zip / .tar.gz / .tgz / .tar）")
