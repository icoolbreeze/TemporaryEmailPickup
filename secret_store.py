"""Windows DPAPI helpers for locally persisted mailbox credentials."""

from __future__ import annotations

import base64
import ctypes
import os
from ctypes import wintypes


class SecretStoreError(RuntimeError):
    pass


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _blob_from_bytes(value: bytes) -> tuple[_DataBlob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(value)
    blob = _DataBlob(
        len(value),
        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
    )
    return blob, buffer


def protect_secret(value: str) -> str:
    """Encrypt text for the current Windows user with DPAPI."""
    if os.name != "nt":
        raise SecretStoreError("Outlook 凭据持久化需要 Windows DPAPI")
    source, buffer = _blob_from_bytes(value.encode("utf-8"))
    output = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptProtectData(
        ctypes.byref(source),
        "TemporaryEmailPickup",
        None,
        None,
        None,
        0x01,
        ctypes.byref(output),
    ):
        raise SecretStoreError(f"DPAPI 加密失败：{ctypes.get_last_error()}")
    try:
        encrypted = ctypes.string_at(output.pbData, output.cbData)
        return "dpapi:" + base64.b64encode(encrypted).decode("ascii")
    finally:
        kernel32.LocalFree(output.pbData)
        del buffer


def unprotect_secret(value: str) -> str:
    """Decrypt text previously returned by :func:`protect_secret`."""
    if os.name != "nt" or not value.startswith("dpapi:"):
        raise SecretStoreError("Outlook 凭据不是有效的 DPAPI 数据")
    try:
        encrypted = base64.b64decode(value[6:], validate=True)
    except (ValueError, TypeError) as exc:
        raise SecretStoreError("Outlook 凭据编码无效") from exc
    source, buffer = _blob_from_bytes(encrypted)
    output = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptUnprotectData(
        ctypes.byref(source),
        None,
        None,
        None,
        None,
        0x01,
        ctypes.byref(output),
    ):
        raise SecretStoreError(f"DPAPI 解密失败：{ctypes.get_last_error()}")
    try:
        return ctypes.string_at(output.pbData, output.cbData).decode("utf-8")
    finally:
        kernel32.LocalFree(output.pbData)
        del buffer

