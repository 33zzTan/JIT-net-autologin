"""保活与断线重连引擎。

解决什么问题
------------
原来的程序只在**开机那一刻**登录一次，进程退出后就再没人管网络。
网关的空闲超时、会话到期、DHCP 换 IP、睡眠唤醒、WiFi 抖动……
任何一条都能让你在中途掉线，而且掉线后不会自己回来。

本模块负责"登录之后"的那部分：一直盯着网络，掉了就静默重连。

三层策略
--------
1. **心跳**：每次巡检都会往公网发一个极小的请求（generate_204 之类），
   让网关 / NAT 的会话一直有流量，从源头上减少"空闲超时"被踢的概率。
   巡检间隔本身就是心跳间隔，所以默认 60 秒一次已经足够。
2. **看门狗**：连续几次探针都不通，并且门户确认"确实未登录"，才动手重登。
3. **重登**：默认只走纯 HTTP 静默提交（不开任何窗口），成功后用探针复查。

不折腾的保证
------------
* **三重门**：多探针 + 连续失败阈值 + 门户二次确认，缺一不重登
  （避免一次抖动就把你踢下线重来）。
* **限流**：每小时最多重登 N 次；失败按 30s → 2min → 5min → 15min 退避。
* **账号错误立即停手**：门户说密码错误/欠费，直接暂停，绝不反复试到锁号。
* **防互踢**：连续出现"刚登录就掉"（被手机等设备抢占），退避 30 分钟不硬抢。
* **单实例**：命名互斥体，第二个实例直接退出。

可测试性
--------
探针、门户判定、登录动作、时钟、休眠全部可以注入，
`_probe/test_keepalive.py` 因此能在**不碰真实网络**的前提下跑完整流程。
"""
from __future__ import annotations

import ctypes
import json
import os
import signal
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

from . import portal
from .logging_setup import APP_DIR, LOG_DIR

# 日志里用的名字（与主程序同一个 logger，但落在不同的文件里）
LOG_NAME = "jitauth"

#: 状态文件：给 --status / 托盘读，进程重启后也不会丢历史
STATE_FILE = os.path.join(LOG_DIR, "keepalive.state.json")

#: 默认探针。任何一个通就算在线 —— 单一探针被墙/被拦会误判成断线。
DEFAULT_PROBES: list[dict] = [
    {"url": "http://connect.rom.miui.com/generate_204", "expect": "204"},
    {"url": "http://www.msftconnecttest.com/connecttest.txt",
     "expect": "Microsoft Connect Test"},
    {"url": "http://captive.apple.com/hotspot-detect.html", "expect": "Success"},
]

# 状态
ONLINE = "ONLINE"
SUSPECT = "SUSPECT"
RECOVERING = "RECOVERING"
DEGRADED = "DEGRADED"
PAUSED = "PAUSED"

# 门户判定
P_ONLINE = "online"
P_OFFLINE = "offline"
P_UNREACHABLE = "unreachable"
P_UNKNOWN = "unknown"


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
def keepalive_cfg(cfg: dict) -> dict:
    """取出 keepalive 段（老配置没有这一段时给一套合理默认）。"""
    return cfg.get("keepalive") or {}


@dataclass
class Settings:
    """把配置规整成好用的类型，避免每处都要 get()+类型转换。"""

    enabled: bool = True
    check_interval: float = 60.0
    heartbeat_interval: float = 240.0
    probe_timeout: float = 4.0
    fail_threshold: int = 3
    confirm_portal: bool = True
    on_sleep_wake: bool = True
    sleep_jump_threshold: float = 90.0
    wifi_reconnect: bool = True
    relogin_enabled: bool = True
    relogin_engine: str = "auto"          # auto = 先 HTTP，失败立刻用浏览器窗口
    max_per_hour: int = 12
    backoff_seconds: list[float] = field(default_factory=lambda: [30, 120, 300, 900])
    verify_timeout: float = 25.0
    flap_window: float = 600.0
    flap_threshold: int = 3
    flap_hold: float = 1800.0
    fatal_hold: float = 21600.0
    portal_retry_after: float = 300.0
    notify: bool = True
    log_summary_minutes: float = 30.0

    @classmethod
    def from_cfg(cls, cfg: dict) -> "Settings":
        raw = keepalive_cfg(cfg)
        rel = raw.get("relogin") or {}

        def num(src: dict, key: str, default: float) -> float:
            try:
                return float(src.get(key, default))
            except (TypeError, ValueError):
                return float(default)

        def boolean(src: dict, key: str, default: bool) -> bool:
            value = src.get(key, default)
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)

        backoff = rel.get("backoff_seconds") or [30, 120, 300, 900]
        try:
            backoff = sorted({max(5.0, float(x)) for x in backoff})
        except (TypeError, ValueError):
            backoff = [30.0, 120.0, 300.0, 900.0]
        if not backoff:
            backoff = [30.0, 120.0, 300.0, 900.0]

        return cls(
            enabled=boolean(raw, "enabled", True),
            check_interval=max(10.0, num(raw, "check_interval", 60)),
            heartbeat_interval=max(0.0, num(raw, "heartbeat_interval", 240)),
            probe_timeout=max(1.0, num(raw, "probe_timeout", 4)),
            fail_threshold=max(1, int(num(raw, "fail_threshold", 3))),
            confirm_portal=boolean(raw, "confirm_portal", True),
            on_sleep_wake=boolean(raw, "on_sleep_wake", True),
            sleep_jump_threshold=max(10.0, num(raw, "sleep_jump_threshold", 90)),
            wifi_reconnect=boolean(raw, "wifi_reconnect", True),
            relogin_enabled=boolean(rel, "enabled", True),
            relogin_engine=_engine_name(rel.get("engine")),
            max_per_hour=max(1, int(num(rel, "max_per_hour", 12))),
            backoff_seconds=backoff,
            verify_timeout=max(5.0, num(rel, "verify_timeout", 25)),
            flap_window=max(60.0, num(rel, "flap_window", 600)),
            flap_threshold=max(2, int(num(rel, "flap_threshold", 3))),
            flap_hold=max(60.0, num(rel, "flap_hold", 1800)),
            fatal_hold=max(60.0, num(rel, "fatal_hold", 21600)),
            portal_retry_after=max(30.0, num(raw, "portal_retry_after", 300)),
            notify=boolean(raw, "notify", True),
            log_summary_minutes=max(5.0, num(raw, "log_summary_minutes", 30)),
        )


# --------------------------------------------------------------------------- #
# 探针
# --------------------------------------------------------------------------- #
@dataclass
class ProbeEndpoint:
    url: str
    expect: str = "204"

    @property
    def name(self) -> str:
        return self.url.split("//", 1)[-1].split("/", 1)[0]

    @property
    def marker_mode(self) -> bool:
        return self.expect.strip().lower() != "204"


def build_endpoints(cfg: dict) -> list[ProbeEndpoint]:
    raw = (keepalive_cfg(cfg) or {}).get("probes")
    if not raw:
        raw = DEFAULT_PROBES
    out: list[ProbeEndpoint] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            out.append(ProbeEndpoint(url=item.strip(), expect="204"))
        elif isinstance(item, dict) and str(item.get("url") or "").strip():
            out.append(ProbeEndpoint(url=str(item["url"]).strip(),
                                     expect=str(item.get("expect") or "204")))
    # 保底：一条都没配时用默认，别让看门狗变成只会睡觉
    return out or [ProbeEndpoint(url=e["url"], expect=e["expect"]) for e in DEFAULT_PROBES]


@dataclass
class ProbeResult:
    online: bool = False
    hits: list[str] = field(default_factory=list)
    fails: dict[str, str] = field(default_factory=dict)
    elapsed: float = 0.0

    def summary(self) -> str:
        if self.online:
            return "在线（" + "、".join(self.hits) + "）"
        detail = "；".join(f"{k}: {v}" for k, v in list(self.fails.items())[:3])
        return "不通" + (f"（{detail}）" if detail else "")


def check_endpoint(ep: ProbeEndpoint, timeout: float) -> tuple[bool, str]:
    """探测单个端点。返回 (是否在线, 说明)。

    * ``expect == "204"``：只认 HTTP 204（被门户劫持时会是 200/302）
    * 其它：认状态 200 + 内容里出现指定文字（MS / Apple 的联网检测页）
    """
    req = urllib.request.Request(
        ep.url,
        headers={
            "User-Agent": portal.USER_AGENT,
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Connection": "close",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = int(resp.status)
            final = resp.geturl() or ep.url
            # 跳到了别的域名 = 被门户劫持了
            if _host(final) and _host(final) != _host(ep.url):
                return False, f"被劫持到 {_host(final)}"
            if not ep.marker_mode:
                return (True, "204") if status == 204 else (False, f"HTTP {status}（可能是门户劫持）")
            if status != 200:
                return False, f"HTTP {status}"
            body = resp.read(4096).decode("utf-8", errors="replace")
            if ep.expect.lower() in body.lower():
                return True, "内容匹配"
            return False, "内容不匹配（可能是门户劫持）"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001 - 超时/DNS/连接错误都算不通
        return False, f"{type(exc).__name__}: {exc}"[:120]


def _host(url: str) -> str:
    try:
        return urllib.parse.urlsplit(url).netloc.lower()
    except Exception:  # noqa: BLE001
        return ""


def probe(endpoints: list[ProbeEndpoint], timeout: float) -> ProbeResult:
    """并发探所有端点，**任一通即在线**。

    用守护线程而不是线程池：DNS 卡住时不会拖住进程退出。
    """
    started = time.time()
    result = ProbeResult()
    if not endpoints:
        return result
    lock = threading.Lock()
    done = threading.Event()
    total = len(endpoints)
    finished = [0]

    def worker(ep: ProbeEndpoint) -> None:
        ok, why = check_endpoint(ep, timeout)
        with lock:
            if ok:
                result.hits.append(ep.name)
            else:
                result.fails[ep.name] = why
            finished[0] += 1
            if result.hits or finished[0] >= total:
                done.set()

    threads = [threading.Thread(target=worker, args=(ep,), daemon=True) for ep in endpoints]
    for t in threads:
        t.start()
    done.wait(timeout + 2.0)
    result.online = bool(result.hits)
    result.elapsed = time.time() - started
    return result


# --------------------------------------------------------------------------- #
# 门户判定
# --------------------------------------------------------------------------- #
def classify_portal(cfg: dict, logger=None) -> tuple[str, str]:
    """访问门户首页，判断当前到底是"在线 / 未登录 / 打不开 / 说不清"。

    这是重登前的**第二道确认**：探针不通不一定是掉线（也可能只是探针被拦），
    必须门户自己说"你是登录页"，才认为真的掉线。
    """
    url = cfg["portal"]["url"]
    timeout = float(cfg["portal"].get("timeout", 8))
    try:
        sess = portal.Session(timeout=timeout)
        status, html, _final = sess.request(url, timeout=timeout)
    except portal.PortalError as exc:
        return P_UNREACHABLE, str(exc)[:200]
    except Exception as exc:  # noqa: BLE001
        return P_UNREACHABLE, f"{type(exc).__name__}: {exc}"[:200]

    try:
        drcom = portal.drcom_online_state(html)
        if drcom is not None:
            return (P_ONLINE if drcom[0] else P_OFFLINE), drcom[1]
        if portal.pick_form(html) is not None:
            return P_OFFLINE, "门户页上有登录表单（未登录）"
        ok, why = portal.judge(html, status)
        if ok:
            return P_ONLINE, why
        if status >= 400:
            return P_UNKNOWN, why
        return P_UNKNOWN, why
    except Exception as exc:  # noqa: BLE001
        return P_UNKNOWN, f"{type(exc).__name__}: {exc}"[:200]


# --------------------------------------------------------------------------- #
# 单实例
# --------------------------------------------------------------------------- #
ERROR_ALREADY_EXISTS = 183


class SingleInstance:
    """Windows 命名互斥体实现的单实例锁（进程退出自动释放）。"""

    def __init__(self, name: str = "JITAUTH-Campus-KeepAlive") -> None:
        self.name = name
        self._handle = None

    def acquire(self) -> bool:
        try:
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.CreateMutexW(None, False, self.name)
            if not handle:
                return True  # 拿不到互斥体就别拦着自己跑
            last = kernel32.GetLastError()
            self._handle = handle
            return last != ERROR_ALREADY_EXISTS
        except Exception:  # noqa: BLE001 - 非 Windows 或受限环境
            return True

    def release(self) -> None:
        if not self._handle:
            return
        try:
            ctypes.windll.kernel32.CloseHandle(self._handle)
        except Exception:  # noqa: BLE001
            pass
        self._handle = None

    @classmethod
    def is_running(cls, name: str = "JITAUTH-Campus-KeepAlive") -> bool | None:
        """检查是否已有实例在跑。None = 判断不了。"""
        try:
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenMutexW(0x00100000, False, name)  # SYNCHRONIZE
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:  # noqa: BLE001
            return None


# --------------------------------------------------------------------------- #
# 引擎
# --------------------------------------------------------------------------- #
class KeepAliveEngine:
    """保活状态机。所有副作用都可以注入，方便离线测试。"""

    def __init__(
        self,
        cfg: dict,
        logger,
        *,
        probe_fn: Callable[[], ProbeResult] | None = None,
        portal_fn: Callable[[], tuple[str, str]] | None = None,
        login_fn: Callable[[], tuple[bool, str]] | None = None,
        browser_fn: Callable[[], tuple[bool, str]] | None = None,
        wifi_fn: Callable[[], tuple[bool, str]] | None = None,
        notify_fn: Callable[[str, str, str], None] | None = None,
        now_fn: Callable[[], float] = time.time,
        sleep_fn: Callable[[float], None] = time.sleep,
        state_path: str | None = None,
        config_path: str | None = None,
        on_state: Callable[[dict], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.logger = logger
        self.settings = Settings.from_cfg(cfg)
        self.endpoints = build_endpoints(cfg)

        self._probe_fn = probe_fn
        self._portal_fn = portal_fn
        self._login_fn = login_fn
        self._browser_fn = browser_fn
        self._wifi_fn = wifi_fn
        self._notify_fn = notify_fn
        self.now = now_fn
        self._sleep = sleep_fn
        self.state_path = state_path or STATE_FILE
        self.config_path = config_path
        self.on_state = on_state

        self._cfg_mtime = _mtime(config_path) if config_path else 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._commands: list[tuple[str, dict]] = []
        self._tray_notify: Callable[[str, str, str], None] | None = None

        # 状态
        self.state = ONLINE
        self.started_at = self.now()
        self.failures = 0
        self.backoff_index = 0
        self.next_attempt_at = 0.0
        self.paused_until = 0.0
        self.paused_reason = ""
        self.flap_count = 0
        self.flap_hold_until = 0.0
        self.relogin_times: list[float] = []
        self.last_relogin_at = 0.0
        self.last_online_at = 0.0
        self.offline_since = 0.0
        self.last_probe: ProbeResult | None = None
        self.last_portal: tuple[str, str] = ("", "")
        self.last_error = ""
        self.last_check_at = 0.0
        self.last_heartbeat_at = 0.0
        self.last_traffic_at = 0.0
        self.last_summary_at = self.now()
        self.relogins_total = 0
        self.relogin_success_total = 0
        self.checks_total = 0
        self.online_seconds = 0.0
        self._last_tick_at = self.now()
        self._notified: dict[str, float] = {}
        self._throttle: dict[str, float] = {}
        self._sleep_jump = 0.0

    # ---------------- 生命周期 ---------------- #
    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def request(self, command: str, **kwargs: Any) -> None:
        """从别的线程发命令：check / pause / resume / stop。"""
        with self._lock:
            self._commands.append((command, kwargs))
        self._wake.set()

    def run(self) -> None:
        self.logger.info("保活看门狗启动：巡检 %g 秒 / 心跳 %g 秒 / 连续 %d 次失败判定断线",
                         self.settings.check_interval, self.settings.heartbeat_interval,
                         self.settings.fail_threshold)
        self._write_state()
        while not self._stop.is_set():
            try:
                self._drain_commands()
                if self._stop.is_set():
                    break
                self.tick()
            except Exception as exc:  # noqa: BLE001 - 看门狗绝不能因为一次异常就死掉
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.logger.exception("巡检出错（已忽略，继续运行）：%s", exc)
            self._write_state()
            self._idle(self._next_wait())
        self.logger.info("保活看门狗已退出（运行 %.1f 分钟，累计 %d 次巡检 / %d 次重登）",
                         (self.now() - self.started_at) / 60.0,
                         self.checks_total, self.relogins_total)
        self._write_state()

    def _next_wait(self) -> float:
        """下一次巡检前睡多久。

        常规是 check_interval；但如果刚失败过、退避时间还没到，
        就只睡到退避结束（否则 30 秒的退避会被 60 秒的巡检间隔吃掉）。
        """
        wait = self.settings.check_interval
        now = self.now()
        if self.next_attempt_at and self.next_attempt_at > now:
            wait = min(wait, self.next_attempt_at - now)
        return max(3.0, wait)

    def _drain_commands(self) -> None:
        with self._lock:
            commands, self._commands = self._commands, []
        for command, kwargs in commands:
            if command == "check":
                self.logger.info("收到「立即检测」请求")
                self.next_attempt_at = 0.0
                self.paused_until = 0.0
            elif command == "pause":
                minutes = float(kwargs.get("minutes", 30))
                self.paused_until = self.now() + minutes * 60.0
                self.paused_reason = f"手动暂停 {minutes:g} 分钟"
                self.logger.info("保活已暂停 %g 分钟", minutes)
            elif command == "resume":
                self.paused_until = 0.0
                self.paused_reason = ""
                self.next_attempt_at = 0.0
                self.logger.info("保活已恢复")
            elif command == "stop":
                self.stop()
                return

    def _idle(self, seconds: float) -> None:
        """可被打断的等待，同时识别"刚从睡眠里醒来"。"""
        self._wake.clear()
        wall0 = time.time()
        deadline = wall0 + max(0.5, seconds)
        while not self._stop.is_set():
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            if remaining > seconds * 3:
                # 系统时钟被往回拨了，重设一次，别睡过头
                deadline = time.time() + seconds
                remaining = seconds
            if self._wake.wait(min(1.0, remaining)):
                self._wake.clear()
                break
        drift = (time.time() - wall0) - max(0.5, seconds)
        if drift > self.settings.sleep_jump_threshold:
            self._sleep_jump = drift
            self.logger.info("检测到系统休眠/唤醒（时钟跳过 %.0f 秒），立即复查网络", drift)

    # ---------------- 单次巡检 ---------------- #
    def tick(self) -> str:
        now = self.now()
        self.checks_total += 1
        self.last_check_at = now
        self._reload_config_if_changed()

        # 累计在线时长（用两次 tick 的间隔近似）
        if self.state == ONLINE:
            self.online_seconds += max(0.0, now - self._last_tick_at)
        self._last_tick_at = now

        if not self.settings.enabled:
            self.state = PAUSED
            self.paused_reason = "配置里 keepalive.enabled = false"
            return "disabled"

        if self.paused_until and now < self.paused_until:
            self.state = PAUSED
            return "paused"

        result = self._probe()
        self.last_probe = result
        self.last_traffic_at = now
        if result.online:
            self._on_online(now, result)
            self._maybe_heartbeat(now)
            self._maybe_summary(now)
            return "online"

        return self._on_offline(now, result)

    def _probe(self) -> ProbeResult:
        if self._probe_fn is not None:
            return self._probe_fn()
        return probe(self.endpoints, self.settings.probe_timeout)

    def _portal_state(self) -> tuple[str, str]:
        if self._portal_fn is not None:
            return self._portal_fn()
        return classify_portal(self.cfg, self.logger)

    def _on_online(self, now: float, result: ProbeResult) -> None:
        self.last_online_at = now
        if self.state in (SUSPECT, RECOVERING, DEGRADED, PAUSED):
            outage = (now - self.offline_since) if self.offline_since else 0.0
            if self.state != PAUSED:
                self.logger.info("网络已恢复在线%s", f"（断开 {outage:.0f} 秒）" if outage > 5 else "")
            self._notify("recovered", "info", "校园网已恢复",
                         f"网络已恢复在线{f'（断开 {outage:.0f} 秒）' if outage > 5 else ''}")
            self.offline_since = 0.0
        self.state = ONLINE
        self.failures = 0
        self.backoff_index = 0
        self.next_attempt_at = 0.0
        self.last_error = ""

    def _on_offline(self, now: float, result: ProbeResult) -> str:
        self.failures += 1
        self.state = SUSPECT
        if self.offline_since <= 0:
            self.offline_since = now
        self.last_error = result.summary()
        self.logger.debug("探针不通（连续第 %d/%d 次）：%s", self.failures,
                          self.settings.fail_threshold, self.last_error)
        if self.failures < self.settings.fail_threshold:
            return "suspect"

        # ---- 第二道确认：看门户到底怎么说 ----
        pstate, why = self._portal_state()
        self.last_portal = (pstate, why)
        if pstate == P_ONLINE:
            # 门户说还在线，只是外网探针不通：可能探针被拦 / 出口路由问题，别乱重登
            self.state = DEGRADED
            self.next_attempt_at = now + self.settings.portal_retry_after
            self._log_throttled("probe_blocked", "warning",
                                "探针连续 %d 次不通，但门户显示仍在登录状态（%s）；"
                                "可能是探针被拦截，暂不重登", self.failures, why)
            self._notify("probe_blocked", "warning", "网络异常（门户仍在线）",
                         "外网探针不通但门户显示已登录，已跳过重登，稍后再试", 900)
            return "portal_online"

        if pstate == P_UNREACHABLE:
            self.state = DEGRADED
            self.next_attempt_at = now + self.settings.portal_retry_after
            self._log_throttled("portal_unreachable", "warning",
                                "门户本身打不开（%s）；可能是网关维护或 WiFi 掉线", why)
            self._try_wifi_fix()
            return "portal_unreachable"

        if pstate == P_UNKNOWN:
            self.state = DEGRADED
            self.next_attempt_at = now + self.settings.portal_retry_after
            self._log_throttled("portal_unknown", "warning",
                                "门户状态说不清（%s），本轮不重登", why)
            return "portal_unknown"

        # ---- 确认未登录 → 重登 ----
        return self._recover(now, why)

    def _recover(self, now: float, portal_why: str) -> str:
        if not self.settings.relogin_enabled:
            self.state = DEGRADED
            self.next_attempt_at = now + self.settings.check_interval
            self.logger.warning("确认已断线（%s），但配置关闭了自动重登"
                                "（keepalive.relogin.enabled = false）", portal_why)
            return "relogin_disabled"

        # 被其他设备抢占 → 别硬抢，退避
        if self.flap_hold_until and now < self.flap_hold_until:
            self.state = DEGRADED
            self.next_attempt_at = self.flap_hold_until
            self.logger.info("上轮判定为被其他设备抢占，%.0f 分钟后再试",
                             (self.flap_hold_until - now) / 60)
            return "flap_hold"

        # 限流
        self.relogin_times = [t for t in self.relogin_times if now - t < 3600]
        if len(self.relogin_times) >= self.settings.max_per_hour:
            self.state = DEGRADED
            self.next_attempt_at = now + 900
            self.logger.warning("最近一小时已重登 %d 次，达到上限（%d），暂停 15 分钟",
                                len(self.relogin_times), self.settings.max_per_hour)
            self._notify("rate_limited", "warning", "重连过于频繁",
                         "一小时内重登次数已达上限，暂停 15 分钟", 1800)
            return "rate_limited"

        if self.next_attempt_at and now < self.next_attempt_at:
            self.state = DEGRADED
            return "backoff"

        # WiFi 先修一下（掉线也可能是 WiFi 断了）
        self._try_wifi_fix()

        self.state = RECOVERING
        engine = self.settings.relogin_engine
        self.logger.info("确认已断线（%s），开始自动重登（%s）", portal_why,
                         {"http": "纯 HTTP", "browser": "浏览器窗口",
                          "auto": "先 HTTP，失败立刻用浏览器窗口"}.get(engine, "auto"))
        self._notify("recovering", "warning", "检测到断线", "正在自动重连…", 300)

        # 尝试次数一起计入限流，避免"失败不计数"导致把门户刷爆
        self.relogin_times.append(now)
        self.relogins_total += 1

        started = self.now()
        ok, message = False, "未尝试任何登录方式"

        # 第一级：纯 HTTP（安静、快；门户是传统表单时才有效）
        if engine in ("http", "auto"):
            ok, message = self._http_login()
            self.logger.info("HTTP 重登%s：%s", "成功" if ok else "未成功", message)
            if not ok and _is_fatal(message):
                return self._pause_for_fatal(message)

        # 第二级：弹出登录窗口，自动填表 + 点登录（和开机那次同一条路径）
        if not ok and engine in ("browser", "auto"):
            self.logger.info("改用浏览器窗口重登（门户登录页是 JS 渲染时，HTTP 提交不了）")
            ok, message = self._browser_login()
            self.logger.info("浏览器重登%s：%s", "成功" if ok else "未成功", message)
            if not ok and _is_fatal(message):
                return self._pause_for_fatal(message)

        if not ok:
            self._schedule_backoff()
            self.state = DEGRADED
            self.last_error = message
            self._notify("relogin_failed", "error", "自动重连失败",
                         f"{message}；约 {self._current_backoff():.0f} 秒后重试", 120)
            return "relogin_failed"

        # 重登接口说成功，再用探针复查（门户经常"报喜不报忧"）
        verified = self._verify(self.settings.verify_timeout)
        elapsed = self.now() - started

        if verified:
            self.relogin_success_total += 1
            self._note_relogin_ok()
            self.state = ONLINE
            self.failures = 0
            self.backoff_index = 0
            self.next_attempt_at = 0.0
            self.offline_since = 0.0
            self.last_online_at = self.now()
            self.logger.info("重登成功并已联网验证通过（耗时 %.1f 秒）：%s", elapsed, message)
            self._notify("recovered", "info", "校园网已自动重连",
                         f"静默重登成功（耗时 {elapsed:.1f} 秒）")
            return "recovered"

        self._schedule_backoff()
        self._note_relogin_ok()
        self.state = DEGRADED
        self.last_error = "重登接口返回成功，但外网探针仍不通"
        self.logger.warning("重登接口返回成功（%s），但 %.0f 秒内外网探针仍不通",
                            message, self.settings.verify_timeout)
        self._notify("verify_failed", "warning", "重登后仍未联网",
                     "门户返回成功，但外网仍不通；可能是出口问题，稍后重试", 600)
        return "verify_failed"

    def _http_login(self) -> tuple[bool, str]:
        """纯 HTTP 表单提交（安静，但只对传统表单页面有效）。"""
        try:
            if self._login_fn is not None:
                return self._login_fn()
            return portal.login_via_http(self.cfg, self.logger)
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"

    def _browser_login(self) -> tuple[bool, str]:
        """弹出程序自己的浏览器窗口，自动填表并点登录（同开机那条路径）。

        这就是为什么重登要留第二级：本校门户的登录页是 JS 渲染的，
        原始 HTML 里没有 ``<form>``，纯 HTTP 提交必然失败。
        """
        if self._browser_fn is not None:
            try:
                return self._browser_fn()
            except Exception as exc:  # noqa: BLE001
                return False, f"浏览器重登出错：{type(exc).__name__}: {exc}"

        from . import browser as browser_mod

        try:
            ok, message, session = browser_mod.login_via_browser(
                self.cfg, self.logger, visible=True)
        except Exception as exc:  # noqa: BLE001
            return False, f"浏览器重登出错：{type(exc).__name__}: {exc}"

        if session is not None:
            try:
                if bool(self.cfg["browser"].get("close_after_login", False)):
                    session.quit(force=True)
                else:
                    # 只断开调试连接，窗口留着让你能看见结果
                    session.close_connection()
            except Exception as exc:  # noqa: BLE001
                self.logger.debug("浏览器会话收尾失败：%s", exc)
        return ok, message

    def _pause_for_fatal(self, message: str) -> str:
        self.paused_until = self.now() + self.settings.fatal_hold
        self.paused_reason = f"账号问题：{message}"
        self.state = PAUSED
        self.last_error = message
        self.logger.error("门户提示账号/密码问题（%s），停止自动重登 %.0f 小时，请检查配置",
                          message, self.settings.fatal_hold / 3600)
        self._notify("fatal", "error", "自动登录已暂停",
                     f"门户提示账号问题：{message}；请检查 config.json", 0)
        return "fatal"

    def _verify(self, budget: float) -> bool:
        deadline = self.now() + budget
        while True:
            if self._probe().online:
                return True
            if self.now() >= deadline:
                return False
            self._sleep(min(3.0, max(0.5, deadline - self.now())))

    def _note_relogin_ok(self) -> None:
        now = self.now()
        if self.last_relogin_at and (now - self.last_relogin_at) < self.settings.flap_window:
            self.flap_count += 1
        else:
            self.flap_count = 0
        self.last_relogin_at = now
        if self.flap_count >= self.settings.flap_threshold:
            self.flap_hold_until = now + self.settings.flap_hold
            self.flap_count = 0
            self.logger.warning("连续 %d 次刚登录就被踢下线，疑似被其他设备抢占同一账号；"
                                "退避 %.0f 分钟，不再硬抢", self.settings.flap_threshold,
                                self.settings.flap_hold / 60)
            self._notify("flap", "warning", "账号可能在别处登录",
                         "刚连上就掉线，已暂停重连 30 分钟，避免两端互相踢", 0)

    def _schedule_backoff(self) -> None:
        index = min(self.backoff_index, len(self.settings.backoff_seconds) - 1)
        wait = self.settings.backoff_seconds[index]
        self.backoff_index = min(self.backoff_index + 1, len(self.settings.backoff_seconds) - 1)
        self.next_attempt_at = self.now() + wait
        self.logger.info("%.0f 秒后重试（第 %d 次退避）", wait, index + 1)

    def _current_backoff(self) -> float:
        index = min(self.backoff_index, len(self.settings.backoff_seconds) - 1)
        return self.settings.backoff_seconds[index]

    def _try_wifi_fix(self) -> None:
        if not self.settings.wifi_reconnect:
            return
        ssid = str(self.cfg["wifi"].get("ssid", "")).strip()
        if not ssid:
            return
        try:
            if self._wifi_fn is not None:
                ok, message = self._wifi_fn()
            else:
                from . import wifi as wifi_mod
                current, _diag = wifi_mod.get_current_ssid()
                if current and current.strip().lower() == ssid.lower():
                    return
                self.logger.info("当前 WiFi 不是 %s（%s），尝试重连", ssid, current or "未连接")
                ok, message = wifi_mod.ensure_connected(
                    ssid,
                    timeout=int(self.cfg["wifi"].get("connect_timeout", 25)),
                    require_visible=bool(self.cfg["wifi"].get("require_visible", True)),
                    connect_if_scan_unavailable=bool(
                        self.cfg["wifi"].get("connect_if_scan_unavailable", False)),
                    enable_radio_if_off=bool(self.cfg["wifi"].get("enable_radio_if_off", True)),
                    enable_wait=float(self.cfg["wifi"].get("enable_wait", 3)),
                    logger=self.logger,
                )
            self.logger.info("WiFi 重连结果：%s（%s）", "成功" if ok else "失败", message)
        except Exception as exc:  # noqa: BLE001
            self.logger.debug("WiFi 重连出错：%s", exc)

    def _maybe_heartbeat(self, now: float) -> None:
        """巡检本身就是心跳；这里只处理"巡检间隔比心跳间隔还长"的情况。"""
        interval = self.settings.heartbeat_interval
        if interval <= 0:
            return
        if now - self.last_traffic_at < interval:
            return
        self.last_heartbeat_at = now
        self.last_traffic_at = now
        try:
            check_endpoint(self.endpoints[0], min(3.0, self.settings.probe_timeout))
        except Exception:  # noqa: BLE001
            pass
        self.logger.debug("已发送保活心跳（%s）", self.endpoints[0].name)

    def _maybe_summary(self, now: float) -> None:
        if now - self.last_summary_at < self.settings.log_summary_minutes * 60:
            return
        self.last_summary_at = now
        self.logger.info("[摘要] 已在线 %.1f 小时｜本次运行 %d 次巡检 / %d 次重登｜状态 %s",
                         self.online_seconds / 3600.0, self.checks_total,
                         self.relogins_total, self.state)

    # ---------------- 通知 ---------------- #
    def _log_throttled(self, key: str, level: str, message: str, *args,
                       interval: float = 600.0) -> None:
        """同一个告警 10 分钟内只写一次 INFO/WARNING，避免整夜刷日志。"""
        now = self.now()
        last = self._throttle.get(key)
        if last is not None and now - last < interval:
            self.logger.debug(message, *args)
            return
        self._throttle[key] = now
        getattr(self.logger, level)(message, *args)

    def _notify(self, key: str, level: str, title: str, text: str,
                min_interval: float = 300.0) -> None:
        if not self.settings.notify or self._notify_fn is None:
            return
        now = self.now()
        last = self._notified.get(key)
        if min_interval > 0 and last is not None and now - last < min_interval:
            return
        self._notified[key] = now
        try:
            self._notify_fn(level, title, text)
        except Exception as exc:  # noqa: BLE001
            self.logger.debug("发送通知失败：%s", exc)

    def set_notifier(self, fn: Callable[[str, str, str], None] | None) -> None:
        self._notify_fn = fn

    # ---------------- 配置热更新 ---------------- #
    def _reload_config_if_changed(self) -> None:
        if not self.config_path:
            return
        mtime = _mtime(self.config_path)
        if not mtime or mtime == self._cfg_mtime:
            return
        self._cfg_mtime = mtime
        try:
            from . import config as config_mod
            fresh = config_mod.load(self.config_path)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("重新加载配置失败（继续用旧配置）：%s", exc)
            return
        account_changed = (fresh.get("account") or {}) != (self.cfg.get("account") or {})
        self.cfg = fresh
        self.settings = Settings.from_cfg(fresh)
        self.endpoints = build_endpoints(fresh)
        if account_changed and self.paused_until:
            # 账号改了，说明你修了密码：立刻解除暂停，重新开始
            self.paused_until = 0.0
            self.paused_reason = ""
            self.next_attempt_at = 0.0
            self.logger.info("检测到账号配置变更，已解除暂停并重新开始尝试")
        self.logger.info("检测到 config.json 变更，已重新加载（巡检 %g 秒 / 重登 %s）",
                         self.settings.check_interval,
                         "开" if self.settings.relogin_enabled else "关")

    # ---------------- 状态快照 ---------------- #
    def snapshot(self) -> dict:
        now = self.now()
        paused = bool(self.paused_until and now < self.paused_until)
        return {
            "pid": os.getpid(),
            "state": self.state,
            "status_text": self.status_text(),
            "started_at": _iso(self.started_at),
            "uptime_seconds": round(now - self.started_at, 1),
            "last_check": _iso(self.last_check_at) if self.last_check_at else None,
            "last_online": _iso(self.last_online_at) if self.last_online_at else None,
            "last_relogin": _iso(self.last_relogin_at) if self.last_relogin_at else None,
            "offline_since": _iso(self.offline_since) if self.offline_since and self.state != ONLINE else None,
            "relogins_total": self.relogins_total,
            "relogin_success_total": self.relogin_success_total,
            "relogins_last_hour": sum(1 for t in self.relogin_times if now - t < 3600),
            "checks_total": self.checks_total,
            "online_seconds": round(self.online_seconds, 1),
            "failures": self.failures,
            "paused": paused,
            "paused_reason": self.paused_reason if paused else "",
            "paused_until": _iso(self.paused_until) if paused else None,
            "flap_hold_until": _iso(self.flap_hold_until) if self.flap_hold_until > now else None,
            "probe": self.last_probe.summary() if self.last_probe else "",
            "portal": f"{self.last_portal[0]}: {self.last_portal[1]}" if self.last_portal[0] else "",
            "last_error": self.last_error,
            "next_attempt": _iso(self.next_attempt_at) if self.next_attempt_at > now else None,
            "updated_at": _iso(now),
        }

    def status_text(self) -> str:
        now = self.now()
        if self.paused_until and now < self.paused_until:
            left = (self.paused_until - now) / 60
            return f"已暂停（剩 {left:.0f} 分钟）"
        if self.flap_hold_until and now < self.flap_hold_until:
            left = (self.flap_hold_until - now) / 60
            return f"退避中（剩 {left:.0f} 分钟）"
        if self.state == RECOVERING:
            return "正在重连…"
        if self.state == DEGRADED:
            return "异常（等待重试）"
        if self.state == SUSPECT:
            return "疑似断线（确认中）"
        if self.state == ONLINE:
            return "在线"
        return self.state

    def _write_state(self) -> None:
        snapshot = self.snapshot()
        try:
            os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(snapshot, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.state_path)
        except OSError as exc:
            self.logger.debug("写入状态文件失败：%s", exc)
        if self.on_state is not None:
            try:
                self.on_state(snapshot)
            except Exception:  # noqa: BLE001
                pass


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def _mtime(path: str | None) -> float:
    try:
        return os.path.getmtime(path) if path else 0.0
    except OSError:
        return 0.0


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _is_fatal(message: str) -> bool:
    text = (message or "").lower()
    return any(word.lower() in text for word in portal.FATAL_WORDS) or "账号/密码问题" in message


def _engine_name(value) -> str:
    """重登方式：auto（先 HTTP 再用浏览器）/ http / browser。"""
    name = str(value or "auto").strip().lower()
    return name if name in ("auto", "http", "browser") else "auto"


def read_state(path: str | None = None) -> dict:
    """读状态文件（--status 用）。没有就返回空字典。"""
    try:
        with open(path or STATE_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def format_status(state: dict) -> str:
    """把状态字典排成人能看的一屏。"""
    if not state:
        return ("还没有保活记录（看门狗可能从未启动过）。\n"
                "启动方式：powershell -ExecutionPolicy Bypass -File install_task.ps1")
    running = SingleInstance.is_running()
    pid = state.get("pid")
    alive = _pid_alive(pid) if pid else False
    if running is False and not alive:
        head = "看门狗：未运行（状态文件是上次退出时留下的）"
    else:
        head = f"看门狗：运行中（PID {pid}）"
    lines = [
        head,
        f"当前状态：{state.get('status_text') or state.get('state') or '未知'}",
        f"启动时间：{state.get('started_at')}（已运行 {float(state.get('uptime_seconds') or 0) / 60:.1f} 分钟）",
        f"最近巡检：{state.get('last_check') or '无'}",
        f"最近在线：{state.get('last_online') or '无'}",
        f"最近重登：{state.get('last_relogin') or '无'}",
        f"重登次数：尝试 {state.get('relogins_total', 0)} 次 / 成功 {state.get('relogin_success_total', 0)} 次"
        f"（最近一小时 {state.get('relogins_last_hour', 0)} 次）",
        f"累计巡检：{state.get('checks_total', 0)} 次",
        f"累计在线：{float(state.get('online_seconds') or 0) / 3600:.1f} 小时",
        f"探针结果：{state.get('probe') or '无'}",
        f"门户判定：{state.get('portal') or '无'}",
    ]
    if state.get("last_error"):
        lines.append(f"最近错误：{state['last_error']}")
    if state.get("paused"):
        lines.append(f"暂停原因：{state.get('paused_reason')}（到 {state.get('paused_until')}）")
    if state.get("next_attempt"):
        lines.append(f"下次尝试：{state['next_attempt']}")
    lines.append(f"状态更新时间：{state.get('updated_at')}")
    return "\n".join(lines)


def _pid_alive(pid: Any) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        kernel32.CloseHandle(handle)
        return True
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------- #
# 常驻入口
# --------------------------------------------------------------------------- #
def run_watch(cfg: dict, logger, *, config_path: str | None = None,
              use_tray: bool = True) -> int:
    """``python -m jitauth --watch`` 的实现：拉起引擎 + 托盘，阻塞到退出。"""
    lock = SingleInstance()
    if not lock.acquire():
        logger.info("已经有一个保活实例在运行，本进程直接退出（不会重复登录）")
        return 0

    engine = KeepAliveEngine(cfg, logger, config_path=config_path or cfg.get("_path"))
    loop = threading.Thread(target=engine.run, name="keepalive-loop", daemon=True)
    loop.start()

    tray = None
    if use_tray:
        try:
            from .tray import TrayApp

            tray = TrayApp(
                engine,
                logger,
                log_path=os.path.join(LOG_DIR, "keepalive.log"),
                config_path=str(engine.config_path or ""),
                app_dir=APP_DIR,
            )
            engine.set_notifier(tray.notify)
            _install_signal_handlers(engine, tray)
            tray.run()
        except Exception as exc:  # noqa: BLE001
            logger.warning("托盘不可用（%s），改为无界面后台运行（功能不受影响）", exc)
            tray = None

    if tray is None:
        _install_signal_handlers(engine, None)
        logger.info("以无界面模式运行；停止请用 Stop-ScheduledTask 或结束进程")
        try:
            while loop.is_alive():
                loop.join(1.0)
        except KeyboardInterrupt:
            logger.info("收到 Ctrl+C，准备退出")

    engine.stop()
    loop.join(timeout=15)
    lock.release()
    return 0


def _install_signal_handlers(engine: KeepAliveEngine, tray) -> None:
    def handler(signum, _frame):
        engine.request("stop")
        if tray is not None:
            tray.stop()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, handler)
        except Exception:  # noqa: BLE001 - pythonw / 非主线程下可能不支持
            pass