"""WiFi 模块单元测试：netsh 输出解析、连接判定、扫描兜底。

不修改真实网络，只替换底层命令执行函数。

用法: python tests/test_wifi.py
"""
from __future__ import annotations

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

# 安全阀：不允许测试真的切换 WiFi
os.environ.setdefault("JIT_NO_WIFI_SWITCH", "1")

from jitauth import wifi  # noqa: E402

failures: list[str] = []

IFACE_CONNECTED = """
There is 1 interface on the system:

    Name                   : WLAN
    Description            : Intel(R) Wi-Fi 6 AX201 160MHz
    GUID                   : 8f0e1b1e-0000-0000-0000-000000000000
    Physical address       : aa:bb:cc:dd:ee:ff
    State                  : connected
    SSID                   : JIT-Stu
    BSSID                  : 11:22:33:44:55:66
    Network type           : Infrastructure
    Radio type             : 802.11ac
    Signal                 : 88%
"""

IFACE_ELEVATION = """
There is 1 interface on the system:

Function WlanQueryInterface returns error 5:
The requested operation requires elevation (Run as administrator).
"""

IFACE_CHINESE_CONNECTED = """
系统上有 1 个接口:

    名称                   : WLAN
    描述                   : Intel(R) Wi-Fi 6 AX201
    状态                   : 已连接
    SSID                   : JIT-Stu
    BSSID                  : 11:22:33:44:55:66
"""

NETWORKS = """
Interface name : WLAN
There are 3 networks currently visible.

SSID 1 : JIT-Stu
    Network type            : Infrastructure
    Authentication          : WPA2-Enterprise
    Encryption              : CCMP
    Signal                  : 90%

SSID 2 : JIT-Stu-5G
    Network type            : Infrastructure
    Authentication          : WPA2-Enterprise
    Signal                  : 70%

SSID 3 : AAACCCTTT
    Network type            : Infrastructure
    Authentication          : WPA2-Personal
    Signal                  : 55%
"""

NETWORKS_NO_LOCATION = """
Interface name : WLAN
Network shell commands need location permission to access WLAN information.
Or, to open the Location page in the Settings app, press Windows logo key + R:
The requested operation requires elevation.
"""

PROFILES_CN = """
接口 WLAN 上的配置文件:

用户配置文件
-------------------
    所有用户配置文件 : AAACCCTTT
    所有用户配置文件 : JIT-Stu
"""

PROFILES = """
Profiles on interface WLAN:

Group policy profiles (read only)
---------------------------------
    <None>

User profiles
------------------
    All User Profile     : AAACCCTTT
    All User Profile     : JIT-Stu
    All User Profile     : 1-102
"""


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        failures.append(f"{name}: {detail}")


def patch_run(mapping: dict[str, tuple[int, str]]):
    """把 wifi._run 换成一个按命令关键字返回固定输出的假实现。"""
    def fake_run(args, timeout=20):
        joined = " ".join(args)
        for key, value in mapping.items():
            if key in joined:
                return value
        return 0, ""
    return fake_run


def main() -> int:
    real_run = wifi._run

    print("== 读取当前 SSID ==")
    try:
        wifi._run = patch_run({"show interfaces": (0, IFACE_CONNECTED)})
        ssid, src = wifi.get_current_ssid()
        check("英文输出解析", ssid == "JIT-Stu", f"得到 {ssid!r}")
        check("来源为 netsh", src == "netsh", f"得到 {src!r}")

        wifi._run = patch_run({"show interfaces": (0, IFACE_CHINESE_CONNECTED)})
        ssid, src = wifi.get_current_ssid()
        check("中文输出解析", ssid == "JIT-Stu", f"得到 {ssid!r}")

        wifi._run = patch_run({
            "show interfaces": (1, IFACE_ELEVATION),
            "ipconfig": (0, "Wireless LAN adapter WLAN:\n\n   SSID                   : JIT-Stu\n   BSSID  : 11:22:33\n"),
        })
        ssid, src = wifi.get_current_ssid()
        check("权限不足时回退 ipconfig", ssid == "JIT-Stu" and src == "ipconfig", f"得到 {ssid!r}/{src!r}")

        wifi._run = patch_run({
            "show interfaces": (1, IFACE_ELEVATION),
            "ipconfig": (0, "Ethernet adapter Ethernet:\n   Media State . . . : Media disconnected\n"),
        })
        ssid, src = wifi.get_current_ssid()
        check("未连接时返回 None", ssid is None, f"得到 {ssid!r}")
    finally:
        wifi._run = real_run

    print("\n== 扫描周边 WiFi ==")
    try:
        wifi._run = patch_run({"show networks": (0, NETWORKS)})
        found, err = wifi.scan_visible_ssids()
        check("扫描出 3 个网络", found == ["JIT-Stu", "JIT-Stu-5G", "AAACCCTTT"], f"得到 {found}")
        check("无错误", err is None, f"得到 {err!r}")

        wifi._run = patch_run({"show networks": (1, NETWORKS_NO_LOCATION)})
        found, err = wifi.scan_visible_ssids()
        check("定位服务未开启时给出原因", found == [] and err is not None, f"得到 {found}/{err!r}")
    finally:
        wifi._run = real_run

    print("\n== 已保存配置检测 ==")
    try:
        wifi._run = patch_run({"show profiles": (0, PROFILES)})
        check("识别到 JIT-Stu 配置", wifi.have_saved_profile("JIT-Stu") is True)
        check("未保存的网络返回 False", wifi.have_saved_profile("NotExists") is False)

        wifi._run = patch_run({"show profiles": (1, IFACE_ELEVATION)})
        check("读不到配置时乐观处理", wifi.have_saved_profile("JIT-Stu") is True)

        wifi._run = patch_run({"show profiles": (0, PROFILES_CN)})
        check("中文系统配置列表", wifi.have_saved_profile("JIT-Stu") is True)
        check("中文系统未保存的网络", wifi.have_saved_profile("NotExists") is False)
    finally:
        wifi._run = real_run

    print("\n== 切网保护（安全阀）==")
    check("环境变量已生效", wifi.switching_disabled() is True, "JIT_NO_WIFI_SWITCH 未生效")
    real_run_for_guard = wifi._run
    guard_calls: list[str] = []
    try:
        wifi._run = lambda args, timeout=20: (guard_calls.append(" ".join(args)), (0, ""))[1]
        ok, reason = wifi.connect("JIT-Stu", timeout=2)
        check("保护开启时 connect 被拒绝", ok is False and "JIT_NO_WIFI_SWITCH" in reason, reason)
        check("保护开启时没有调用 netsh connect",
              not any("connect" in c for c in guard_calls), f"calls={guard_calls}")
    finally:
        wifi._run = real_run_for_guard

    print("\n== ensure_connected 决策（在桩命令下验证逻辑，不触碰真实网络）==")
    real_guard = wifi.switching_disabled
    # 只放开安全阀，_run 仍然是假的 → 永远不会真的执行 netsh connect
    wifi.switching_disabled = lambda: False  # type: ignore[assignment]
    try:
        # 已经在 JIT-Stu 上 → 直接通过，不发起连接
        calls: list[str] = []
        def fake_run_connected(args, timeout=20):
            calls.append(" ".join(args))
            if "show interfaces" in " ".join(args):
                return 0, IFACE_CONNECTED
            if "show networks" in " ".join(args):
                return 0, NETWORKS
            return 0, ""
        wifi._run = fake_run_connected
        ok, reason = wifi.ensure_connected("JIT-Stu", timeout=3)
        check("已连接时直接通过", ok and reason == "already-connected", f"{ok}/{reason!r}")
        check("没有发起 connect", not any("connect" in c for c in calls), f"calls={calls}")

        # 连的是别的网络，列表里有 JIT-Stu → 应该发起连接
        other_calls: list[str] = []
        def fake_run_other(args, timeout=20):
            joined = " ".join(args)
            other_calls.append(joined)
            if "show interfaces" in joined:
                return 0, IFACE_CONNECTED.replace("SSID                   : JIT-Stu", "SSID                   : AAACCCTTT")
            if "show networks" in joined:
                return 0, NETWORKS
            if "show profiles" in joined:
                return 0, PROFILES
            if "connect" in joined:
                return 0, "Connection request was completed successfully."
            return 0, ""
        wifi._run = fake_run_other
        ok, reason = wifi.ensure_connected("JIT-Stu", timeout=2)
        # 假的 show interfaces 永远显示 AAACCCTTT，所以会等到超时；关键是"确实发起了连接"
        check("列表里有目标网络时会尝试连接",
              any("connect" in c for c in other_calls), f"calls={other_calls}")
        check("连接未成功时返回失败（不会假装连上）", ok is False, f"{ok}/{reason!r}")

        # 列表里没有 JIT-Stu → 直接退出，不连接
        miss_calls: list[str] = []
        no_target = NETWORKS.replace("SSID 1 : JIT-Stu", "SSID 1 : OtherNet")
        def fake_run_missing(args, timeout=20):
            joined = " ".join(args)
            miss_calls.append(joined)
            if "show interfaces" in joined:
                return 0, IFACE_CONNECTED.replace("SSID                   : JIT-Stu", "SSID                   : AAACCCTTT")
            if "show networks" in joined:
                return 0, no_target
            return 0, ""
        wifi._run = fake_run_missing
        ok, reason = wifi.ensure_connected("JIT-Stu", timeout=2)
        check("未搜索到目标网络时退出", ok is False and "未搜索到" in reason, f"{ok}/{reason!r}")
        check("未搜索到时没有发起 connect",
              not any("connect" in c for c in miss_calls), f"calls={miss_calls}")

        # 扫描不可用且要求扫描 → 退出
        def fake_run_noscan(args, timeout=20):
            joined = " ".join(args)
            if "show interfaces" in joined:
                return 1, IFACE_ELEVATION
            if "show networks" in joined:
                return 1, NETWORKS_NO_LOCATION
            if "ipconfig" in joined:
                return 0, ""
            return 0, ""
        wifi._run = fake_run_noscan
        ok, reason = wifi.ensure_connected("JIT-Stu", timeout=2, require_visible=True,
                                           connect_if_scan_unavailable=False)
        check("无法扫描且要求扫描时退出", ok is False, f"{ok}/{reason!r}")
    finally:
        wifi._run = real_run
        wifi.switching_disabled = real_guard  # type: ignore[assignment]

    print("\n== 无线开关（WiFi 没开就先打开）==")
    real_radio_state = wifi.radio_state
    real_run_ps = wifi._run_powershell
    real_enable_netsh = wifi._enable_via_netsh
    real_guard2 = wifi.switching_disabled
    # 这一段用桩替换掉所有真实调用，因此可以放开安全阀；
    # 真实读取无线开关的用例放在后面单独做（只读）。
    wifi.switching_disabled = lambda: False  # type: ignore[assignment]
    try:
        # 已开启 → 不应有任何开关动作，也不应等待
        actions: list[str] = []
        wifi.radio_state = lambda: "On"  # type: ignore[assignment]
        wifi._run_powershell = lambda s, timeout=25: (actions.append("ps"), (0, ""))[1]  # type: ignore[assignment]
        t0 = time.time()
        ok, reason = wifi.enable_radio(wait_seconds=3)
        check("WiFi 已开时直接通过", ok is True and reason == "WiFi 已开启", f"{ok}/{reason!r}")
        check("已开时不做开关动作", actions == [], str(actions))
        check("已开时不等待", time.time() - t0 < 0.5, f"{time.time() - t0:.2f}s")

        # 关闭 → 打开成功：应调用开关、等待约 3 秒、复查状态
        seq = {"n": 0}
        actions.clear()

        def fake_state_flip():
            seq["n"] += 1
            return "Off" if seq["n"] == 1 else "On"

        wifi.radio_state = fake_state_flip  # type: ignore[assignment]
        wifi._run_powershell = lambda s, timeout=25: (actions.append("ps"), (0, "SET=Allowed"))[1]  # type: ignore[assignment]
        t0 = time.time()
        ok, reason = wifi.enable_radio(wait_seconds=3)
        elapsed = time.time() - t0
        check("关闭时会去打开", bool(actions), str(actions))
        check("打开后返回成功", ok is True, reason)
        check("等待了约 3 秒", 2.5 <= elapsed <= 4.5, f"{elapsed:.2f}s")
        check("打开后复查了状态", seq["n"] >= 2, str(seq))

        # 关闭 → 系统接口被拒 → 兜底启用网卡
        netsh_calls: list[str] = []
        wifi.radio_state = lambda: "Off"  # type: ignore[assignment]
        wifi._run_powershell = lambda s, timeout=25: (0, "SET=DeniedBySystem")  # type: ignore[assignment]
        wifi._enable_via_netsh = lambda: (netsh_calls.append("netsh"), True)[1]  # type: ignore[assignment]
        ok, reason = wifi.enable_radio(wait_seconds=0)
        check("系统接口失败时兜底启用网卡", bool(netsh_calls), str(netsh_calls))
        check("兜底后仍视为可继续", ok is True, reason)

        # 关闭 → 全部失败 → 返回失败并给出可操作提示
        wifi.radio_state = lambda: "Off"  # type: ignore[assignment]
        wifi._run_powershell = lambda s, timeout=25: (0, "SET=DeniedBySystem")  # type: ignore[assignment]
        wifi._enable_via_netsh = lambda: False  # type: ignore[assignment]
        ok, reason = wifi.enable_radio(wait_seconds=0)
        check("无法打开时返回失败", ok is False, reason)
        check("提示里包含手动指引", "手动打开" in reason, reason)

        # 读不到状态 → 不阻断，直接继续
        wifi.radio_state = lambda: None  # type: ignore[assignment]
        ok, reason = wifi.enable_radio(wait_seconds=0)
        check("读不到状态时不阻断", ok is True, reason)
    finally:
        wifi.radio_state = real_radio_state  # type: ignore[assignment]
        wifi._run_powershell = real_run_ps  # type: ignore[assignment]
        wifi._enable_via_netsh = real_enable_netsh  # type: ignore[assignment]
        wifi.switching_disabled = real_guard2  # type: ignore[assignment]

    print("\n== 切网保护对无线开关同样有效 ==")
    real_radio_state2 = wifi.radio_state
    try:
        wifi.radio_state = lambda: "Off"  # type: ignore[assignment]
        ok, reason = wifi.enable_radio(wait_seconds=0)
        check("保护开启时不真的打开 WiFi（但可继续）", ok is True, reason)
        check("说明里注明是测试模式", "测试模式" in reason, reason)
    finally:
        wifi.radio_state = real_radio_state2  # type: ignore[assignment]

    print("\n== 真实读取无线开关（只读）==")
    state = wifi.radio_state()
    check("能读到无线开关状态", state in ("On", "Off", "NOTFOUND", None), repr(state))
    print(f"  当前状态：{state}")

    print("\n== 参数安全处理 ==")
    check("去掉引号/换行", wifi.safe_ssid('a"b\nc') == "abc", repr(wifi.safe_ssid('a"b\nc')))

    print("\n" + ("全部通过" if not failures else f"失败 {len(failures)} 项："))
    for f in failures:
        print("  -", f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
