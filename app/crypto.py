"""可选 .aac 包加密：scrypt 派生密钥 + AES-256-GCM。

加密包以固定魔术字节开头，导入时据此自动识别；密钥只在内存中存在，
不落盘、不记录。密码错误或文件损坏时 AEAD 校验失败，绝不返回残缺数据。
"""

from __future__ import annotations

import hashlib
import os

MAGIC = b"AACENC1\n"
_SALT_LEN = 16
_NONCE_LEN = 12
_SCRYPT = {"n": 1 << 14, "r": 8, "p": 1, "dklen": 32}


def available() -> bool:
    try:
        import cryptography  # noqa: F401
    except ImportError:
        return False
    return True


def is_encrypted(data: bytes) -> bool:
    return data.startswith(MAGIC)


def _key(password: str, salt: bytes) -> bytes:
    if not password:
        raise ValueError("密码不能为空")
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, **_SCRYPT)


def encrypt(data: bytes, password: str) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt = os.urandom(_SALT_LEN)
    nonce = os.urandom(_NONCE_LEN)
    ciphertext = AESGCM(_key(password, salt)).encrypt(nonce, data, MAGIC)
    return MAGIC + salt + nonce + ciphertext


def decrypt(data: bytes, password: str) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if not is_encrypted(data):
        raise ValueError("不是加密的配置包")
    head = len(MAGIC)
    salt = data[head:head + _SALT_LEN]
    nonce = data[head + _SALT_LEN:head + _SALT_LEN + _NONCE_LEN]
    ciphertext = data[head + _SALT_LEN + _NONCE_LEN:]
    if len(salt) != _SALT_LEN or len(nonce) != _NONCE_LEN or not ciphertext:
        raise ValueError("加密包结构损坏")
    try:
        return AESGCM(_key(password, salt)).decrypt(nonce, ciphertext, MAGIC)
    except Exception as exc:  # noqa: BLE001
        raise ValueError("密码错误或文件已损坏") from exc
