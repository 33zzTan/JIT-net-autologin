"""保活/断线重连引擎测试（全部离线，不碰真实校园网）。

用法: python tests/test_keepalive.py

覆盖：
  1. 配置解析、探针列表、状态机、退避、限流、防互踢、账号错误停手
  2. 探针端点判定（本地 HTTP 服务器，真实 socket）
  3. 端到端：起 tests/mock_portal.py，让引擎从"未登录"自己登进去
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from jitauth import config as config_mod  # noqa: E402
from jitauth import keepalive  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        failures.append(f"{name}: {detail}")


# --------------------------------------------------------------------------- #
# 测试替身
# --------------------------------------------------------------------------- #
class FakeClock:
    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.t = start

    def now(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds

    def sleep(self, seconds: float) -> None:
        self.t += max(0.0, seconds)


class FakeNetwork:
    """可控的探针 / 门户 / 登录。"""

    def __init__(self) -> None:
        self.online = False
        self.portal = keepalive.P_OFFLINE
        self.portal_reason = "门户是登录页"
        self.login_ok = True
        self.login_message = "认证成功"
        self.login_calls = 0
        # 浏览器重登（第二级兜底）
        self.browser_ok = False
        self.browser_message = "浏览器重登未成功"
        self.browser_calls = 0
        self.probe_calls = 0
        self.wifi_calls = 0
        self.notifications: list[tuple[str, str, str]] = []
        # 每次登录后是否真的恢复
        self.login_recovers = True
        # 登录后只恢复 probe_after_login 次，模拟"刚登录就被踢"
        self.flap_mode = False

    def probe(self) -> keepalive.ProbeResult:
        self.probe_calls += 1
        if self.online:
            return keepalive.ProbeResult(online=True, hits=["fake"])
        return keepalive.ProbeResult(online=False, fails={"fake": "timeout"})

    def portal_state(self) -> tuple[str, str]:
        return self.portal, self.portal_reason

    def login(self) -> tuple[bool, str]:
        self.login_calls += 1
        if self.login_ok:
            self.online = True
            if self.flap_mode:
                # 登录成功但下一轮又掉（模拟被手机抢占）
                self._flap_armed = True
        return self.login_ok, self.login_message

    def browser(self) -> tuple[bool, str]:
        """模拟"弹出浏览器窗口自动登录"。"""
        self.browser_calls += 1
        if self.browser_ok:
            self.online = True
        return self.browser_ok, self.browser_message

    def wifi(self) -> tuple[bool, str]:
        self.wifi_calls += 1
        return True, "已连接"


def make_engine(net: FakeNetwork, clock: FakeClock, **overrides) -> keepalive.KeepAliveEngine:
    cfg = config_mod.load()
    cfg["keepalive"].update({
        "check_interval": 60,
        "fail_threshold": 3,
        "wifi_reconnect": True,
        "notify": True,
    })
    # 默认配置里 WiFi 名是空的（交给设置向导），这里显式给一个，才能测到 WiFi 修复分支
    cfg.setdefault("wifi", {})["ssid"] = "TestWiFi"
    for key, value in overrides.items():
        if key.startswith("relogin_"):
            cfg["keepalive"]["relogin"][key[len("relogin_"):]] = value
        else:
            cfg["keepalive"][key] = value

    engine = keepalive.KeepAliveEngine(
        cfg,
        _Quiet(),
        probe_fn=net.probe,
        portal_fn=net.portal_state,
        login_fn=net.login,
        browser_fn=net.browser,
        wifi_fn=net.wifi,
        notify_fn=lambda level, title, text: net.notifications.append((level, title, text)),
        now_fn=clock.now,
        sleep_fn=clock.sleep,
        state_path=os.path.join(tempfile.gettempdir(), "jit-test-keepalive.state.json"),
    )
    engine.settings.notify = True
    return engine


class _Quiet:
    """静默 logger（测试时不刷屏，也不写日志文件）。"""

    def debug(self, *a, **k):
        pass

    info = warning = error = debug

    def exception(self, *a, **k):
        pass


# --------------------------------------------------------------------------- #
# 1. 配置
# --------------------------------------------------------------------------- #
def test_config() -> None:
    print("\n== 配置解析 ==")
    cfg = config_mod.load()
    settings = keepalive.Settings.from_cfg(cfg)
    check("默认巡检间隔 20 秒（快速模式）", settings.check_interval == 20,
          str(settings.check_interval))
    check("默认 1 轮失败就判定断线", settings.fail_threshold == 1,
          str(settings.fail_threshold))
    check("默认开启静默重登", settings.relogin_enabled is True)
    check("默认退避序列", settings.backoff_seconds == [30.0, 120.0, 300.0, 900.0],
          str(settings.backoff_seconds))

    weird = {"keepalive": {
        "check_interval": "abc",
        "fail_threshold": 0,
        "enabled": "false",
        "relogin": {"enabled": "no", "backoff_seconds": [], "max_per_hour": 0},
    }}
    s2 = keepalive.Settings.from_cfg(weird)
    check("坏值回落到默认间隔", s2.check_interval == 20.0, str(s2.check_interval))
    check("阈值下限为 1", s2.fail_threshold == 1, str(s2.fail_threshold))
    check("字符串 false 能识别", s2.enabled is False and s2.relogin_enabled is False)
    check("空退避序列有兜底", s2.backoff_seconds == [30.0, 120.0, 300.0, 900.0],
          str(s2.backoff_seconds))
    check("max_per_hour 下限为 1", s2.max_per_hour == 1, str(s2.max_per_hour))

    check("默认重登方式 auto", settings.relogin_engine == "auto", settings.relogin_engine)
    check("非法 engine 回落到 auto",
          keepalive.Settings.from_cfg(
              {"keepalive": {"relogin": {"engine": "whatever"}}}).relogin_engine == "auto")
    check("engine=browser 能识别",
          keepalive.Settings.from_cfg(
              {"keepalive": {"relogin": {"engine": "BROWSER"}}}).relogin_engine == "browser")

    eps = keepalive.build_endpoints({})
    check("默认三个探针", len(eps) == 3, str([e.name for e in eps]))
    eps2 = keepalive.build_endpoints({"keepalive": {"probes": [
        "http://a.example/generate_204",
        {"url": "http://b.example/x", "expect": "Success"},
        {"url": ""},
    ]}})
    check("支持字符串和对象两种写法", [e.url for e in eps2] == [
        "http://a.example/generate_204", "http://b.example/x"], str([e.url for e in eps2]))
    check("marker 模式识别", eps2[1].marker_mode and not eps2[0].marker_mode)


# --------------------------------------------------------------------------- #
# 2. 探针（真实本地 HTTP）
# --------------------------------------------------------------------------- #
class _ProbeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:
        pass

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/gen204":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/marker":
            body = b"Microsoft Connect Test"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/portal":  # 被劫持：跳转到门户
            self.send_response(302)
            self.send_header("Location", "/login")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/fake204":  # 状态码不是 204
            body = b"<html>login</html>"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()


def test_probes() -> None:
    print("\n== 探针判定（本地 HTTP 服务器）==")
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ProbeHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    try:
        ok, why = keepalive.check_endpoint(keepalive.ProbeEndpoint(f"{base}/gen204"), 2)
        check("204 → 在线", ok is True, why)
        ok, why = keepalive.check_endpoint(
            keepalive.ProbeEndpoint(f"{base}/marker", "Microsoft Connect Test"), 2)
        check("内容匹配 → 在线", ok is True, why)
        ok, why = keepalive.check_endpoint(
            keepalive.ProbeEndpoint(f"{base}/marker", "Something Else"), 2)
        check("内容不匹配 → 离线", ok is False, why)
        ok, why = keepalive.check_endpoint(keepalive.ProbeEndpoint(f"{base}/portal"), 2)
        check("302 跳转 → 离线（被劫持）", ok is False, why)
        ok, why = keepalive.check_endpoint(keepalive.ProbeEndpoint(f"{base}/fake204"), 2)
        check("200 冒充 204 → 离线", ok is False, why)

        result = keepalive.probe([
            keepalive.ProbeEndpoint(f"{base}/portal"),
            keepalive.ProbeEndpoint(f"{base}/gen204"),
        ], 2)
        check("多个探针任一通即在线", result.online is True, result.summary())

        result = keepalive.probe([
            keepalive.ProbeEndpoint(f"{base}/portal"),
            keepalive.ProbeEndpoint(f"{base}/fake204"),
        ], 2)
        check("全部不通 → 离线", result.online is False, result.summary())

        result = keepalive.probe([keepalive.ProbeEndpoint("http://127.0.0.1:1/none")], 1)
        check("连接被拒 → 离线且不抛异常", result.online is False, result.summary())
    finally:
        server.shutdown()
        server.server_close()


# --------------------------------------------------------------------------- #
# 3. 状态机
# --------------------------------------------------------------------------- #
def test_state_machine() -> None:
    print("\n== 状态机：三次确认才重登 ==")
    net = FakeNetwork()
    clock = FakeClock()
    engine = make_engine(net, clock)

    engine.tick()
    check("第 1 次不通 → SUSPECT", engine.state == keepalive.SUSPECT, engine.state)
    check("第 1 次不重登", net.login_calls == 0, str(net.login_calls))

    clock.advance(60)
    engine.tick()
    check("第 2 次仍不重登", net.login_calls == 0, str(net.login_calls))

    clock.advance(60)
    engine.tick()
    check("第 3 次确认后重登一次", net.login_calls == 1, str(net.login_calls))
    check("重登后恢复 ONLINE", engine.state == keepalive.ONLINE, engine.state)
    check("记录成功次数", engine.relogin_success_total == 1, str(engine.relogin_success_total))
    check("发了「正在重连 / 已恢复」通知", len(net.notifications) >= 2, str(net.notifications))

    print("\n== 门户说还在线时，不重登 ==")
    net = FakeNetwork()
    clock = FakeClock()
    engine = make_engine(net, clock)
    net.online = False
    net.portal = keepalive.P_ONLINE
    net.portal_reason = "门户显示已登录"
    for i in range(3):
        engine.tick()
        clock.advance(60)
    check("门户在线 → 不重登", net.login_calls == 0, str(net.login_calls))
    check("状态标记为 DEGRADED", engine.state == keepalive.DEGRADED, engine.state)

    print("\n== 门户登录页没有表单：HTTP 失败后自动弹浏览器 ==")
    net = FakeNetwork()
    clock = FakeClock()
    engine = make_engine(net, clock)
    net.login_ok = False
    net.login_message = "门户页面里没有找到登录表单（可能需要浏览器渲染）"
    net.browser_ok = True
    net.browser_message = "页面出现成功/已在线提示"
    for i in range(3):
        engine.tick()
        clock.advance(60)
    check("HTTP 失败后调用了浏览器", net.browser_calls == 1, str(net.browser_calls))
    check("浏览器成功后恢复 ONLINE", engine.state == keepalive.ONLINE, engine.state)
    check("记一次成功重登", engine.relogin_success_total == 1, str(engine.relogin_success_total))

    print("\n== engine=browser：直接弹窗，不走 HTTP ==")
    net = FakeNetwork()
    clock = FakeClock()
    engine = make_engine(net, clock, relogin_engine="browser")
    net.login_ok = False
    net.browser_ok = True
    for i in range(3):
        engine.tick()
        clock.advance(60)
    check("没有尝试 HTTP", net.login_calls == 0, str(net.login_calls))
    check("直接用了浏览器", net.browser_calls == 1, str(net.browser_calls))
    check("恢复 ONLINE", engine.state == keepalive.ONLINE, engine.state)

    print("\n== 两级都失败才退避 ==")
    net = FakeNetwork()
    clock = FakeClock()
    engine = make_engine(net, clock)
    net.login_ok = False
    net.browser_ok = False
    for i in range(2):
        engine.tick()
        clock.advance(60)
    engine.tick()  # 第 3 次达到阈值：两级都试，都失败 → 排退避
    check("HTTP 和浏览器都试过", net.login_calls == 1 and net.browser_calls == 1,
          f"http={net.login_calls} browser={net.browser_calls}")
    check("进入 DEGRADED 且已排退避",
          engine.state == keepalive.DEGRADED and engine.next_attempt_at > clock.now(),
          f"{engine.state} / {engine.next_attempt_at} vs {clock.now()}")

    print("\n== 门户打不开：不重登，先修 WiFi ==")
    net = FakeNetwork()
    clock = FakeClock()
    engine = make_engine(net, clock)
    net.portal = keepalive.P_UNREACHABLE
    for i in range(3):
        engine.tick()
        clock.advance(60)
    check("门户不可达 → 不重登", net.login_calls == 0, str(net.login_calls))
    check("尝试了 WiFi 修复", net.wifi_calls >= 1, str(net.wifi_calls))

    print("\n== 账号密码错误：立即停手 ==")
    net = FakeNetwork()
    clock = FakeClock()
    engine = make_engine(net, clock)
    net.login_ok = False
    net.login_message = "门户返回：密码错误（账号/密码问题，请检查配置）"
    for i in range(3):
        engine.tick()
        clock.advance(60)
    engine.tick()
    clock.advance(60)
    engine.tick()
    check("账号错误只试一次就停", net.login_calls == 1, str(net.login_calls))
    check("进入 PAUSED", engine.state == keepalive.PAUSED, engine.state)
    check("暂停时间约 6 小时", engine.paused_until - clock.now() > 5 * 3600,
          str(engine.paused_until - clock.now()))
    check("暂停期间不再重登", net.login_calls == 1, str(net.login_calls))

    print("\n== 退避：失败后不会立刻重试 ==")
    net = FakeNetwork()
    clock = FakeClock()
    engine = make_engine(net, clock, relogin_backoff_seconds=[30, 60])
    net.login_ok = False
    net.login_message = "门户没有给出明确结果"
    engine.tick()
    clock.advance(60)
    engine.tick()
    clock.advance(60)
    engine.tick()
    first_calls = net.login_calls
    check("第一次失败后记了下次时间", engine.next_attempt_at > clock.now(),
          f"{engine.next_attempt_at} vs {clock.now()}")
    engine.tick()
    check("退避期内不重登", net.login_calls == first_calls, f"{net.login_calls} vs {first_calls}")
    clock.advance(31)
    engine.tick()
    check("退避结束后允许重试", net.login_calls > first_calls, str(net.login_calls))
    check("_next_wait 会缩到退避结束", engine._next_wait() <= 60.0, str(engine._next_wait()))

    print("\n== 限流：每小时有上限 ==")
    net = FakeNetwork()
    clock = FakeClock()
    engine = make_engine(net, clock, relogin_max_per_hour=2)
    net.login_ok = False
    net.login_message = "失败"
    for _ in range(40):
        engine.tick()
        clock.advance(20)
    check("一小时内的尝试次数不超过上限太多", net.login_calls <= 3, str(net.login_calls))

    print("\n== 防互踢：连续几次刚登录就掉就退避 ==")
    net = FakeNetwork()
    clock = FakeClock()
    engine = make_engine(net, clock, relogin_flap_threshold=3, relogin_flap_window=600)
    net.portal = keepalive.P_OFFLINE
    for i in range(4):
        # 每次：连续 3 次失败 → 重登成功 → 但马上又被踢下线
        net.online = False
        for _ in range(3):
            engine.tick()
            clock.advance(30)
    check("连续被踢后进入退避", engine.flap_hold_until > clock.now(),
          f"{engine.flap_hold_until} vs {clock.now()}")
    calls_after_hold = net.login_calls
    net.online = False
    engine.tick()
    clock.advance(30)
    engine.tick()
    clock.advance(30)
    engine.tick()
    check("退避期内不再抢登", net.login_calls == calls_after_hold,
          f"{net.login_calls} vs {calls_after_hold}")

    print("\n== 关闭重登 = 严格纯心跳模式 ==")
    net = FakeNetwork()
    clock = FakeClock()
    engine = make_engine(net, clock, relogin_enabled=False)
    for i in range(5):
        engine.tick()
        clock.advance(60)
    check("relogin.enabled=false → 从不重登", net.login_calls == 0, str(net.login_calls))
    check("状态为 DEGRADED", engine.state == keepalive.DEGRADED, engine.state)


# --------------------------------------------------------------------------- #
# 4. 状态文件 / 单实例 / 小工具
# --------------------------------------------------------------------------- #
def test_state_file() -> None:
    print("\n== 状态文件与展示 ==")
    path = os.path.join(tempfile.gettempdir(), "jit-test-state.json")
    if os.path.exists(path):
        os.remove(path)
    check("没有文件时读空字典", keepalive.read_state(path) == {})

    net = FakeNetwork()
    clock = FakeClock()
    engine = make_engine(net, clock)
    engine.state_path = path
    net.online = True
    engine.tick()
    engine._write_state()

    data = keepalive.read_state(path)
    check("状态文件可回读", data.get("state") == keepalive.ONLINE, str(data.get("state")))
    check("快照含状态文案", bool(data.get("status_text")), str(data))
    text = keepalive.format_status(data)
    check("format_status 能生成可读文本", "看门狗" in text and "当前状态" in text, text[:60])

    check("_is_fatal 认密码错误", keepalive._is_fatal("密码错误，请重试") is True)
    check("_is_fatal 不误伤普通失败", keepalive._is_fatal("门户没有给出明确结果") is False)

    lock = keepalive.SingleInstance("JIT-TEST-KeepAlive")
    first = lock.acquire()
    second = keepalive.SingleInstance("JIT-TEST-KeepAlive").acquire()
    check("单实例：第一次拿到锁", first is True)
    check("单实例：第二次拿不到", second is False, str(second))
    lock.release()


# --------------------------------------------------------------------------- #
# 5. 端到端（mock 门户）
# --------------------------------------------------------------------------- #
def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_e2e_mock_portal() -> None:
    print("\n== 端到端：mock 门户（真的发 HTTP 表单登录）==")
    port = _free_port()
    mock = os.path.join(HERE, "mock_portal.py")
    proc = subprocess.Popen([sys.executable, mock, "drcom", str(port)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            check("mock 门户启动", False, "10 秒内没起来")
            return

        cfg = config_mod.load()
        cfg["portal"]["url"] = f"http://127.0.0.1:{port}/"
        cfg["portal"]["timeout"] = 3
        cfg["account"] = {"username": "20260001", "password": "testpass123", "isp": "中国电信"}
        cfg["keepalive"].update({
            "fail_threshold": 2,
            "wifi_reconnect": False,
            "check_interval": 5,
        })
        cfg["keepalive"]["probes"] = [{"url": f"http://127.0.0.1:{port}/generate_204",
                                       "expect": "204"}]
        cfg["keepalive"]["relogin"].update({"verify_timeout": 8, "backoff_seconds": [5, 5]})

        clock = FakeClock(time.time())
        engine = keepalive.KeepAliveEngine(
            cfg,
            _Quiet(),
            now_fn=clock.now,
            sleep_fn=lambda s: (clock.advance(s), time.sleep(min(s, 0.05))),
            state_path=os.path.join(tempfile.gettempdir(), "jit-test-e2e.state.json"),
        )

        # 前两次：未登录 → SUSPECT
        engine.tick()
        check("e2e：第 1 次探测为离线", engine.state == keepalive.SUSPECT, engine.state)
        clock.advance(5)
        engine.tick()
        check("e2e：第 2 次达到阈值并重登成功",
              engine.state == keepalive.ONLINE, engine.state)
        check("e2e：确实调用了真实 HTTP 登录", engine.relogin_success_total == 1,
              str(engine.relogin_success_total))

        clock.advance(5)
        engine.tick()
        check("e2e：登录后保持在线", engine.state == keepalive.ONLINE, engine.state)

        # 模拟网关把会话踢掉
        import urllib.request
        urllib.request.urlopen(f"http://127.0.0.1:{port}/logout", timeout=3).read()
        clock.advance(5)
        engine.tick()
        clock.advance(5)
        engine.tick()
        check("e2e：被踢后又自动登回去", engine.state == keepalive.ONLINE, engine.state)
        check("e2e：累计两次成功重登", engine.relogin_success_total == 2,
              str(engine.relogin_success_total))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


# --------------------------------------------------------------------------- #
def main() -> int:
    test_config()
    test_probes()
    test_state_machine()
    test_state_file()
    test_e2e_mock_portal()

    print("\n" + ("全部通过" if not failures else f"失败 {len(failures)} 项："))
    for item in failures:
        print("  -", item)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())