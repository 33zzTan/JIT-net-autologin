"""Windows 托盘图标（纯 ctypes，不依赖 pystray / pywin32）。

为什么要自己写
--------------
项目一直坚持"只用标准库 + 自带依赖"，`vendor/` 里只有 Selenium。
pystray / pywin32 都要额外安装，违背了这个原则，所以这里直接调 shell32
的 ``Shell_NotifyIconW``，几十行 ctypes 就能实现托盘 + 气泡通知。

功能
----
* 托盘图标按状态变色：绿=在线，橙=疑似断线/异常，红=重连失败，灰=已暂停
* 悬停提示显示当前状态、运行时长、重登次数
* 右键菜单：立即检测 / 暂停或恢复 / 打开日志 / 打开配置目录 / 退出
* 断线、重连成功、账号错误等事件用系统气泡通知

线程模型
--------
Win32 的窗口消息必须在**创建窗口的那个线程**里泵，所以：

* 主线程：``run()`` → 创建隐藏窗口 + 托盘图标 + 消息循环
* 后台线程：保活引擎循环
* 引擎线程通过 ``notify()`` 直接调 Shell_NotifyIcon 发气泡（shell 内部是消息投递，线程安全）
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import threading

WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_COMMAND = 0x0111
WM_TIMER = 0x0113
WM_NULL = 0x0000
WM_USER = 0x0400
WM_TRAYICON = WM_USER + 1
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205

NIM_ADD = 0
NIM_MODIFY = 1
NIM_DELETE = 2
NIM_SETVERSION = 4

NIF_MESSAGE = 0x01
NIF_ICON = 0x02
NIF_TIP = 0x04
NIF_INFO = 0x10
NIF_SHOWTIP = 0x80

NIIF_NONE = 0
NIIF_INFO = 1
NIIF_WARNING = 2
NIIF_ERROR = 3

NOTIFYICON_VERSION_4 = 4

MF_STRING = 0x0000
MF_SEPARATOR = 0x0800
MF_GRAYED = 0x0001
MF_DISABLED = 0x0002

TPM_LEFTALIGN = 0x0000
TPM_BOTTOMALIGN = 0x0020
TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100
TPM_NONOTIFY = 0x0080

IDI_APPLICATION = 32512
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
LR_DEFAULTSIZE = 0x0040

ID_CHECK = 1001
ID_PAUSE = 1002
ID_LOG = 1003
ID_FOLDER = 1004
ID_CONFIG = 1005
ID_QUIT = 1006
ID_ACCOUNT = 1007

TIMER_ID = 1
TIMER_MS = 2000

CLASS_NAME = "JITAUTH_TrayWindow"

#: 状态 → (图标颜色, 提示前缀)
STATE_STYLE = {
    "ONLINE": ((52, 168, 83), "在线"),
    "SUSPECT": ((251, 188, 5), "疑似断线"),
    "RECOVERING": ((66, 133, 244), "正在重连"),
    "DEGRADED": ((234, 67, 53), "异常"),
    "PAUSED": ((120, 124, 128), "已暂停"),
}


class TrayUnavailable(RuntimeError):
    """没有桌面 / 托盘创建失败。调用方应退回无界面模式。"""


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wt.DWORD),
        ("Data2", wt.WORD),
        ("Data3", wt.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("hWnd", wt.HWND),
        ("uID", wt.UINT),
        ("uFlags", wt.UINT),
        ("uCallbackMessage", wt.UINT),
        ("hIcon", wt.HICON),
        ("szTip", wt.WCHAR * 128),
        ("dwState", wt.DWORD),
        ("dwStateMask", wt.DWORD),
        ("szInfo", wt.WCHAR * 256),
        ("uVersion", wt.UINT),
        ("szInfoTitle", wt.WCHAR * 64),
        ("dwInfoFlags", wt.DWORD),
        ("guidItem", GUID),
        ("hBalloonIcon", wt.HICON),
    ]


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wt.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE),
        ("hIcon", wt.HICON),
        ("hCursor", wt.HANDLE),
        ("hbrBackground", wt.HBRUSH),
        ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR),
    ]


def _load_api():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    user32.DefWindowProcW.restype = ctypes.c_ssize_t
    user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    user32.CreateWindowExW.restype = wt.HWND
    user32.CreateWindowExW.argtypes = [
        wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID,
    ]
    user32.DestroyWindow.argtypes = [wt.HWND]
    user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
    user32.DispatchMessageW.restype = ctypes.c_ssize_t
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
    user32.PostQuitMessage.argtypes = [ctypes.c_int]
    user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    user32.LoadIconW.restype = wt.HICON
    user32.LoadIconW.argtypes = [wt.HINSTANCE, wt.LPCWSTR]
    user32.LoadImageW.restype = wt.HANDLE
    user32.LoadImageW.argtypes = [wt.HINSTANCE, wt.LPCWSTR, wt.UINT, ctypes.c_int,
                                  ctypes.c_int, wt.UINT]
    user32.CreatePopupMenu.restype = wt.HMENU
    user32.AppendMenuW.argtypes = [wt.HMENU, wt.UINT, ctypes.c_size_t, wt.LPCWSTR]
    user32.TrackPopupMenu.restype = ctypes.c_int
    user32.TrackPopupMenu.argtypes = [wt.HMENU, wt.UINT, ctypes.c_int, ctypes.c_int,
                                      ctypes.c_int, wt.HWND, wt.LPVOID]
    user32.DestroyMenu.argtypes = [wt.HMENU]
    user32.SetForegroundWindow.argtypes = [wt.HWND]
    user32.SetTimer.argtypes = [wt.HWND, ctypes.c_size_t, wt.UINT, wt.LPVOID]
    user32.KillTimer.argtypes = [wt.HWND, ctypes.c_size_t]
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
    user32.GetMessageW.restype = ctypes.c_int

    shell32.Shell_NotifyIconW.restype = wt.BOOL
    shell32.Shell_NotifyIconW.argtypes = [wt.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]

    kernel32.GetModuleHandleW.restype = wt.HMODULE
    kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
    return user32, shell32, kernel32


class TrayApp:
    """托盘图标 + 右键菜单。``run()`` 会阻塞直到用户点"退出"。"""

    def __init__(self, engine, logger, *, log_path: str, config_path: str,
                 app_dir: str) -> None:
        self.engine = engine
        self.logger = logger
        self.log_path = log_path
        self.config_path = config_path
        self.app_dir = app_dir

        self._user32, self._shell32, self._kernel32 = _load_api()
        self._hwnd = None
        self._icons: dict[str, int] = {}
        self._icon_files: dict[str, str] = {}
        self._lock = threading.Lock()
        self._quit = threading.Event()
        self._wndproc = None
        self._last_state = ""
        self._last_tip = ""

    # ---------------- 图标 ---------------- #
    def _icon_path(self, state: str) -> str:
        color = STATE_STYLE.get(state, STATE_STYLE["PAUSED"])[0]
        path = os.path.join(self.app_dir, ".cache", "tray", f"tray-{state.lower()}.ico")
        if os.path.exists(path):
            return path
        try:
            from PIL import Image, ImageDraw  # 有就用，画个圆点

            image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            draw = ImageDraw.Draw(image)
            draw.ellipse((6, 6, 58, 58), fill=color + (255,), outline=(255, 255, 255, 255), width=4)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            image.save(path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64)])
            return path
        except Exception:  # noqa: BLE001 - 没有 PIL 就用系统默认图标
            return ""

    def _icon(self, state: str) -> int:
        if state in self._icons:
            return self._icons[state]
        handle = None
        path = self._icon_path(state)
        if path:
            handle = self._user32.LoadImageW(None, path, IMAGE_ICON, 0, 0, LR_LOADFROMFILE)
        if not handle:
            handle = self._user32.LoadIconW(None, ctypes.cast(IDI_APPLICATION, wt.LPCWSTR))
        self._icons[state] = int(handle or 0)
        self._icon_files[state] = path
        return self._icons[state]

    # ---------------- 托盘 ---------------- #
    def _nid(self, flags: int) -> NOTIFYICONDATAW:
        data = NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        data.hWnd = self._hwnd
        data.uID = 1
        data.uFlags = flags
        data.uCallbackMessage = WM_TRAYICON
        return data

    def _add_icon(self) -> None:
        data = self._nid(NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_SHOWTIP)
        data.hIcon = self._icon("ONLINE")
        data.szTip = "校园网保活：启动中"
        if not self._shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(data)):
            raise TrayUnavailable("Shell_NotifyIcon(NIM_ADD) 失败（可能没有桌面会话）")
        version = self._nid(NIF_SHOWTIP)
        version.uVersion = NOTIFYICON_VERSION_4
        self._shell32.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(version))

    def _remove_icon(self) -> None:
        if not self._hwnd:
            return
        self._shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid(0)))

    def _refresh(self) -> None:
        """把引擎状态映射到图标和悬停提示（每 2 秒一次）。"""
        try:
            snap = self.engine.snapshot()
        except Exception:  # noqa: BLE001
            return
        state = str(snap.get("state") or "ONLINE")
        if snap.get("paused"):
            state = "PAUSED"
        minutes = float(snap.get("uptime_seconds") or 0) / 60
        tip = (f"校园网保活：{snap.get('status_text')}｜已运行 {minutes:.0f} 分钟"
               f"｜重登 {snap.get('relogin_success_total', 0)} 次")
        if tip == self._last_tip and state == self._last_state:
            return
        data = self._nid(NIF_ICON | NIF_TIP | NIF_SHOWTIP)
        data.hIcon = self._icon(state)
        data.szTip = tip[:127]
        self._shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(data))
        self._last_state = state
        self._last_tip = tip

    def notify(self, level: str, title: str, text: str) -> None:
        """发一个系统气泡通知（引擎线程调用）。"""
        if not self._hwnd:
            return
        flags = {"info": NIIF_INFO, "warning": NIIF_WARNING, "error": NIIF_ERROR}.get(
            level, NIIF_INFO)
        with self._lock:
            data = self._nid(NIF_INFO)
            data.szInfoTitle = (title or "校园网保活")[:63]
            data.szInfo = (text or "")[:255]
            data.dwInfoFlags = flags
            self._shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(data))

    # ---------------- 菜单 ---------------- #
    def _show_menu(self) -> None:
        snap = self.engine.snapshot()
        paused = bool(snap.get("paused"))
        minutes = float(snap.get("uptime_seconds") or 0) / 60
        status_line = (f"状态：{snap.get('status_text')}　运行 {minutes:.0f} 分钟　"
                       f"重登 {snap.get('relogin_success_total', 0)} 次")

        menu = self._user32.CreatePopupMenu()
        try:
            self._user32.AppendMenuW(menu, MF_STRING | MF_GRAYED, 0, status_line)
            self._user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            self._user32.AppendMenuW(menu, MF_STRING, ID_CHECK, "立即检测 / 重连")
            self._user32.AppendMenuW(menu, MF_STRING, ID_PAUSE,
                                     "恢复保活" if paused else "暂停保活 30 分钟")
            self._user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            self._user32.AppendMenuW(menu, MF_STRING, ID_ACCOUNT, "账号设置…")
            self._user32.AppendMenuW(menu, MF_STRING, ID_LOG, "打开日志")
            self._user32.AppendMenuW(menu, MF_STRING, ID_CONFIG, "打开配置文件")
            self._user32.AppendMenuW(menu, MF_STRING, ID_FOLDER, "打开程序目录")
            self._user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            self._user32.AppendMenuW(menu, MF_STRING, ID_QUIT, "退出保活")

            point = wt.POINT()
            self._user32.GetCursorPos(ctypes.byref(point))
            # 经典坑：不先 SetForegroundWindow，点菜单外面菜单不会消失
            self._user32.SetForegroundWindow(self._hwnd)
            cmd = self._user32.TrackPopupMenu(
                menu, TPM_LEFTALIGN | TPM_BOTTOMALIGN | TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY,
                point.x, point.y, 0, self._hwnd, None)
            self._user32.PostMessageW(self._hwnd, WM_NULL, 0, 0)
            if cmd:
                self._on_command(int(cmd))
        finally:
            self._user32.DestroyMenu(menu)

    def _on_command(self, cmd: int) -> None:
        if cmd == ID_CHECK:
            self.engine.request("check")
        elif cmd == ID_PAUSE:
            if self.engine.snapshot().get("paused"):
                self.engine.request("resume")
            else:
                self.engine.request("pause", minutes=30)
        elif cmd == ID_ACCOUNT:
            self._open_setup()
        elif cmd in (ID_LOG, ID_FOLDER, ID_CONFIG):
            target = {ID_LOG: self.log_path, ID_FOLDER: self.app_dir,
                      ID_CONFIG: self.config_path}[cmd]
            self._open(target)
        elif cmd == ID_QUIT:
            self.logger.info("用户从托盘菜单退出保活")
            self.engine.request("stop")
            self._quit.set()
            self._user32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)

    def _open_setup(self) -> None:
        """打开账号设置窗口。

        用子进程而不是在托盘线程里直接开 tkinter：托盘的消息循环不能阻塞，
        而且设置完保存 config.json 后，保活引擎靠 mtime 变化自动热加载，不用重启。
        """
        import subprocess
        import sys

        exe = sys.executable or "pythonw.exe"
        try:
            subprocess.Popen([exe, "-m", "jitauth", "--setup"],
                             cwd=self.app_dir, close_fds=True)
            self.logger.info("已打开账号设置窗口")
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("打开账号设置窗口失败：%s", exc)

    @staticmethod
    def _open(path: str) -> None:
        try:
            os.startfile(path)  # noqa: S606 - 打开本地文件，路径来自本程序自身
        except Exception:  # noqa: BLE001
            pass

    # ---------------- 消息循环 ---------------- #
    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        if msg == WM_TRAYICON:
            event = int(lparam) & 0xFFFF
            if event in (WM_RBUTTONUP, WM_LBUTTONUP):
                self._show_menu()
            elif event == WM_LBUTTONDBLCLK:
                self._open(self.log_path)
            return 0
        if msg == WM_TIMER:
            self._refresh()
            return 0
        if msg == WM_COMMAND:
            self._on_command(int(wparam) & 0xFFFF)
            return 0
        if msg == WM_CLOSE:
            self._user32.KillTimer(hwnd, TIMER_ID)
            self._remove_icon()
            self._user32.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            self._user32.PostQuitMessage(0)
            return 0
        return self._user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def run(self) -> int:
        hinst = self._kernel32.GetModuleHandleW(None)
        self._wndproc = WNDPROC(self._wnd_proc)

        wc = WNDCLASSW()
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = hinst
        wc.lpszClassName = CLASS_NAME
        atom = self._user32.RegisterClassW(ctypes.byref(wc))
        if not atom:
            # 已经注册过也无所谓（重复启动的场景）
            if ctypes.get_last_error() not in (1410,):  # ERROR_CLASS_ALREADY_EXISTS
                raise TrayUnavailable(f"RegisterClassW 失败（错误码 {ctypes.get_last_error()}）")

        self._hwnd = self._user32.CreateWindowExW(
            0, CLASS_NAME, "校园网保活", 0, 0, 0, 0, 0, None, None, hinst, None)
        if not self._hwnd:
            raise TrayUnavailable(f"CreateWindowExW 失败（错误码 {ctypes.get_last_error()}）")

        self._add_icon()
        self._user32.SetTimer(self._hwnd, TIMER_ID, TIMER_MS, None)
        self._refresh()
        self.logger.info("托盘图标已就绪（右键可查看状态/暂停/退出）")

        msg = wt.MSG()
        while self._user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            self._user32.TranslateMessage(ctypes.byref(msg))
            self._user32.DispatchMessageW(ctypes.byref(msg))
        self._remove_icon()
        return 0

    def stop(self) -> None:
        """从别的线程请求退出。"""
        if self._hwnd:
            self._user32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)
        else:
            self._quit.set()