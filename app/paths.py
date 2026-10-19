"""Portable relative paths confined to a resolved filesystem root."""

from pathlib import Path, PurePosixPath, PureWindowsPath


def relative_target(root: Path, relative: str, *, allow_root: bool = False) -> Path | None:
    if not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative or "\0" in relative:
        return None
    name = PurePosixPath(relative)
    if name.is_absolute() or PureWindowsPath(relative).drive or ".." in name.parts:
        return None
    try:
        base = root.resolve()
        target = base.joinpath(*name.parts).resolve()
        if not target.is_relative_to(base) or (target == base and not allow_root):
            return None
        return root.joinpath(*name.parts)
    except (OSError, RuntimeError, ValueError):
        return None
