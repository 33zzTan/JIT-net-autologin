"""门户表单解析与运营商匹配的回归测试（离线，不需要浏览器）。

用法: python tests/test_form_parse.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from jitauth import portal  # noqa: E402

ACCOUNT = {"username": "20260001", "password": "testpass123", "isp": "中国电信"}

CASES = {
    # 每个 label 包住自己的 radio
    "radio-label": (
        """<form method="post"><input name="userName"><input name="passWord" type="password">
<label><input type="radio" name="netType" value="1">校园网</label>
<label><input type="radio" name="netType" value="2">中国电信</label>
<label><input type="radio" name="netType" value="3">中国移动</label>
<input type="submit"></form>""",
        "netType", "2",
    ),
    # radio 后面的 span 是说明
    "radio-span": (
        """<form method="post"><input name="user"><input type="password" name="pwd">
<input type="radio" name="sel" value="a"><span>校园网</span>
<input type="radio" name="sel" value="b"><span>中国电信</span>
<input type="radio" name="sel" value="c"><span>中国移动</span>
<input type="submit"></form>""",
        "sel", "b",
    ),
    # 表格布局，radio 和文字分在不同 td
    "radio-table": (
        """<form method="post"><input name="u"><input type="password" name="p"><table>
<tr><td><input type="radio" name="ispid" value="0"></td><td>校园网</td></tr>
<tr><td><input type="radio" name="ispid" value="1"></td><td>中国电信</td></tr>
<tr><td><input type="radio" name="ispid" value="2"></td><td>中国移动</td></tr>
</table><input type="submit"></form>""",
        "ispid", "1",
    ),
    # Dr.COM：select 值与运营商名称不一致，需要按 option 文本匹配
    "drcom-select": (
        """<form method="post"><input name="0"><input type="password" name="1">
<select name="2"><option value="0">校园网</option><option value="1" selected>中国电信</option>
<option value="2">中国移动</option></select><input type="submit" name="3" value="登录"></form>""",
        "2", "1",
    ),
    # Dr.COM：纯数字隐藏字段
    "drcom-hidden": (
        """<form method="post"><input name="0"><input type="password" name="1">
<input type="hidden" name="2" value="0"><input type="submit" name="3" value="Login"></form>""",
        "2", "1",
    ),
    # 普通下拉
    "select-isp": (
        """<form action="/login.do" method="post"><input name="username">
<input type="password" name="password">
<select name="isp"><option value="ct">中国电信</option><option value="cu">中国联通</option></select>
<button>登 录</button></form>""",
        "isp", "ct",
    ),
    # 字段名完全没有 isp 线索
    "opaque-name": (
        """<form method="post"><input name="u"><input type="password" name="p">
<input type="radio" name="carrier" value="2" checked><span>中国电信</span>
<input type="radio" name="carrier" value="3"><span>中国联通</span><input type="submit"></form>""",
        "carrier", "2",
    ),
}

CREDENTIAL_CASES = {
    "named": """<form method="post"><input name="username"><input type="password" name="password">
        <input type="submit"></form>""",
    "short": """<form method="post"><input name="u"><input type="password" name="p"><input type="submit"></form>""",
    "id-only": """<form method="post"><input id="account" type="text"><input id="pwd" type="password">
        <input type="submit"></form>""",
    "chinese": """<form method="post"><input name="账号"><input type="password" name="密码">
        <input type="submit"></form>""",
    "email-type": """<form method="post"><input name="loginName" type="email">
        <input name="loginPwd" type="password"><button>登录</button></form>""",
}

failures: list[str] = []


def main() -> int:
    print("== 运营商字段识别 ==")
    for name, (html, want_field, want_value) in CASES.items():
        form = portal.pick_form(html)
        if form is None:
            failures.append(f"{name}: 没解析出表单")
            print(f"  FAIL {name}: 没解析出表单")
            continue
        field, value = portal.find_isp_field(form, ACCOUNT["isp"], set())
        got_field = field.name if field else None
        ok = got_field == want_field and value == want_value
        if not ok:
            failures.append(f"{name}: 得到 {got_field}={value}，期望 {want_field}={want_value}")
        print(f"  {'OK  ' if ok else 'FAIL'} {name:14s} {got_field} = {value}")

    print("\n== 账号/密码字段识别 ==")
    for name, html in CREDENTIAL_CASES.items():
        form = portal.pick_form(html)
        payload, notes = portal.build_payload(form, html, ACCOUNT)
        got_user = any(v == ACCOUNT["username"] for k, v in payload.items())
        got_pass = any(v == ACCOUNT["password"] for k, v in payload.items())
        ok = got_user and got_pass
        if not ok:
            failures.append(f"{name}: payload={payload}")
        print(f"  {'OK  ' if ok else 'FAIL'} {name:14s} payload={payload}")

    print("\n" + ("全部通过" if not failures else f"失败 {len(failures)} 项："))
    for f in failures:
        print("  -", f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
