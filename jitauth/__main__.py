"""校园网自动登录主程序。

流程：
  1. 检查当前 WiFi 是否已是 JIT-Stu；不是则扫描并连接
     —— 开机后 WiFi/门户可能要过一会儿才就绪，可用 wifi.wait_for_ssid_seconds 等一会儿再说
  2. 判断是否已经能上网，能上网就跳过
  3. 先看门户是不是已经登录了（Dr.COM 注销页），已登录就直接结束，绝不开窗口
  4. 未登录时用**程序自己的浏览器窗口**打开门户 → 填好账号密码 → 自动点登录
     （不会启动、也不会复用你日常在用的浏览器）
  5. 窗口默认保留在认证结果页，方便你确认或手动点一下登录

用法:
  python -m jitauth                 # 正常执行（开机登录一次）
  python -m jitauth --setup         # 账号设置向导（学号/密码/运营商）
  python -m jitauth --silent        # 不弹黑框（计划任务用）
  python -m jitauth --watch         # 常驻保活：掉线自动重连（托盘可看状态）
  python -m jitauth --status        # 查看保活是否在跑、最近状态
  python -m jitauth --browser       # 自动登录时显示浏览器窗口（默认就是可见窗口）
  python -m jitauth --no-login      # 只连 WiFi + 打开登录页，不自动填表
  python -m jitauth --diagnose      # 只抓取门户页面并保存，便于排查
  python -m jitauth --verify-http-login  # 实测纯 HTTP 登录是否还能用（不开窗口）
  python -m jitauth --force         # 忽略"已联网"判断，强制登录一次
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from jitauth import browser as browser_mod  # type: ignore
    from jitauth import config as config_mod  # type: ignore
    from jitauth import keepalive  # type: ignore
    from jitauth import portal  # type: ignore
    from jitauth import setup_ui  # type: ignore
    from jitauth import wifi  # type: ignore
    from jitauth.logging_setup import LOG_DIR, get_logger  # type: ignore
else:
    from . import browser as browser_mod
    from . import config as config_mod
    from . import keepalive
    from . import portal
    from . import setup_ui
    from . import wifi
    from .logging_setup import LOG_DIR, get_logger


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="校园网自动登录（开机认证 + 保活重连）")
    parser.add_argument("--silent", action="store_true", help="不输出到控制台")
    parser.add_argument("--browser", action="store_true", help="自动登录时显示浏览器窗口")
    parser.add_argument("--no-login", action="store_true", help="只连 WiFi + 打开登录页，不自动填表")
    parser.add_argument("--http-only", action="store_true", help="自动登录只用 HTTP 方式，不启动浏览器")
    parser.add_argument("--diagnose", action="store_true", help="抓取门户页面结构用于排查")
    parser.add_argument("--force", action="store_true", help="已联网也强制登录一次")
    parser.add_argument("--config", default=None, help="指定配置文件路径")
    parser.add_argument("--skip-wifi", action="store_true", help="跳过 WiFi 检查直接登录")
    parser.add_argument("--watch", action="store_true",
                        help="常驻保活模式：一直盯着网络，掉线自动重连")
    parser.add_argument("--no-tray", action="store_true",
                        help="保活模式下不显示托盘图标（纯后台）")
    parser.add_argument("--status", action="store_true", help="查看保活运行状态后退出")
    parser.add_argument("--verify-http-login", action="store_true",
                        help="实测纯 HTTP 登录是否可用（不开浏览器窗口）")
    parser.add_argument("--setup", action="store_true",
                        help="账号设置向导（学号/密码/运营商），也可直接双击 run.bat")
    parser.add_argument("--console", action="store_true",
                        help="设置向导用命令行问答，不弹窗口")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    # 计划任务/批处理不方便传参时，可用环境变量指定配置
    cfg_path = args.config or os.environ.get("JIT_CONFIG") or None

    # ---------- 只查状态：不碰网络、秒回 ----------
    if args.status:
        _print_status()
        return 0

    cfg = config_mod.load(cfg_path)
    setup_reason = config_mod.setup_reason(cfg)

    # ---------- 常驻保活模式 ----------
    if args.watch:
        watch_logger = get_logger(quiet=args.silent,
                                  keep_days=int(cfg["log"].get("keep_days", 14)),
                                  filename="keepalive.log")
        watch_logger.info("=" * 58)
        watch_logger.info("校园网保活看门狗启动（%s）", time.strftime("%Y-%m-%d %H:%M:%S"))
        watch_logger.info("运行环境：%s ｜ 工作目录：%s", sys.executable or "?", os.getcwd())
        if setup_reason:
            watch_logger.warning("账号还没配置好（%s）；本次只做心跳保活，不会自动重登。"
                                 "请运行 run.bat 或 python -m jitauth --setup", setup_reason)
            cfg.setdefault("keepalive", {}).setdefault("relogin", {})["enabled"] = False
        return keepalive.run_watch(cfg, watch_logger, config_path=cfg.get("_path"),
                                   use_tray=not args.no_tray)

    logger = get_logger(quiet=args.silent or bool(cfg["behavior"].get("quiet")),
                        keep_days=int(cfg["log"].get("keep_days", 14)))

    started = time.time()
    logger.info("=" * 58)
    logger.info("校园网自动登录启动（%s）", time.strftime("%Y-%m-%d %H:%M:%S"))
    logger.info("运行环境：%s ｜ 工作目录：%s", sys.executable or "?", os.getcwd())

    # ---------- 首次运行 / 设置向导 ----------
    if args.setup or setup_reason:
        if not args.setup and args.silent:
            # 开机任务：没有桌面交互，别弹窗，也别拿空账号去撞门户
            logger.error("账号还没配置好（%s），跳过本次登录。", setup_reason)
            logger.error("请在命令行运行：run.bat --setup（或在托盘菜单点「账号设置」）")
            return 2
        if not args.setup:
            logger.warning("检测到账号还没配置好（%s），先打开设置向导 …", setup_reason)
        rc = setup_ui.run_setup(cfg_path, force_console=args.console)
        if rc != 0:
            logger.warning("设置向导被取消，本次不执行登录")
            return 1
        cfg = config_mod.load(cfg_path)
        setup_reason = config_mod.setup_reason(cfg)
        if setup_reason:
            logger.error("设置仍未完成（%s），本次不执行登录", setup_reason)
            return 2
        logger.info("账号配置完成，继续执行登录流程")

    # ---------- 实测纯 HTTP 登录（诊断用，不做完整流程） ----------
    if args.verify_http_login:
        return _verify_http_login(cfg, logger, args)

    ssid = str(cfg["wifi"].get("ssid", "")).strip()
    portal_url = cfg["portal"]["url"]
    probe_url = str(cfg["portal"].get("probe_url", ""))
    probe_timeout = float(cfg["portal"].get("probe_timeout", 5))

    # ---------- 1. 检测 WiFi 列表里有没有目标网络，有就连接 ----------
    if args.skip_wifi:
        logger.info("按参数要求跳过 WiFi 检查")
    elif not ssid:
        logger.info("没有配置 WiFi 名称（wifi.ssid 为空），跳过 WiFi 检查，直接处理门户认证")
    else:
        ok, reason = _ensure_wifi_with_wait(cfg, ssid, logger)
        if not ok:
            stop = bool(cfg["behavior"].get("stop_if_wifi_missing", True))
            logger.warning("WiFi 条件未满足：%s", reason)
            if stop and not _already_online(cfg, args, probe_url, probe_timeout):
                logger.warning("未检测到 %s，本次不执行（如需强制登录请用 --skip-wifi）", ssid)
                return 0
            if not stop:
                logger.info("配置允许在未连上 %s 时继续", ssid)

    # ---------- 2. 诊断模式 ----------
    if args.diagnose:
        portal.dump_diagnostics(cfg, logger)
        logger.info("诊断完成")
        return 0

    # ---------- 3. 先看是否已经登录（Dr.COM 注销页 = 已登录）----------
    # 这一步在**开浏览器之前**做：已经登录就直接结束，绝不白开一个登录窗口。
    auto_login = bool(cfg["behavior"].get("auto_login", True)) and not args.no_login
    open_portal = bool(cfg["behavior"].get("open_portal", True))
    online = _already_online(cfg, args, probe_url, probe_timeout)
    if online:
        logger.info("网络已连通，无需再次认证")

    already, why = _check_already_logged_in(cfg)
    if already:
        logger.info("门户显示已登录：%s", why)
        logger.info("无需再登录，程序直接结束（不打开任何浏览器窗口）")
        logger.info("完成，总耗时 %.1f 秒", time.time() - started)
        return 0

    problems = config_mod.validate(cfg) if auto_login else []
    if problems:
        for problem in problems:
            logger.error("配置问题：%s（无法自动登录，改为只打开登录页）", problem)
        auto_login = False

    success = False
    message = ""
    handled = False          # 浏览器是否已经接管了"打开登录页"这件事
    skipped_online = online and not args.force
    if auto_login and not skipped_online:
        success, message, handled = _do_login(cfg, logger, args, probe_url, probe_timeout)
    elif skipped_online:
        logger.info("已联网，跳过自动登录（--force 可强制）")

    # ---------- 4. 打开登录页 ----------
    if handled:
        # 浏览器窗口已经打开并停在登录页上，不要再另开一个
        logger.info("登录页已在程序自己的浏览器窗口中打开")
    elif open_portal:
        _open_login_page(cfg, logger)
    else:
        logger.info("配置要求不打开登录网站（behavior.open_portal=false）")

    # ---------- 5. 结果 ----------
    if skipped_online:
        logger.info("完成（已联网，未执行自动登录），总耗时 %.1f 秒", time.time() - started)
        return 0

    if success:
        if probe_url and not portal.internet_ok(probe_url, probe_timeout):
            if bool(cfg["behavior"].get("probe_required", False)):
                logger.warning("外网探测未通过，但门户已提示成功（probe_required=true 时视为失败）")
                success = False
            else:
                logger.info("门户报告成功；外网探测未通过（可能是门户统计延迟或探针被拦截）")
        else:
            logger.info("联网验证通过")
        logger.info("完成，总耗时 %.1f 秒", time.time() - started)
        return 0

    if auto_login:
        logger.warning("自动登录未成功：%s", message)
        if handled:
            logger.warning("账号密码已填在程序自己的浏览器窗口里，点一下「登录」按钮即可")
        else:
            logger.warning("登录页没能打开，可手动运行 run.bat 或访问 %s", portal_url)
        logger.warning("也可运行 'python -m jitauth --diagnose' 抓取页面结构排查")
        logger.info("完成，总耗时 %.1f 秒", time.time() - started)
        return 1

    logger.info("完成（未执行自动登录），总耗时 %.1f 秒", time.time() - started)
    return 0


# --------------------------------------------------------------------------- #
# WiFi
# --------------------------------------------------------------------------- #
def _ensure_wifi_with_wait(cfg: dict, ssid: str, logger) -> tuple[bool, str]:
    """检查/连接 WiFi；开机初期 WiFi 或网卡还没就绪时，按配置再等一会儿重试。"""
    kwargs = dict(
        timeout=int(cfg["wifi"].get("connect_timeout", 25)),
        require_visible=bool(cfg["wifi"].get("require_visible", True)),
        connect_if_scan_unavailable=bool(cfg["wifi"].get("connect_if_scan_unavailable", False)),
        enable_radio_if_off=bool(cfg["wifi"].get("enable_radio_if_off", True)),
        enable_wait=float(cfg["wifi"].get("enable_wait", 3)),
        logger=logger,
    )
    budget = float(cfg["wifi"].get("wait_for_ssid_seconds", 0) or 0)
    poll = max(3.0, float(cfg["wifi"].get("wait_poll_seconds", 10) or 10))
    deadline = time.time() + max(0.0, budget)

    while True:
        ok, reason = wifi.ensure_connected(ssid, **kwargs)
        if ok or time.time() >= deadline:
            return ok, reason
        logger.info("还没就绪（%s），%.0f 秒后再试一次（开机后网络可能需要一点时间）", reason, poll)
        time.sleep(poll)


def _already_online(cfg: dict, args, probe_url: str, probe_timeout: float) -> bool:
    if args.force:
        return False
    if not bool(cfg["behavior"].get("skip_if_already_online", True)):
        return False
    if not probe_url:
        return False
    return portal.internet_ok(probe_url, probe_timeout)


# --------------------------------------------------------------------------- #
# 门户在线状态
# --------------------------------------------------------------------------- #
def _check_already_logged_in(cfg: dict) -> tuple[bool, str]:
    """访问门户首页，判断是否已经处于登录状态（带重试）。

    Dr.COM 已登录时给的是「注销页」，见 portal.drcom_online_state()。
    只要确认已登录就返回 True，调用方应直接结束、不开浏览器。

    开机时门户可能还没通（WiFi 刚连上），单次失败就下结论会误判成"需要登录"，
    所以按 portal.check_attempts / check_interval 重试几次。
    """
    url = cfg["portal"]["url"]
    timeout = float(cfg["portal"].get("timeout", 8))
    attempts = max(1, int(cfg["portal"].get("check_attempts", 3) or 1))
    interval = max(0.0, float(cfg["portal"].get("check_interval", 3) or 0))
    logger = logging.getLogger("jitauth")

    last_reason = "门户不可达，无法判断在线状态"
    for attempt in range(1, attempts + 1):
        try:
            sess = portal.Session(timeout=timeout)
            status, html, _ = sess.request(url, timeout=timeout)
        except portal.PortalError as exc:
            last_reason = f"门户不可达，无法判断在线状态（{exc}）"
        else:
            ok, why = portal.judge(html, status)
            if ok:
                return True, why
            if "未给出明确结果" in why or "未显示已登录" in why:
                # 页面没给出明确结论，可能是在跳转，再试一次
                last_reason = why
            else:
                return False, why
        if attempt < attempts:
            logger.debug("第 %d 次判断在线状态未果（%s），%.0f 秒后重试", attempt, last_reason, interval)
            time.sleep(interval)
    return False, last_reason


# --------------------------------------------------------------------------- #
# 浏览器
# --------------------------------------------------------------------------- #
def _open_login_page(cfg: dict, logger) -> None:
    """用**程序自己的**浏览器窗口打开登录网站。

    注意：这里不会调用系统默认浏览器，也不会碰你日常在用的浏览器。
    """
    url = cfg["portal"]["url"]
    if str(cfg["browser"].get("mode", "auto")).lower() == "off":
        logger.info("配置 browser.mode=off，不打开浏览器窗口")
        return
    try:
        port, kind = browser_mod.launch_own_browser(cfg, logger, url)
    except browser_mod.BrowserUnavailable as exc:
        logger.warning("没能打开程序自己的浏览器窗口：%s",
                       str(exc).splitlines()[0] if str(exc) else exc)
        return
    except Exception as exc:  # noqa: BLE001
        logger.warning("打开浏览器窗口出错：%s", exc)
        return
    session = browser_mod.BrowserSession(kind=kind, logger=logger, port=port,
                                         keep_open=True, initial_url=url)
    session.focus()
    logger.info("已用程序自己的浏览器窗口打开登录网站：%s（%s，调试端口 %d）", url, kind, port)


def _do_login(cfg: dict, logger, args,
              probe_url: str, probe_timeout: float) -> tuple[bool, str, bool]:
    """执行自动登录，返回 (是否成功, 说明, 浏览器是否已接管打开登录页)。

    主路径是用程序自己的浏览器窗口打开登录页并自动提交；窗口起不来时退回纯 HTTP。
    """
    retry_times = max(1, int(cfg["behavior"].get("retry_times", 3)))
    retry_interval = float(cfg["behavior"].get("retry_interval", 8))
    success = False
    message = ""
    handled = False        # 浏览器是否已在页面上操作过
    session = None

    for attempt in range(1, retry_times + 1):
        logger.info("---- 第 %d/%d 次自动登录尝试 ----", attempt, retry_times)
        if session is not None:
            # 只断开上一次的调试连接，窗口留着（下次会自动复用同一个窗口）
            try:
                session.close_connection()
            except Exception:  # noqa: BLE001
                pass

        # a. 浏览器主路径：打开登录页 → 填好账号密码 → 点击登录
        if not args.http_only:
            session, ok, message, handled = _browser_login(cfg, logger, args)
            if ok:
                logger.info("浏览器登录成功：%s", message)
                success = True
                break
            logger.warning("浏览器登录未成功：%s", message)
        else:
            logger.info("按参数要求不使用浏览器")

        # b. HTTP 兜底
        ok, http_message = portal.login_via_http(cfg, logger)
        if ok:
            logger.info("HTTP 登录成功：%s", http_message)
            success = True
            message = http_message
            break
        logger.warning("HTTP 登录未成功：%s", http_message)
        if not message:
            message = http_message
        if "账号/密码问题" in http_message:
            logger.error("疑似账号或密码错误，停止重试以免账号被锁")
            break

        # c. 复查是否其实已经通了
        if probe_url and portal.internet_ok(probe_url, probe_timeout):
            success = True
            message = "复查确认网络已连通"
            logger.info(message)
            break

        if attempt < retry_times:
            logger.info("%.0f 秒后重试 ...", retry_interval)
            time.sleep(retry_interval)

    _keep_driver_open(session, cfg, logger)
    return success, message, handled


def _browser_login(cfg: dict, logger, args) -> tuple[object | None, bool, str, bool]:
    """用**程序自己的浏览器窗口**打开登录页并自动登录。

    返回 (session, 是否成功, 说明, 是否已接管页面)。
    * 只用程序自己的窗口；窗口起不来时**不会**改用你的浏览器（退回 HTTP 即可）。
    * 只要窗口成功打开过页面（即使登录没成功），"是否已接管页面"就为 True：
      此时账号密码已经填好，窗口要留着让你手动点登录，也不该再另开一个登录页。
    """
    mode = str(cfg["browser"].get("mode", "auto")).lower()
    if mode == "off" and not args.browser:
        return None, False, "配置已关闭浏览器方式", False

    # 流程是"打开登录页 → 填好 → 自动点登录"，所以用可见窗口
    force_visible = args.browser or mode in ("visible", "auto")
    ok, message, session, *extra = browser_mod.login_via_browser(
        cfg, logger, visible=force_visible)
    if ok:
        return session, True, message, True

    if session is not None:
        # 浏览器起来了但没登录成功：把表单重新填好，留给你手动点
        browser_mod.refill_form(cfg, session, logger)
        return session, False, message, True

    logger.warning("程序自己的浏览器窗口无法启动：%s", message)
    return None, False, message, False


def _keep_driver_open(session, cfg: dict, logger) -> None:
    """登录结束后怎么处理程序自己的浏览器窗口。

    默认**保留**窗口（你能看到它，需要时手动点「登录」）。
    只有 browser.close_after_login=true，或 keep_open_seconds>0 时才关掉。
    """
    if session is None:
        return
    close_after = bool(cfg["browser"].get("close_after_login", False))
    keep = int(cfg["browser"].get("keep_open_seconds", 0) or 0)
    if keep > 0:
        logger.info("保持自动化浏览器窗口 %d 秒", keep)
        time.sleep(keep)
    if close_after or keep > 0:
        try:
            session.quit(force=True)
        except Exception as exc:  # noqa: BLE001
            logger.debug("关闭浏览器窗口失败：%s", exc)
        logger.info("已关闭程序自己的浏览器窗口")
    else:
        logger.info("浏览器窗口保留（可以在窗口里确认结果，或手动点「登录」）")


# --------------------------------------------------------------------------- #
# 保活：状态查询 / 纯 HTTP 登录实测
# --------------------------------------------------------------------------- #
def _print_status() -> None:
    """打印保活状态（--status）。只读文件，不碰网络。"""
    if sys.stdout is None:
        return
    print(keepalive.format_status(keepalive.read_state()))
    path = os.path.join(LOG_DIR, "keepalive.log")
    if os.path.exists(path):
        print("")
        print("--- logs\\keepalive.log 最后 12 行 ---")
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh.readlines()[-12:]:
                    print("  " + line.rstrip())
        except OSError:
            pass


def _verify_http_login(cfg: dict, logger, args) -> int:
    """实测"纯 HTTP 静默登录"在本校门户上还能不能用。

    保活的核心就是这条路：它不开任何窗口。所以先在真实环境验证一次，
    以后掉线时才敢放心地静默重登。

    安全默认：当前网络是通的就**不测**（避免把你在用的会话踢掉），
    要强制测必须显式加 --force。
    """
    endpoints = keepalive.build_endpoints(cfg)
    timeout = float(cfg["portal"].get("probe_timeout", 5))
    logger.info("正在探测当前网络 ...")
    first = keepalive.probe(endpoints, timeout)
    logger.info("探针结果：%s", first.summary())

    if first.online and not args.force:
        logger.warning("当前网络是通的（会话还在），为避免把你正在用的连接踢掉，本次不做登录测试。")
        logger.warning("想测就先去门户点「注销」（或断开 WiFi 再连上），然后重新运行：")
        logger.warning("    python -m jitauth --verify-http-login")
        logger.warning("确实要强制测（会短暂断网）：加 --force")
        return 0

    pstate, why = keepalive.classify_portal(cfg, logger)
    logger.info("门户判定：%s（%s）", pstate, why)

    logger.info("开始纯 HTTP 登录（不开浏览器窗口）...")
    ok, message = portal.login_via_http(cfg, logger)
    logger.info("HTTP 登录结果：%s（%s）", "成功" if ok else "失败", message)
    if not ok:
        logger.error("HTTP 登录不可用；保活只能退回浏览器方式（会开窗口），请把结论反馈出来")
        return 1

    after = keepalive.probe(endpoints, timeout)
    logger.info("复查探针：%s", after.summary())
    if after.online:
        logger.info("结论：纯 HTTP 登录可用，保活可以全静默运行")
        return 0
    logger.warning("结论：接口说登录成功，但外网仍不通（可能是出口/统计延迟）")
    return 2


if __name__ == "__main__":
    sys.exit(main())
