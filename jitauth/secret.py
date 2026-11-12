"""密码保护：Windows DPAPI 加密（CryptProtectData / CryptUnprotectData）。

为什么用它
----------
这个工具必须在本地存学号密码（要自动填表）。明文存进 config.json 有两个问题：

1. 一旦把整个目录打包发给别人 / 误传到网盘，密码就泄露了；
2. 仓库里只要有人手滑提交一次，历史里就永远删不掉。

DPAPI 用**当前 Windows 用户**的凭据加密，密文只能在同一台机器、同一个用户下解开。
所以即使 config.json 被拷走，别人也解不出密码。

代价：换电脑/换 Windows 用户后要重新输入一次密码。这是有意的取舍。

格式
----
``password_enc`` 存成 ``dpapi:<base64>``，前缀方便以后换算法（比如 macOS 的钥匙串）。
"""
from __future__ import annotations

import base64
import ctypes
import sys
from ctypes import wintypes

PREFIX = "dpapi:"

# CRYPTPROTECT_UI_FORBIDDEN：不弹任何系统对话框（后台任务里必须）
_CRYPTPROTECT_UI_FORBIDDEN = 0x1


class DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


class SecretError(RuntimeError):
    """加解密失败（不是 Windows、账户不同、密文损坏等）。"""


def _libraries():
    if sys.platform != "win32":
        raise SecretError("DPAPI 只在 Windows 上可用")
    return (ctypes.WinDLL("crypt32", use_last_error=True),
            ctypes.WinDLL("kernel32", use_last_error=True))


def _as_blob(data: bytes) -> tuple[DATA_BLOB, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data, len(data))
    blob = DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    return blob, buffer  # 必须把 buffer 一起返回，否则会被 GC 掉


def _free(blob: DATA_BLOB) -> bytes:
    try:
        return ctypes.string_at(blob.pbData, blob.cbData)
    finally:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.LocalFree(blob.pbData)


def _protect(data: bytes) -> bytes:
    crypt32, _ = _libraries()
    blob_in, _keep = _as_blob(data)
    blob_out = DATA_BLOB()
    ok = crypt32.CryptProtectData(
        ctypes.byref(blob_in), None, None, None, None,
        _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
    if not ok:
        raise SecretError(f"CryptProtectData 失败（错误码 {ctypes.get_last_error()}）")
    return _free(blob_out)


def _unprotect(data: bytes) -> bytes:
    crypt32, _ = _libraries()
    blob_in, _keep = _as_blob(data)
    blob_out = DATA_BLOB()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None,
        _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
    if not ok:
        raise SecretError(
            f"CryptUnprotectData 失败（错误码 {ctypes.get_last_error()}）"
            "：密文不是这台机器/这个用户加密的，或者 config.json 被改坏了")
    return _free(blob_out)


def available() -> bool:
    """当前环境能不能用 DPAPI（真跑一次往返，不只是看平台）。"""
    try:
        return _unprotect(_protect(b"jit-probe")) == b"jit-probe"
    except Exception:  # noqa: BLE001
        return False


def protect(text: str) -> str:
    """明文 → ``dpapi:<base64>``。"""
    if not text:
        return ""
    token = base64.b64encode(_protect(text.encode("utf-8"))).decode("ascii")
    return PREFIX + token


def unprotect(token: str) -> str:
    """``dpapi:<base64>`` → 明文。"""
    token = (token or "").strip()
    if not token:
        return ""
    if token.startswith(PREFIX):
        token = token[len(PREFIX):]
    try:
        raw = base64.b64decode(token.encode("ascii"), validate=True)
    except Exception as exc:  # noqa: BLE001
        raise SecretError(f"密文格式不对：{exc}") from exc
    return _unprotect(raw).decode("utf-8")


def is_encrypted(value: str) -> bool:
    return (value or "").strip().startswith(PREFIX)