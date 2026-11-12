"""配置模块测试：默认值、合并、容错、校验、密码加密存取。

用法: python tests/test_config.py
不需要 config.json（用 config.example.json 和临时文件测）。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from jitauth import config, secret  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        failures.append(f"{name}: {detail}")


def main() -> int:
    print("== 默认值与路径 ==")
    check("CONFIG_PATH 指向项目根目录的 config.json",
          os.path.normcase(config.CONFIG_PATH) == os.path.normcase(os.path.join(ROOT, "config.json")),
          config.CONFIG_PATH)
    check("示例配置存在", os.path.isfile(config.EXAMPLE_PATH), config.EXAMPLE_PATH)
    check("WiFi 默认 JIT-Stu（本校固定）", config.DEFAULTS["wifi"]["ssid"] == "JIT-Stu",
          repr(config.DEFAULTS["wifi"]["ssid"]))
    check("运营商默认留空（必须自己选）", config.DEFAULTS["account"]["isp"] == "",
          repr(config.DEFAULTS["account"]["isp"]))
    check("门户地址默认 10.50.0.10", config.DEFAULTS["portal"]["url"] == "http://10.50.0.10/",
          config.DEFAULTS["portal"]["url"])

    print("\n== 读取示例配置 ==")
    example = config.load(config.EXAMPLE_PATH)
    check("成功读取", example["_loaded_ok"] is True, str(example["_loaded_ok"]))
    check("示例里没有密码", not example["account"]["password"])
    check("示例被判定为「需要设置」", config.needs_setup(example) is True,
          config.setup_reason(example))
    check("需要设置的原因提到学号", "学号" in config.setup_reason(example),
          config.setup_reason(example))
    check("示例校验会报缺账号", any("username" in p for p in config.validate(example)),
          str(config.validate(example)))

    print("\n== 合并与容错 ==")
    with tempfile.TemporaryDirectory() as tmp:
        partial = os.path.join(tmp, "partial.json")
        with open(partial, "w", encoding="utf-8") as fh:
            json.dump({"account": {"username": "u1"}, "wifi": {"ssid": "Other"}}, fh)
        cfg2 = config.load(partial)
        check("局部覆盖生效", cfg2["account"]["username"] == "u1" and cfg2["wifi"]["ssid"] == "Other")
        check("未覆盖项保留默认", cfg2["portal"]["url"] == "http://10.50.0.10/", cfg2["portal"]["url"])
        check("默认项完整", "script_timeout" in cfg2["browser"], str(cfg2["browser"]))

        broken = os.path.join(tmp, "broken.json")
        with open(broken, "w", encoding="utf-8") as fh:
            fh.write("{ this is not json ")
        cfg3 = config.load(broken)
        check("坏配置不崩溃并回退默认", cfg3["wifi"]["ssid"] == "JIT-Stu", repr(cfg3["wifi"]["ssid"]))
        check("坏配置标记为未读取", cfg3["_loaded_ok"] is False)

        missing = config.load(os.path.join(tmp, "nope.json"))
        check("文件不存在时回退默认", missing["portal"]["url"] == "http://10.50.0.10/")

        with open(os.path.join(tmp, "withmeta.json"), "w", encoding="utf-8") as fh:
            json.dump({"wifi": {"ssid": "X", "_comment": "注释不该进配置"}}, fh)
        cfg4 = config.load(os.path.join(tmp, "withmeta.json"))
        check("下划线注释键被忽略", "_comment" not in cfg4["wifi"], str(cfg4["wifi"]))

    print("\n== 校验逻辑 ==")
    empty = {"account": {"username": "", "password": ""}, "wifi": {"ssid": ""},
             "portal": {"url": "10.50.0.10"}}
    problems = config.validate(config._merge(config.DEFAULTS, empty))
    check("空账号被检出", any("username" in p for p in problems), str(problems))
    check("空密码被检出", any("password" in p for p in problems), str(problems))
    check("空运营商被检出", any("isp" in p for p in problems), str(problems))
    check("坏 URL 被检出", any("url" in p for p in problems), str(problems))

    print("\n== 明文保存 ==")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "config.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"wifi": {"ssid": "Keep"}, "portal": {"url": "http://a/"},
                       "keepalive": {"check_interval": 30}}, fh)
        used = config.save_account(path, username="20260001", password="pw123",
                                   isp="中国移动", store="plain")
        saved = json.load(open(path, encoding="utf-8"))
        check("明文模式返回 plain", used == "plain", used)
        check("密码写进 password", saved["account"].get("password") == "pw123",
              str(saved["account"]))
        check("没有残留密文", "password_enc" not in saved["account"], str(saved["account"]))
        check("其它配置被保留", saved["keepalive"]["check_interval"] == 30
              and saved["portal"]["url"] == "http://a/", str(saved))
        check("WiFi 名不被设置向导改动", saved["wifi"]["ssid"] == "Keep", str(saved["wifi"]))
        reloaded = config.load(path)
        check("重新加载后可用", not config.needs_setup(reloaded),
              config.setup_reason(reloaded))

    print("\n== 加密保存（DPAPI）==")
    if not secret.available():
        print("  (当前环境不支持 DPAPI，跳过)")
    else:
        check("protect/unprotect 往返", secret.unprotect(secret.protect("中文密码123")) == "中文密码123")
        check("密文带 dpapi: 前缀", secret.protect("x").startswith("dpapi:"))
        check("is_encrypted 识别正确",
              secret.is_encrypted(secret.protect("x")) and not secret.is_encrypted("plain"))
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            used = config.save_account(path, username="20260001", password="s3cret",
                                       isp="中国电信", store="dpapi")
            saved = json.load(open(path, encoding="utf-8"))
            check("加密模式返回 dpapi", used == "dpapi", used)
            check("文件里没有明文密码", "password" not in saved["account"], str(saved["account"]))
            check("文件里是密文", secret.is_encrypted(saved["account"].get("password_enc", "")),
                  str(saved["account"])[:60])
            reloaded = config.load(path)
            check("加载时自动解密", reloaded["account"]["password"] == "s3cret",
                  reloaded["account"].get("_password_error", ""))
            check("来源标记为 dpapi", reloaded["account"].get("_password_source") == "dpapi",
                  str(reloaded["account"].get("_password_source")))

    print("\n" + ("全部通过" if not failures else f"失败 {len(failures)} 项："))
    for f in failures:
        print("  -", f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())