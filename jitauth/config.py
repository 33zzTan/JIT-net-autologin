"""配置加载模块。

从 config.json 读取配置，带默认值，缺失项不会导致崩溃。

本项目只服务本校：WiFi 名（JIT-Stu）和门户地址（10.50.0.10）是固定默认值，
普通用户唯一需要改的是 `account` 里的学号 / 密码 / 运营商。

关于账号密码
------------
* ``account.password_enc`` 是 DPAPI 密文（``dpapi:...``），只有本机本用户能解；
* ``account.password`` 是明文（兼容旧配置，也允许你手动改）；
* ``load()`` 会把密文解密到内存里的 ``password`` 字段，所以下游代码不用关心存的是哪种。
"""
from __future__ import annotations

import copy
import json
import os

from . import secret

PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))       # .../jitauth
PROJECT_DIR = os.path.dirname(PACKAGE_DIR)                     # 项目根目录
CONFIG_PATH = os.path.join(PROJECT_DIR, "config.json")
EXAMPLE_PATH = os.path.join(PROJECT_DIR, "config.example.json")

DEFAULTS: dict = {
    "wifi": {
        "ssid": "JIT-Stu",
        "connect_timeout": 25,
        "require_visible": True,
        "connect_if_scan_unavailable": False,
        "enable_radio_if_off": True,
        "enable_wait": 3,
        "wait_for_ssid_seconds": 0,
        "wait_poll_seconds": 10,
    },
    "portal": {
        "url": "http://10.50.0.10/",
        "timeout": 8,
        "probe_url": "http://connect.rom.miui.com/generate_204",
        "probe_timeout": 5,
        "check_attempts": 3,
        "check_interval": 3,
    },
    "account": {
        "username": "",
        "password": "",
        "password_enc": "",
        "isp": "",
    },
    "browser": {
        "browser": "auto",
        "mode": "auto",
        "engine": "auto",
        "profile": "dedicated",
        "user_data_dir": "",
        "open_portal_after_login": True,
        "keep_open_seconds": 0,
        "close_after_login": False,
        "launch_timeout": 25,
        "page_load_timeout": 25,
        "script_timeout": 10,
        "find_timeout": 12,
    },
    "behavior": {
        "open_portal": True,
        "auto_login": True,
        "stop_if_wifi_missing": True,
        "skip_if_already_online": True,
        "retry_times": 3,
        "retry_interval": 8,
        "probe_required": False,
        "quiet": False,
    },
    "log": {
        "keep_days": 14,
    },
    # ---- 保活 / 断线自动重连（登录之后一直盯着网络）----
    "keepalive": {
        "enabled": True,
        "check_interval": 60,          # 巡检间隔（秒），本身也是一次心跳流量
        "heartbeat_interval": 240,     # 额外心跳（巡检间隔比它长时才用得上）
        "probe_timeout": 4,
        "fail_threshold": 3,           # 连续几次探针失败才判定断线
        "confirm_portal": True,        # 判定断线前先看门户页
        "on_sleep_wake": True,         # 休眠唤醒后立即复查
        "sleep_jump_threshold": 90,
        "wifi_reconnect": True,        # 断线时先尝试重连 WiFi
        "portal_retry_after": 300,     # 门户打不开/说不清时，隔多久再看
        "notify": True,                # 系统气泡通知
        "log_summary_minutes": 30,
        "probes": [
            {"url": "http://connect.rom.miui.com/generate_204", "expect": "204"},
            {"url": "http://www.msftconnecttest.com/connecttest.txt",
             "expect": "Microsoft Connect Test"},
            {"url": "http://captive.apple.com/hotspot-detect.html", "expect": "Success"},
        ],
        "relogin": {
            "enabled": True,           # false = 只保活不重登（严格纯心跳模式）
            "engine": "auto",          # auto = 先 HTTP，失败立刻弹浏览器窗口；http / browser 可强制
            "max_per_hour": 12,
            "backoff_seconds": [30, 120, 300, 900],
            "verify_timeout": 25,
            "flap_window": 600,
            "flap_threshold": 3,
            "flap_hold": 1800,
            "fatal_hold": 21600,
        },
    },
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if key.startswith("_"):
            continue
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load(path: str | None = None) -> dict:
    """读取配置；文件缺失或损坏时退回默认值。

    如果 ``account.password_enc`` 存在，会把密码解密到 ``account.password``。
    """
    cfg_path = path or CONFIG_PATH
    raw = _read_raw(cfg_path)
    cfg = _merge(DEFAULTS, raw)
    cfg["_path"] = cfg_path
    cfg["_loaded_ok"] = bool(raw)
    _resolve_password(cfg)
    return cfg


def _read_raw(path: str) -> dict:
    """原样读 JSON（不合并默认值），用于回写时保留用户的其它配置和注释。"""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 - 配置坏了也要能启动
        return {}


def _write_raw(path: str, data: dict) -> None:
    """原子写回（先写 .tmp 再替换），避免写一半断电把配置弄坏。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def _resolve_password(cfg: dict) -> None:
    account = cfg.setdefault("account", {})
    enc = str(account.get("password_enc") or "").strip()
    if enc:
        try:
            if not str(account.get("password") or ""):
                account["password"] = secret.unprotect(enc)
            account["_password_source"] = "dpapi"
        except Exception as exc:  # noqa: BLE001 - 换机器/换用户会解不开
            account["_password_error"] = str(exc)
            account.setdefault("password", "")
    else:
        account["_password_source"] = "plain" if account.get("password") else "none"


# --------------------------------------------------------------------------- #
# 首次运行 / 设置向导
# --------------------------------------------------------------------------- #
def setup_reason(cfg: dict) -> str:
    """还缺什么，才需要跑设置向导。空字符串表示已配好。"""
    account = cfg.get("account") or {}
    if not str(account.get("username") or "").strip():
        return "还没有填写学号/账号"
    if not str(account.get("password") or ""):
        error = str(account.get("_password_error") or "")
        return "密码为空" + (f"（解密失败：{error}）" if error else "")
    if not str(account.get("isp") or "").strip():
        return "还没有选择运营商"
    return ""


def needs_setup(cfg: dict) -> bool:
    return bool(setup_reason(cfg))


def save_account(path: str | None = None, *, username: str, password: str, isp: str,
                 store: str = "dpapi") -> str:
    """把账号写回 config.json，保留文件里的其它配置。

    ``store``：``dpapi`` = 加密保存（默认）；``plain`` = 明文保存。
    返回实际使用的存储方式（DPAPI 不可用时自动降级成明文）。
    """
    cfg_path = path or CONFIG_PATH
    raw = _read_raw(cfg_path)
    account = raw.setdefault("account", {})
    account["username"] = str(username or "").strip()
    account["isp"] = str(isp or "").strip()

    if store == "dpapi" and secret.available():
        account["password_enc"] = secret.protect(password)
        account.pop("password", None)
        used = "dpapi"
    else:
        account["password"] = password
        account.pop("password_enc", None)
        used = "plain"

    _write_raw(cfg_path, raw)
    return used


def validate(cfg: dict) -> list[str]:
    """返回配置问题列表（空列表表示没问题）。"""
    problems: list[str] = []
    account = cfg["account"]
    if not str(account.get("username", "")).strip():
        problems.append("account.username 为空（请运行设置向导）")
    if not str(account.get("password", "")):
        error = str(account.get("_password_error") or "")
        problems.append("account.password 为空或无法解密" + (f"：{error}" if error else "（请运行设置向导）"))
    if not str(account.get("isp", "")).strip():
        problems.append("account.isp 为空（请选择运营商）")
    url = str(cfg["portal"].get("url", ""))
    if not url.startswith("http"):
        problems.append("portal.url 必须以 http:// 或 https:// 开头")
    return problems
