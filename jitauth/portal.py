"""校园网认证门户登录核心。

策略（按顺序尝试，任一成功即返回）：
  1. ---- 纯 HTTP 登录 ----
     * 取门户首页，自动识别登录表单（字段名、action、method、charset 全部自适应）
     * 回填 用户名 / 密码 / 运营商，提交
     * 识别返回内容判断成功与否
  2. ---- 浏览器兜底 ----
     交给 browser.py 用 Edge 真实渲染页面再自动填写（应对 JS 加密/动态页面）

本模块只依赖标准库，便于在任何 Python 3.8+ 上运行。
"""
from __future__ import annotations

import gzip
import http.cookiejar
import io
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any
from zlib import decompress as _zlib_decompress

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0"
)

SUCCESS_WORDS = (
    "认证成功", "登录成功", "登陆成功", "成功", "success", "succeed",
    "logout", "注销", "已经在线", "已在线", "active", "欢迎使用",
)
FAILURE_WORDS = (
    "密码错误", "账号或密码", "用户名或密码", "认证失败", "登录失败", "登陆失败",
    "失败", "error", "invalid", "incorrect", "用户名不存在", "余额不足", "已欠费",
    "不在线", "超时", "forbidden",
)
# 这些词出现时说明是账号本身有问题，重试无意义
FATAL_WORDS = ("密码错误", "用户名不存在", "账号或密码", "用户名或密码", "已欠费", "余额不足")

ISP_ALIASES = {
    "中国电信": ["中国电信", "电信", "telecom", "chinanet", "ctcc", "tel", "1"],
    "中国移动": ["中国移动", "移动", "mobile", "cmcc", "cmnet", "2"],
    "中国联通": ["中国联通", "联通", "unicom", "cucc", "3"],
    "校园网": ["校园网", "校园", "校内", "campus", "local", "教育网", "0"],
}

USERNAME_HINTS = ("username", "user", "name", "account", "acct", "login", "uname", "userid", "账号", "用户名", "0")
PASSWORD_HINTS = ("password", "passwd", "pwd", "pass", "密码", "1")
ISP_HINTS = ("isp", "operator", "service", "net", "domain", "carrier", "运营商", "2")
SUBMIT_HINTS = ("login", "submit", "logon", "ok", "button", "登录", "登陆", "3")


class PortalError(Exception):
    """门户交互失败。"""


class AuthFailed(PortalError):
    """账号或密码错误等不可重试的失败。"""


@dataclass
class Field:
    tag: str
    type: str
    name: str
    value: str
    label: str = ""
    options: list[tuple[str, str]] = field(default_factory=list)
    checked: bool = False
    element_id: str = ""

    @property
    def key(self) -> str:
        """提交时使用的字段名：优先 name，没有 name 时退回 id。"""
        return self.name or self.element_id


@dataclass
class Form:
    action: str = ""
    method: str = "post"
    fields: list[Field] = field(default_factory=list)

    def by_type(self, *types: str) -> list[Field]:
        wanted = {t.lower() for t in types}
        return [f for f in self.fields if f.type.lower() in wanted]

    def named(self, name: str) -> Field | None:
        for f in self.fields:
            if f.key == name:
                return f
        return None


# --------------------------------------------------------------------------- #
# 极简 HTML 解析：抓 form / input / select / option / base / title
# --------------------------------------------------------------------------- #
class _FormParser(HTMLParser):
    #: 这些标签里的文字视为其前面 radio/checkbox 的说明文字
    LABEL_HOLDERS = ("input", "label", "td", "span", "font", "div", "b", "strong", "p")

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[Form] = []
        self.base_href: str = ""
        self.title: str = ""
        self._cur_form: Form | None = None
        self._cur_select: Field | None = None
        self._in_title = False
        self._label_stack: list[str] = []
        self._label_text = ""

    # -- 说明文字归属 ------------------------------------------------------ #
    def _begin_label_scope(self, tag: str) -> None:
        if tag not in self.LABEL_HOLDERS:
            return
        if tag == "input":
            self._label_stack.append(tag)
            return
        # 遇到新的块级标签：先结束上一个"紧跟输入框"的作用域
        while self._label_stack and self._label_stack[-1] == "input":
            self._label_stack.pop()
        self._label_stack.append(tag)

    def _end_label_scope(self, tag: str) -> None:
        if tag not in self.LABEL_HOLDERS or not self._label_stack:
            return
        if self._label_stack[-1] == tag:
            self._label_stack.pop()
            if not self._label_stack:
                self._assign_label(self._label_text)
                self._label_text = ""
            return
        if tag in self._label_stack:
            # 标签未正确闭合，回退到匹配位置
            idx = len(self._label_stack) - 1 - self._label_stack[::-1].index(tag)
            del self._label_stack[idx:]
            if not self._label_stack:
                self._assign_label(self._label_text)
                self._label_text = ""

    def _flush_label(self) -> None:
        if self._label_stack:
            self._assign_label(self._label_text)
            self._label_text = ""
            self._label_stack.clear()

    def _assign_label(self, text: str) -> None:
        """把最近一段文字补回它前面最近的 radio/checkbox。"""
        text = (text or "").strip()
        if not text or self._cur_form is None:
            return
        for fld in reversed(self._cur_form.fields):
            if fld.type in ("radio", "checkbox"):
                if not fld.label:
                    fld.label = text
                return
            if fld.type in ("text", "password", "submit", "select"):
                return

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {k.lower(): (v or "") for k, v in attrs}
        tag = tag.lower()
        if tag == "base" and attr.get("href"):
            self.base_href = attr["href"].strip()
        elif tag == "title":
            self._in_title = True
        elif tag == "form":
            self._cur_form = Form(
                action=attr.get("action", "").strip(),
                method=(attr.get("method") or "post").strip().lower(),
            )
            self.forms.append(self._cur_form)
        elif tag == "input" and self._cur_form is not None:
            field_type = (attr.get("type") or "text").strip().lower()
            self._cur_form.fields.append(
                Field(
                    tag="input",
                    type=field_type,
                    name=attr.get("name", "").strip(),
                    value=attr.get("value", ""),
                    checked="checked" in attr,
                    element_id=attr.get("id", "").strip(),
                )
            )
            # 说明文字可能写在同一层（<input type=radio><span>中国电信</span>）
            if field_type in ("radio", "checkbox"):
                self._begin_label_scope("input")
        elif tag == "select" and self._cur_form is not None:
            self._cur_select = Field(
                tag="select",
                type="select",
                name=attr.get("name", "").strip(),
                value="",
            )
            self._cur_form.fields.append(self._cur_select)
        elif tag == "option" and self._cur_select is not None:
            value = attr.get("value", "")
            self._cur_select.options.append((value, ""))
            if "selected" in attr and not self._cur_select.value:
                self._cur_select.value = value
        elif tag == "textarea" and self._cur_form is not None:
            self._cur_form.fields.append(
                Field(tag="textarea", type="textarea", name=attr.get("name", "").strip(), value="")
            )
        elif tag == "button" and self._cur_form is not None:
            self._cur_form.fields.append(
                Field(
                    tag="button",
                    type=(attr.get("type") or "submit").strip().lower(),
                    name=attr.get("name", "").strip(),
                    value=attr.get("value", ""),
                )
            )
        else:
            self._begin_label_scope(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "form":
            self._flush_label()
            self._cur_form = None
        elif tag == "select":
            self._flush_label()
            self._cur_select = None
        elif tag == "title":
            self._in_title = False
        else:
            self._end_label_scope(tag)

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        if self._label_stack:
            self._label_text += data
        if self._cur_select is not None and self._cur_select.options:
            value, label = self._cur_select.options[-1]
            self._cur_select.options[-1] = (value, (label + data).strip())


def parse_html(html: str) -> _FormParser:
    parser = _FormParser()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001 - 页面畸形也不能崩
        pass
    return parser


# --------------------------------------------------------------------------- #
# HTTP 层
# --------------------------------------------------------------------------- #
class Session:
    """带 cookie 与自适应编码的极简 HTTP 会话。"""

    def __init__(self, timeout: float = 8.0) -> None:
        self.timeout = timeout
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar),
            urllib.request.HTTPRedirectHandler(),
        )
        self.opener.addheaders = [
            ("User-Agent", USER_AGENT),
            ("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"),
            ("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.8"),
            ("Accept-Encoding", "gzip, deflate"),
            ("Connection", "keep-alive"),
        ]
        self.last_url = ""

    # -- 内部工具 ---------------------------------------------------------- #
    @staticmethod
    def _decompress(raw: bytes, encoding: str) -> bytes:
        enc = (encoding or "").lower()
        try:
            if "gzip" in enc:
                return gzip.decompress(raw)
            if "deflate" in enc:
                try:
                    return _zlib_decompress(raw)
                except Exception:  # noqa: BLE001
                    return _zlib_decompress(raw, -15)
        except Exception:  # noqa: BLE001
            return raw
        return raw

    @staticmethod
    def _decode(raw: bytes, content_type: str) -> str:
        charset = ""
        m = re.search(r"charset\s*=\s*[\"']?([\w-]+)", content_type or "", re.I)
        if m:
            charset = m.group(1)
        candidates = [charset] if charset else []
        head = raw[:4096].decode("latin-1", errors="ignore")
        m2 = re.search(r"charset\s*=\s*[\"']?([\w-]+)", head, re.I)
        if m2:
            candidates.append(m2.group(1))
        candidates += ["utf-8", "gbk", "gb2312", "big5", "latin-1"]
        for enc in candidates:
            if not enc:
                continue
            try:
                return raw.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
        return raw.decode("utf-8", errors="replace")

    def request(
        self,
        url: str,
        data: dict[str, str] | None = None,
        method: str | None = None,
        timeout: float | None = None,
        encoding: str = "utf-8",
    ) -> tuple[int, str, str]:
        """返回 (status, 文本, 最终URL)。网络错误会抛 PortalError。"""
        body = None
        if data is not None:
            body = urllib.parse.urlencode(data, encoding=encoding).encode(encoding, errors="replace")
        req = urllib.request.Request(url, data=body, method=(method or ("POST" if body else "GET")).upper())
        if body is not None:
            req.add_header("Content-Type", f"application/x-www-form-urlencoded; charset={encoding}")
        if self.last_url:
            req.add_header("Referer", self.last_url)
        try:
            with self.opener.open(req, timeout=timeout or self.timeout) as resp:
                raw = resp.read()
                raw = self._decompress(raw, resp.headers.get("Content-Encoding", ""))
                text = self._decode(raw, resp.headers.get("Content-Type", ""))
                self.last_url = resp.geturl()
                return resp.status, text, self.last_url
        except urllib.error.HTTPError as exc:
            raw = b""
            try:
                raw = exc.read()
                raw = self._decompress(raw, exc.headers.get("Content-Encoding", "") if exc.headers else "")
            except Exception:  # noqa: BLE001
                pass
            text = self._decode(raw, (exc.headers.get("Content-Type", "") if exc.headers else ""))
            self.last_url = url
            return exc.code, text, url
        except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as exc:
            raise PortalError(f"无法访问 {url}：{exc}") from exc


# --------------------------------------------------------------------------- #
# 联网状态判定
# --------------------------------------------------------------------------- #
def internet_ok(probe_url: str, timeout: float = 5.0) -> bool:
    """通过 generate_204 类探针判断是否已经可以上网。"""
    if not probe_url:
        return False
    try:
        req = urllib.request.Request(probe_url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 204:
                body = resp.read(2048)
                text = body.decode("utf-8", errors="replace").lower()
                # 被门户劫持时会返回一个登录页，里面必然没有 "generate_204"
                if resp.status == 200 and ("success" in text or len(body) <= 2):
                    return True
                return False
            return True
    except urllib.error.HTTPError as exc:
        # 有些探针服务用 200/204，被劫持时常见 302 → 门户
        return exc.code == 204
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------- #
# 表单处理
# --------------------------------------------------------------------------- #
def _looks_like_login_form(form: Form) -> int:
    """给表单打分，分越高越可能是登录表单。"""
    score = 0
    types = {f.type for f in form.fields}
    names = " ".join(f.name.lower() for f in form.fields)
    if "password" in types:
        score += 50
    if re.search(r"pass|pwd|密码", names):
        score += 20
    if re.search(r"user|name|account|账号", names):
        score += 15
    if any(f.type in ("submit", "image", "button") for f in form.fields):
        score += 5
    if form.method == "post":
        score += 5
    if re.search(r"login|logon|auth|认证|登录", form.action, re.I):
        score += 10
    return score


def pick_form(html: str) -> Form | None:
    parsed = parse_html(html)
    if not parsed.forms:
        return None
    return max(parsed.forms, key=_looks_like_login_form)


def _match_field(
    fields: list[Field],
    hints: tuple[str, ...],
    exclude: set[str],
    allowed_types: set[str] | None = None,
) -> Field | None:
    best: tuple[int, Field] | None = None
    for f in fields:
        key = f.key
        if not key or key in exclude:
            continue
        if allowed_types is not None:
            if f.type not in allowed_types:
                continue
        elif f.type in ("hidden", "submit", "button", "image", "reset", "checkbox", "radio", "file"):
            continue
        low = key.lower()
        score = 0
        for idx, hint in enumerate(hints):
            if low == hint:
                score = max(score, 100 - idx)
            elif hint in low:
                score = max(score, 60 - idx)
        if score and (best is None or score > best[0]):
            best = (score, f)
    return best[1] if best else None


def _isp_value(field: Field, wanted: str) -> str | None:
    """在 select/radio/隐藏字段里找到与"中国电信"匹配的值。"""
    aliases = ISP_ALIASES.get(wanted, [wanted])
    aliases_lower = [a.lower() for a in aliases if a]

    if field.type == "select" and field.options:
        for value, label in field.options:
            label_low = (label or "").lower()
            value_low = (value or "").lower()
            for alias in aliases_lower:
                if alias and alias in label_low:
                    return value
            for alias in aliases_lower:
                if alias and alias.isdigit() and value_low == alias:
                    return value
        return None

    # radio / hidden：真实值优先，其次看 <label> 文本
    # 注意：纯数字别名只用于"精确相等"，绝不参与子串匹配
    # （否则 '1' 会误匹配 value='1' 的"校园网"）
    value_low = (field.value or "").strip().lower()
    label_low = (field.label or "").strip().lower()
    for alias in aliases_lower:
        if alias.isdigit():
            continue
        if alias and (value_low == alias or label_low == alias):
            return field.value
    for alias in aliases_lower:
        if alias.isdigit():
            continue
        if alias and alias in label_low:
            return field.value
    for alias in aliases_lower:
        if alias.isdigit() and value_low == alias:
            return field.value
    return None


def _label_matches_isp(field: Field, wanted: str) -> bool:
    """只按"文字"判断这个 radio/checkbox 是不是目标运营商（不看数字值）。"""
    aliases = [a for a in ISP_ALIASES.get(wanted, [wanted]) if a and not a.isdigit()]
    text = f"{field.label} {field.value} {field.element_id}".strip().lower()
    if not text:
        return False
    for alias in aliases:
        if alias.lower() in text:
            return True
    return False


def find_isp_field(form: Form, isp: str, exclude: set[str]) -> tuple[Field | None, str | None]:
    """找出运营商字段并给出应填的值。

    优先级：
      1. 按名字锁定运营商控件（select 或 radio 组），再在选项里按文字匹配
      2. select 的下拉项里直接含该运营商名称
      3. radio 组里有选项的文字能匹配该运营商
      4. 纯数字隐藏字段（Dr.COM 风格）
    """
    mapped_num = {"中国电信": "1", "中国移动": "2", "中国联通": "3", "校园网": "0"}.get(isp)

    # 1) 按字段名锁定运营商控件（netType / isp / operator / carrier ...）
    named = _match_field(form.fields, ISP_HINTS, exclude)
    if named is not None:
        value = _isp_value(named, isp)
        if value is not None:
            return named, value

    # 2) select：选项文字里含运营商名
    for f in form.fields:
        if f.type != "select" or not f.key or f.key in exclude or not f.options:
            continue
        value = _isp_value(f, isp)
        if value is not None:
            return f, value

    # 3) radio：只在"组内选项文字"里按运营商名匹配
    #    注意不能靠 _isp_value 的数字别名，否则 中国电信 的别名 '1'
    #    会命中 value='1' 的"校园网"
    radio_groups: dict[str, list[Field]] = {}
    for f in form.fields:
        if f.type == "radio" and f.key and f.key not in exclude:
            radio_groups.setdefault(f.key, []).append(f)
    for name, group in radio_groups.items():
        for f in group:
            if _label_matches_isp(f, isp):
                return f, f.value
        # 该组没有文字线索时，若组内某个选项的 value 正好等于标准映射值则用它
        if mapped_num is not None:
            for f in group:
                if (f.value or "").strip() == mapped_num:
                    return f, f.value

    # 4) Dr.COM：字段名就是 "2"
    for f in form.fields:
        if f.key == "2" and f.key not in exclude:
            return f, (mapped_num or f.value or None)

    return None, None


TEXT_INPUT_TYPES = ("text", "email", "tel", "search", "number", "url", "")


def _fillable(fields: list[Field], types: tuple[str, ...]) -> list[Field]:
    out = []
    for f in fields:
        if not f.key:
            continue
        if f.type in ("hidden", "submit", "button", "image", "reset", "file"):
            continue
        if types and f.type not in types:
            continue
        out.append(f)
    return out


def pick_credential_fields(form: Form, used: set[str]) -> tuple[Field | None, Field | None, list[str]]:
    """识别用户名框和密码框，带与浏览器端一致的兜底链。

    兜底顺序：名字线索 → 第一个密码框 → 密码框之前最近的文本框 → 表单内第一个文本框
    """
    notes: list[str] = []
    user_field = _match_field(form.fields, USERNAME_HINTS, used)
    pass_field = _match_field(form.fields, PASSWORD_HINTS, used)

    if pass_field is None:
        pwds = _fillable(form.fields, ("password",))
        if pwds:
            pass_field = pwds[0]
            notes.append("按类型定位到密码框" + (f"（{pass_field.key}）" if pass_field.key else ""))

    if user_field is None:
        texts = _fillable(form.fields, TEXT_INPUT_TYPES)
        if pass_field is not None and pass_field in texts:
            idx = texts.index(pass_field)
            before = texts[:idx] if idx > 0 else texts
            if before:
                user_field = before[-1]
        if user_field is None and pass_field is not None and pass_field in form.fields:
            # 密码框所在表单里第一个文本框
            order = [f for f in form.fields if f in texts]
            if order and order[0] is not pass_field:
                user_field = order[0]
            elif order:
                user_field = order[0] if order[0] is not pass_field else (order[1] if len(order) > 1 else None)
        if user_field is None and texts:
            user_field = texts[0]
        if user_field is not None:
            notes.append("按位置推断用户名框" + (f"（{user_field.key}）" if user_field.key else ""))

    return user_field, pass_field, notes


def build_payload(form: Form, html: str, account: dict) -> tuple[dict[str, str], list[str]]:
    """根据识别出的表单构造提交数据。

    返回 (payload, 说明列表)。"""
    notes: list[str] = []
    payload: dict[str, str] = {}

    # 1) 先原样带上所有 hidden / 提交按钮字段（可能含 token）
    for f in form.fields:
        key = f.key
        if not key:
            continue
        if f.type == "hidden":
            payload[key] = f.value
        elif f.type in ("submit", "button", "image") and f.value:
            payload[key] = f.value

    used: set[str] = set()
    user_field, pass_field, cred_notes = pick_credential_fields(form, used)
    notes.extend(cred_notes)

    if user_field is not None:
        payload[user_field.key] = str(account.get("username", ""))
        used.add(user_field.key)
        notes.append(f"用户名 → {user_field.key}")
    else:
        notes.append("未识别到用户名输入框")

    if pass_field is not None:
        payload[pass_field.key] = str(account.get("password", ""))
        used.add(pass_field.key)
        notes.append(f"密码 → {pass_field.key}")
    else:
        notes.append("未识别到密码输入框")

    isp = str(account.get("isp", ""))
    isp_field, isp_value = find_isp_field(form, isp, used)
    if isp_field is not None:
        if isp_value is not None:
            payload[isp_field.key] = isp_value
            notes.append(f"运营商({isp}) → {isp_field.key}={isp_value}")
        else:
            notes.append(f"识别到运营商字段 {isp_field.key}，但没找到匹配项，保留原值")
            if isp_field.value:
                payload[isp_field.key] = isp_field.value
        used.add(isp_field.key)
    else:
        notes.append("未识别到运营商字段（继续提交）")

    # Dr.COM 门户常见字段名是纯数字，做一次补齐（只在字段确实存在时）
    all_names = {f.key for f in form.fields}
    for alias, value in (
        ("0", str(account.get("username", ""))),
        ("1", str(account.get("password", ""))),
        ("3", "Login"),
    ):
        if alias in all_names and alias not in used:
            payload.setdefault(alias, value)
    if "2" in all_names:
        isp_num = {"中国电信": "1", "中国移动": "2", "中国联通": "3", "校园网": "0"}.get(
            str(account.get("isp", "")), ""
        )
        if isp_num:
            payload.setdefault("2", isp_num)

    # 常见的"记住我"之类：默认不勾选，避免额外副作用
    return payload, notes


def form_target(form: Form, page_url: str, base_href: str) -> str:
    action = (form.action or "").strip()
    if action.lower().startswith("javascript:") or action == "#":
        action = ""
    base = base_href or page_url
    if not action:
        return page_url
    return urllib.parse.urljoin(base, action)


def drcom_online_state(raw_html: str) -> tuple[bool, str] | None:
    """识别 Dr.COM 门户的"已登录"页面。

    已登录时门户给的是**注销页**：标题「注销页」，脚本里有
    `uid='账号@后缀'`、`v4ip='...'`、`authlogoutpath=...`。
    看到这个页面就说明已经认证过了，不需要再登录。

    返回 (是否已登录, 说明)；不是 Dr.COM 页面时返回 None。
    """
    if not raw_html or "Dr.COM" not in raw_html:
        return None
    uid = re.search(r"\buid\s*=\s*'([^']*)'", raw_html)
    logout_page = bool(re.search(r"authlogoutpath|Logout&ver|注销页", raw_html, re.I))
    has_ip = bool(re.search(r"\bv4ip\s*=\s*'([^']+)'", raw_html))
    if uid and uid.group(1).strip() and (logout_page or has_ip):
        return True, f"门户显示已登录（账号 {uid.group(1).strip()}）"
    if logout_page or "Dr.COMWebLoginID_3" in raw_html:
        return False, "门户当前是未登录状态"
    return None


def judge(html: str, status: int) -> tuple[bool, str]:
    """根据响应内容判断登录结果。

    注意：`<script>` 里的内容会被剔除后再判断。Dr.COM 门户的登录/注销页把
    大量参数写在脚本里（例如注销页就有 `authlogoutpath` 字样），
    如果不剔除就会把"注销"当成登录成功标志，产生假成功。
    Dr.COM 的在线/离线判定交给 `drcom_online_state()`，它专门读脚本里的变量。
    """
    drcom = drcom_online_state(html)
    if drcom is not None:
        return drcom

    text = re.sub(r"<script.*?</script>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S | re.I)
    visible = re.sub(r"<[^>]+>", " ", text)
    visible = re.sub(r"\s+", " ", visible)
    low = visible.lower()

    for word in FATAL_WORDS:
        if word.lower() in low:
            return False, f"门户返回：{word}（账号/密码问题，请检查配置）"

    # 明确的失败标志优先（避免"失败"和"成功"同时出现时误判）
    hits_fail = [w for w in FAILURE_WORDS if w.lower() in low]
    hits_success = [w for w in SUCCESS_WORDS if w.lower() in low]

    # "注销"只有在同时没有失败词、且页面看起来确实是在线状态时才算成功；
    # 单独出现"注销"很可能是注销页本身，不能当成功。
    strong_success = [w for w in hits_success if w not in ("注销", "logout")]
    if strong_success and not hits_fail:
        return True, f"门户返回成功提示：{strong_success[0]}"
    if hits_fail:
        return False, f"门户返回失败提示：{hits_fail[0]}"
    if hits_success and not hits_fail:
        # 只剩"注销/logout"这类弱标志：需要页面同时透露在线信息才认可
        if re.search(r"\buid\b|\bv4ip\b|在线|已登录|剩余", visible, re.I):
            return True, f"页面显示在线状态：{hits_success[0]}"
        return False, "页面只是注销/登录入口，未显示已登录"
    if status >= 400:
        return False, f"HTTP {status}"
    return False, "门户未给出明确结果"


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def login_via_http(cfg: dict, logger, session: Session | None = None) -> tuple[bool, str]:
    """纯 HTTP 登录。返回 (是否成功, 说明)。"""
    portal_url = cfg["portal"]["url"]
    timeout = float(cfg["portal"].get("timeout", 8))
    account = cfg["account"]
    sess = session or Session(timeout=timeout)

    try:
        status, html, final_url = sess.request(portal_url, timeout=timeout)
    except PortalError as exc:
        return False, str(exc)
    logger.debug("门户首页 HTTP %s，长度 %d，URL=%s", status, len(html), final_url)

    parsed = parse_html(html)
    logger.debug("页面标题：%s", parsed.title.strip()[:120])

    form = pick_form(html)
    if form is None:
        # 没有表单：可能已经在线，也可能需要跳转
        ok, why = judge(html, status)
        if ok:
            return True, why
        return False, "门户页面里没有找到登录表单（可能需要浏览器渲染）"

    payload, notes = build_payload(form, html, account)
    for note in notes:
        logger.debug("  %s", note)

    target = form_target(form, final_url, parsed.base_href)
    logger.info("提交登录表单：%s %s（%d 个字段）", form.method.upper(), target, len(payload))
    logger.debug("提交字段：%s", ", ".join(sorted(payload.keys())))

    try:
        if form.method == "get":
            query = urllib.parse.urlencode(payload)
            sep = "&" if "?" in target else "?"
            status2, html2, _ = sess.request(f"{target}{sep}{query}", timeout=timeout)
        else:
            status2, html2, _ = sess.request(target, data=payload, method="post", timeout=timeout)
    except PortalError as exc:
        return False, f"提交表单失败：{exc}"

    logger.debug("登录响应 HTTP %s，长度 %d", status2, len(html2))
    ok, why = judge(html2, status2)
    if ok:
        return True, why

    # 有些门户提交后是 302 到成功页/门户首页，再取一次确认
    if status2 in (301, 302, 303) or "window.location" in html2.lower():
        try:
            status3, html3, _ = sess.request(portal_url, timeout=timeout)
            ok3, why3 = judge(html3, status3)
            if ok3:
                return True, why3
        except PortalError:
            pass
    return False, why


def dump_diagnostics(cfg: dict, logger) -> str:
    """把门户首页保存下来，便于离线分析表单结构。"""
    import os
    from .logging_setup import APP_DIR

    portal_url = cfg["portal"]["url"]
    sess = Session(timeout=float(cfg["portal"].get("timeout", 8)))
    try:
        status, html, final_url = sess.request(portal_url)
    except PortalError as exc:
        return f"抓取失败：{exc}"
    out_dir = os.path.join(APP_DIR, "logs")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "portal_dump.html")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(f"<!-- status={status} url={final_url} -->\n")
        fh.write(html)
    logger.info("门户首页已保存到 %s（%d 字节）", path, len(html))
    form = pick_form(html)
    if form:
        lines = [f"找到表单：action={form.action!r} method={form.method}"]
        for f in form.fields:
            opts = ""
            if f.options:
                opts = " 选项=" + ", ".join(f"{v}({l})" for v, l in f.options)
            lines.append(f"  <{f.tag} type={f.type} name={f.name!r} value={f.value!r}>{opts}")
        summary = "\n".join(lines)
    else:
        summary = "没有解析到表单"
    logger.info("表单结构：\n%s", summary)
    return summary
