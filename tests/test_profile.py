"""浏览器 profile 选择测试（无头，不启动浏览器）。

验证：
* 默认（dedicated）**只**用程序自己的 profile，绝不出现你日常浏览器的 profile
* 只有显式 profile="user" 才会用到你自己的浏览器配置
* 自定义 user_data_dir（绝对/相对路径）生效
* 占用检测（SingletonLock / lockfile）
* 新版 CDP 路径的关键常量与语法

用法: python tests/test_profile.py
"""
from __future__ import annotations

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

os.environ.setdefault("JIT_NO_WIFI_SWITCH", "1")

from jitauth import browser as bm  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        failures.append(f"{name}: {detail}")


def types(cands: list[tuple[str, str, str]]) -> list[str]:
    return [c[2] for c in cands]


def main() -> int:
    print("== 默认配置：只用程序自己的窗口 ==")
    cfg = {"browser": {"browser": "edge", "user_data_dir": ""}}
    cands = bm._profile_candidates(cfg)
    check("只有 dedicated 一种候选", types(cands) == ["dedicated"], str(cands))
    check("没有出现你的浏览器 profile", "user" not in types(cands), str(cands))
    check("指向项目内 browser-profile",
          cands[0][0] == os.path.join(ROOT, "browser-profile"), cands[0][0])
    check("目录已创建", os.path.isdir(cands[0][0]), cands[0][0])

    print("\n== profile=dedicated：同样只用程序自己的窗口 ==")
    cfg = {"browser": {"browser": "edge", "profile": "dedicated", "user_data_dir": ""}}
    cands = bm._profile_candidates(cfg)
    check("只有 dedicated", types(cands) == ["dedicated"], str(cands))

    print("\n== profile=user：显式选择你自己的浏览器（不推荐）==")
    cfg = {"browser": {"browser": "edge", "profile": "user", "user_data_dir": ""}}
    cands = bm._profile_candidates(cfg)
    check("第一个是你的浏览器", types(cands)[0] == "user", str(cands))
    check("profile 名字非空", all(c[1] for c in cands), str(cands))
    check("指向 Edge 的 User Data",
          all("Microsoft" in c[0] and "Edge" in c[0] for c in cands), str(cands))

    print("\n== 浏览器类型影响 profile 路径 ==")
    cfg_edge = {"browser": {"browser": "edge", "profile": "user", "user_data_dir": ""}}
    cfg_chrome = {"browser": {"browser": "chrome", "profile": "user", "user_data_dir": ""}}
    edge_dir = bm._profile_candidates(cfg_edge)[0][0]
    chrome_dir = bm._profile_candidates(cfg_chrome)[0][0]
    check("Edge 与 Chrome 路径不同", edge_dir != chrome_dir, f"{edge_dir} vs {chrome_dir}")
    check("Chrome 路径含 Google", "Google" in chrome_dir, chrome_dir)

    print("\n== 占用检测 ==")
    with tempfile.TemporaryDirectory() as tmp:
        check("空目录不算被占用", bm._profile_locked(tmp) is False, tmp)
        lock = os.path.join(tmp, "SingletonLock")
        with open(lock, "w", encoding="utf-8") as fh:
            fh.write("x")
        check("有 SingletonLock 视为被占用", bm._profile_locked(tmp) is True, tmp)
        os.remove(lock)
        lockfile = os.path.join(tmp, "lockfile")
        with open(lockfile, "w", encoding="utf-8") as fh:
            fh.write("x")
        check("有 lockfile 视为被占用", bm._profile_locked(tmp) is True, tmp)

    print("\n== 自定义 user_data_dir ==")
    with tempfile.TemporaryDirectory() as tmp:
        custom = os.path.join(tmp, "my-profile")
        cfg = {"browser": {"browser": "edge", "profile": "dedicated", "user_data_dir": custom}}
        cands = bm._profile_candidates(cfg)
        check("使用自定义目录", cands[0][0] == custom, str(cands))
        check("目录已创建", os.path.isdir(custom), custom)
        rel = {"browser": {"browser": "edge", "profile": "dedicated", "user_data_dir": "rel-prof"}}
        cands_rel = bm._profile_candidates(rel)
        check("相对路径解析到项目目录",
              os.path.isabs(cands_rel[0][0]) and cands_rel[0][0].startswith(ROOT),
              cands_rel[0][0])

    print("\n== CDP 路径的关键约定 ==")
    check("调试端口是整数元组", isinstance(bm.CDP_PORTS, tuple) and all(isinstance(p, int) for p in bm.CDP_PORTS),
          str(bm.CDP_PORTS))
    check("防关闭脚本会覆盖 window.close", "window.close" in bm.ANTI_CLOSE_JS, bm.ANTI_CLOSE_JS)
    check("有统一的会话类", hasattr(bm, "BrowserSession"), "")
    check("窗口默认保留（close_after_login 默认 False）",
          bm._close_after_login({"browser": {}}) is False, "")
    check("显式关闭时才会关", bm._close_after_login({"browser": {"close_after_login": True}}) is True, "")
    check("缓存驱动查找不抛异常", isinstance(bm._find_cached_driver("edge"), str), "")
    check("本地门户脚本可用", bm.build_probe_promise("http://x/y").strip().startswith("return (function"),
          bm.build_probe_promise("http://x/y").strip()[:40])

    print("\n" + ("全部通过" if not failures else f"失败 {len(failures)} 项："))
    for f in failures:
        print("  -", f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
