"""judge() 判定逻辑测试 —— 用真实抓到的门户页面防止"假成功"回归。

用法: python tests/test_judge.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from jitauth import portal  # noqa: E402

failures: list[str] = []

REAL_LOGOUT_PAGE = os.path.join(ROOT, "logs", "real_01_before.html")
REAL_LOGIN_PAGE = os.path.join(ROOT, "logs", "real_03_login_page.html")


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        failures.append(f"{name}: {detail}")


# Dr.COM 注销页：标题是"注销页"，脚本里有 authlogoutpath，但页面其实已登录
DRCOM_LOGOUT_HTML = """<!DOCTYPE html><html><head><meta charset="gb2312">
<title>注销页</title></head><body>
<script>
uid='20260001@telecom';pwd='';v4ip='172.18.84.162';ispid=0;
authsuccess='Dr.COMWebLoginID_3.htm';authfail='Dr.COMWebLoginID_2.htm';
authlogoutpath='/eportal/?c=ACSetting&a=Logout&ver=1.0';
</script>
</body><script>page.run(1);</script></html>"""

# 明文写在页面上的成功提示
PLAIN_SUCCESS_HTML = "<html><head><title>结果</title></head><body>认证成功，您已在线。</body></html>"
PLAIN_FAIL_HTML = "<html><body>认证失败：用户名或密码错误，请重新输入。</body></html>"
BOTH_HTML = "<html><body><h2>认证成功</h2><p>上次登录失败记录已清除</p></body></html>"


def main() -> int:
    print("== Dr.COM 注销页 = 已登录（按用户确认的口径）==")
    ok, why = portal.judge(DRCOM_LOGOUT_HTML, 200)
    check("注销页判定为已登录", ok is True, f"{ok}/{why!r}")
    check("说明里带上账号", "20260001" in why, why)

    if os.path.exists(REAL_LOGOUT_PAGE):
        html = open(REAL_LOGOUT_PAGE, encoding="utf-8", errors="replace").read()
        ok, why = portal.judge(html, 200)
        check("真实注销页判定为已登录", ok is True, f"{ok}/{why!r}")
        print(f"    （真实页面判定：{ok} / {why}）")
        st = portal.drcom_online_state(html)
        check("drcom_online_state 同样识别为已登录", bool(st and st[0]), str(st))
    else:
        print("  (没有真实页面样本，跳过)")

    print("\n== 非 Dr.COM 页面不被误判 ==")
    ok, why = portal.judge("<html><body>注销</body></html>", 200)
    check("普通页面出现「注销」不算成功", ok is False, f"{ok}/{why!r}")

    print("\n== 明确的成功/失败提示 ==")
    ok, why = portal.judge(PLAIN_SUCCESS_HTML, 200)
    check("认证成功 → 成功", ok is True, f"{ok}/{why!r}")
    ok, why = portal.judge(PLAIN_FAIL_HTML, 200)
    check("用户名或密码错误 → 失败", ok is False, f"{ok}/{why!r}")
    ok, why = portal.judge(BOTH_HTML, 200)
    check("成功与失败同时出现时不误报成功", ok is False, f"{ok}/{why!r}")

    print("\n== 其它边界 ==")
    ok, why = portal.judge("<html><body></body></html>", 200)
    check("空页面 → 失败（不猜成功）", ok is False, f"{ok}/{why!r}")
    ok, why = portal.judge("<html><body>什么也没有</body></html>", 500)
    check("HTTP 500 → 失败", ok is False, f"{ok}/{why!r}")
    ok, why = portal.judge("<html><body>密码错误，请重试</body></html>", 200)
    check("密码错误 → 失败且标记为账号问题", ok is False and "密码错误" in why, f"{ok}/{why!r}")
    ok, why = portal.judge("<html><body>您已在线，欢迎使用</body></html>", 200)
    check("已在线 → 成功", ok is True, f"{ok}/{why!r}")

    print("\n" + ("全部通过" if not failures else f"失败 {len(failures)} 项："))
    for f in failures:
        print("  -", f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
