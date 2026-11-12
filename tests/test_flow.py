"""决策流程测试：验证 "检测 JIT-Stu → 连接 → 打开登录网站（已填好账号密码）→ 自动登录"。

不启动真实浏览器（替换掉打开登录页的动作），用本地模拟门户做真实 HTTP 登录。

用法: python tests/test_flow.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

# 安全阀：任何情况下都不允许测试真的切换 WiFi
os.environ.setdefault("JIT_NO_WIFI_SWITCH", "1")

from jitauth import __main__ as app  # noqa: E402
from jitauth import portal  # noqa: E402

PORT = 8820
BASE = f"http://127.0.0.1:{PORT}/"
ACCOUNT = {"username": "20260001", "password": "testpass123", "isp": "中国电信"}

failures: list[str] = []


class StubDriver:
    """假浏览器驱动：只记录调用，不做真实操作。"""

    def __init__(self) -> None:
        self.current_url = "about:blank"
        self.quit_called = False

    def execute_script(self, script, *args):
        return None

    def quit(self) -> None:
        self.quit_called = True


class PortalOpener:
    """替换 _open_login_page：记录是否去打开登录页（现在打开的是程序自己的浏览器窗口）。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, cfg, logger):
        self.calls.append(cfg["portal"]["url"])

    @property
    def opened(self) -> list[str]:
        return list(self.calls)


class BrowserSpy:
    """替换 browser_mod.login_via_browser：记录是否被调用，可选择让它成功。

    返回 (成功?, 说明, driver, 是否已接管页面)。
    """

    def __init__(self, login_ok: bool = False) -> None:
        self.calls: list[dict] = []
        self.login_ok = login_ok
        self.refilled = 0

    def __call__(self, cfg, logger, visible=None, submit=True):
        self.calls.append({"visible": visible, "submit": submit})
        if self.login_ok:
            return True, "测试：浏览器登录成功", StubDriver(), True
        return False, "测试：浏览器未成功", StubDriver(), True

    @property
    def called(self) -> bool:
        return bool(self.calls)


def make_cfg(**over) -> str:
    cfg = {
        "wifi": {"ssid": "JIT-Stu", "connect_timeout": 3, "require_visible": True},
        "portal": {"url": BASE, "timeout": 5, "probe_url": "", "probe_timeout": 2},
        "account": dict(ACCOUNT),
        "browser": {"browser": "edge", "mode": "auto", "find_timeout": 8,
                    "page_load_timeout": 15, "keep_open_seconds": 0, "leave_open": True},
        "behavior": {"open_portal": True, "auto_login": True, "stop_if_wifi_missing": True,
                     "skip_if_already_online": True, "retry_times": 1, "retry_interval": 1,
                     "probe_required": False, "quiet": False},
        "log": {"keep_days": 1},
    }
    for section, values in over.items():
        # 用 dict 合并而不是 update，否则会把整个小节替换掉
        cfg[section] = {**cfg.get(section, {}), **values}
    path = os.path.join(HERE, "_flow_cfg.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
    return path


def run_flow(cfg_path: str, argv: list[str], wifi_ok: bool | None = None,
             reason: str = "", browser_ok: bool = False) -> tuple[int, PortalOpener, BrowserSpy]:
    """调用 main()，返回 (退出码, 打开登录页的记录, 浏览器登录记录)。

    wifi_ok=None 表示使用真实的 WiFi 检查。
    browser_ok=True 模拟"浏览器兜底登录成功"。
    """
    opener = PortalOpener()
    spy = BrowserSpy(login_ok=browser_ok)
    real_open = app._open_login_page
    real_ensure = app.wifi.ensure_connected
    real_browser = app.browser_mod.login_via_browser
    real_check = app._check_already_logged_in

    def fake_ensure(ssid, timeout=25, require_visible=True,
                    connect_if_scan_unavailable=False,
                    enable_radio_if_off=True, enable_wait=3.0, logger=None):
        if logger is not None:
            logger.info("[测试] WiFi 检查：%s（%s）", "成功" if wifi_ok else "失败", reason)
        return bool(wifi_ok), reason

    app._open_login_page = opener  # type: ignore[assignment]
    app.browser_mod.login_via_browser = spy  # type: ignore[assignment]
    # 模拟门户（mock_portal）不是 Dr.COM，这里显式声明"未登录"，避免真实探测干扰
    app._check_already_logged_in = lambda cfg: (False, "测试：未登录")  # type: ignore[assignment]
    if wifi_ok is not None:
        app.wifi.ensure_connected = fake_ensure  # type: ignore[assignment]
    try:
        rc = app.main(["--config", cfg_path, "--silent"] + argv)
    finally:
        app._open_login_page = real_open  # type: ignore[assignment]
        app.browser_mod.login_via_browser = real_browser  # type: ignore[assignment]
        app.wifi.ensure_connected = real_ensure  # type: ignore[assignment]
        app._check_already_logged_in = real_check  # type: ignore[assignment]
    return rc, opener, spy


def portal_logged_in() -> bool:
    page = urllib.request.urlopen(BASE, timeout=3).read().decode("utf-8", "replace")
    return "认证成功" in page


def reset_portal() -> None:
    try:
        urllib.request.urlopen(BASE + "logout", timeout=2).read()
    except Exception:
        pass


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        failures.append(f"{name}: {detail}")


def main() -> int:
    server = subprocess.Popen([sys.executable, os.path.join(HERE, "mock_portal.py"), "drcom", str(PORT)],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(40):
        try:
            urllib.request.urlopen(BASE, timeout=1).read()
            break
        except Exception:
            time.sleep(0.2)
    try:
        # ---- 0. 门户已登录（Dr.COM 注销页）→ 直接结束，绝不开任何窗口 ----
        print("\n== 0. 门户显示已登录：应直接结束，不开任何浏览器窗口 ==")
        cfg = make_cfg()
        real_check = app._check_already_logged_in
        app._check_already_logged_in = lambda c: (True, "测试：门户显示已登录（账号 x@telecom）")
        opener0 = PortalOpener()
        spy0 = BrowserSpy()
        real_open0 = app._open_login_page
        real_browser0 = app.browser_mod.login_via_browser
        real_ensure0 = app.wifi.ensure_connected
        app._open_login_page = opener0
        app.browser_mod.login_via_browser = spy0
        app.wifi.ensure_connected = lambda *a, **k: (True, "测试：已连接")
        try:
            rc0 = app.main(["--config", cfg, "--silent"])
        finally:
            app._check_already_logged_in = real_check
            app._open_login_page = real_open0
            app.browser_mod.login_via_browser = real_browser0
            app.wifi.ensure_connected = real_ensure0
        check("返回码为 0", rc0 == 0, f"rc={rc0}")
        check("没有打开登录页", opener0.opened == [], f"opened={opener0.opened}")
        check("没有启动浏览器窗口", not spy0.called, str(spy0.calls))

        # ---- 1. 没搜到 JIT-Stu → 不执行，也不打开登录网站 ----
        print("\n== 1. 未搜索到 JIT-Stu：应直接退出，不开浏览器 ==")
        cfg = make_cfg()
        rc, opener, spy = run_flow(cfg, [], wifi_ok=False, reason="未搜索到 JIT-Stu")
        check("返回码为 0", rc == 0, f"rc={rc}")
        check("没有打开登录网站", opener.opened == [], f"opened={opener.opened}")
        check("没有尝试浏览器登录", not spy.called, str(spy.calls))

        # ---- 2. 关闭自动登录：只打开登录网站 ----
        print("\n== 2. 连上 JIT-Stu + 关闭自动登录：只打开登录网站 ==")
        cfg = make_cfg(behavior={"auto_login": False})
        rc, opener, spy = run_flow(cfg, [], wifi_ok=True, reason="已连接到 JIT-Stu")
        check("打开了登录网站", opener.opened == [BASE], f"opened={opener.opened}")
        check("没有尝试浏览器登录", not spy.called, str(spy.calls))
        check("返回码为 0", rc == 0, f"rc={rc}")

        # ---- 3. 自动登录：浏览器窗口里完成（填表 + 提交）----
        print("\n== 3. 连上 JIT-Stu + 自动登录：应在浏览器窗口里完成 ==")
        reset_portal()
        cfg = make_cfg()
        rc, opener, spy = run_flow(cfg, [], wifi_ok=True, reason="已连接到 JIT-Stu",
                                   browser_ok=True)
        check("动用了浏览器登录", spy.called, str(spy.calls))
        check("用的是可见窗口", bool(spy.calls) and spy.calls[0]["visible"] is True, str(spy.calls))
        check("没有再另开登录页", opener.opened == [], f"opened={opener.opened}")
        check("返回码为 0", rc == 0, f"rc={rc}")

        # ---- 3b. 浏览器失败 → HTTP 兜底成功 ----
        print("\n== 3b. 浏览器失败时，应用 HTTP 兜底并成功 ==")
        reset_portal()
        cfg = make_cfg()
        rc, opener, spy = run_flow(cfg, [], wifi_ok=True, reason="已连接到 JIT-Stu",
                                   browser_ok=False)
        check("尝试了浏览器登录", spy.called, str(spy.calls))
        check("HTTP 兜底登录成功", portal_logged_in(), "门户未显示认证成功")
        check("没有再另开登录页（窗口已保留）", opener.opened == [], f"opened={opener.opened}")
        check("返回码为 0", rc == 0, f"rc={rc}")

        # ---- 3c. 浏览器不可用（没 driver）→ HTTP 兜底，且失败时另开登录页 ----
        print("\n== 3c. 浏览器完全不可用时：应用 HTTP 兜底 ==")
        reset_portal()
        cfg = make_cfg()
        real_spy_cls = BrowserSpy

        class DeadBrowser(BrowserSpy):
            def __call__(self, cfg, logger, visible=None, submit=True):
                self.calls.append({"visible": visible, "submit": submit})
                return None, "测试：浏览器不可用", None, False

        spy_dead = DeadBrowser()
        real_open = app._open_login_page
        real_ensure = app.wifi.ensure_connected
        real_browser = app.browser_mod.login_via_browser
        app._open_login_page = opener = PortalOpener()
        app.browser_mod.login_via_browser = spy_dead
        app.wifi.ensure_connected = lambda *a, **k: (True, "测试：已连接")
        try:
            rc = app.main(["--config", cfg, "--silent"])
        finally:
            app._open_login_page = real_open
            app.browser_mod.login_via_browser = real_browser
            app.wifi.ensure_connected = real_ensure
        check("HTTP 兜底登录成功", portal_logged_in(), "门户未显示认证成功")
        check("返回码为 0", rc == 0, f"rc={rc}")

        # ---- 4. WiFi 检查失败但已联网：仍打开登录网站 ----
        print("\n== 4. WiFi 检查失败但已联网：应继续打开登录网站 ==")
        try:
            req = urllib.request.Request(BASE, data=b"0=20260001&1=testpass123&2=1", method="POST")
            urllib.request.urlopen(req, timeout=3).read()
        except Exception as exc:
            print(f"  (准备在线状态失败：{exc})")
        cfg = make_cfg(portal={"probe_url": BASE + "generate_204"})
        check("探针此时返回在线", portal.internet_ok(BASE + "generate_204", 2), "探针未返回 204")
        rc, opener, spy = run_flow(cfg, [], wifi_ok=False, reason="无法扫描 WiFi")
        check("仍然打开了登录网站", opener.opened == [BASE], f"opened={opener.opened}")

        # ---- 5. 已联网 → 跳过自动登录，但仍打开登录页 ----
        print("\n== 5. 已联网：应跳过自动登录 ==")
        cfg = make_cfg(portal={"probe_url": BASE + "generate_204"})
        rc, opener, spy = run_flow(cfg, [], wifi_ok=True, reason="已连接到 JIT-Stu")
        check("打开了登录网站", opener.opened == [BASE], f"opened={opener.opened}")
        check("没有尝试登录", not spy.called, str(spy.calls))
        # 已联网时跳过登录不算失败（rc 仅为诊断信息，用例关注"没有重复登录"）

        # ---- 6. open_portal=false → 不开浏览器 ----
        print("\n== 6. open_portal=false：不应打开浏览器 ==")
        cfg = make_cfg(behavior={"open_portal": False, "auto_login": False})
        rc, opener, spy = run_flow(cfg, [], wifi_ok=True, reason="已连接到 JIT-Stu")
        check("没有打开登录网站", opener.opened == [], f"opened={opener.opened}")

        # ---- 6b. open_portal=false + 自动登录：不应弹窗口，但仍尝试登录 ----
        print("\n== 6b. open_portal=false + 自动登录：不弹窗口但应登录成功 ==")
        reset_portal()
        cfg = make_cfg(behavior={"open_portal": False, "auto_login": True})
        rc, opener, spy = run_flow(cfg, [], wifi_ok=True, reason="已连接到 JIT-Stu")
        check("没有打开登录网站", opener.opened == [], f"opened={opener.opened}")
        check("HTTP 登录成功", portal_logged_in(), "门户未显示认证成功")
        check("返回码为 0", rc == 0, f"rc={rc}")

        # ---- 7. --no-login：只打开登录页，不登录 ----
        print("\n== 7. --no-login：打开登录页但不登录 ==")
        reset_portal()
        cfg = make_cfg()
        rc, opener, spy = run_flow(cfg, ["--no-login"], wifi_ok=True, reason="已连接到 JIT-Stu")
        check("打开了登录网站", opener.opened == [BASE], f"opened={opener.opened}")
        check("没有尝试登录", not spy.called, str(spy.calls))
        check("门户仍处于未登录", not portal_logged_in(), "不应登录成功")

        # ---- 8. 真实 WiFi 读取（只读，绝不切网）----
        print("\n== 8. 真实 WiFi 读取（只读；环境变量已禁止切网）==")
        from jitauth import wifi as wifi_mod
        current, source = wifi_mod.get_current_ssid()
        check("能读到当前 WiFi（只读操作）", current is not None, f"source={source}")
        ssids, scan_err = wifi_mod.scan_visible_ssids()
        if scan_err:
            print(f"  扫描不可用：{scan_err}")
        else:
            print(f"  扫描到 {len(ssids)} 个网络，"
                  f"{'含' if any(s.lower() == 'jit-stu' for s in ssids) else '不含'} JIT-Stu")
        check("切网保护已生效", wifi_mod.switching_disabled(), "JIT_NO_WIFI_SWITCH 未生效")
        ok, reason = wifi_mod.connect("JIT-Stu", timeout=5)
        check("connect() 拒绝真实切网", ok is False and "JIT_NO_WIFI_SWITCH" in reason, reason)
        after, _ = wifi_mod.get_current_ssid()
        check("网络未被切换", after == current, f"{current!r} -> {after!r}")
    finally:
        server.kill()

    print("\n" + ("全部通过" if not failures else f"失败 {len(failures)} 项："))
    for f in failures:
        print("  -", f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
