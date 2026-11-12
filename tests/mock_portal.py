"""本地模拟校园网门户，用于离线端到端自测。

运行：python tests/mock_portal.py [variant]
variant: drcom(默认) / select / radio / js(必须浏览器执行)
"""
from __future__ import annotations

import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

VARIANT = sys.argv[1] if len(sys.argv) > 1 else "drcom"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8811

USER = "20260001"
PWD = "testpass123"

STATE = {"logged_in": False, "attempts": 0, "last": {}}

DRCOM_FORM = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>认证</title></head><body>
<div class="box">
<form name="f1" method="post" action="/">
<input type="hidden" name="wlanacname" value="JIT-AC-01">
<input type="hidden" name="nasip" value="10.50.0.1">
<table>
<tr><td>用户名</td><td><input type="text" name="0" size="20"></td></tr>
<tr><td>密码</td><td><input type="password" name="1" size="20"></td></tr>
<tr><td>运营商</td><td><select name="2">
  <option value="0">校园网</option>
  <option value="1" selected>中国电信</option>
  <option value="2">中国移动</option>
  <option value="3">中国联通</option>
</select></td></tr>
</table>
<input type="submit" name="3" value="登录">
</form></div></body></html>"""

SELECT_FORM = """<!DOCTYPE html><html><head><meta charset="gbk"><title>校园网登录</title></head><body>
<form action="/login.do" method="post">
<input type="text" name="username" placeholder="请输入账号">
<input type="password" name="password" placeholder="请输入密码">
<select name="isp">
  <option value="ct">中国电信</option>
  <option value="cm">中国移动</option>
  <option value="cu">中国联通</option>
</select>
<button type="submit">登 录</button>
</form></body></html>"""

RADIO_FORM = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>登录</title></head><body>
<form action="/" method="post">
<input name="userName" type="text" id="userName">
<input name="passWord" type="password" id="passWord">
<label><input type="radio" name="netType" value="1">校园网</label>
<label><input type="radio" name="netType" value="2">中国电信</label>
<label><input type="radio" name="netType" value="3">中国移动</label>
<input type="submit" value="登录">
</form></body></html>"""

JS_FORM = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>登录</title>
<script>
function doLogin(){
  var u=document.getElementById('u').value, p=document.getElementById('p').value;
  var s=document.getElementById('s').value;
  if(u==='%s' && p==='%s' && s==='ct'){ document.getElementById('msg').innerHTML='认证成功'; }
  else { document.getElementById('msg').innerHTML='用户名或密码错误'; }
  return false;
}
</script></head><body>
<form onsubmit="return doLogin()">
<input id="u" name="u" type="text"><input id="p" name="p" type="password">
<select id="s" name="s"><option value="ct">中国电信</option><option value="cu">中国联通</option></select>
<button type="submit">登录</button>
</form><div id="msg"></div></body></html>""" % (USER, PWD)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # 安静点
        pass

    def _send(self, body: str, status: int = 200, ctype: str = "text/html") -> None:
        blob = body.encode("gbk" if "gbk" in ctype else "utf-8")
        self.send_response(status)
        self.send_header("Content-Type", f"{ctype}; charset={'gbk' if 'gbk' in ctype else 'utf-8'}")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def _page(self) -> str:
        if STATE["logged_in"]:
            return ("<!DOCTYPE html><html><head><meta charset='utf-8'><title>在线状态</title></head>"
                    "<body><h2>认证成功</h2><p>您已在线，欢迎使用。</p>"
                    "<a href='/logout'>注销</a></body></html>")
        return {"drcom": DRCOM_FORM, "select": SELECT_FORM,
                "radio": RADIO_FORM, "js": JS_FORM}[VARIANT]

    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path == "/logout":
            STATE["logged_in"] = False
            self._send(self._page())
            return
        if path == "/generate_204":
            # 模拟联网探针：已认证返回 204，未认证重定向到门户
            if STATE["logged_in"]:
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self.send_response(302)
                self.send_header("Location", "/")
                self.send_header("Content-Length", "0")
                self.end_headers()
            return
        if path in ("/", "/login.do", "/index.html"):
            self._send(self._page())
            return
        if path == "/favicon.ico":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._send("<html><body>404</body></html>", status=404)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        charset = "gbk" if "gbk" in (self.headers.get("Content-Type") or "") else "utf-8"
        data = {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode(charset, "replace")).items()}
        STATE["attempts"] += 1
        STATE["last"] = data
        print(f"  [mock] POST#{STATE['attempts']} 字段: {data}", flush=True)

        if VARIANT == "drcom":
            ok = data.get("0") == USER and data.get("1") == PWD and data.get("2") == "1"
        elif VARIANT == "select":
            ok = data.get("username") == USER and data.get("password") == PWD and data.get("isp") == "ct"
        elif VARIANT == "radio":
            ok = data.get("userName") == USER and data.get("passWord") == PWD and data.get("netType") == "2"
        else:  # js 变体：服务端不处理，只有浏览器执行 JS 才会成功
            ok = False

        if ok:
            STATE["logged_in"] = True
            self._send("<html><head><meta charset='utf-8'><title>结果</title></head>"
                       "<body>认证成功，您已在线。<a href='/logout'>注销</a></body></html>")
        else:
            self._send("<html><head><meta charset='utf-8'><title>结果</title></head>"
                       "<body>认证失败：用户名或密码错误，请重新输入。</body></html>", ctype="text/html; charset=utf-8")


if __name__ == "__main__":
    print(f"mock portal [{VARIANT}] http://127.0.0.1:{PORT}/", flush=True)
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
