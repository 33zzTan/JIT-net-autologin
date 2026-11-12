"""无线网络检测与连接模块。

设计要点：
* 该脚本在普通用户权限下运行，因此 `netsh wlan show interfaces` / `show networks`
  在部分机器上会失败（需要定位服务或管理员权限）。这里做了多级降级：
  1) netsh 读取当前 SSID
  2) ipconfig /all 的 "SSID" 字段兜底
  3) netsh 扫描可见网络；若因权限失败，则退化为"直接尝试连接"，连接失败即视为不在范围内
* 连接使用系统已保存的 JIT-Stu 配置文件（无需再输 WiFi 密码）。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time

#: 设置为 1/true/yes 时，本模块不会真正切换 WiFi（防止测试意外断网）
NO_SWITCH_ENV = "JIT_NO_WIFI_SWITCH"


def switching_disabled() -> bool:
    return os.environ.get(NO_SWITCH_ENV, "").strip().lower() in ("1", "true", "yes")

CREATE_NO_WINDOW = 0x08000000
#: 匹配 "SSID : xxx" 和 "SSID 1 : xxx"（扫描列表），但不匹配 "BSSID : xxx"
SSID_RE = re.compile(r"^\s*SSID(?:\s+\d+)?\s*:\s*(.+?)\s*$", re.I)
#: 匹配 "All User Profile     : xxx"（netsh 里冒号前常有多个空格）
PROFILE_RE = re.compile(r"All\s+User\s+Profile\b[^:]*:\s*(.+?)\s*$", re.I)
CJK = re.compile(r"[\u4e00-\u9fff]")


def _run(args: list[str], timeout: int = 20) -> tuple[int, str]:
    """执行命令并返回 (returncode, 输出)。不抛异常。"""
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except FileNotFoundError:
        return 127, ""
    except subprocess.TimeoutExpired:
        return 124, ""
    except OSError as exc:
        return 1, str(exc)

    def decode(blob: bytes | None) -> str:
        if not blob:
            return ""
        for enc in ("utf-8", "gbk", "cp936", "latin-1"):
            try:
                return blob.decode(enc)
            except UnicodeDecodeError:
                continue
        return blob.decode("utf-8", errors="replace")

    return proc.returncode, decode(proc.stdout) + decode(proc.stderr)


def _has_cjk(text: str) -> bool:
    return bool(CJK.search(text or ""))


def safe_ssid(ssid: str) -> str:
    """防止把奇怪字符拼进 netsh 参数。"""
    cleaned = re.sub(r'["\r\n\t]', "", ssid or "").strip()
    return cleaned


# --------------------------------------------------------------------------- #
# 当前连接状态
# --------------------------------------------------------------------------- #
def get_current_ssid() -> tuple[str | None, str]:
    """返回 (当前连接的 SSID 或 None, 诊断信息)。"""
    rc, out = _run(["netsh", "wlan", "show", "interfaces"])
    if rc == 0 and out:
        for line in out.splitlines():
            # 注意：netsh 输出行首有缩进，必须用 search 而不是 match
            m = SSID_RE.search(line)
            if m:
                name = m.group(1).strip()
                if name and name.upper() != "BSSID":
                    return name, "netsh"
    diag = "netsh 读取接口失败"
    if not _has_cjk(out) and out:
        diag += f"（{out.strip().splitlines()[-1][:120]}）"
    elif _has_cjk(out):
        diag += "（可能需要管理员权限或打开定位服务）"

    rc2, out2 = _run(["ipconfig", "/all"])
    if rc2 == 0:
        wifi_section = False
        for line in out2.splitlines():
            low = line.lower()
            if "wireless" in low or "wlan" in low or "无线" in line:
                wifi_section = True
            if wifi_section and "ssid" in low and "bssid" not in low:
                name = line.split(":", 1)[-1].strip()
                if name:
                    return name, "ipconfig"
    return None, diag


# --------------------------------------------------------------------------- #
# 扫描可见网络
# --------------------------------------------------------------------------- #
def scan_visible_ssids() -> tuple[list[str], str | None]:
    """扫描周边 WiFi，返回 (SSID 列表, 失败原因或 None)。"""
    rc, out = _run(["netsh", "wlan", "show", "networks"], timeout=25)
    if rc != 0 or not out:
        return [], "netsh 扫描失败（可能未开启定位服务或权限不足）"
    if "location permission" in out.lower() or "定位" in out:
        return [], "系统未开启定位服务，无法扫描 WiFi 列表"
    if "requires elevation" in out.lower() or "elevation" in out.lower():
        return [], "扫描 WiFi 需要管理员权限"

    found: list[str] = []
    for line in out.splitlines():
        m = SSID_RE.search(line)
        if m:
            name = m.group(1).strip()
            if name and name not in found:
                found.append(name)
    return found, None


def have_saved_profile(ssid: str) -> bool:
    """检查系统里是否已有该 WiFi 的已保存配置。

    Windows 是中文时 netsh 输出为「所有用户配置文件 : xxx」，
    因此这里不依赖具体文字，只取最后一个冒号后面的内容比对。
    """
    rc, out = _run(["netsh", "wlan", "show", "profiles"])
    if rc != 0 or not out:
        return True  # 判断不了就乐观处理，交给连接步骤报错
    target = ssid.strip().lower()
    for line in out.splitlines():
        m = PROFILE_RE.search(line)
        if m and m.group(1).strip().lower() == target:
            return True
        # 兜底：这一行以该 SSID 结尾
        if line.strip().lower().endswith(target) and ":" in line:
            return True
    return False


# --------------------------------------------------------------------------- #
# 连接
# --------------------------------------------------------------------------- #
def connect(ssid: str, timeout: int = 25) -> tuple[bool, str]:
    """连接指定 WiFi，返回 (是否成功, 说明)。

    安全阀：设置环境变量 JIT_NO_WIFI_SWITCH=1 后只检查、不真正切网，
    避免自动化测试把当前网络断掉。
    """
    ssid = safe_ssid(ssid)
    if not ssid:
        return False, "SSID 为空"

    if switching_disabled():
        return False, f"已设置 {NO_SWITCH_ENV}，跳过真实连接（保护当前网络）"

    rc, out = _run(["netsh", "wlan", "connect", f"name={ssid}", f"ssid={ssid}"], timeout=30)
    combined = out or ""
    if rc != 0 or "requires elevation" in combined.lower():
        return False, f"发起连接失败：{combined.strip()[:200] or '未知错误'}"

    deadline = time.time() + max(5, timeout)
    while time.time() < deadline:
        current, _ = get_current_ssid()
        if current and current.strip().lower() == ssid.strip().lower():
            return True, f"已连接到 {current}"
        time.sleep(2)
    current, diag = get_current_ssid()
    return False, f"连接超时（当前 SSID：{current or '未知'}；{diag}）"


# --------------------------------------------------------------------------- #
# 无线开关（WiFi 关着时先打开）
# --------------------------------------------------------------------------- #
PS_RADIO_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime | Out-Null
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
})[0]
function Await($task, $type) {
    $m = $asTaskGeneric.MakeGenericMethod($type)
    $t = $m.Invoke($null, @($task))
    [void]$t.Wait(8000)
    return $t.Result
}
[void][Windows.Devices.Radios.Radio, Windows.System.Devices, ContentType = WindowsRuntime]
[void][Windows.Devices.Radios.RadioAccessStatus, Windows.System.Devices, ContentType = WindowsRuntime]
[void][Windows.Devices.Radios.RadioState, Windows.System.Devices, ContentType = WindowsRuntime]
$radios = Await ([Windows.Devices.Radios.Radio]::GetRadiosAsync()) ([System.Collections.Generic.IReadOnlyList[Windows.Devices.Radios.Radio]])
$wifi = $radios | Where-Object { $_.Kind -eq [Windows.Devices.Radios.RadioKind]::WiFi } | Select-Object -First 1
if (-not $wifi) { Write-Output 'RADIO=NOTFOUND'; exit 0 }
__BODY__
"""

_PS_BODY_READ = "Write-Output ('RADIO=' + $wifi.State)"
_PS_BODY_ENABLE = (
    "$status = Await ($wifi.SetStateAsync([Windows.Devices.Radios.RadioState]::On)) "
    "([Windows.Devices.Radios.RadioAccessStatus])\n"
    "Write-Output ('SET=' + $status)"
)


def _radio_script(action: str) -> str:
    body = _PS_BODY_ENABLE if action == "enable" else _PS_BODY_READ
    return PS_RADIO_SCRIPT.replace("__BODY__", body)


def _run_powershell(script: str, timeout: int = 25) -> tuple[int, str]:
    return _run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        timeout=timeout,
    )


def radio_state() -> str | None:
    """读取 WiFi 无线电开关状态：'On' / 'Off' / 'NOTFOUND' / None（读不到）。"""
    rc, out = _run_powershell(_radio_script("read"))
    if rc != 0 or not out:
        return None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("RADIO="):
            return line.split("=", 1)[1].strip() or None
    return None


def _enable_via_netsh() -> bool:
    """兜底：直接启用无线网卡（通常需要管理员权限）。"""
    rc, out = _run(["netsh", "interface", "show", "interface"])
    if rc != 0 or not out:
        return False
    name = ""
    for line in out.splitlines():
        if "WLAN" in line or "Wireless" in line or "无线" in line:
            parts = line.split()
            if parts:
                name = parts[-1]
                break
    if not name:
        return False
    rc2, _ = _run(["netsh", "interface", "set", "interface", f"name={name}", "admin=ENABLED"])
    return rc2 == 0


def enable_radio(wait_seconds: float = 3.0, logger=None) -> tuple[bool, str]:
    """确保 WiFi 是开着的；关着就先打开。

    返回 (WiFi 是否处于开启状态, 说明)。真正打开后会等待 wait_seconds 秒。
    """
    def say(msg: str, level: str = "info") -> None:
        if logger is not None:
            getattr(logger, level)(msg)

    state = radio_state()
    if state is None:
        say("读不到无线开关状态（将直接尝试扫描）", "warning")
        return True, "无法读取无线开关状态"

    if state.lower() == "on":
        say("WiFi 已开启")
        return True, "WiFi 已开启"

    say(f"WiFi 当前是关闭状态（{state}），正在打开 ...", "warning")
    if switching_disabled():
        say(f"已设置 {NO_SWITCH_ENV}，跳过打开 WiFi（保护当前网络）", "warning")
        return True, "测试模式：跳过打开 WiFi"

    script = _radio_script("enable")
    rc, out = _run_powershell(script, timeout=30)
    result = ""
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("SET="):
            result = line.split("=", 1)[1].strip()
    ok = (rc == 0 and result in ("Allowed", "Unspecified", "Success"))

    if not ok:
        say(f"通过系统接口打开 WiFi 失败（{result or '未知原因'}），尝试启用无线网卡 ...", "warning")
        ok = _enable_via_netsh()

    # 等网卡就绪后再确认一次
    time.sleep(max(0.0, wait_seconds))
    state2 = radio_state()
    if state2 is not None and state2.lower() == "on":
        say(f"WiFi 已打开，等待 {wait_seconds:g} 秒后继续")
        return True, "WiFi 已打开"

    # 有些驱动在重启网卡后读不到状态，此时只要启用了就继续尝试连接
    if ok:
        say(f"已启用无线网卡（状态：{state2 or '未知'}），等待 {wait_seconds:g} 秒后继续", "warning")
        return True, "已启用无线网卡"

    msg = ("无法自动打开 WiFi，请手动打开无线开关后重试"
           "（也可能是权限不足，需要以管理员身份运行一次）")
    say(msg, "error")
    return False, msg


def ensure_connected(
    ssid: str,
    timeout: int = 25,
    require_visible: bool = True,
    connect_if_scan_unavailable: bool = False,
    enable_radio_if_off: bool = True,
    enable_wait: float = 3.0,
    logger=None,
) -> tuple[bool, str]:
    """确保已连接到目标 WiFi。

    流程：WiFi 没开就先打开（等 enable_wait 秒）→ 扫描确认目标网络 → 连接。

    返回 (是否应该继续执行登录, 说明)。
    若目标网络不在范围内则返回 (False, 原因)，调用方应直接退出。
    """
    log = logger

    def say(msg: str, level: str = "info") -> None:
        if log is not None:
            getattr(log, level)(msg)

    # 0. WiFi 没开就先打开，然后等几秒再扫描
    if enable_radio_if_off:
        ok, message = enable_radio(wait_seconds=enable_wait, logger=logger)
        if not ok:
            return False, message

    current, diag = get_current_ssid()
    if current:
        say(f"当前已连接 WiFi：{current}（来源：{diag}）")
        if current.strip().lower() == ssid.strip().lower():
            say(f"已处于目标网络 {ssid}，无需重连")
            return True, "already-connected"
    else:
        say(f"未能读取当前 WiFi 状态：{diag}", "warning")

    # 扫描列表，确认 JIT-Stu 是否存在
    visible, scan_err = scan_visible_ssids()
    if scan_err:
        say(f"WiFi 扫描不可用：{scan_err}", "warning")
        if require_visible and not connect_if_scan_unavailable:
            say(f"无法确认 {ssid} 是否在范围内，按「未搜索到网络即不执行」的规则跳过", "warning")
            return False, f"无法扫描 WiFi：{scan_err}"
        say(f"退化为直接尝试连接 {ssid}")
    else:
        say(f"扫描到 {len(visible)} 个 WiFi：{', '.join(visible[:12])}{' ...' if len(visible) > 12 else ''}")
        if not any(v.strip().lower() == ssid.strip().lower() for v in visible):
            say(f"WiFi 列表里没有 {ssid}，本次不执行", "warning")
            return False, f"未搜索到 {ssid}"

    if not have_saved_profile(ssid):
        say(f"系统里没有 {ssid} 的已保存配置，无法自动连接（请先手动连接一次）", "error")
        return False, f"缺少 {ssid} 的已保存配置"

    say(f"正在连接 {ssid} ...")
    ok, message = connect(ssid, timeout=timeout)
    say(message, "info" if ok else "error")
    return ok, message
