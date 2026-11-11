# 项目长期要点（ESP32-S3 + 4.2" 墨水屏面板）

## 硬件定稿

- 主控：**ESP32-S3-WROOM N16R8**（16MB Flash + 8MB PSRAM，44pin，双 Type-C）。实测 chip_id：QFN56 rev0.2，内置 8MB PSRAM，MAC `1c:29:04:38:1f:78`，`gc.mem_free()` ≈ 8.3MB。
- 屏幕：**4.2" 400×300 黑白墨水屏（SSD1619A）** + 通用 SPI 驱动板（24P 座、8pin 排针），套装约 ¥56。
- 屏到货前不要买排线：e-paper 的 FPC 与面板压合一体、屏自带；只需要杜邦线 8 根。
- 供电：有插座就 5V1A 充电头，**不要买电池**（长期满电浮充会鼓包）。没插座才走 18650 + 带锂电接口的低功耗板。
- 完整 BOM 与选型过程见 `docs/BOM与选型.md`。

## 固件与工具

- MicroPython 固件必须用 **SPIRAM_OCT** 变体（N16R8 是 Octal PSRAM）：`bin/ESP32_GENERIC_S3-SPIRAM_OCT-20260824-v1.29.0.bin`。
- 烧录：`tools/flash.sh`（zsh 下 glob 无匹配会中断，脚本已用 `ls /dev/cu.usb*` 兜住；**输出别接 `| head`**，会 SIGPIPE 打断写入）。
- 上传：`tools/up.py`。朴素分块上传会静默丢字节，必须校验（现版本 base64 + SHA1 回读 + 自动重传）。
- **`tools/up.py` 省略远端名时按本地文件名存**，所以 `up.py firmware/epaper_main.py` 会在板上生成 `epaper_main.py`——**而开机自启的是 `main.py`**。正确写法：`python3 tools/up.py firmware/epaper_main.py main.py`。11-02 就因为漏了第二个参数，白跑了两次 4 分钟上传，屏上一行没变。
- 板端改了模块后要 **Ctrl+D 软重启**再 import，否则命中 `sys.modules` 缓存，跑的还是旧代码。

## 数据源（结论，别改回去）

| 用途 | 源 | 要点 |
|---|---|---|
| A股/港股/大宗 共 7 个 | 腾讯 `qt.gtimg.cn/q=` | 明文 HTTP。指数用 `~` 分隔（[3]价、[32]涨跌幅%）；`hf_XAU/hf_CL/hf_SI` 用 `,` 分隔（[0]价、[1]涨跌幅%）。**港股（恒生科技）必须用 `r_hkHSTECH`**：无前缀的 `hkHSTECH` 是延时 15 分钟行情，`r_` 才是实时；A股 `sh/sz` 与 `hf_` 加 `r_` 反而取不到，只对港股做映射 |
| **日经225 / 韩国KOSPI** | **新浪** `znb_NKY` / `znb_KOSPI` | 明文 HTTP，**必带 `Referer: https://finance.sina.com.cn`**。字段 [1]=点位、[3]=涨跌幅% |
| **美股三格＝指数期货** `hf_NQ` / `hf_YM` / `hf_ES` | **新浪** `hq.sinajs.cn/list=` | 与上面同一个请求（一次拿 5 个码）。期货字段 [0]=最新、[7]=昨收，**新浪不给涨跌幅，自己算 (最新-昨收)/昨收**。腾讯只有 `hf_ES`，`hf_NQ/hf_YM` 无数据 → 三个统一走新浪 |
| **涨跌停家数** | **同花顺** `data.10jqka.com.cn/dataapi/limit_up/` | `limit_up_pool`（涨停）/ `lower_limit_pool`（跌停），只读 `data.page.total`。`limit=1` 让响应降到 ~300 字节。HTTPS，板端单次 ~0.8s，只在交易时段调用。**东财 `getTopicDTPool` 返回 rc:206 空数据，不可用** |
| 东财 | 仅兜底 | 公司网络下会被 **IP 级限流**（push2 全子域名返回 0 字节）。批量接口 `clist/get?fs=m:100` 一次给 63 个指数，注意其 NDX 是纳斯达克综合不是纳指100 |

架构结论：**板子直连公网**，不要走家里聚合服务（TLS 次数一样都是 1 次，却多 4 个故障点）。`quote_url` 留空即直连。详细决策见 `dashboard/QUOTE-SOURCE.md`。

### 各源「时间戳」字段位置（查延迟就看这张表，2026-11-05 全量实测）

| 源 | 代码 | 时间戳 | 格式 | 实测滞后 |
|---|---|---|---|---|
| 腾讯 A 股 | `sh000001` 等 | **[30]** | `20261105114400` | <5s（A 股本身 3 秒一笔） |
| 腾讯 港股 | `r_hkHSTECH` | **[30]** | `2026/11/05 11:44:38` | 实时（`hk` 无 `r_` 前缀则恒滞后 15min） |
| 腾讯 大宗 | `hf_XAU/hf_CL/hf_SI` | **[6]** | `11:55:00` | 0~5s。**注意是逗号分隔**，`[1]` 已是涨跌幅，别用股票那套 `[30]/[32]` |
| 新浪 全球指数 | `znb_NKY/znb_KOSPI` | **[7]** | `11:54:20` | 10~15s。**`[7]` 是新浪服务器时间不是行情时间**；`[4]`（`2:12 AM`）和 `[5]`（时间戳停在 2025 年）都是垃圾字段，别信 |
| 新浪 期货 | `hf_NQ/hf_ES/hf_YM/hf_NK` | **[6]** | `11:54:32` | 3~10s。`[7]`=昨收、`[8]`=今开（用腾讯 `hf_ES` 的涨跌幅反证过：用 [7] 算 -0.21% vs 腾讯 -0.22%，用 [8] 算 -0.42% 是错的） |
| 同花顺 涨跌停 | `limit_up_pool` | 无 | — | 按 `date` 查当日累计，203ms 往返 |

排查手法：**同标的跨源对比**。腾讯有 `hf_ES`，新浪也有 `hf_ES`，两边同一秒取回就能判定谁延迟、哪个字段是昨收。`r_` 前缀也是这么试出来的。

**其他源都没有延迟问题**（2026-11-05 逐格核对），迟滞只在 15 秒以内，量级远小于 60 秒的刷新周期。


## 真机才暴露的坑（代码里已修）

1. **固件无 gbk 解码器** → 一律在 bytes 上切分，不要 `decode("gbk")`。
2. **`SSLSocket` 没有 `settimeout`** → `_tls_wrap()` 用 try/except AttributeError 兜。
3. **time 纪元是 2000-01-01** → HTTP Date 对时减 `946684800`；RTC 存 UTC，显示端 +`tz`。
4. `urequests` 无 timeout 会卡死主循环 → 全部用自写 `http_get()`（裸 socket + 硬超时 + HTTP/1.0）。
5. 用 `send()/recv()`，不用 `write()/read()`（老固件没有）。
6. `json.loads` 只吃 str，先 `.decode()`。
7. **【最坑的一条】板上 `main.py` 曾经是 `epaper_main.py` 的整份副本（70KB），改 `epaper_main.py` 永不生效。** 症状极迷惑：`webrepl.py --put` 报成功、SHA1 回读一致、`os.stat` 大小也对，重启后行为纹丝不动——因为开机执行的是 `main.py` 那份自包含旧代码，`epaper_main.py` 根本没人 import。恒生科技的实时接口修复因此被埋了整整一轮。
   - 现在 `main.py` 是 3 行薄壳（`import epaper_main` + `epaper_main.main()`），`epaper_main.py` 末尾改成 `if __name__ == "__main__": main()`，两边职责分清。
   - **判断板上跑的是哪版**：看网页状态行的「档 800s / 60s」有没有出现；或 Ctrl-C 打断后在 REPL 里 `b"LUNCH_FROM" in open("epaper_main.py","rb").read()`。
   - **通用教训**：ESP32 上"上传成功 + 校验一致"≠"代码生效"，必须验证**开机入口**加载的是哪个文件。
8. **WebREPL 在 `main.py` 运行时 REPL 交互不通，但发 `\x03`（Ctrl-C）能打断主循环抢回 `>>>`**，此时 `--put` 和任意 REPL 代码都能用。测完记得 `machine.reset()` 恢复，否则屏停止刷新。读 70KB 文件用 `get_file` 会卡死，改用 REPL 里 `open(...).read()` 判断特征字节。

## 屏与刷新（2026-11-02 真机重测，旧结论已推翻）

- 400×300，3 列 × 4 行 = 12 格：`COL_X=(0,133,266)`、`CELL_W=133`、`GRID_Y0=28`、`ROW_H=66`、页码点 y=294。
- **每格显示的是「代号 + 无符号涨跌幅%」，不是点位**（`draw()` 取 `v[0]`，而 `v=(涨跌幅%, 点位)`）。点位只是顺带存着，屏上看不到。
- 12 格顺序（2026-11-02 定稿，`ITEMS` 顺序 = 屏幕从左到右、从上到下）：
  `SSE CHINEXT STAR / HSTECH N225 KS11 / NQ ES YM / GOLD OIL SILVER`
- 顶栏右侧一行附加指标：`58/11 10030` = `涨停家数/跌停家数` + **两市当日成交额累计（亿元，实时值，不标单位）**。成交额取腾讯 `sh000001`+`sz399001` 第 38 字段（万元）相加后 /1e4。**别显示外推预测值**——11 点时预测约 2.4 倍于实际，看起来像数据错误；`est_amount()` 保留但默认不调用。
- 数字 scale3（24px 高），超宽自动降 scale2（paren 模式 `(123.45)` 会到 168px）。
- **这块 SSD1619A 没有可用的局部刷新**，只有全刷一条路：
  - 只有 `0x22=0xF7` 能驱动像素；`0xC7/0xCF` 是空转（BUSY 840ms、屏不动）。
  - **单独发 `0x04` 之后，后续 `0xF7` 全部失效**（840ms 空转）。旧 `init_part()` 的 `0x04` 就是"局刷不生效"的根因。驱动里现在完全不发 `0x04/0x01/0x06`。
  - 窗口 `0x44/0x45` 在能驱动的模式下被忽略，一律整屏 300 行扫；窗口只决定 RAM 写入范围。
  - `0x26` 是红平面不是旧帧；黑白模式 R 位不参与 LUT 选择（datasheet：`LUTBW=LUTBB`、`LUTWW=LUTWB`），**双 RAM 差分不成立**。
  - 自定义 233 字节波形表能被 `0xC7` 执行，但整屏统一驱动、与 RAM 数据无关，画不出图案。
  - `0x1A=0x1E00`（30°C 档）把全刷 5.2s → **2.9s**，出图正常，是唯一可靠提速手段。
  - **`0x22=0xFF` 与 `0xF7` 等价**（2026-11-03 实测 3166 vs 3169ms，BUSY 都是 ~0ms 拉高、3.17s 回落）：bit3 mode2 在这块屏没有独立 OTP 波形。`0xC7/0xCF/0xF7/0xFF` 已全部试完，**弱闪/无闪的软件路径不存在**。
  - **`0x20` 之后 BUSY 有 0~几十毫秒延迟才拉高**，必须 `wait_assert()` 先等拉高再 `ReadBusy()`，否则会 25ms 就"刷完"（正常 3.1s），随后 `sleep()` 打进还在跑的波形里，屏会被留在半死状态（后续 `0x44` 卡 15s、刷不出图，只能断电恢复）。
- 刷新策略：内容真变了才刷（`show()` 做内容去重）；**刷完立刻 `epd.sleep()`**（`0x10=0x01` 进深睡撤高压），下次刷前必须重走 `epd_init()`（内部 `reset()` 唤醒），多约 230ms。
- **分时段间隔（2026-11-03）**：`in_session()` 判 09:00-15:30 → `quote_sec`(120s)，其余工作时段 → `quote_sec_off`(600s)。`QuotePage.due()` 每次重算 ttl，跨时段立即切档。约 234 次/天（原 390），减 40% 闪屏；100 万次 ≈ 11.7 年（五折 5.9 年）。
- 寿命护栏（厂家 FAQ）：理想 100 万次、非局刷产品**间隔 ≥180s**、**刷完要 sleep/断电否则长期高压可能烧屏**。代码护栏：`quote_sec >= 120`、`quote_sec_off >= 180` 且不小于 `quote_sec`；`full_every` 已废弃恒置 1。
- **本地 `firmware/config.json` 与板端 `config.json` 内容可能不同**。改板端配置用 `tools/sh.py` 就地改 json，别整份上传覆盖。
- 跌的表示：`mode` = `box` 加框（默认）/ `paren` 括号 / `inv` 反白 / `plain` 无。设计上是**无符号数字 + 形态**，不用颜色。

## 本机环境

- `tools/webrepl.py --put` 传大文件会**静默截断**（57KB → 25KB，且 SHA1 还报"一致"），大文件一律用 `tools/up.py` 串口传。
- `pip install` / `brew install` 会被安全策略 **SIGKILL**（`dangerouslyDisableSandbox` 也一样）。可靠路径：curl 拉 wheel/sdist → 手动解压 → `PYTHONPATH` 直跑。有了 `tools/esptool-lib/`，别再重装。
- 沙盒 shell 有 `http_proxy`，`curl 127.0.0.1` 要加 `--noproxy '*'`。
- BSD grep 不支持 `\|` 多模式，用 `grep -E` 或 Grep 工具。

## 当前进度

- [x] 固件烧录 + PSRAM 验证
- [x] WiFi 连通（hxq-office，板子 IP 172.16.48.206）
- [x] 取数链路真机验证：12/12 标的 423ms、屏幕时钟正确
- [x] 屏到货 → 接线 → `epaper_main.py` 传成 `main.py` → 开机自启
- [x] 局部刷新调研（结论：本屏没有局刷，见上）
- [ ] 按键翻页（GPIO4）实测
- [ ] 网页控制台实测
