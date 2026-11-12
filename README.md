# 校园网自动登录（Windows）

![platform](https://img.shields.io/badge/platform-Windows%2010%2F11-0078D6)
![python](https://img.shields.io/badge/python-3.10%2B-3776AB)
![license](https://img.shields.io/badge/license-MIT-green)

开机自动连上校园网并完成 Portal 认证；**登录之后继续盯着，掉线自动静默重连**。

只针对本校（JIT-Stu / `10.50.0.10`）写死，**你唯一要填的是学号、密码和运营商**。
不用装任何浏览器驱动，不碰你日常在用的浏览器，密码默认加密保存。

---

## 它解决什么问题

校园网一般有两种"掉线"：

1. **开机后没登录** —— 需要手动开浏览器点一次认证；
2. **登录后过一段时间又掉了** —— 网关空闲超时、会话到期、DHCP 换 IP、睡眠唤醒、WiFi 抖动，
   而大多数自动登录脚本只在开机跑一次，之后就不管了。

这个工具两件事都管：

| 手段 | 解决什么 | 代价 |
| --- | --- | --- |
| 开机登录一次（45 秒后） | 开机就要能上网 | 一个浏览器窗口 |
| 心跳保活（每 60 秒一次探针） | 网关"空闲超时"把你踢掉 | 几乎为零 |
| 看门狗 + 静默重登 | 会话到期、换 IP、唤醒、WiFi 抖动 | 一个常驻进程（约 30 MB） |

---

## 特性

- **只用程序自己的浏览器窗口**：独立的 `browser-profile/`，绝不启动、也绝不复用你日常在用的浏览器
- **不需要浏览器驱动**：直接用 DevTools 协议（CDP）填表提交，不用联网下载 msedgedriver
- **默认全静默重连**：掉线后用纯 HTTP 重登，不弹窗、不开浏览器
- **防误判**：多探针 + 连续失败阈值 + 门户二次确认，三道闸门都过了才重登
- **防锁号**：每小时最多重登 12 次、失败指数退避、密码错误立即停手 6 小时
- **防互踢**：连续出现"刚登录就掉"会退避 30 分钟，不参与和手机的拉锯
- **密码加密保存**：Windows DPAPI，密文只有本机本用户能解开
- **托盘图标**：颜色表示状态，右键可暂停/立即重连/改账号/看日志
- **纯标准库**：除可选的 Selenium 兜底外没有任何第三方依赖（托盘用 ctypes 手写）

---

## 快速开始

### 1. 准备

- Windows 10 / 11
- Python 3.10 或更高（安装时勾选 **Add Python to PATH**）
- 先手动连一次校园网 WiFi，让系统记住密码（之后程序用系统已保存的配置连接）

### 2. 下载并运行

```cmd
git clone https://github.com/<your-name>/campus-net-autologin.git
cd campus-net-autologin
run.bat
```

第一次运行会**自动弹出设置窗口**，只需要填三样：

```
┌─ 校园网自动登录 · 账号设置 ────────────────┐
│  学号 / 账号   [ 20260001____ ]            │
│  密码          [ ••••••••••   ] [显示]     │
│  运营商        [ 请选择          ▾ ]       │
│  选错套餐会上不了网                        │
│  密码保存方式  (•) 加密保存  ( ) 明文保存   │
│  [ 测试登录 ]        [ 取消 ] [ 保存 ]     │
└────────────────────────────────────────────┘
```

- **运营商必须自己选**：中国电信 / 中国移动 / 中国联通 / 校园网 —— 选错等于给别人交网费
- 想先验证账号对不对，点「测试登录」；如果当前已经在线，它会提示"无需再测"（不会把你踢下线）
- 保存后，密码默认加密成 `"password_enc": "dpapi:..."`

> WiFi 名（`JIT-Stu`）和门户地址（`10.50.0.10`）是写死的，界面上不出现，也不用改。
>
> 没有桌面环境（SSH / 纯命令行）时加 `--console`：
> `python -m jitauth --setup --console`

### 3. 注册开机自启 + 保活

```powershell
powershell -ExecutionPolicy Bypass -File install_task.ps1
```

会注册两个计划任务（普通权限即可，不需要管理员）：

| 任务 | 触发 | 作用 |
| --- | --- | --- |
| `JIT-Campus-AutoLogin` | 登录 Windows 后 45 秒 | 连 WiFi → 登录一次 → 退出 |
| `JIT-Campus-KeepAlive` | 登录 Windows 后 105 秒 | 常驻：每 60 秒巡检，掉线静默重登 |

常用参数：

```powershell
powershell -ExecutionPolicy Bypass -File install_task.ps1 -DelaySeconds 60  # 改延迟
powershell -ExecutionPolicy Bypass -File install_task.ps1 -NoWatchdog      # 只要开机登录
powershell -ExecutionPolicy Bypass -File install_task.ps1 -Status          # 看状态/日志
powershell -ExecutionPolicy Bypass -File install_task.ps1 -Diagnose        # 环境自检
powershell -ExecutionPolicy Bypass -File install_task.ps1 -Remove          # 全部卸载
```

---

## 命令行

```cmd
run.bat                          正常执行（登录一次）
run.bat --setup                  打开账号设置窗口
run.bat --setup --console        命令行问答式设置
run.bat --watch                  常驻保活（托盘图标）
run.bat --watch --no-tray        常驻保活，不要托盘
run.bat --status                 查看保活状态（不碰网络）
run.bat --verify-http-login      实测纯 HTTP 登录是否可用
run.bat --diagnose               抓取门户页面结构，排查识别问题
run.bat --browser                登录时显示浏览器窗口
run.bat --no-login               只打开登录页，不自动填表
run.bat --http-only              只用 HTTP 登录，不启动浏览器
run.bat --force                  已联网也强制登录一次
run.bat --skip-wifi              跳过 WiFi 检查
run.bat --config 路径            使用指定配置文件
```

---

## 配置

所有配置在 `config.json`（第一次保存账号时自动生成）。各字段都有注释，完整示例见
[`config.example.json`](config.example.json)。几个关键项：

```jsonc
{
  "wifi": { "ssid": "JIT-Stu" },              // 本校固定
  "portal": { "url": "http://10.50.0.10/" },  // 本校固定
  "account": {
    "username": "20260001",                   // ← 改这里
    "isp": "中国电信",                        // ← 改这里（电信/移动/联通/校园网）
    "password_enc": "dpapi:..."               // ← 设置窗口自动写；也可手写 "password": "明文"
  },
  "keepalive": {
    "check_interval": 60,                     // 巡检间隔（也是心跳）
    "fail_threshold": 3,                      // 连续几次不通才判定断线
    "relogin": {
      "enabled": true,                        // false = 只保活不重登
      "max_per_hour": 12,
      "backoff_seconds": [30, 120, 300, 900]
    }
  }
}
```

### 只改账号就行

这个工具只服务本校：WiFi 名固定 `JIT-Stu`，门户地址固定 `10.50.0.10`，
登录表单的字段名、提交地址、编码都是**自动识别**的。

所以日常唯一需要动的就是设置窗口里的三样：学号、密码、运营商。
换电脑、改密码时重新跑一次设置即可。

如果哪天登不上去，先 `run.bat --diagnose` 抓一份门户页面结构，能看出是不是门户改版了。

---

## 工作原理

### 登录（开机那一次）

```
检测/连接 WiFi → 外网探针判断是否已在线 → 看门户是否已是登录态
   → 未登录：拉起自己的浏览器窗口 → 注入脚本填账号密码+运营商 → 点登录 → 验证
   → 窗口起不来：退回纯 HTTP 提交表单
   → 都不行：把表单填好、窗口留着，你手动点一下
```

### 保活（登录之后）

```
每 60 秒：并发探 3 个独立端点（任一通即在线）
  在线 → 什么都不做
  连续 3 次不通 → 看门户：
      门户说"还在线"  → 只是探针被拦，不重登
      门户打不开      → 可能网关维护/WiFi 断了，先修 WiFi，等 5 分钟
      门户是登录页    → 确认掉线 → 纯 HTTP 静默重登 → 探针复查
```

失败按 `30s → 2min → 5min → 15min` 退避；一小时最多 12 次；密码错误直接暂停 6 小时。

### 托盘状态

| 颜色 | 含义 |
| --- | --- |
| 🟢 绿 | 在线 |
| 🟡 黄 | 疑似断线，确认中 |
| 🔵 蓝 | 正在重连 |
| 🔴 红 | 异常（重连失败 / 退避中） |
| ⚪ 灰 | 已暂停 |

右键菜单：立即检测 / 暂停保活 / **账号设置…** / 打开日志 / 退出。

---

## 安全

- **密码默认用 Windows DPAPI 加密**（`password_enc`），密文只能在同一台机器、同一个 Windows 用户下解开。
  把 `config.json` 拷给别人也解不出来。
- 想手动改密码就把保存方式选「明文」，或者直接在 `config.json` 里写 `"password"` 字段。
- 程序**只用自己独立的浏览器 profile**，不会读取、也不会污染你日常浏览器的 cookie。
- `config.json`、`logs/`、`browser-profile/` 都已经写进 `.gitignore`，不会被提交。
- 用 DPAPI 的代价：换电脑 / 重装系统后要重新输入一次密码（这是有意的取舍）。

---

## 常见问题

**Q：提示"缺少 JIT-Stu 的已保存配置"？**
先在 Windows 里手动连一次该 WiFi 并勾选"自动连接"，系统里有配置后程序才能免密重连。

**Q：提示"WiFi 扫描不可用 / 需要开启定位服务"？**
Windows 的 `netsh wlan show networks` 需要定位权限：设置 → 隐私和安全性 → 位置 → 打开。
或者把 `wifi.require_visible` 改成 `false`、`wifi.connect_if_scan_unavailable` 改成 `true`。

**Q：保活一直在重连但连不上？**
看 `logs/keepalive.log` 最后一次重登的返回内容：

| 日志 | 原因 | 处理 |
| --- | --- | --- |
| `门户提示账号/密码问题` | 密码改了/欠费 | 托盘「账号设置」改一下，改完自动生效 |
| `门户本身打不开` | 网关维护 / WiFi 断了 | 等，或检查 WiFi |
| `疑似被其他设备抢占` | 手机也登了这个账号 | 退出手机上的认证 |
| `探针连续 N 次不通，但门户显示仍在登录` | 探针被校园网拦了 | 改 `keepalive.probes` 里的地址 |

**Q：托盘图标没出现？**
可能被折叠进任务栏的「^」里；也可能是 `--no-tray` 启动的。
日志里如果有 `托盘不可用`，说明当前会话没有桌面，功能不受影响。

**Q：不想让它常驻？**
`install_task.ps1 -NoWatchdog` 重新注册，或者托盘右键「退出保活」+ `Stop-ScheduledTask -TaskName JIT-Campus-KeepAlive`。

**Q：HTTP 重登到底能不能用？**
在本校历史上验证过。建议先跑一次 `run.bat --verify-http-login`（先在门户点注销）确认；
如果不可用，把 `keepalive.relogin.enabled` 改成 `false`，退化成纯心跳保活。

---

## 开发

```cmd
rem 全部离线测试：不切网、不开浏览器、不需要真实门户
python tests\test_config.py
python tests\test_wifi.py
python tests\test_form_parse.py
python tests\test_flow.py
python tests\test_profile.py
python tests\test_judge.py
python tests\test_keepalive.py
```

`tests/test_keepalive.py` 会起一个本地 mock 门户（`tests/mock_portal.py`），
真实地走一遍"未登录 → 静默重登 → 被踢 → 再登回去"的完整流程。

可选依赖：只有 `browser.engine = "selenium"` 时才需要 Selenium。缺了不影响主路径，
要装的话：

```cmd
python tools\fetch_vendor.py
```

### 目录结构

```
jitauth/
  __main__.py     入口：命令行参数、主流程
  config.py       配置加载 / 保存 / 校验
  secret.py       DPAPI 密码加解密
  setup_ui.py     设置向导（tkinter + 控制台兜底）
  portal.py       门户表单识别、HTTP 登录、在线判定
  browser.py      程序自己的浏览器窗口（CDP / Selenium）
  cdp.py          极简 Chrome DevTools 协议客户端
  wifi.py         WiFi 检测 / 连接 / 开关
  keepalive.py    保活引擎：探针、状态机、静默重登、退避限流
  tray.py         托盘图标（纯 ctypes）
  logging_setup.py 日志（按天轮转）
tests/            离线测试 + mock 门户
tools/            fetch_vendor.py（可选依赖下载）
docs/             设计文档
```

---

## 免责声明

本项目仅用于自己设备的网络认证自动化，请遵守所在学校的网络使用规定。
不要在公共电脑上保存账号密码；不要把 `config.json` 上传到任何公开仓库。

## License

[MIT](LICENSE)