"""浏览器自动化登录（CDP 直连版，2026-09 重写）。

设计原则
--------
1. **只用「程序自己的浏览器窗口」**
   独立 ``--user-data-dir``（默认项目内 ``browser-profile/``）。
   绝不启动、绝不复用你日常在用的 Edge/Chrome 配置，也不会用系统默认浏览器打开页面。
   想强制用你自己的配置，必须显式写 ``browser.profile = "user"``（不推荐）。

2. **默认不依赖 Selenium / msedgedriver**
   自己把 Edge 拉起来，用 DevTools 协议（CDP）填表、提交。
   不需要浏览器驱动、不需要联网下载任何东西 —— 开机时（还没认证、没外网）也能 1~2 秒把窗口弹出来。
   Selenium 只在 ``browser.engine = "selenium"`` 或 CDP 连窗口都打不开时兜底。

3. **窗口不会自己消失**
   * 屏蔽页面脚本里的 ``window.close()``（校园网门户经常这么干，窗口一闪就没了）；
   * 万一窗口真的被关掉，会自动重新打开一次，而不是直接放弃。

4. **窗口默认保留**
   登录结束后窗口留在认证结果页，方便你确认 / 手动点一下「登录」。
   想让它自动关掉：``browser.close_after_login = true``。
"""
from __future__ import annotations

import glob as _glob
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENDOR_DIR = os.path.join(APP_DIR, "vendor")

#: 依次尝试的远程调试端口（第一个没被占用、且能起来就用它）
CDP_PORTS = (9223, 9224, 9225)

CREATE_NO_WINDOW = 0x08000000

EDGE_PATHS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]
CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]

#: 屏蔽页面里的 window.close()：校园网门户的注销页/成功页会自己关窗口
ANTI_CLOSE_JS = (
    "(function(){try{var noop=function(){};"
    "Object.defineProperty(window,'close',{value:noop,writable:true,configurable:true});"
    "window.close=noop;}catch(e){}return true;})()"
)


class BrowserUnavailable(Exception):
    """找不到任何可用浏览器，或者窗口拉不起来。"""


class ScriptError(RuntimeError):
    """页面里的 JS 执行出错（多半是页面正在跳转）。"""


def _first_line(exc: object) -> str:
    text = str(exc) if exc is not None else ""
    return text.splitlines()[0].strip() if text.strip() else ""


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def ensure_vendor_on_path() -> None:
    if os.path.isdir(VENDOR_DIR) and VENDOR_DIR not in sys.path:
        sys.path.insert(0, VENDOR_DIR)


def selenium_available() -> bool:
    ensure_vendor_on_path()
    try:
        import selenium  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


def find_browser(prefer: str = "auto") -> tuple[str, str]:
    """返回 (类型, 可执行文件路径)；类型为 'edge' 或 'chrome'。"""
    order = ["edge", "chrome"] if prefer in ("auto", "edge") else ["chrome", "edge"]
    for kind in order:
        paths = EDGE_PATHS if kind == "edge" else CHROME_PATHS
        for path in paths:
            if os.path.isfile(path):
                return kind, path
    raise BrowserUnavailable("系统里没有找到 Edge 或 Chrome")


def default_browser() -> tuple[str, str]:
    """按系统默认浏览器决定用哪个，找不到就退回可用浏览器。"""
    try:
        import winreg  # type: ignore

        key = r"SOFTWARE\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as handle:
            prog_id = str(winreg.QueryValueEx(handle, "ProgId")[0]).lower()
        if "chrome" in prog_id:
            return find_browser("chrome")
        if "edge" in prog_id:
            return find_browser("edge")
    except Exception:  # noqa: BLE001
        pass
    return find_browser("auto")


def _browser_name(cfg: dict) -> str:
    """只要浏览器类型名（edge/chrome），不需要它一定装着。"""
    prefer = str(cfg.get("browser", {}).get("browser", "auto") or "auto").lower()
    if prefer in ("edge", "chrome"):
        return prefer
    try:
        kind, _ = default_browser()
        return kind
    except BrowserUnavailable:
        return "edge"


def _browser_kind(cfg: dict) -> tuple[str, str]:
    """按配置决定用哪个浏览器，返回 (类型, 可执行文件)。"""
    prefer = str(cfg.get("browser", {}).get("browser", "auto") or "auto").lower()
    if prefer in ("auto", ""):
        return default_browser()
    return find_browser(prefer)


def _user_data_dir(cfg: dict | None = None) -> str:
    """程序自己的独立浏览器配置目录（保存门户会话，和你的浏览器互不影响）。"""
    custom = ""
    if cfg is not None:
        custom = str(cfg.get("browser", {}).get("user_data_dir", "") or "")
    path = custom or os.path.join(APP_DIR, "browser-profile")
    if not os.path.isabs(path):
        path = os.path.join(APP_DIR, path)
    os.makedirs(path, exist_ok=True)
    return path


def _default_profile_dirs(browser: str) -> list[tuple[str, str]]:
    """返回浏览器「用户默认配置目录」的候选 (profile目录, profile名)。"""
    local = os.environ.get("LOCALAPPDATA", "")
    if browser == "edge":
        base = os.path.join(local, "Microsoft", "Edge", "User Data")
        return [(base, "Default"), (base, "Profile 1")]
    base = os.path.join(local, "Google", "Chrome", "User Data")
    return [(base, "Default"), (base, "Profile 1")]


def _profile_candidates(cfg: dict) -> list[tuple[str, str, str]]:
    """返回 [(profile目录, profile名, 类型)]，类型为 dedicated / user。

    * 默认（``profile`` 省略或为 ``dedicated``）**只有**程序自己的窗口，
      不存在「专用窗口起不来就偷偷用你的浏览器」这种回退。
    * 只有显式写 ``browser.profile = "user"`` 才会返回你日常浏览器的配置目录。
    """
    browser_cfg = cfg.get("browser", {})
    chosen = str(browser_cfg.get("profile", "dedicated") or "dedicated").lower()
    if chosen == "user":
        kind = _browser_name(cfg)
        return [(directory, name, "user") for directory, name in _default_profile_dirs(kind)]
    return [(_user_data_dir(cfg), "", "dedicated")]


def _profile_locked(directory: str) -> bool:
    """判断该 profile 是否正被浏览器占用（占用时不能再用它启动）。"""
    for name in ("SingletonLock", "lockfile"):
        path = os.path.join(directory, name)
        if os.path.exists(path):
            return True
    return False


def _find_timeout(cfg: dict) -> float:
    return float(cfg["browser"].get("find_timeout", 12))


def _find_timeout_cfg(cfg: dict, key: str, default: float) -> float:
    try:
        return float(cfg.get("browser", {}).get(key, default))
    except (TypeError, ValueError):
        return default


def _close_after_login(cfg: dict) -> bool:
    return bool(cfg.get("browser", {}).get("close_after_login", False))


# --------------------------------------------------------------------------- #
# CDP 基础工具
# --------------------------------------------------------------------------- #
def _http_json(port: int, path: str, timeout: float = 2.0):
    url = f"http://127.0.0.1:{port}{path}"
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.load(resp)


def _port_alive(port: int) -> bool:
    try:
        with socket.socket() as sock:
            sock.settimeout(0.3)
            return sock.connect_ex(("127.0.0.1", port)) == 0
    except OSError:
        return False


def _debug_version(port: int) -> dict | None:
    """返回 /json/version 的内容；说明这个端口真的是浏览器调试端口。"""
    try:
        data = _http_json(port, "/json/version", timeout=2.0)
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


def _live_debug_port() -> int | None:
    for port in CDP_PORTS:
        if _port_alive(port) and _debug_version(port):
            return port
    return None


def _wait_port_gone(port: int, timeout: float = 4.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _port_alive(port):
            return True
        time.sleep(0.2)
    return not _port_alive(port)


#: 记录我们自己拉起来的浏览器进程（端口 → Popen），关闭窗口时用得上
_LAUNCHED: dict[int, subprocess.Popen] = {}


def _terminate_launched(port: int, logger=None) -> None:
    proc = _LAUNCHED.pop(port, None)
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.terminate()
            if logger is not None:
                logger.debug("已终止调试端口 %d 上的浏览器进程", port)
    except Exception:  # noqa: BLE001
        pass


def _debug_port_usable(port: int) -> bool:
    """这个调试端口上的浏览器真的能连上吗？（Edge 后台残留也会占着端口）"""
    targets = _page_targets(port)
    if not targets:
        try:
            _create_page_target(port, "about:blank")
        except Exception:  # noqa: BLE001
            return False
        targets = _page_targets(port)
    if not targets:
        return False
    try:
        from .cdp import CDPClient  # 延迟导入
    except Exception:  # noqa: BLE001
        return False
    for target in targets:
        try:
            with CDPClient(str(target.get("webSocketDebuggerUrl")), timeout=8.0) as client:
                client.call("Runtime.enable")
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _page_targets(port: int) -> list[dict]:
    try:
        data = _http_json(port, "/json/list", timeout=2.0)
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(data, list):
        return []
    return [t for t in data if isinstance(t, dict) and t.get("type") == "page"
            and t.get("webSocketDebuggerUrl")]


def _create_page_target(port: int, url: str) -> None:
    """浏览器还活着但一个标签页都没有时，新建一个。"""
    info = _debug_version(port) or {}
    ws = str(info.get("webSocketDebuggerUrl") or "")
    if not ws:
        raise BrowserUnavailable("浏览器调试接口不可用")
    from .cdp import CDPClient  # 延迟导入，避免影响主流程

    with CDPClient(ws) as client:
        client.call("Target.createTarget", {"url": url})


def _wait_debug_port(port: int, proc: subprocess.Popen | None, timeout: float) -> bool:
    """等调试端口就绪；进程提前退出（说明被已有实例接管/启动失败）就立刻放弃。"""
    deadline = time.time() + max(3.0, timeout)
    while time.time() < deadline:
        if _port_alive(port) and _debug_version(port):
            return True
        if proc is not None and proc.poll() is not None:
            return False
        time.sleep(0.25)
    return False


def launch_own_browser(cfg: dict, logger, url: str | None = None) -> tuple[int, str]:
    """启动（或复用）程序自己的浏览器窗口，返回 (调试端口, 浏览器类型)。

    已经有一个带调试端口的窗口在跑（比如上次运行留下的）就直接复用，不会开第二个。
    """
    kind, exe = _browser_kind(cfg)
    profile = _user_data_dir(cfg)
    if not url:
        url = str(cfg["portal"]["url"])

    live = _live_debug_port()
    if live is not None:
        if _debug_port_usable(live):
            logger.info("复用已经打开的程序自己的浏览器窗口（调试端口 %d）", live)
            return live, kind
        logger.warning("调试端口 %d 上的窗口没有响应，重新打开一个", live)
        _terminate_launched(live, logger)
        _wait_port_gone(live, 5.0)

    timeout = _find_timeout_cfg(cfg, "launch_timeout", 25.0)
    errors: list[str] = []
    for port in CDP_PORTS:
        if _port_alive(port):
            errors.append(f"端口 {port} 被占用")
            continue
        cmd = [
            exe,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--no-service-autorun",
            "--disable-sync",
            "--disable-features=msEdgeFirstRunExperience",
            "--remote-allow-origins=*",
            "--window-size=1200,900",
            "--lang=zh-CN",
            "--new-window",
            url,
        ]
        try:
            proc = subprocess.Popen(cmd, close_fds=True)
        except OSError as exc:
            errors.append(f"启动 {os.path.basename(exe)} 失败：{exc}")
            continue

        if _wait_debug_port(port, proc, timeout):
            _LAUNCHED[port] = proc
            logger.info("已启动程序自己的浏览器窗口（%s，调试端口 %d）", kind, port)
            return port, kind

        if proc.poll() is not None:
            errors.append(f"浏览器刚启动就退出了（端口 {port}）")
        else:
            errors.append(f"调试端口 {port} 未就绪")
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass

    detail = "；".join(errors) or "未知原因"
    raise BrowserUnavailable(
        f"没能打开程序自己的浏览器窗口（{kind}）：{detail}\n"
        f"  配置目录：{profile}\n"
        "  如果那里已经有一个窗口开着，先关掉它再试一次。"
    )


class BrowserSession:
    """统一浏览器会话：上层只用 get() / execute_script() / quit()。

    * CDP 模式：程序自己启动的窗口，默认**不关闭**。
    * Selenium 模式：driver 不为 None，行为与以前一致。

    execute_script 的语义和 Selenium 一样：脚本被包在函数体里执行，
    想拿返回值就自己写 ``return ...``。
    """

    def __init__(self, *, kind: str, logger, port: int | None = None,
                 driver=None, keep_open: bool = True, initial_url: str = "") -> None:
        self.kind = kind
        self.logger = logger
        self.port = port
        self.driver = driver
        self.keep_open = keep_open
        self.initial_url = initial_url
        self._client = None
        self._target_id: str | None = None

    # ---------------- 内部：CDP 连接 ---------------- #
    def _pick_target(self) -> dict:
        targets = _page_targets(self.port or 0)
        if not targets:
            _create_page_target(self.port or 0, self.initial_url or "about:blank")
            for _ in range(25):
                time.sleep(0.2)
                targets = _page_targets(self.port or 0)
                if targets:
                    break
        if not targets:
            raise BrowserUnavailable("浏览器里没有可用的标签页")
        for target in targets:
            if str(target.get("url", "")).startswith("http"):
                return target
        return targets[0]

    def _connect(self):
        if self._client is not None:
            return self._client
        if not self.port:
            raise BrowserUnavailable("浏览器调试端口未知")
        try:
            from .cdp import CDPClient  # 延迟导入
        except Exception as exc:  # noqa: BLE001
            raise BrowserUnavailable(f"CDP 客户端不可用：{exc}") from exc

        target = self._pick_target()
        client = CDPClient(str(target.get("webSocketDebuggerUrl")))
        client.connect()
        self._client = client
        self._target_id = str(target.get("id") or "") or None
        for method in ("Page.enable", "Runtime.enable"):
            try:
                client.call(method)
            except Exception as exc:  # noqa: BLE001
                self.logger.debug("%s 失败：%s", method, _first_line(exc))
        self._harden()
        return client

    def _harden(self) -> None:
        """屏蔽 window.close()，防止门户页面把我们的窗口关掉。"""
        client = self._client
        if client is None:
            return
        try:
            client.call("Page.addScriptToEvaluateOnNewDocument", {"source": ANTI_CLOSE_JS})
        except Exception as exc:  # noqa: BLE001
            self.logger.debug("注入防关闭脚本失败：%s", _first_line(exc))
        try:
            client.evaluate(ANTI_CLOSE_JS)
        except Exception:  # noqa: BLE001
            pass

    # ---------------- 对外接口 ---------------- #
    def focus(self) -> None:
        """把窗口恢复到前台，避免「开了但看不见」。"""
        if self.driver is not None:
            try:
                self.driver.switch_to.window(self.driver.current_window_handle)
            except Exception:  # noqa: BLE001
                pass
            return
        try:
            client = self._connect()
        except Exception:  # noqa: BLE001
            return
        try:
            client.call("Page.bringToFront")
        except Exception:  # noqa: BLE001
            pass
        try:
            info = client.call("Browser.getWindowForTarget") or {}
            window_id = info.get("windowId")
            if window_id is not None:
                client.call("Browser.setWindowBounds",
                            {"windowId": window_id, "bounds": {"windowState": "normal"}})
        except Exception:  # noqa: BLE001
            pass

    def get(self, url: str) -> None:
        if self.driver is not None:
            self.driver.get(url)
            return
        self.initial_url = url
        client = self._connect()
        try:
            client.call("Page.navigate", {"url": url})
        except Exception:  # noqa: BLE001 - 标签页可能已经没了，换一个再试
            self.reset()
            client = self._connect()
            client.call("Page.navigate", {"url": url})

    def execute_script(self, script: str, timeout: float = 20.0):
        if self.driver is not None:
            return self.driver.execute_script(script)
        client = self._connect()
        wrapper = "(function(){\n" + script + "\n})()"
        result = client.evaluate(wrapper, timeout=timeout)
        if not isinstance(result, dict):
            return None
        details = result.get("exceptionDetails")
        if details:
            text = ""
            if isinstance(details, dict):
                exc = details.get("exception") or {}
                text = str(exc.get("description") or details.get("text") or "")
            raise ScriptError(_first_line(text) or "页面脚本执行出错")
        return (result.get("result") or {}).get("value")

    def is_alive(self) -> bool:
        if self.driver is not None:
            try:
                _ = self.driver.window_handles
                return True
            except Exception:  # noqa: BLE001
                return False
        if not self.port:
            return False
        targets = _page_targets(self.port)
        if not targets:
            return False
        if self._target_id and all(str(t.get("id") or "") != self._target_id for t in targets):
            # 原来的标签页没了，但浏览器还在：换一个标签页继续用
            self.reset()
        return True

    def reset(self) -> None:
        """断开调试连接（下次调用会重新找标签页）。"""
        client, self._client = self._client, None
        self._target_id = None
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    def reopen(self, url: str) -> None:
        """窗口/标签页被关掉后重新打开一个。"""
        self.reset()
        self.initial_url = url
        self.get(url)

    def quit(self, force: bool = False) -> None:
        """关闭窗口。CDP 模式下默认**保留**窗口（keep_open）。"""
        if self.driver is not None:
            _safe_quit_driver(self.driver, self.logger)
            return
        if not force and self.keep_open:
            return
        client = self._client
        if client is not None:
            try:
                client.call("Browser.close")
            except Exception:  # noqa: BLE001
                pass
        self.reset()
        if self.port and not _wait_port_gone(self.port, 4.0):
            # Edge 的"启动增强"等后台进程可能还占着端口，兜底终止它
            _terminate_launched(self.port, self.logger)

    def close_connection(self) -> None:
        self.reset()


def _safe_quit_driver(driver, logger, timeout: float = 10.0) -> None:
    """带超时的 driver.quit()：msedgedriver 卡死时不能把整个程序拖住。"""
    done = threading.Event()

    def worker() -> None:
        try:
            driver.quit()
        except Exception as exc:  # noqa: BLE001
            logger.debug("关闭浏览器失败：%s", _first_line(exc))
        finally:
            done.set()

    thread = threading.Thread(target=worker, name="jit-quit", daemon=True)
    thread.start()
    if not done.wait(timeout):
        logger.warning("关闭浏览器超时（%.0f 秒），不再等待", timeout)


# --------------------------------------------------------------------------- #
# 页面脚本
# --------------------------------------------------------------------------- #
LOGIN_JS_TEMPLATE = r"""
(function () {
  const cfg = __JIT_CFG__;
  const norm = s => (s || '').replace(/\s+/g, '').toLowerCase();
  const isVisible = el => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const st = window.getComputedStyle(el);
    return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none';
  };
  const setValue = el => {
    if (!el) return false;
    el.focus();
    const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement : HTMLInputElement;
    const setter = Object.getOwnPropertyDescriptor(proto.prototype, 'value').set;
    setter.call(el, el.__jitValue);
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
    el.dispatchEvent(new Event('keyup', {bubbles: true}));
    el.dispatchEvent(new Event('blur', {bubbles: true}));
    return true;
  };

  const inputs = Array.from(document.querySelectorAll('input, textarea')).filter(isVisible);
  const TEXT_TYPES = ['text', 'email', 'tel', 'search', 'number', 'url', '']; // '' = 无 type 属性

  // ---------- 用户名 ----------
  const userHints = ['username', 'user', 'account', 'acct', 'uname', 'userid', 'login', 'name', '账号', '用户名', '0'];
  const passHints = ['password', 'passwd', 'pwd', 'pass', '密码', '1'];
  const ispHints  = ['isp', 'operator', 'service', 'carrier', 'nettype', 'net', '运营商'];

  const pick = (hints, types) => {
    let best = null, bestScore = 0;
    for (const el of inputs) {
      if (types && !types.includes((el.type || 'text').toLowerCase())) continue;
      const key = norm(el.name + ' ' + el.id + ' ' + (el.placeholder || ''));
      let score = 0;
      hints.forEach((h, i) => {
        const hh = norm(h);
        if (key === hh) score = Math.max(score, 100 - i);
        else if (key.includes(hh)) score = Math.max(score, 60 - i);
      });
      if (score > bestScore) { bestScore = score; best = el; }
    }
    return best;
  };

  // 与 pick 相同的打分，但没有名字线索时返回分数最高者，保证一定有结果
  const pickAny = (hints, types) => {
    let best = null, bestScore = 0;
    for (const el of inputs) {
      if (types && !types.includes((el.type || 'text').toLowerCase())) continue;
      const key = norm(el.name + ' ' + el.id + ' ' + (el.placeholder || ''));
      let score = 0;
      hints.forEach((h, i) => {
        const hh = norm(h);
        if (key === hh) score = Math.max(score, 100 - i);
        else if (key.includes(hh)) score = Math.max(score, 60 - i);
      });
      if (score > bestScore) { bestScore = score; best = el; }
      else if (!best) { best = el; }
    }
    return best;
  };

  const userEl0 = pickAny(userHints, TEXT_TYPES);
  let passEl = pickAny(passHints, ['password']);

  // 兜底一：整页第一个可见密码框
  if (!passEl) {
    const pwds = inputs.filter(el => (el.type || '').toLowerCase() === 'password');
    if (pwds.length) passEl = pwds[0];
  }

  // 兜底二：用户名框与密码框通常相邻（取密码框之前的最后一个文本框）
  let userEl = userEl0;
  if (!userEl && passEl) {
    const texts = inputs.filter(el => TEXT_TYPES.includes((el.type || 'text').toLowerCase()));
    const idx = texts.indexOf(passEl);
    const before = idx > 0 ? texts.slice(0, idx) : texts;
    if (before.length) userEl = before[before.length - 1];
  }

  // 兜底三：密码框所在表单里的第一个可见文本框
  if (passEl && !userEl && passEl.form) {
    const inForm = Array.from(passEl.form.querySelectorAll('input'))
      .filter(isVisible)
      .filter(el => el !== passEl)
      .filter(el => TEXT_TYPES.includes((el.type || 'text').toLowerCase()));
    if (inForm.length) userEl = inForm[0];
  }

  // 兜底四：整页第一个可见文本框（密码框都没有时）
  if (!userEl && !passEl) {
    const texts = inputs.filter(el => TEXT_TYPES.includes((el.type || 'text').toLowerCase()));
    if (texts.length) userEl = texts[0];
  }

  const ispEl  = pick(ispHints, ['text', 'select-one', '']);

  const done = {user: false, pass: false, isp: null, submits: []};

  if (userEl) { userEl.__jitValue = cfg.username; done.user = setValue(userEl); }
  if (passEl) { passEl.__jitValue = cfg.password; done.pass = setValue(passEl); }

  // ---------- 运营商 ----------
  const ispAliases = cfg.ispAliases || [];
  const matchIsp = (label, value) => {
    const l = norm(label), v = norm(value);
    return ispAliases.some(a => {
      const aa = norm(a);
      if (!aa) return false;
      return l === aa || v === aa || (l && l.includes(aa));
    });
  };

  // select 下拉：优先字段名像 isp/运营商，否则看选项里是否有运营商名称
  for (const sel of Array.from(document.querySelectorAll('select')).filter(isVisible)) {
    const key = norm(sel.name + ' ' + sel.id);
    const nameLooksIsp = ispHints.some(h => key.includes(norm(h)));
    let matched = null;
    for (const opt of Array.from(sel.options)) {
      if (matchIsp(opt.textContent, opt.value)) { matched = opt; break; }
    }
    if (!matched && !nameLooksIsp) continue;
    if (matched) {
      sel.value = matched.value;
      sel.dispatchEvent(new Event('change', {bubbles: true}));
      done.isp = matched.textContent.trim() + '=' + matched.value;
      break;
    }
  }

  // radio / 标签点击
  if (!done.isp) {
    for (const radio of Array.from(document.querySelectorAll('input[type=radio]')).filter(isVisible)) {
      const label = radio.closest('label');
      const text = (label ? label.textContent : '') + ' ' + (radio.value || '') + ' ' + (radio.id || '');
      if (matchIsp(text, radio.value)) {
        radio.click();
        done.isp = (text.trim().slice(0, 30)) + '=' + radio.value;
        break;
      }
    }
  }

  // ---------- 是否自动提交 ----------
  if (cfg.submit === false) {
    return {
      user: done.user, pass: done.pass, isp: done.isp, clicked: false, submitted: false,
      userField: userEl ? (userEl.name || userEl.id) : null,
      passField: passEl ? (passEl.name || passEl.id) : null,
      forms: document.forms.length,
      title: document.title
    };
  }

  // ---------- 找到登录按钮 ----------
  const submitHints = ['login', 'logon', 'submit', 'ok', '登录', '登陆', '连接', '认证'];
  const clickable = Array.from(document.querySelectorAll(
    'button, input[type=submit], input[type=button], input[type=image], a'
  )).filter(isVisible);
  let buttons = clickable.filter(el => {
    const key = norm((el.value || '') + ' ' + (el.textContent || '') + ' ' + (el.id || '') + ' ' + (el.name || ''));
    return submitHints.some(h => key.includes(norm(h)));
  });
  if (!buttons.length) {
    const form = (passEl && passEl.form) || (userEl && userEl.form) || null;
    if (form) {
      buttons = Array.from(form.querySelectorAll('button, input[type=submit], input[type=button], input[type=image]'))
        .filter(isVisible);
    }
  }

  // 有些门户用 <a> 或 onclick 触发登录
  let clicked = false;
  const targetForm = (passEl && passEl.form) || (userEl && userEl.form) || null;
  for (const el of buttons) {
    try { el.click(); clicked = true; break; } catch (e) { /* 继续尝试下一个 */ }
  }
  if (!clicked && targetForm) {
    // 优先 requestSubmit：会正常触发 onsubmit 校验
    if (typeof targetForm.requestSubmit === 'function') {
      const explicit = buttons.find(b => b.tagName !== 'A');
      try { targetForm.requestSubmit(explicit || undefined); clicked = true; }
      catch (e) {
        try { targetForm.requestSubmit(); clicked = true; }
        catch (e2) { targetForm.submit(); clicked = true; }
      }
    } else {
      targetForm.submit();
      clicked = true;
    }
  }

  return {
    user: done.user, pass: done.pass, isp: done.isp, clicked: clicked, submitted: true,
    userField: userEl ? (userEl.name || userEl.id) : null,
    passField: passEl ? (passEl.name || passEl.id) : null,
    forms: document.forms.length,
    title: document.title
  };
})()
"""

READBACK_JS = r"""
return (function () {
  const inputs = Array.from(document.querySelectorAll('input'));
  const pwd = inputs.filter(i => (i.type || '').toLowerCase() === 'password')[0] || null;
  const texts = inputs.filter(i => ['text','email','tel','search','','number'].includes((i.type || 'text').toLowerCase()));
  let user = null;
  if (pwd) {
    const i = texts.indexOf(pwd);
    user = i > 0 ? texts[i - 1] : (texts[0] || null);
  } else {
    user = texts[0] || null;
  }
  return {
    userFilled: !!(user && user.value),
    passFilled: !!(pwd && pwd.value),
    userLen: user ? user.value.length : 0,
    passLen: pwd ? pwd.value.length : 0
  };
})()
"""


def build_login_js(payload: dict) -> str:
    """把配置注入到登录脚本里（避免依赖 arguments 传递）。"""
    literal = json.dumps(payload, ensure_ascii=False)
    return LOGIN_JS_TEMPLATE.replace("__JIT_CFG__", literal)


#: 页面状态脚本（Selenium 风格：自带 return；CDP 走同一个包装）
INFO_JS = r"""
return (function () {
  let html = '';
  try { html = document.documentElement ? document.documentElement.outerHTML : ''; } catch (e) {}
  const body = document.body ? document.body.innerText : '';
  const uid = /uid\s*=\s*'([^']*)'/.exec(html);
  return {
    url: location.href,
    title: document.title,
    ready: document.readyState,
    hasPassword: !!document.querySelector('input[type=password]'),
    online_hint: !!(uid && uid[1]),
    net_ok: (typeof window.__jitNetOk === 'boolean') ? window.__jitNetOk : null,
    snippet: (body || '').replace(/\s+/g, ' ').slice(0, 1200)
  };
})();
"""

#: Selenium 版探针：execute_async_script 用回调（注意脚本是「函数体」，
#: 这里的 arguments 就是 Selenium 传进来的参数，最后一个是回调）
PROBE_JS_TEMPLATE = r"""
const url = __JIT_PROBE__;
const done = arguments[arguments.length - 1];
if (!url) {
  window.__jitNetOk = false;
  done({net_ok: false});
} else if (window.__jitProbeDone) {
  done({net_ok: window.__jitNetOk});
} else {
  const finish = (ok) => { window.__jitProbeDone = true; window.__jitNetOk = ok; done({net_ok: ok}); };
  const img = new Image();
  const timer = setTimeout(() => finish(false), 4000);
  img.onload = () => { clearTimeout(timer); finish(true); };
  img.onerror = () => { clearTimeout(timer); finish(false); };
  img.src = url + (url.includes('?') ? '&' : '?') + 'jit=' + Date.now();
}
"""

#: CDP 版探针：返回 Promise，靠 awaitPromise 等结果
PROBE_PROMISE_TEMPLATE = r"""
return (function () {
  const url = __JIT_PROBE__;
  if (!url) { window.__jitNetOk = false; return Promise.resolve(false); }
  return new Promise(function (resolve) {
    let done = false;
    const finish = function (ok) {
      if (done) return;
      done = true;
      window.__jitNetOk = ok;
      resolve(ok);
    };
    const img = new Image();
    const timer = setTimeout(function () { finish(false); }, 4000);
    img.onload = function () { clearTimeout(timer); finish(true); };
    img.onerror = function () { clearTimeout(timer); finish(false); };
    img.src = url + (url.indexOf('?') >= 0 ? '&' : '?') + 'jit=' + Date.now();
  });
})();
"""


def build_probe_js(probe_url: str) -> str:
    return PROBE_JS_TEMPLATE.replace("__JIT_PROBE__", json.dumps(probe_url or "", ensure_ascii=False))


def build_probe_promise(probe_url: str) -> str:
    return PROBE_PROMISE_TEMPLATE.replace("__JIT_PROBE__", json.dumps(probe_url or "", ensure_ascii=False))


# 兼容旧引用
VERIFY_JS = INFO_JS


def _is_success(verify: dict | None, cfg: dict) -> tuple[bool, str]:
    if not verify:
        return False, "浏览器未返回页面状态"
    if verify.get("net_ok"):
        return True, "浏览器内探测外网已连通"
    title = (verify.get("title") or "")
    snippet = (verify.get("snippet") or "").lower()
    if verify.get("online_hint") and not verify.get("hasPassword"):
        return True, "页面显示已登录（uid）"
    if re.search(r"密码错误|用户名不存在|已欠费|余额不足|认证失败|登录失败", snippet):
        return False, "门户提示账号或密码错误"
    if re.search(r"认证成功|登录成功|登陆成功|成功|success|注销|logout|已经在线|已在线|欢迎使用", snippet):
        return True, "页面出现成功/已在线提示"
    if not verify.get("hasPassword") and "登录" not in title and "认证" not in title:
        # 登录表单消失且不是登录页，通常表示已进入在线状态
        return True, "登录表单已消失"
    return False, "页面仍停留在登录界面"


def isp_aliases(cfg: dict) -> list[str]:
    """把「中国电信」展开成一批可用于匹配页面文字的别名。"""
    isp = str(cfg["account"].get("isp", ""))
    return [isp] + {
        "中国电信": ["电信", "telecom", "chinanet", "ctcc", "ct"],
        "中国移动": ["移动", "mobile", "cmcc", "cmnet"],
        "中国联通": ["联通", "unicom", "cucc"],
        "校园网": ["校园网", "campus", "校内"],
    }.get(isp, [])


def _login_payload(cfg: dict, submit: bool) -> dict:
    account = cfg["account"]
    return {
        "username": account.get("username", ""),
        "password": account.get("password", ""),
        "ispAliases": isp_aliases(cfg),
        "submit": submit,
    }


def _readback(session, logger) -> dict:
    """回读页面，确认账号密码确实填进去了。"""
    try:
        result = session.execute_script(READBACK_JS)
        return result if isinstance(result, dict) else {}
    except Exception as exc:  # noqa: BLE001
        logger.debug("回读填表结果失败：%s", _first_line(exc))
        return {}


def _as_return(script: str) -> str:
    """把「表达式脚本」包成自带 return 的脚本。

    注意：不能写成 ``"return " + script`` —— 模板字符串以换行开头时，
    JS 的自动分号插入会把 ``return`` 变成空返回（结果永远是 undefined）。
    """
    body = (script or "").strip()
    while body.endswith(";"):
        body = body[:-1].rstrip()
    return "return (" + body + ");"


def refill_form(cfg: dict, session, logger) -> bool:
    """回到登录页并把账号密码重新填好（供手动点击登录）。

    自动提交失败后调用：保证你看到的窗口里表单是填好的状态。
    """
    if session is None:
        return False
    try:
        session.get(cfg["portal"]["url"])
        time.sleep(1.5)
        session.execute_script(build_login_js(_login_payload(cfg, submit=False)))
        readback = _readback(session, logger)
        if readback.get("userFilled") and readback.get("passFilled"):
            logger.info("已重新填好账号密码，请手动点击「登录」")
            return True
        logger.warning("重新填表未完全成功（用户名:%s 密码:%s）",
                       readback.get("userFilled"), readback.get("passFilled"))
        return False
    except Exception as exc:  # noqa: BLE001
        logger.debug("重新填表失败：%s", _first_line(exc))
        return False


# --------------------------------------------------------------------------- #
# 主路径：CDP 直连（不依赖驱动）
# --------------------------------------------------------------------------- #
def _same_page(current: str, wanted: str) -> bool:
    """当前页是不是已经在目标站点上了（用于判断"页面还没打开"）。"""
    if not current or not wanted:
        return False
    if current.startswith("about:") or current.startswith("edge://") or current.startswith("chrome://"):
        return False
    try:
        cur = urllib.parse.urlsplit(current)
        want = urllib.parse.urlsplit(wanted)
    except Exception:  # noqa: BLE001
        return False
    if cur.netloc and want.netloc and cur.netloc != want.netloc:
        return False
    base = want.path.rstrip("/")
    return cur.path.startswith(base) if base else True


def _wait_page_ready(session: BrowserSession, cfg: dict, logger, url: str = "") -> dict | None:
    """等页面出现登录框（或确认门户页上确实没有登录框）。"""
    timeout = max(6.0, _find_timeout(cfg))
    deadline = time.time() + timeout
    last: dict | None = None
    navigated = False
    while time.time() < deadline:
        try:
            info = session.execute_script(INFO_JS)
        except Exception as exc:  # noqa: BLE001
            logger.debug("读取页面状态失败（可能正在跳转）：%s", _first_line(exc))
            info = None
        if isinstance(info, dict):
            last = info
            if info.get("hasPassword") or info.get("online_hint"):
                return info
            current = str(info.get("url") or "")
            on_portal = _same_page(current, url) if url else True
            if not on_portal:
                # 还在空白页/欢迎页上：明确导航到门户，别指望命令行参数
                if not navigated:
                    logger.debug("当前标签页是 %s，正在导航到 %s", current or "空白页", url)
                    navigated = True
                    try:
                        session.get(url)
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("导航到门户失败：%s", _first_line(exc))
                time.sleep(0.5)
                continue
            if str(info.get("ready") or "").lower() == "complete":
                # 门户页加载完了却没有密码框：可能是已登录页，也可能是错误页
                time.sleep(0.8)
                try:
                    again = session.execute_script(INFO_JS)
                except Exception:  # noqa: BLE001
                    again = None
                if isinstance(again, dict):
                    last = again
                return last
        time.sleep(0.5)
    return last


def _fill_form(cfg: dict, session: BrowserSession, logger, attempts: int = 3) -> tuple[bool, dict | None]:
    """填好账号密码并回读确认（不提交）。"""
    script = build_login_js(_login_payload(cfg, submit=False))
    result: dict | None = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            raw = session.execute_script(_as_return(script))
            if isinstance(raw, dict):
                result = raw
        except Exception as exc:  # noqa: BLE001
            logger.debug("填表脚本执行异常（第 %d 次）：%s", attempt, _first_line(exc))
        readback = _readback(session, logger)
        if readback.get("userFilled") and readback.get("passFilled"):
            isp_info = (result or {}).get("isp")
            logger.info("已在浏览器窗口里填好账号密码（用户名 %d 位、密码 %d 位）%s",
                        readback.get("userLen", 0), readback.get("passLen", 0),
                        f"；运营商：{isp_info}" if isp_info else "")
            return True, result
        if attempt < attempts:
            time.sleep(1.0)
    return False, result


def _merge_state(state: dict | None, info: dict | None) -> dict | None:
    if not info:
        return state
    merged = dict(state or {})
    merged.update(info)
    return merged


def _login_with_cdp(cfg: dict, logger, submit: bool) -> tuple[bool, str, BrowserSession | None]:
    portal_url = str(cfg["portal"]["url"])
    port, kind = launch_own_browser(cfg, logger, portal_url)
    session = BrowserSession(kind=kind, logger=logger, port=port,
                             keep_open=not _close_after_login(cfg), initial_url=portal_url)
    session.focus()
    # 明确导航一次：不能指望命令行参数一定生效（有时第一个标签页还是空白页/欢迎页）
    try:
        session.get(portal_url)
    except Exception as exc:  # noqa: BLE001
        logger.debug("导航到门户失败：%s", _first_line(exc))

    info = _wait_page_ready(session, cfg, logger, portal_url)
    if info is None:
        return False, "浏览器窗口开了，但页面一直没就绪", session

    ok, why = _is_success(info, cfg)
    if ok and not info.get("hasPassword"):
        logger.info("门户已经是登录状态（%s），不需要再填表", why)
        return True, why, session

    if not info.get("hasPassword"):
        # 没有密码框，也不是已登录页：把登录页重新打开一次再判断
        logger.debug("当前页面没有密码框（标题：%s），重新打开登录页", info.get("title"))
        try:
            session.get(portal_url)
            time.sleep(1.0)
            info = _wait_page_ready(session, cfg, logger, portal_url) or info
        except Exception as exc:  # noqa: BLE001
            logger.debug("重新打开登录页失败：%s", _first_line(exc))

    filled, fill_result = _fill_form(cfg, session, logger)
    if not submit:
        if filled:
            return True, "已填入账号密码", session
        return False, f"没能填好账号密码（脚本结果：{fill_result}）", session

    if not filled:
        return False, "页面上没找到用户名/密码框，没能自动登录", session

    if (fill_result or {}).get("isp") is None:
        logger.warning("没能在页面上选中运营商「%s」，可能该门户无需选择", isp_aliases(cfg)[0])

    # ---- 提交 ----
    try:
        result = session.execute_script(_as_return(build_login_js(_login_payload(cfg, submit=True))))
        logger.debug("浏览器提交结果：%s", result)
    except Exception as exc:  # noqa: BLE001
        logger.debug("提交脚本执行异常（页面可能已跳转）：%s", _first_line(exc))

    probe = build_probe_promise(str(cfg["portal"].get("probe_url", "")))
    deadline = time.time() + max(6.0, _find_timeout(cfg))
    last: dict | None = None
    while time.time() < deadline:
        time.sleep(1.0)
        if not session.is_alive():
            logger.warning("程序自己的浏览器窗口被关掉了，正在重新打开 ...")
            try:
                port, kind = launch_own_browser(cfg, logger, portal_url)
            except BrowserUnavailable as exc:
                return False, f"窗口被关闭且无法重新打开：{exc}", session
            session.close_connection()
            session = BrowserSession(kind=kind, logger=logger, port=port,
                                     keep_open=not _close_after_login(cfg), initial_url=portal_url)
            session.focus()
            try:
                session.get(portal_url)
            except Exception as exc:  # noqa: BLE001
                logger.debug("重新打开登录页失败：%s", _first_line(exc))
        try:
            net_ok = session.execute_script(probe)
            if net_ok is not None:
                state = dict(last or {})
                state["net_ok"] = bool(net_ok)
                last = state
        except Exception as exc:  # noqa: BLE001
            logger.debug("外网探针未完成：%s", _first_line(exc))
        try:
            info = session.execute_script(INFO_JS)
        except Exception as exc:  # noqa: BLE001
            logger.debug("读取页面状态失败（可能正在跳转）：%s", _first_line(exc))
            continue
        last = _merge_state(last, info if isinstance(info, dict) else None)
        ok, why = _is_success(last, cfg)
        if ok:
            return True, why, session

    ok, why = _is_success(last, cfg)
    if not ok:
        logger.debug("浏览器最终页面状态：%s", last)
        refill_form(cfg, session, logger)
    return ok, why, session


# --------------------------------------------------------------------------- #
# 兜底：Selenium（默认不用；engine=selenium 或 CDP 连窗口都打不开时）
# --------------------------------------------------------------------------- #
def _find_cached_driver(kind: str) -> str:
    """找上一次缓存的 msedgedriver/chromedriver（找到就不用联网下载）。"""
    name = "msedgedriver.exe" if kind == "edge" else "chromedriver.exe"
    pattern = os.path.join(APP_DIR, ".cache", "selenium", "**", name)
    try:
        matches = sorted(_glob.glob(pattern, recursive=True))
    except Exception:  # noqa: BLE001
        return ""
    return matches[-1] if matches else ""


def _build_driver(cfg: dict, kind: str, exe: str, headless: bool, profile_dir: str,
                  profile_name: str, logger):
    """按指定 profile 创建 WebDriver。"""
    from selenium import webdriver
    from selenium.webdriver.edge.options import Options as EdgeOptions
    from selenium.webdriver.edge.service import Service as EdgeService
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    from selenium.webdriver.chrome.service import Service as ChromeService

    opts = EdgeOptions() if kind == "edge" else ChromeOptions()
    opts.binary_location = exe
    opts.add_argument(f"--user-data-dir={profile_dir}")
    if profile_name:
        opts.add_argument(f"--profile-directory={profile_name}")
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-default-browser-check")
    opts.add_argument("--no-service-autorun")
    opts.add_argument("--disable-sync")
    opts.add_argument("--window-size=1200,900")
    opts.add_argument("--lang=zh-CN")
    # 不使用 --disable-extensions：复用 profile 时禁用扩展容易启动异常
    if headless:
        opts.add_argument("--headless=new")
        opts.add_argument("--disable-gpu")
    opts.page_load_strategy = "eager"
    try:
        opts.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
        opts.add_experimental_option("useAutomationExtension", False)
    except Exception:  # noqa: BLE001
        pass

    selenium_cache = os.path.join(APP_DIR, ".cache", "selenium")
    os.makedirs(selenium_cache, exist_ok=True)
    os.environ.setdefault("SE_CACHE_PATH", selenium_cache)
    os.environ.setdefault("SE_AVOID_STATS", "true")

    cached = _find_cached_driver(kind)
    if cached:
        # 直接用本地缓存的驱动，跳过 Selenium Manager 的联网探测（开机时能省 40+ 秒）
        logger.debug("使用本地缓存的驱动：%s", cached)
        service = EdgeService(executable_path=cached) if kind == "edge" else ChromeService(executable_path=cached)
    else:
        service = EdgeService() if kind == "edge" else ChromeService()

    if kind == "edge":
        driver = webdriver.Edge(options=opts, service=service)
    else:
        driver = webdriver.Chrome(options=opts, service=service)

    driver.set_page_load_timeout(float(cfg["browser"].get("page_load_timeout", 25)))
    driver.set_script_timeout(float(cfg["browser"].get("script_timeout", 10)))
    try:
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": ANTI_CLOSE_JS})
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"},
        )
    except Exception:  # noqa: BLE001
        pass
    return driver


def _make_driver(cfg: dict, headless: bool, logger):
    """创建 WebDriver（只用程序自己的 profile，不会回退到你的浏览器）。"""
    ensure_vendor_on_path()
    try:
        import selenium  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        raise BrowserUnavailable(f"Selenium 不可用：{exc}") from exc

    kind, exe = _browser_kind(cfg)
    logger.debug("使用浏览器（Selenium）：%s (%s)", kind, exe)

    errors: list[str] = []
    for profile_dir, profile_name, ptype in _profile_candidates(cfg):
        if ptype == "user" and not os.path.isdir(profile_dir):
            continue
        label = profile_name or os.path.basename(profile_dir)
        try:
            driver = _build_driver(cfg, kind, exe, headless, profile_dir, profile_name, logger)
            if ptype == "user":
                logger.info("已启动浏览器（按配置复用你的浏览器配置：%s）", label)
            else:
                logger.info("已启动程序自己的浏览器窗口（Selenium）")
            return driver
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{ptype}/{label}: {_first_line(exc)}")
            logger.debug("用配置 %s 启动浏览器失败：%s", label, exc)

    detail = "；".join(errors) if errors else "没有可用的配置目录"
    raise BrowserUnavailable(f"启动 {kind} 失败：{detail}")


def _login_with_selenium(cfg: dict, logger, visible: bool | None, submit: bool
                         ) -> tuple[bool, str, BrowserSession | None]:
    if not selenium_available():
        return False, "Selenium 不可用（vendor 目录缺失？）", None

    if visible is None:
        mode = str(cfg["browser"].get("mode", "auto")).lower()
        visible = mode == "visible"

    driver = None
    session: BrowserSession | None = None
    try:
        driver = _make_driver(cfg, headless=not visible, logger=logger)
        session = BrowserSession(kind="selenium", logger=logger, driver=driver,
                                 keep_open=not _close_after_login(cfg),
                                 initial_url=str(cfg["portal"]["url"]))
        portal_url = cfg["portal"]["url"]
        action = "填入账号密码" if not submit else "自动登录"
        logger.info("浏览器打开门户（%s）：%s（%s）",
                    action, portal_url, "可见窗口" if visible else "后台窗口")
        driver.get(portal_url)
        time.sleep(1.5)

        _wait_page_ready(session, cfg, logger, str(portal_url))
        filled, fill_result = _fill_form(cfg, session, logger)

        if not submit:
            if filled:
                return True, "已填入账号密码", session
            return False, f"没能填好账号密码（脚本结果：{fill_result}）", session

        if not filled:
            return False, "页面上没找到用户名/密码框，没能自动登录", session

        try:
            result = driver.execute_script(build_login_js(_login_payload(cfg, submit=True)))
            logger.debug("浏览器提交结果：%s", result)
        except Exception as exc:  # noqa: BLE001
            logger.debug("提交脚本执行异常（页面可能已跳转）：%s", _first_line(exc))

        probe_js = build_probe_js(str(cfg["portal"].get("probe_url", "")))
        deadline = time.time() + max(6.0, _find_timeout(cfg))
        last: dict | None = None
        while time.time() < deadline:
            time.sleep(1.0)
            try:
                if probe_js:
                    driver.execute_async_script(probe_js)
            except Exception as exc:  # noqa: BLE001 - 探针失败不影响主判断
                logger.debug("外网探针未完成：%s", _first_line(exc))
            try:
                probe = session.execute_script(INFO_JS)
            except Exception as exc:  # noqa: BLE001 - 页面跳转中可能短暂报错
                logger.debug("读取页面状态失败（可能正在跳转）：%s", _first_line(exc))
                continue
            last = _merge_state(last, probe if isinstance(probe, dict) else None)
            ok, why = _is_success(last, cfg)
            if ok:
                return True, why, session
        ok, why = _is_success(last, cfg)
        if not ok:
            logger.debug("浏览器最终页面状态：%s", last)
            refill_form(cfg, session, logger)
        return ok, why, session
    except BrowserUnavailable as exc:
        if driver is not None:
            _safe_quit_driver(driver, logger)
        return False, str(exc), None
    except Exception as exc:  # noqa: BLE001
        logger.debug("浏览器登录异常：%s", exc, exc_info=True)
        if driver is not None:
            _safe_quit_driver(driver, logger)
        return False, f"浏览器登录出错：{exc}", None


# --------------------------------------------------------------------------- #
# 对外入口
# --------------------------------------------------------------------------- #
def login_via_browser(cfg: dict, logger, visible: bool | None = None,
                      submit: bool = True) -> tuple[bool, str, BrowserSession | None]:
    """用「程序自己的浏览器窗口」打开门户，填入账号密码；submit=False 时只填表不提交。

    返回 (是否成功, 说明, session)。session 不为 None 时窗口已经打开，
    由调用方决定是否关闭（默认保留）。

    顺序：CDP 直连（默认）→ Selenium（兜底）。**任何情况下都不会启动你的浏览器。**
    """
    engine = str(cfg.get("browser", {}).get("engine", "auto") or "auto").lower()
    errors: list[str] = []

    if engine != "selenium":
        try:
            ok, message, session = _login_with_cdp(cfg, logger, submit=submit)
            if session is not None:
                return ok, message, session
            errors.append(message)
        except BrowserUnavailable as exc:
            errors.append(_first_line(exc))
            logger.warning("CDP 方式打开窗口失败：%s", _first_line(exc))
        except Exception as exc:  # noqa: BLE001
            errors.append(_first_line(exc))
            logger.debug("CDP 方式异常：%s", exc, exc_info=True)

    ok, message, session = _login_with_selenium(cfg, logger, visible=visible, submit=submit)
    if session is not None:
        return ok, message, session
    errors.append(message)
    return False, "；".join([e for e in errors if e]) or "浏览器不可用", None
