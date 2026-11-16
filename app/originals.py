"""CSV 原件存储。

原件按字节保存为文件，文件名即内容的 SHA-256，便于去重与核验：

    data/originals/<sha256>.csv

写盘顺序保证“有库记录必有原件”：

1. 先写同目录下的 ``.tmp/<uuid>.part`` 并 ``fsync``；
2. 再 ``os.replace`` 到内容寻址的最终路径（POSIX 原子重命名）；
3. 最后 ``fsync`` 目录，之后调用方才提交数据库事务。

因此崩溃只可能留下未被数据库引用的孤儿文件，不可能出现数据库已引用、
文件却不存在或内容不完整的状态。启动时会保守清理孤儿/临时文件。
"""
from __future__ import annotations

import hashlib
import os
import re
import time
import uuid
from pathlib import Path

# 500 行 CSV 远小于该上限；设上限只是为了避免异常大文件占用内存
MAX_UPLOAD_BYTES = 2 * 1024 * 1024
_STORED_NAME_RE = re.compile(r"^[0-9a-f]{64}\.csv$")
ORPHAN_MIN_AGE_SECONDS = 3600


class FileIntegrityError(Exception):
    """原件缺失或 SHA-256 不符；映射为 HTTP 500。"""


def originals_dir(db_path: str | Path) -> Path:
    return Path(db_path).resolve().parent / "originals"


def _tmp_dir(base: Path) -> Path:
    return base / ".tmp"


def _fsync_dir(path: Path) -> None:
    """对目录做 fsync，确保 rename 在掉电后仍可见（不支持时静默跳过）。"""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return _sha256(data)


def stored_name_for(sha256: str) -> str:
    return f"{sha256}.csv"


def validate_stored_name(stored_name: str) -> None:
    if not _STORED_NAME_RE.match(stored_name or ""):
        raise FileIntegrityError("原件存储名非法")


def store_original(data: bytes, sha256: str, db_path: str | Path) -> str:
    """把原件原子落盘；返回存储名。已存在同内容文件时安全覆盖。"""
    if _sha256(data) != sha256:
        raise FileIntegrityError("上传内容与 SHA-256 不一致")
    base = originals_dir(db_path)
    tmp_dir = _tmp_dir(base)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    base.mkdir(parents=True, exist_ok=True)

    stored_name = stored_name_for(sha256)
    final = base / stored_name
    tmp = tmp_dir / f"{uuid.uuid4().hex}.part"

    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, final)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise

    _fsync_dir(base)
    return stored_name


def ensure_original(
    data: bytes, sha256: str, stored_name: str, db_path: str | Path
) -> None:
    """确保数据库引用的原件存在且字节一致；缺失/损坏时用本次上传的相同内容修复。"""
    validate_stored_name(stored_name)
    path = originals_dir(db_path) / stored_name
    try:
        existing = path.read_bytes()
    except OSError:
        existing = None
    if existing is not None and _sha256(existing) == sha256:
        return
    store_original(data, sha256, db_path)


def read_verified(stored_name: str, sha256: str, db_path: str | Path) -> bytes:
    """读取原件并做 SHA-256 核验；不通过则拒绝返回字节。"""
    validate_stored_name(stored_name)
    path = originals_dir(db_path) / stored_name
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise FileIntegrityError(f"原件文件缺失：{stored_name}") from exc
    if _sha256(data) != sha256:
        raise FileIntegrityError(
            f"原件 SHA-256 核验失败：期望 {sha256}，实际 {_sha256(data)}"
        )
    return data


def cleanup_orphans(
    db_path: str | Path,
    referenced_names: set[str],
    min_age_seconds: int = ORPHAN_MIN_AGE_SECONDS,
) -> int:
    """清理崩溃遗留的临时文件与未被数据库引用的孤儿原件。

    只清理超过 ``min_age_seconds`` 的文件，避免误删另一个实例
    正在提交（文件已落盘、事务尚未提交）的上传。
    """
    base = originals_dir(db_path)
    if not base.exists():
        return 0
    cutoff = time.time() - min_age_seconds
    removed = 0

    tmp_dir = _tmp_dir(base)
    if tmp_dir.is_dir():
        for p in tmp_dir.iterdir():
            try:
                if p.is_file() and p.stat().st_mtime < cutoff:
                    p.unlink()
                    removed += 1
            except OSError:
                pass

    for p in base.iterdir():
        try:
            if (
                p.is_file()
                and p.name not in referenced_names
                and p.stat().st_mtime < cutoff
            ):
                p.unlink()
                removed += 1
        except OSError:
            pass
    return removed
