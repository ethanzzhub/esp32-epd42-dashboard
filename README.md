# ESP32-S3 + 4.2" 墨水屏 · 隐蔽行情面板

办公室桌面上的一块墨水屏：远看像一张打印纸，实际是 12 个全球指数的行情看板。
主控 **ESP32-S3-WROOM N16R8**（16MB Flash + 8MB PSRAM），屏 **400×300 黑白（SSD1619A）**，局部刷新 0.5s 不闪屏。

**当前状态（2026-11-11）**：固件已烧好、WiFi 已连通、屏已接线点亮，12 格行情真机运行中。所有逻辑在 `firmware/epaper_main.py`，`firmware/main.py` 只是 3 行启动壳。

---

## 目录结构

```
ESP32-墨水屏/
├── README.md               本文件（入口）
├── .gitignore
├── firmware/               ★ 要传到板子上的固件（MicroPython）
│   ├── main.py             3 行启动壳：import epaper_main → main()
│   ├── epaper_main.py      主程序：行情页、切页、刷新策略、网页控制台
│   ├── epd42.py            屏驱动（SSD1619A / 微雪 epd4in2_V2 的 MicroPython 移植）
│   ├── webconfig.py        网页配置控制台（epaper_main 会调用）
│   ├── webrepl_cfg.py      WebREPL 配置（WiFi 运维用）
│   ├── config.json         板端运行配置（WiFi、刷新档位、显示模式等）
│   └── README-EPD42.md     接线表 / 配置项 / 刷新策略 / 排错
├── bin/                    MicroPython 固件（烧录用）
│   └── ESP32_GENERIC_S3-SPIRAM_OCT-20260824-v1.29.0.bin
├── tools/                  ★ 主机侧工具
│   ├── flash.sh            一键烧录（自动找串口 → 擦除 → 写入 → 校验）
│   ├── up.py / up2.py      文件上传器（raw REPL，base64 分块 + SHA1 校验）
│   ├── down.py             把板上文件拉回本地
│   ├── sh.py               在板端 raw REPL 执行一段代码
│   ├── webrepl.py          通过 WiFi 操作板子（WebREPL 官方协议）
│   ├── wrepl.py             WebREPL 客户端（上传/下载 + REPL）
│   ├── wreboot.py          通过 WebREPL 软重启
│   ├── mon.py / watch_boot.py / watch_cold.py   串口监听（崩溃/启动抓日志）
│   ├── power_cycle.py / wait_port.py            冷启动 / 等串口
│   ├── preview_quote.py    电脑端像素级版式预览（行情页）
│   ├── font8x8.h           8×8 点阵字库（预览工具用，非板端）
│   └── esptool-lib/        离线 esptool 4.8.1 依赖库（本机 pip 被安全策略杀掉）
├── debug/                  板上诊断脚本（不依赖屏幕，串口看输出）
│   └── selftest.py         连 WiFi + 对时 + 取 12 标的，一条命令看全链路
├── dashboard/              可选的配套（手机看板 / 聚合服务）
│   ├── server.py           聚合服务：/api/q 返回 12 标的 JSON，/ 出看板页
│   ├── kiosk.html          二手手机/香橙派用的看板页（单文件，file:// 可跑）
│   └── QUOTE-SOURCE.md     数据源与取数架构的决策记录
└── docs/
    ├── MEMORY.md           ★ 项目长期要点（先看这个）
    ├── 开发日志.md         完整开发记录
    ├── BOM与选型.md        硬件选型全过程与踩坑
    └── img/                实拍 / 预览截图
```

> 仓库已清理：一次性实验脚本（`tools/_*`）、旧固件副本（`backup/`）、C3 旁支固件（`firmware_c3/`）、独立 Arduino splash 子项目（`esp32-epd42-smt-splash/`）均不入库。

---

## 三步跑起来

### 1. 烧固件（已做过，重刷才需要）

```bash
cd tools
./flash.sh              # 自动找串口 → erase → write → verify
./flash.sh chipinfo     # 只查芯片信息
```

板子进不了下载模式时：按住 **BOOT** → 短按 **RST** → 松开 BOOT。

### 2. 传程序

```bash
cd tools
python3 up.py ../firmware/epd42.py
python3 up.py ../firmware/epaper_main.py          # 主逻辑（全部代码都在这里）
python3 up.py ../firmware/main.py                 # 薄壳入口，只有 import + main()
```

> **千万别再把 `epaper_main.py` 整份传成 `main.py`。** 以前就是这么干的，结果板上 `main.py` 成了 70KB 的自包含副本，之后所有对 `epaper_main.py` 的修改（包括恒生科技改用实时接口）上传后 SHA1 校验全对、重启却毫无变化——开机跑的一直是那份旧 `main.py`。现在 `main.py` 只有 3 行，**改逻辑只动 `epaper_main.py`**。
>
> 验证板上到底跑的哪版：打开网页控制台看状态行有没有「档 800s / 档 60s」这一项，有 = 新代码，没有 = 还在跑旧副本。

`up.py` 会自动找串口（`/dev/cu.usbmodem*` → `/dev/cu.usbserial*` → `/dev/ttyUSB*`）；要指定就用 `ESP_PORT=/dev/cu.usbserial-110 python3 up.py ...`。

### 3. 验证（不接屏也能跑）

```bash
python3 up.py ../debug/selftest.py
# 串口 REPL 里：
import selftest
selftest.run()
```

预期输出：WiFi IP → 对时结果 → `取到 12 / 12`。

---

## 数据源现状（重要，别改回去）

| 标的 | 来源 | 接口 |
|---|---|---|
| 上证/科创综指/创业板/恒生科技/黄金/原油/白银（7 个） | **腾讯** | `http://qt.gtimg.cn/q=...` 明文 HTTP |
| **日经225 / 韩国KOSPI** | **新浪** | `znb_NKY` / `znb_KOSPI`，明文 HTTP，**必须带 Referer** |
| **美股三格＝指数期货** NQ / YM / ES | **新浪** | `hf_NQ` / `hf_YM` / `hf_ES`，与上一行同一个请求；期货只给最新价和昨收，涨跌幅自己算 |

**腾讯港股必须加 `r_` 前缀**：`hkHSTECH` 是延时 15 分钟的行情（时间戳恒落后本地 15min，板端显示涨跌幅会比手机快讯差 0.1~0.3 个百分点），`r_hkHSTECH` 才是实时。A 股 `sh/sz` 与大宗 `hf_` 本身就是实时，加 `r_` 反而取不到，所以固件里只对港股做了映射（`TX_RT`），且实时码与延时码一次同发、实时优先、延时兜底。

**东财已降级为兜底**：公司网络下 `push2.eastmoney.com` 会被 IP 级限流（全子域名返回 0 字节，恢复要等很久）。腾讯确实没有日经/韩股代码（试过 14 个候选全空），Stooq/Yahoo 在公司网络不可达。

> 板子直连公网，不走家里聚合服务（TLS 次数一样但多 4 个故障点）。`quote_url` 留空即直连；聚合服务仅作可选配套，见 `dashboard/`。

---

## 接线（屏已到货，按此接好）

1. 屏的 24P 排线插驱动板（锁扣压紧），驱动板 8pin 接 ESP32（见 `firmware/README-EPD42.md` 接线表）；
2. 核对 `epaper_main.py` 顶部 `DEFAULT` 的引脚（默认 sck=12 / sda=11 / cs=10 / dc=9 / rst=8 / busy=7 / btn=4）；
3. 传 `epaper_main.py` + `main.py` 两个文件（`main.py` 是薄壳入口，**不是** `epaper_main.py` 的副本），然后上电。

---

## 已知的三个真机坑（代码里已修，别再踩）

1. **固件没有 gbk 解码器** —— `decode("gbk")` 直接 LookupError。腾讯/新浪响应一律在 **bytes** 上切分（数字字段是 ASCII）。
2. **`SSLSocket` 没有 `settimeout`** —— `_tls_wrap()` 里用 try/except AttributeError 兜住。
3. **这块板 time 纪元是 2000-01-01**（`time.localtime(0)[0] == 2000`）—— HTTP Date 对时必须减 `946684800`，否则年份 +30。RTC 统一存 **UTC**，显示端再 +`tz`。

另外：本机 `pip install` / `brew install` 会被安全策略 SIGKILL，装东西走「curl 拉 wheel/sdist 手动解压」的路子（离线 esptool 已在 `tools/esptool-lib/`）。

---

## 推送 GitHub

仓库已整理干净，可直接提交。建议流程：

```bash
git init
git add .
git commit -m "初始提交：ESP32-S3 墨水屏行情面板"
git remote add origin git@github.com:<你的用户名>/<仓库名>.git
git push -u origin main
```

> 注意 `.gitignore` 已排除 `.workbuddy/`（本机 agent 记忆数据，勿入公开仓库）、`.DS_Store`、`__pycache__`、`*.pyc`。
