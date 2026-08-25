# InkScry

EPD-nRF5 固件墨水屏（4.2 寸三色）专用的 Claude Code / Codex 状态仪表盘。
电脑端渲染 1-bit 位图 → BLE 分包推送 → 事件驱动刷新，不修改设备固件。

## 架构

```
Claude Code ──hooks──> inkscry.cli ──> renderer (PIL, 400x300 双平面)
                                     ├─> monitor (解析 ~/.claude/projects/*.jsonl)
                                     └─> quota   (Codex/Claude/Kimi/GLM/MiniMax/DeepSeek/NewAPI/Sub2API/Mirasim)
                                            │
                                     ble.py (bleak) ──BLE──> 墨水屏
```

## 硬件实测档案（EPD-nRF5 4.2 寸三色屏，2026-08 验证）

- 广播名由固件/卖家配置决定（InkScry 按名称扫描时不区分大小写）；macOS 可按 UUID 直连，无需广播
- 实际面板为 **UC8176 三色屏**（400x300，黑/白/红，`model_id=0x03`），非手册描述的纯黑白屏
- 固件 APP_VERSION `0x19`，**不支持 RLE**（mtu 上报无 `rle=1`），走老版半字节 header
- 固件在收到 `INIT` 之后才上报 `mtu=244`，必须在 INIT 后再协商
- 订阅 notify 后固件先推 20 字节二进制 `epd_config_t`（model_id 在 offset 7）
- 电压可测但固件未暴露到 BLE（仅供自带日历/时钟 GUI 使用），读取需自定义固件

## BLE 协议（对齐 tsl0922/EPD-nRF5，含固件源码核对）

- Service `62750001-…`，Characteristic `62750002-…`（Write + Notify），`62750003-…`（版本）
- 指令：`0x01` INIT / `0x02` CLEAR / `0x03` SEND_CMD / `0x04` SEND_DATA / `0x05` REFRESH
  / `0x06` SLEEP / `0x20` SET_TIME / `0x30` WRITE_IMG
- 上屏序列：`INIT → 等 mtu= → WRITE_IMG 分包（黑白面）→（三色机：红色面）→ REFRESH`
- 分包：`[0x30, header, ...chunk]`，chunk = MTU − 2
- header（老固件，半字节语义：高=0 起始 / 低=F 黑白面）：
  黑白面首包 `0x0F`/后续 `0xFF`；红色面首包 `0x00`/后续 `0xF0`
- header（新固件 RLE，bit 语义：bit0=红面 bit1=首包 bit2=RLE）：`0x06/0x04`、`0x07/0x05`
- 像素格式：行主序、MSB 先、白=1 黑=0（与 PIL mode "1" 的 `tobytes()` 一致）；红面 bit=0 为红
- 流控：每 8 个免应答写插入 1 个带应答写；实测全屏双平面推送 ~9s
- `0x03/0x04` 是 SPI 透传后门，可用于局部窗口写入（见 TODO.md 局部刷新）

## 安装

```bash
pip install .                     # 装成 inkscry 命令（下文 python -m inkscry.cli 均可换用 inkscry）
pip install '.[menubar]'          # 可选：macOS 菜单栏程序 inkscry-bar（rumps）
pip install '.[tray]'             # 可选：Windows/Linux 托盘程序 inkscry-tray（pystray）
# 或只装依赖跑源码：pip install -r requirements.txt
```

支持 macOS / Windows / Linux（bleak 跨平台；Windows 端 BLE 尚未真机
实测）。配置在 `.env` 中（首次使用 `cp .env.example .env` 后填写；
`.env` 含真实凭据，已在 .gitignore 排除）：

```
INKSCRY_DEVICE_NAME=<广播名>
# 填了直连，跳过扫描（更可靠）。macOS 填 CoreBluetooth UUID；
# Windows/Linux 填设备 MAC 地址（AA:BB:CC:DD:EE:FF）
INKSCRY_DEVICE_ADDRESS=<地址>
```

## 使用

```bash
# BLE 推送（需要设备在旁并开机）
python -m inkscry.cli --demo            # 推送演示仪表盘
python -m inkscry.cli --event stop      # 从会话日志取真实数据推送
python -m inkscry.cli --clear           # 清屏
python -m inkscry.cli --sleep           # 屏幕驱动休眠（BLE 不受影响）

# 无硬件调试：只渲染保存预览图
python -m inkscry.cli --demo --save demo.bmp --no-ble

# 额度查询（所有已配置/自动识别的供应商）
python -m inkscry.cli --quota           # 仅打印额度（强制联网刷新）

# 生成 hooks 配置
python -m inkscry.cli --print-hooks

# 定时同步：额度有变化才刷屏（详见「定时同步」一节）
inkscry-bar                             # macOS 菜单栏常驻程序（推荐）
python -m inkscry.cli --watch           # 常驻命令行，默认每 15 分钟检查
python -m inkscry.cli --sync            # 单次检查，配合系统定时器
```

把 `--print-hooks` 输出的 `"hooks"` 块合并进 `~/.claude/settings.json`。
命令按当前平台生成：macOS/Linux 结尾 `&` 后台执行，Windows 用
`start /b`（cmd 语法；WSL 用户请在 WSL 里重新生成），推送不阻塞
Claude 主流程。

## 三色屏支持

设备为三色屏时自动双平面传输（黑白面 + 红色面），`WAITING`/`ERROR`
告警状态的顶部状态栏渲染为红底白字。机型通过连接时的配置上报自动识别
（`protocol.MODEL_INFO`），黑白机型自动忽略红面。

注意：三色屏必须同时写红面，否则残留旧红面数据会花屏。

## 订阅额度（多面板分列）

中部为额度面板区：Codex + 已配置的供应商（Claude/Kimi/GLM/MiniMax/
DeepSeek/NewAPI/Sub2API/Mirasim），订阅制面板显示 5h/1w 窗口剩余量（大数字 +
进度条 + 重置时间；百分比按数据源真实精度显示——小数位非零才带
一位小数，Codex/Kimi 等上游为整数百分点粒度的家显示整数，
mini 窄档一律整数），余额制面板（DeepSeek/NewAPI/Sub2API）显示余额。布局自适应：≤2 个分列排；3 个起切成 2 列
× ⌈n/2⌉ 行网格（列宽 200px；面板内 5h/1w 左右横排，最多 6 个）。
所有布局面板内容均按统一满配高度垂直居中（缺失的重置行/备注行
保留占位空间，标题与数字跨面板严格对齐）；空位一律画
`+` 占位（单面板的右半、奇数网格的末槽）；只有零面板时中部才
显示最近工具调用。窄格自动缩小字号。5 分钟缓存（`~/.cache/inkscry/`，按供应商分文件）。
查询失败回退过期缓存时，该面板**不再显示误导性的旧数字**，
整块换成红色「数据过期」占位（黑白屏黑字），底栏同时列名。
`--no-quota` 整体跳过；`--quota` 单独打印（强制联网刷新）。

面板默认顺序 CODEX → CLAUDE → KIMI → GLM → MINIMAX → DEEPSEEK →
NEWAPI → SUB2API，可用 `INKSCRY_PANEL_ORDER=KIMI,GLM,CODEX` 自定义
（按屏上标题写、大小写不限，未列出的按默认顺序排在后面）。

各面板支持两种配置方式，可混用（同一家以 .env 优先；自建中转类
只有 .env 一种）：

1. **自动识别（零配置）**：Codex 读 `~/.codex/auth.json`（codex login
   自动续期）；Coding Plan 读 `~/.claude/settings.json` 里 Claude Code
   正在用的 `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN`；Mirasim
   检测到 `~/.mirasim` 即启用（桌面客户端本机回环接口，无需凭据）
2. **.env 显式配置**：`INKSCRY_{CODEX,CLAUDE,KIMI,GLM,MINIMAX,DEEPSEEK,NEWAPI,SUB2API}_TOKEN`，
   配了就显示，可同时配多家；Codex 另有 `INKSCRY_CODEX_AUTH` 指定 auth.json
   路径（如第二账号）、`INKSCRY_CODEX_ACCOUNT_ID` 指定工作区；国际版
   域名用 `INKSCRY_GLM_BASE` / `INKSCRY_MINIMAX_BASE` 覆盖；自建中转
   （NewAPI/Sub2API）**必须**同时配 `INKSCRY_{NEWAPI,SUB2API}_BASE`，
   否则跳过（均见 .env 内注释模板）

| 面板 | 自动识别特征 | 接口（对齐 cc-switch） |
|---|---|---|
| CODEX | `~/.codex/auth.json` | `chatgpt.com/backend-api/wham/usage` |
| CLAUDE | macOS Keychain（`Claude Code-credentials`）/ `~/.claude/.credentials.json` | `api.anthropic.com/api/oauth/usage`（需 `anthropic-beta: oauth-2025-04-20` 头） |
| KIMI | `kimi.com/coding` | `{base}/v1/usages` |
| GLM | `bigmodel.cn` / `api.z.ai` | `{域名}/api/monitor/usage/quota/limit` |
| MINIMAX | `minimaxi.com`（国内）/ `minimax.io`（国际） | `{域名}/v1/api/openplatform/coding_plan/remains` |
| DEEPSEEK | `deepseek.com` | `api.deepseek.com/user/balance`（余额模式） |
| NEWAPI | 无（自建，必须配 BASE） | `{base}/api/user/self`（余额模式） |
| SUB2API | 无（自建，必须配 BASE） | `{base}/v1/usage`（余额模式，sk- key） |
| MIRASIM | 本机 `~/.mirasim`（桌面客户端） | `127.0.0.1:{hub}/v1/limits`（本机无鉴权，端口自动探测） |

DeepSeek/NewAPI/Sub2API 是余额制（无订阅窗口），面板走**余额模式**，
与额度面板同构三段式：`bal ¥xx.xx` 金额行（超宽自动缩号不截断）、
占比条（DeepSeek=充值余额占比、NewAPI=剩余占比，Sub2API 无数据不画）、
备注行（DeepSeek 显示会过期的赠送余额「赠 ¥x」、NewAPI 显示「已用 $x」）；
余额不足或账号不可用时金额变红并加 LOW BALANCE 红字。

各家接口的坑（已在 `quota.py` 内处理）：

- 智谱 GLM：`Authorization` **不加 Bearer 前缀**；`limits[]` 里 `unit=3` 为 5h
  窗口、`unit=6` 为周窗口，unit 缺失时按重置时间启发式归档
- MiniMax：接口返回的是**剩余**百分比（其他家为已用，需反转口径）；
  周窗口仅 `current_weekly_status==1` 时有效，部分套餐无周限额
- Kimi：`limits[0].detail` 为 5h 窗口、`usage` 为周窗口
- New API：Token 是控制台的「系统访问令牌」（非 sk- 渠道 key），需配合
  `INKSCRY_NEWAPI_USER_ID`（`New-Api-User` 请求头）；`quota / 500000 = USD`
  （One API 换算惯例）。站点若套了瑞数类 WAF（如 anyrouter.top，全站返回
  JS 挑战页）则接口无法程序化访问，面板自动跳过
- Sub2API：Token 是面板里创建的 **sk- API key**（长期有效，非登录 JWT）；
  走 `/v1/usage`（对齐 cc-switch），返回余额 + 每日消耗，备注行显示
  最近一天的花费「MM-DD $x」
- Mirasim：桌面客户端的**本机接口**（127.0.0.1，无鉴权无 token）。
  hub 端口随进程启动漂移：查询时自动枚举 Mirasim 进程的回环监听
  端口逐个探测 `/v1/limits`（web 端口 4970 返回 SPA 页面、shell 口
  返回无 windows 的 JSON，都会被跳过）；`INKSCRY_MIRASIM_BASE` 可
  显式固定。`windows[]` 的 used/budget 为积分、reset_at 为 epoch 秒。
  账号带档位子额度（如 `7d_fable`，预算约为总周窗的一半、全用
  Fable 时先撞墙）时自动多出一个 **FABLE 独立面板**单独显示它
  （无此窗口的账号自动隐藏；`INKSCRY_FABLE_LABELS` 改名）；
  MIRASIM 面板本身始终显示 5h + 7d 总窗。
  非官方接口（思路对齐 mirasim-quota-widget），客户端更新可能需适配
- NEWAPI/SUB2API 支持**多实例**（多站点/多账号）：`BASE` 和 `TOKEN` 用
  逗号分隔按位置配对，如 `BASE=a,b` + `TOKEN=t1,t2` → `NEWAPI`/`NEWAPI2`
  两个面板（各自独立域名和缓存；`USER_ID` 没配够时复用最后一个值）。
  其他 .env 配置的供应商同样支持（有默认域名的家 BASE 可省略）
- 多实例的面板名可用 `INKSCRY_{X}_LABELS=公司,个人` 自定义（逗号按位置
  配对，可中文，空位回退自动编号）；排序 `INKSCRY_PANEL_ORDER` 里直接
  写自定义名

部分账号只暴露单一窗口：缺失档位不占位，剩余档位独占面板全宽
（自动升大字号档；两档全缺才显示 `--` 占位）。

底部状态栏（三色屏红底白字，黑白屏黑底白字）：右角时间是**本次刷新
时间戳**（墨水屏只在事件时刷新，以此判断屏上数据新旧）；左侧平时显示
事件状态（完成/空闲/运行中…），断网回退过期缓存时变为「数据过期:
<面板名>」警示。

## 电源特性

- 每次推送是独立进程：连接 → 传图 → 断开，两次事件间设备仅低功耗广播
- 推送防抖：普通事件（Stop 等）距上次推送不足 `INKSCRY_PUSH_INTERVAL`
  秒（默认 60）则跳过；过了窗口若屏显数据无变化同样跳过（见「定时
  同步」的比对机制）；等待确认/出错类事件必推不漏
- 断开时固件自动休眠屏幕驱动并关闭 SPI GPIO（`on_disconnect`），无需发 SLEEP
- 墨水屏画面保持零功耗；耗电大头是全刷波形，频繁刷屏比 BLE 连接更费电
- 切勿发送 `0x92 SYS_SLEEP`：主控深睡后停止广播，只能物理按键唤醒

## 定时同步（菜单栏程序 / --watch / --sync）

事件驱动的盲区是「不用 Claude 的时候」——额度重置、余额变动不会
反映到屏上。定时同步补上这块：按间隔查额度（电脑侧 HTTP，免费），
与屏上内容做**屏显精度**的签名比对，有变化才连 BLE 推送（三色全刷
必闪十几秒、毫安级耗电，盲目到点就刷不可取）。空闲一天实际只刷
几次（≈数据真实变化次数），数据新鲜度 ≤ 检查间隔。

三种跑法，共用一套配置（`INKSCRY_SYNC_INTERVAL` 检查间隔秒数、
`INKSCRY_QUIET` 静默时段、`INKSCRY_HEARTBEAT` 心跳，见 .env 注释）：

- **macOS 菜单栏程序（推荐）**：`pip install '.[menubar]'` 后运行
  `inkscry-bar`。菜单栏常年一个「墨」字（同步中「墨…」、失败
  「墨!」），下拉菜单显示屏上各面板数据与上次同步结果，以及管理项：
  「立即刷新」（跳过防抖与比对必推）、暂停/恢复自动同步、同步间隔
  （5/15/30/60 分钟运行期切换）、屏幕管理（查看当前画面 / 清屏并
  暂停）、配置（编辑 .env… / 重载配置——改面板顺序、标签、token
  后点一下即生效，无需重启）。开机自启：系统设置 → 通用 → 登录项
  添加它即可。
- **Windows / Linux 托盘程序**：`pip install '.[tray]'` 后运行
  `inkscry-tray`（pystray）。功能与菜单栏版对齐：托盘图标是 Pillow
  渲染的「迷你墨水屏」（白屏黑框 + 底部红色状态栏，同步中整体
  置灰、失败时框条全红），右键菜单同样提供面板一览与全部管理项。
  开机自启：快捷方式放进「启动」文件夹（`shell:startup`）。
  BLE 部分 Windows 尚未真机实测。
- **常驻命令行**：`inkscry --watch`（跨平台，适合 tmux / 服务器）
- **单次检查**：`inkscry --sync`，配合 launchd / Windows 任务计划 /
  systemd timer 自定节奏

同一套比对也作用于 **hook 事件**：普通事件（Stop 等）过了防抖窗口
后仍会先比对屏上内容镜像（`last_pushed_state.json`，跟随每一次成功
推送更新），数据与告警横幅都没变就跳过刷屏；等待确认/出错类必推
不比。顶部红色告警横幅**参与比对**（否则「没变就跳过」会把过期
告警留在屏上）；状态字与右下角时间戳**不参与**，只搭实质变化的
便车——不为一个词全屏闪一次。

其余细节：hook 推送永远优先（sync 撞上防抖窗口自动让路）；无 hook
时底栏状态按最新会话日志活跃度推断（10 分钟内活跃 → 运行中）；
`INKSCRY_HEARTBEAT>0` 时超过 N 小时无推送则强制刷一次，让时间戳
保持可信（区分「数据没变」和「同步挂了」）。

## 字体

默认使用系统等宽字体（macOS Menlo）。想用像素字体（Dina / Misaki / Fixedsys
等），把 `.ttf`/`.otf` 放进项目 `fonts/` 目录即自动优先加载。
状态栏中文单独走系统中文字体，中文面板标题用其粗体面：
macOS 苹方/冬青黑体(W3/W6)/华文黑体，Windows 微软雅黑(常规/Bold)，
Linux Noto CJK——均不受 `fonts/` 目录影响。面板正文（百分比、时间、
金额）保持等宽字体：数字等宽是百分比与进度条对齐的排版骨架。

## 项目结构

| 文件 | 职责 |
|---|---|
| `inkscry/protocol.py` | 指令常量、RLE 压缩、WRITE_IMG 封包、机型表 |
| `inkscry/ble.py` | bleak 连接/MTU 协商/流控/双色面传输 |
| `inkscry/renderer.py` | 三区布局渲染 → 黑白+红色双平面字节流 |
| `inkscry/monitor.py` | Claude 会话 jsonl 解析（工具/Token/费用/时长） |
| `inkscry/quota.py` | 多供应商额度/余额查询（订阅制 + 余额制），5min 缓存 |
| `inkscry/config.py` | .env 配置加载 |
| `inkscry/cli.py` | hook 事件入口 + 设备指令 + 定时同步（--sync/--watch） |
| `inkscry/menubar.py` | macOS 菜单栏常驻程序（rumps，可选依赖） |
| `inkscry/tray.py` | Windows/Linux 系统托盘常驻程序（pystray，可选依赖） |

## 路线图

- [x] M1 BLE 通信 + 哑图刷新（实测通过）
- [x] M2 Hooks 状态监听 + 会话日志解析
- [x] M3 三区排版渲染 + 红色告警栏
- [x] 多额度面板：Codex/Claude/Kimi/GLM/MiniMax + 余额制（DeepSeek/NewAPI/Sub2API），自动识别或 .env 配置，布局自适应
- [x] 定时同步：变化才刷（--sync / --watch + macOS 菜单栏程序 inkscry-bar）
- 后续见 [TODO.md](TODO.md)（局部刷新 / 固件字库 / 按键回传）
