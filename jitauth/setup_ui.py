"""账号设置向导：只问三件事——学号、密码、运营商。

这个工具只服务本校：WiFi 名（JIT-Stu）和门户地址（10.50.0.10）是固定的，
普通用户不需要、也不应该改，所以界面上干脆不出现。

两种形态，自动选：
* 图形窗口（默认）：第一次运行 run.bat 时自动弹出，也可以 `--setup` 手动打开
* 控制台问答：没有桌面时用，或者加 `--console`
"""
from __future__ import annotations

import copy
import getpass
import sys
import threading

from . import config as config_mod
from . import portal
from . import secret

ISP_CHOICES = ["中国电信", "中国移动", "中国联通", "校园网"]

TITLE = "校园网自动登录 · 账号设置"


# --------------------------------------------------------------------------- #
# 公共
# --------------------------------------------------------------------------- #
def _logger_to(status=None):
    """把日志转成 UI 状态文字，避免设置窗口往日志里刷。"""

    class _Bridge:
        def _emit(self, msg, *args):
            try:
                text = msg % args if args else msg
            except Exception:  # noqa: BLE001
                text = str(msg)
            if status is not None:
                status(text)
            else:
                print(text)

        def debug(self, msg, *a):
            self._emit(msg, *a)

        info = warning = error = debug

        def exception(self, msg, *a):
            self._emit(msg, *a)

    return _Bridge()


def run_setup(cfg_path: str | None = None, *, force_console: bool = False) -> int:
    """跑一次设置向导。返回 0 = 保存了，1 = 取消/失败。"""
    cfg = config_mod.load(cfg_path)
    if not force_console and _gui_available():
        try:
            return _run_gui(cfg, cfg_path)
        except Exception as exc:  # noqa: BLE001 - 图形起不来就退回控制台
            print(f"图形界面启动失败（{exc}），改用命令行模式")
    return _run_console(cfg, cfg_path)


def _gui_available() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import tkinter  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------- #
# 图形界面
# --------------------------------------------------------------------------- #
def _run_gui(cfg: dict, cfg_path: str | None) -> int:
    import tkinter as tk
    from tkinter import messagebox, ttk

    account = cfg.get("account") or {}
    state = {"saved": False}

    root = tk.Tk()
    root.title(TITLE)
    root.resizable(False, False)
    try:
        root.tk.call("tk", "scaling", 1.15)
    except Exception:  # noqa: BLE001
        pass

    body = ttk.Frame(root, padding=16)
    body.grid(row=0, column=0, sticky="nsew")
    body.columnconfigure(1, weight=1)

    # ---- 学号 ----
    ttk.Label(body, text="学号 / 账号").grid(row=0, column=0, sticky="w", padx=(0, 10), pady=8)
    var_user = tk.StringVar(value=str(account.get("username") or ""))
    entry_user = ttk.Entry(body, textvariable=var_user, width=26)
    entry_user.grid(row=0, column=1, columnspan=2, sticky="we", pady=8)

    # ---- 密码 ----
    ttk.Label(body, text="密码").grid(row=1, column=0, sticky="w", padx=(0, 10), pady=8)
    var_pass = tk.StringVar(value=str(account.get("password") or ""))
    entry_pass = ttk.Entry(body, textvariable=var_pass, width=26, show="•")
    entry_pass.grid(row=1, column=1, sticky="we", pady=8)

    var_show = tk.BooleanVar(value=False)
    ttk.Checkbutton(body, text="显示", variable=var_show,
                    command=lambda: entry_pass.config(show="" if var_show.get() else "•")
                    ).grid(row=1, column=2, sticky="w", padx=(8, 0))

    # ---- 运营商 ----
    ttk.Label(body, text="运营商").grid(row=2, column=0, sticky="w", padx=(0, 10), pady=8)
    var_isp = tk.StringVar(value=str(account.get("isp") or ""))
    combo_isp = ttk.Combobox(body, textvariable=var_isp, values=ISP_CHOICES,
                             state="readonly", width=24)
    combo_isp.grid(row=2, column=1, columnspan=2, sticky="we", pady=8)
    combo_isp.set("")  # 必须自己选，不给默认值

    ttk.Label(body, text="选错套餐会上不了网", foreground="#888").grid(
        row=3, column=0, columnspan=3, sticky="w", pady=(0, 6))

    # ---- 密码保存方式 ----
    var_store = tk.StringVar(value="dpapi" if secret.available() else "plain")
    if secret.available():
        store_box = ttk.LabelFrame(body, text="密码保存方式", padding=8)
        store_box.grid(row=4, column=0, columnspan=3, sticky="we", pady=(4, 0))
        ttk.Radiobutton(store_box, text="加密保存（推荐，只有这台电脑能解开）",
                        value="dpapi", variable=var_store).grid(row=0, column=0, sticky="w", padx=4)
        ttk.Radiobutton(store_box, text="明文保存（方便手动改 config.json）",
                        value="plain", variable=var_store).grid(row=1, column=0, sticky="w", padx=4)
    else:
        var_store.set("plain")

    # ---- 状态 ----
    var_status = tk.StringVar(value="填好之后点「保存」；想先验证可以点「测试登录」")
    label_status = ttk.Label(body, textvariable=var_status, foreground="#555", wraplength=380)
    label_status.grid(row=5, column=0, columnspan=3, sticky="w", pady=(10, 2))

    buttons = ttk.Frame(body)
    buttons.grid(row=6, column=0, columnspan=3, sticky="e", pady=(6, 0))

    btn_test = ttk.Button(buttons, text="测试登录")
    btn_test.grid(row=0, column=0, padx=6)
    btn_cancel = ttk.Button(buttons, text="取消", command=root.destroy)
    btn_cancel.grid(row=0, column=1, padx=6)
    btn_save = ttk.Button(buttons, text="保存", command=lambda: on_save())
    btn_save.grid(row=0, column=2, padx=6)

    def set_status(text: str, color: str = "#555") -> None:
        var_status.set(text)
        label_status.config(foreground=color)

    def form_cfg() -> dict:
        test_cfg = copy.deepcopy(cfg)
        test_cfg.setdefault("account", {}).update({
            "username": var_user.get().strip(),
            "password": var_pass.get(),
            "isp": var_isp.get().strip(),
        })
        return test_cfg

    def validate_form() -> str:
        if not var_user.get().strip():
            return "请填写学号/账号"
        if not var_pass.get():
            return "请填写密码"
        if not var_isp.get().strip():
            return "请选择运营商"
        return ""

    # ---- 测试登录 ----
    def on_test() -> None:
        problem = validate_form()
        if problem:
            set_status(problem, "#b45309")
            return
        btn_test.state(["disabled"])
        btn_save.state(["disabled"])
        set_status("正在检测当前网络 …")

        def worker() -> None:
            try:
                test_cfg = form_cfg()
                from . import keepalive

                endpoints = keepalive.build_endpoints(test_cfg)
                timeout = float(test_cfg["portal"].get("probe_timeout", 5))
                if keepalive.probe(endpoints, timeout).online:
                    root.after(0, lambda: set_status(
                        "当前网络已经连通（现在就是登录状态），无需再测。", "#15803d"))
                    return
                root.after(0, lambda: set_status("当前离线，正在用这组账号尝试 HTTP 登录 …"))
                ok, message = portal.login_via_http(
                    test_cfg, _logger_to(lambda text: root.after(
                        0, lambda: set_status(text))))
                if not ok:
                    root.after(0, lambda: set_status(f"登录失败：{message}", "#b91c1c"))
                    return
                if keepalive.probe(endpoints, timeout).online:
                    root.after(0, lambda: set_status("测试成功：这组账号可以正常登录。", "#15803d"))
                else:
                    root.after(0, lambda: set_status(
                        "接口返回成功，但外网还不通（可能只是统计延迟），可以先保存观察。", "#b45309"))
            except Exception as exc:  # noqa: BLE001
                root.after(0, lambda: set_status(f"测试出错：{exc}", "#b91c1c"))
            finally:
                root.after(0, lambda: (btn_test.state(["!disabled"]),
                                       btn_save.state(["!disabled"])))

        threading.Thread(target=worker, daemon=True).start()

    # ---- 保存 ----
    def on_save() -> None:
        problem = validate_form()
        if problem:
            set_status(problem, "#b45309")
            return
        try:
            used = config_mod.save_account(
                cfg_path, username=var_user.get().strip(), password=var_pass.get(),
                isp=var_isp.get().strip(), store=var_store.get())
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(TITLE, f"保存失败：{exc}")
            return
        state["saved"] = True
        how = "已加密保存（DPAPI）" if used == "dpapi" else "已明文保存"
        messagebox.showinfo(TITLE, f"账号已保存。\n{how}\n\n"
                                   "如果保活已经在跑，它会自动重新加载配置，不用重启。")
        root.destroy()

    btn_test.config(command=on_test)
    entry_user.focus_set()
    root.bind("<Return>", lambda _e: on_save())
    root.bind("<Escape>", lambda _e: root.destroy())
    _center(root)
    root.mainloop()
    return 0 if state["saved"] else 1


def _center(window) -> None:
    try:
        window.update_idletasks()
        width = window.winfo_width() or window.winfo_reqwidth()
        height = window.winfo_height() or window.winfo_reqheight()
        x = (window.winfo_screenwidth() - width) // 2
        y = (window.winfo_screenheight() - height) // 3
        window.geometry(f"+{x}+{y}")
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# 控制台
# --------------------------------------------------------------------------- #
def _run_console(cfg: dict, cfg_path: str | None) -> int:
    account = cfg.get("account") or {}

    print()
    print("=" * 48)
    print("  校园网自动登录 · 账号设置")
    print("=" * 48)

    current_user = str(account.get("username") or "")
    user = input(f"学号/账号{f' [{current_user}]' if current_user else ''}: ").strip() or current_user
    if not user:
        print("没有填写学号，已取消。")
        return 1

    try:
        password = getpass.getpass("密码（输入时不显示）: ")
    except (EOFError, KeyboardInterrupt):
        print("\n已取消。")
        return 1
    if not password:
        print("没有填写密码，已取消。")
        return 1

    print("\n运营商：")
    for index, name in enumerate(ISP_CHOICES, start=1):
        mark = " *" if name == str(account.get("isp") or "") else ""
        print(f"  {index}. {name}{mark}")
    current_isp = str(account.get("isp") or "")
    hint = f" [{current_isp}]" if current_isp else ""
    while True:
        raw = input(f"请选择 1-{len(ISP_CHOICES)}{hint}: ").strip()
        if not raw and current_isp:
            isp = current_isp
            break
        if raw.isdigit() and 1 <= int(raw) <= len(ISP_CHOICES):
            isp = ISP_CHOICES[int(raw) - 1]
            break
        if raw in ISP_CHOICES:
            isp = raw
            break
        print("  请输入序号，或直接输入运营商名称。")

    store = "plain"
    if secret.available():
        answer = input("\n密码加密保存吗？只有这台电脑能解开 [Y/n]: ").strip().lower()
        store = "plain" if answer in ("n", "no") else "dpapi"

    used = config_mod.save_account(cfg_path, username=user, password=password,
                                   isp=isp, store=store)
    print()
    print("已保存到:", cfg_path or config_mod.CONFIG_PATH)
    print("保存方式:", "DPAPI 加密" if used == "dpapi" else "明文")
    print("运行 'run.bat' 试一次登录。")
    return 0