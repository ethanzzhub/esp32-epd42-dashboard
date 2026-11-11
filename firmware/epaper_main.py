# 4.2" 墨水屏行情面板（400x300，SSD1619A，黑白）
#
# 屏：400x300 单色。刷新有两条路（2026-11-03 实测）：全刷 0xF7 整屏 3s 翻转；
#      差分 0xCF + 70 字节 LUT + 0x26 旧帧，只驱动变化像素、不闪，约 1s。
# 页：行情 12 格（3 列 x 4 行）
# 切换：定时自动翻页 / 按键（GPIO，按下翻页）/ 网页控制台（/page?i=1）
#
# 刷新策略（墨水屏专用，和 TFT 版最大的区别）：
#   内容真变了才刷 -> 不看时间，指纹一样就不动屏
#   刷一次        -> 数字变化走差分（约 1s，不闪）；换页/每 8 次差分走全刷
#                     （0xF7 整屏 3s 翻转），刷完立刻进深睡（0x10=0x01）
#   分时段间隔    -> 盘中 09:00-15:30 用 quote_sec(120s)，早晚用 quote_sec_off(600s)
#   冷启动        -> clean() 先黑后白各一帧，把上一轮残影驱干净

import json
import time
import network
import gc

try:
    import ntptime
except ImportError:
    ntptime = None
import usocket as socket
from machine import Pin, SPI
import machine
import framebuf

import epd42

# ---------------- 配置 ----------------
CFG_FILE = "config.json"
DEFAULT = {
    "ssid": "hxq-office", "pwd": "Office@321",
    # 引脚：驱动板 8pin -> 主控（默认按 ESP32-S3 随便挑的一组，改这里即可）
    "spi_id": 1, "sck": 12, "sda": 11,
    "cs": 10, "dc": 9, "rst": 8, "busy": 7,
    "btn": 4,                 # 翻页按键 GPIO，接 GND 触发（内部上拉）；设为 -1 关闭
    "tz": 8,
    "page_sec": 60,           # 手动翻页后的停留秒数（到点自动回当前时段该显示的页）
    "quote_sec": 120,         # 行情刷新间隔（秒，盘中 09:00-15:30）。走差分，约 1s 不闪
    "quote_sec_off": 600,     # 非盘中（早晚时段）的行情刷新间隔（秒）。宁慢勿闪
    # 本屏支持差分刷新（2026-11-03 实测）：数字变化走差分（不闪），
    # 每 diff_full_every 次刷新做一次全刷清残影（1=每次全刷=关差分）。
    # full_every 是更早的废弃键，恒为 1，留着只为兼容旧配置文件。
    "diff_full_every": 8,
    "full_every": 1,
    "mode": "box",            # 跌的表示：box=加框 paren=括号 inv=反白 plain=无
    "active_from": 7,         # 工作时段起始小时（含）。时段外不取数不刷屏
    "active_to": 20,          # 工作时段结束小时（不含）。20 = 晚上 8 点后静默
    "weekend_off": True,     # 周六周日全天静默
    "quote_url": "",          # 家里聚合服务（可选）；留空=板子直连公网
    "led": 48,                # 板载 WS2812 状态灯 GPIO（官方引脚图：GPIO48=RGB LED）；-1 = 关闭
    "led_bright": 24,         # 待机亮度（0-255），越大越亮
}


def load_cfg():
    try:
        with open(CFG_FILE) as f:
            c = json.load(f)
    except Exception:
        return dict(DEFAULT)
    for k, v in DEFAULT.items():
        c.setdefault(k, v)
    if not c.get("ssid"):
        # 空 ssid 会让 connect() 直接放弃联网，回退到内置默认
        c["ssid"], c["pwd"] = DEFAULT["ssid"], DEFAULT["pwd"]
    return c


def save_cfg(c):
    with open(CFG_FILE, "w") as f:
        json.dump(c, f)


cfg = load_cfg()


# ---------------- 状态灯：板载 WS2812（GPIO48，官方引脚图确认）----------------
# 状态灯（WS2812，GPIO48）：在线=暗蓝常亮（复刻出厂蓝灯）/ 离线=红呼吸常亮 /
# 刷屏成功=绿闪 / 取数失败=橙闪。led=-1 整颗关掉，led_bright=0 全灭。
try:
    import neopixel as _neo
    _led = _neo.NeoPixel(Pin(cfg["led"]), 1) if cfg["led"] >= 0 else None
except Exception:
    _led = None


def led_set(rgb):
    if _led is None:
        return
    try:
        _led[0] = rgb
        _led.write()
    except Exception:
        pass


_LED_STATE = "idle"                  # idle=在线待机 / offline=离线（红呼吸）
_LED_HOLD = 0                        # 自检锁：此刻之前忽略主循环的状态切换
_LED_FLASH = None                    # 短闪请求：(rgb, 到期 ticks)；到期自动回底色
_LED_PERIOD = 1600                   # 呼吸周期（毫秒）
_LED_FRAME = 40                      # 动画帧间隔（毫秒），25fps 才够顺
_LED_ANIM = False                    # 动画线程是否在工作

import math


def _led_base(now_ms=None):
    """按当前状态算底色。离线时红色呼吸（正弦，谷底全灭，最顺滑）。"""
    b = cfg["led_bright"]
    if b <= 0:
        return (0, 0, 0)
    if _LED_STATE == "offline":
        t = time.ticks_ms() if now_ms is None else now_ms
        ph = (t % _LED_PERIOD) / _LED_PERIOD
        k = (1.0 - math.cos(2 * math.pi * ph)) * 0.5     # 0 -> 1 -> 0，两头无拐点
        return (int(b * k), 0, 0)
    return (0, 0, b)                                     # 暗蓝待机


def _led_frame():
    """当前该显示的颜色：短闪优先，到期自动回底色。"""
    global _LED_FLASH
    f = _LED_FLASH
    if f is not None:
        if time.ticks_diff(f[1], time.ticks_ms()) > 0:
            return f[0]
        _LED_FLASH = None
    return _led_base()


def _led_worker():
    """独立线程跑动画：主循环被刷屏/取数卡住时，呼吸也不掉帧。"""
    while True:
        led_set(_led_frame())
        time.sleep_ms(_LED_FRAME)


def led_idle():
    """清掉短闪，回到当前底色。"""
    global _LED_FLASH
    _LED_FLASH = None


def led_state(s, manual=False):
    """切状态：idle / offline。离线常红呼吸，隔着屋子也看得出掉线。
    manual=True 用于网页自检，锁 30 秒不被主循环立刻改回去。"""
    global _LED_STATE, _LED_HOLD, _LED_FLASH
    if manual:
        _LED_HOLD = time.time() + 30
    elif time.time() < _LED_HOLD:
        return
    if _LED_STATE != s:
        _LED_STATE = s
        _LED_FLASH = None
        if not _LED_ANIM:                    # 线程没起来时才自己写一次
            led_set(_led_base())


def led_tick():
    """兜底：动画线程起不来时由主循环驱动（会掉帧，但至少能亮）。"""
    if not _LED_ANIM:
        led_set(_led_frame())


def led_blink(rgb, ms=110):
    """短闪一次再回底色。已改成非阻塞——只登记请求，写入交给动画线程。"""
    global _LED_FLASH
    _LED_FLASH = (rgb, time.ticks_add(time.ticks_ms(), ms))


def led_evt(color, ms=110):
    """闪一次单色再回底色。color 用 0/1 表示（(0,1,0)=绿），亮度统一走 led_bright。"""
    b = cfg["led_bright"]
    led_blink((color[0] * b, color[1] * b, color[2] * b), ms)


def led_fail(ms=140):
    """橙闪：取数失败（网络通但抓不到数据），这是最难自己发现的静默故障。"""
    b = cfg["led_bright"]
    led_blink((b, int(b * 0.35), 0), ms)


def start_led_anim():
    """启动状态灯动画线程。WS2812 只由这一个线程写，避免并发写坏帧。"""
    global _LED_ANIM
    if _led is None or _LED_ANIM:
        return
    try:
        import _thread
        try:
            _thread.stack_size(4096)
        except Exception:
            pass
        _thread.start_new_thread(_led_worker, ())
        _LED_ANIM = True
        print("led anim: thread ok")
    except Exception as e:
        print("led anim: no thread, fallback", e)


# ---------------- 屏：400x300 ----------------
SCR_W, SCR_H = 400, 300
# 这块 4.2" 面板（E042A2N13N1 / SSD1619A）的 RAM 约定是 1=白、0=黑，
# 与 epd.Clear() 发 0xff 求白一致。写反会整屏变黑底。
WHITE, BLACK = 1, 0
BUF = bytearray(epd42.FRAME_BYTES)
fb = framebuf.FrameBuffer(BUF, SCR_W, SCR_H, framebuf.MONO_HLSB)

spi = None
epd = None


def epd_init():
    global spi, epd
    if epd is None:
        spi = SPI(cfg["spi_id"], baudrate=8_000_000,
                  sck=Pin(cfg["sck"]), mosi=Pin(cfg["sda"]))
        epd = epd42.EPD(spi, cs=Pin(cfg["cs"]), dc=Pin(cfg["dc"]),
                        rst=Pin(cfg["rst"]), busy=Pin(cfg["busy"]))
        try:
            epd.on_wait = web_once       # 刷屏忙等时也响应网页，避免 backlog 堆死
        except Exception:
            pass
    epd.init()
    return epd


btn = None
if cfg["btn"] >= 0:
    btn = Pin(cfg["btn"], Pin.IN, Pin.PULL_UP)

# ---------------- 绘图小工具（framebuf 内置 8x8 字模，按比例放大） ----------------
_gbuf = bytearray(8)
_gfb = framebuf.FrameBuffer(_gbuf, 8, 8, framebuf.MONO_HLSB)


def text(s, x, y, sc=2, c=BLACK):
    """把 8x8 字模按 sc 倍放大画出来；一个字符宽 8*sc"""
    for ch in s:
        _gfb.fill(0)
        _gfb.text(ch, 0, 0, 1)
        for py in range(8):
            row = _gbuf[py]
            if not row:
                continue
            for px in range(8):
                if row & (0x80 >> px):
                    fb.fill_rect(x + px * sc, y + py * sc, sc, sc, c)
        x += 8 * sc
    return x


def text_w(s, sc=2):
    return len(s) * 8 * sc


def center(s, y, sc, x0=0, w=SCR_W, c=BLACK):
    text(s, x0 + (w - text_w(s, sc)) // 2, y, sc, c)


def hline(y, x0=0, x1=SCR_W):
    fb.hline(x0, y, x1 - x0, BLACK)


def vline(x, y0, y1):
    fb.vline(x, y0, y1 - y0, BLACK)


def hhmm(ts=None):
    if not ts:
        return "--:--"
    t = time.localtime(ts + cfg["tz"] * 3600)
    return "%02d:%02d" % (t[3], t[4])


def now_local():
    """本地时间元组（RTC 存 UTC，这里统一 +tz）"""
    return time.localtime(time.time() + cfg["tz"] * 3600)


def is_active(t=None):
    """工作时段内返回 True：非周末 且 active_from <= 时 < active_to。
    t 可传模拟时间元组供测试。weekday: 0=周一 ... 6=周日（板端实测确认）。"""
    t = t or now_local()
    if cfg["weekend_off"] and t[6] >= 5:
        return False
    return cfg["active_from"] <= t[3] < cfg["active_to"]


SESSION_FROM = 9 * 60            # 09:00
SESSION_TO = 16 * 60 + 10        # 16:10：A 股 15:00 就收了，但港股 16:00 收盘后还有
                                 # 10 分钟收市竞价（最终收盘价 16:08 前后才定），
                                 # 尾巴留到这里，收盘价才能及时落屏
LUNCH_FROM = 11 * 60 + 30        # 11:30 A 股 / 港股午休
LUNCH_TO = 13 * 60               # 13:00 复盘


def in_session(t=None):
    """盘中时段：工作日 09:00-16:10，扣掉 11:30-13:00 午休。

    用来决定「盘中刷新间隔」还是「非盘中刷新间隔」：
    12 格里多数标的（日经/韩股/美股期货）在 A 股时段最活跃，其余时间
    数字基本不动，没必要 1 分钟取一次数。午休行情不动，按非盘中降频，
    省掉 90 分钟的无效请求；13:00 复盘时 due() 每轮重算会立刻回到盘中档。
    """
    t = t or now_local()
    if cfg["weekend_off"] and t[6] >= 5:
        return False
    m = t[3] * 60 + t[4]
    if LUNCH_FROM <= m < LUNCH_TO:
        return False
    return SESSION_FROM <= m < SESSION_TO


# 各标的所属市场的交易时段（北京时间，当日分钟数；末位=周末是否开市）。
# A 股 / 港股 / 日经 / 韩股按当地交易所时段换算成北京时间；美股期货与贵金油几乎 24h，
# 标为全开（北京时间任何时刻都有连续报价）。绘制时据此判定：没开盘 → 横杠。
#   开盘  收盘 周末开市
MARKET_SESSION = {
    "SSE": (570, 900, False), "CHINEXT": (570, 900, False), "STAR": (570, 900, False),
    "HSTECH": (570, 960, False),
    "N225": (480, 840, False), "KS11": (480, 870, False),
    "NQ": (0, 1440, True), "ES": (0, 1440, True), "YM": (0, 1440, True),
    "GOLD": (0, 1440, True), "OIL": (0, 1440, True), "SILVER": (0, 1440, True),
}


def market_show_data(tk, t=None):
    """该标的此刻应显示数据（True）还是横杠（False）。

    规则：开盘前 → 横杠；从开盘起、以及当天收盘之后一直显示数据（收盘价保留），
    持续到当天 24:00——次日 0 点已在开盘前，自然又变回横杠。周末休市 → 横杠。
    全开市场（美股期货 / 贵金油，wknd=True）始终显示数据。
    t 可传模拟时间元组（(y,m,d,H,M,S,wd,yday)）供测试。"""
    t = t or now_local()
    a, b, wknd = MARKET_SESSION.get(tk, (0, 1440, True))
    if wknd:
        return True                             # 全开市场：始终有数据
    if t[6] >= 5:
        return False                            # 周末休市 → 横杠
    m = t[3] * 60 + t[4]
    return m >= a                               # 开盘前→横杠；开盘及之后（含收盘后当天）→保留数据


def _hm(s, dflt):
    """"HH:MM" -> 当日分钟数；格式不对就退回 dflt"""
    try:
        a, b = str(s).strip().split(":")
        v = int(a) * 60 + int(b)
        if 0 <= v <= 24 * 60:
            return v
    except Exception:
        pass
    return dflt


def mode_str():
    """网页状态行用：运行中 / 静默(恢复时间)"""
    if is_active():
        return "运行中"
    t = now_local()
    if cfg["weekend_off"] and t[6] >= 5:
        return "周末静默"
    if t[3] < cfg["active_from"]:
        return "静默至 %02d:00" % cfg["active_from"]
    return "静默至明 %02d:00" % cfg["active_from"]


def rate_str():
    """网页状态行用：当前刷新档位（秒）。

    盘中 60s / 午休与早晚走 quote_sec_off —— 「为什么半天不刷」先看这里，
    也用来确认板子跑的是哪版代码（改了 SESSION_TO 之类一眼就能对上）。"""
    try:
        p = panel.cur()
        fn = getattr(p, "ttl_now", None)
        return "%ds" % int(fn() if fn else p.ttl)
    except Exception:
        return "-"


# ---------------- 取数：带硬超时的裸 socket ----------------
def _tls_wrap(s, host, timeout):
    import ssl
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.verify_mode = ssl.CERT_NONE
        ctx.check_hostname = False
        w = ctx.wrap_socket(s, server_hostname=host)
    except Exception:
        try:
            w = ssl.wrap_socket(s, server_hostname=host)
        except Exception:
            return None
    try:
        w.settimeout(timeout)          # 部分固件的 SSLSocket 没有这个方法
    except AttributeError:
        pass
    return w


def http_get(url, timeout=6, maxlen=4096, headers=""):
    s = None
    try:
        proto, _, rest = url.partition("://")
        hostport, _, tail = rest.partition("/")
        host, _, port = hostport.partition(":")
        port = int(port) if port else (443 if proto == "https" else 80)
        ai = socket.getaddrinfo(host, port)[0][-1]
        s = socket.socket()
        s.settimeout(timeout)
        s.connect(ai)
        if proto == "https":
            s = _tls_wrap(s, host, timeout)
            if not s:
                return None
        s.send(("GET /%s HTTP/1.0\r\nHost: %s\r\nUser-Agent: esp32\r\n%s"
                "Accept: */*\r\nConnection: close\r\n\r\n" % (tail, host, headers)).encode())
        buf = b""
        t0 = time.ticks_ms()
        while len(buf) < maxlen and time.ticks_diff(time.ticks_ms(), t0) < timeout * 1000:
            chunk = s.recv(256)
            if not chunk:
                break
            buf += chunk
        i = buf.find(b"\r\n\r\n")
        return buf[i + 4:] if i >= 0 else buf
    except Exception:
        return None
    finally:
        try:
            if s:
                s.close()
        except Exception:
            pass


def fetch_json(url, timeout=6, maxlen=4096):
    raw = http_get(url, timeout, maxlen)
    if not raw:
        return None
    try:
        return json.loads(raw.decode())
    except Exception:
        return None


# ---------------- 标的：4 行 x 3 列 ----------------
ITEMS = [
    ("tx", "sh000001", "SSE"), ("tx", "sz399006", "CHINEXT"), ("tx", "sh000680", "STAR"),
    ("tx", "hkHSTECH", "HSTECH"), ("sn", "znb_NKY", "N225"), ("sn", "znb_KOSPI", "KS11"),
    # 美股三格改用 CME 指数期货：美股权现货在北京时间白天是闭市的死数据，
    # 期货近乎 24 小时连续交易，白天也有跳动。NQ=纳指100、ES=标普500、YM=道指。
    ("shf", "hf_NQ", "NQ"), ("shf", "hf_ES", "ES"), ("shf", "hf_YM", "YM"),
    ("tx", "hf_XAU", "GOLD"), ("tx", "hf_CL", "OIL"), ("tx", "hf_SI", "SILVER"),
]
TX_CODES = [c for s, c, _ in ITEMS if s == "tx"]
# 腾讯 hk 前缀是港股「延时 15 分钟」行情（实测时间戳恒落后本地 15 分钟），
# r_ 前缀才是实时。A 股 sh/sz 与大宗 hf_ 本身已实时，加 r_ 反而取不到，
# 故只对港股做映射。请求时两个码一次带上（实时优先、延时兜底）。
TX_RT = {"hkHSTECH": "r_hkHSTECH"}
# 两市成交额：沪市走 sh000001、深市走 sz399001 的「成交额」字段（腾讯第 38 位，单位万元）。
# sz399001 只用来算总额，不上屏，所以不进 ITEMS。
AMT_CODES = ("sh000001", "sz399001")
# 日经/韩股：腾讯没有这两个代码，东财在公司网络下会被 IP 级限流（实测整段断连），
# 故首选新浪（明文 HTTP，稳定，需带 Referer）。东财批量留作兜底。
# 美股期货同理：腾讯只有标普期货 hf_ES，纳指/道指期货没有 → 三个统一走新浪，一次请求拿全。
SN_CODES = [c for s, c, _ in ITEMS if s in ("sn", "shf")]
SN_TICKER = {"znb_NKY": "N225", "znb_KOSPI": "KS11"}
EM_WANT = ("N225", "KS11")
EM_BATCH = ("https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=100&po=1"
            "&np=1&fltt=2&fs=m:100&fields=f2,f3,f12")


def _sina():
    """新浪一次拿两种货，GBK 直接在 bytes 上切分（数字字段是 ASCII）：

      znb_*  全球指数：f[1]=点位、f[3]=涨跌幅%
      hf_*   外盘期货：f[0]=最新价、f[7]=昨收（新浪不给涨跌幅，自己算）
    """
    out = {}
    raw = http_get("http://hq.sinajs.cn/list=" + ",".join(SN_CODES), 6,
                   headers="Referer: https://finance.sina.com.cn\r\n", maxlen=6144)
    if not raw:
        return out
    for c in SN_CODES:
        key = ('hq_str_%s="' % c).encode()
        i = raw.find(key)
        if i < 0:
            continue
        f = raw[i + len(key):raw.find(b'"', i + len(key))].split(b",")
        if c.startswith("hf_"):
            if len(f) < 8 or not f[0] or not f[7]:
                continue
            try:
                last, prev = float(f[0]), float(f[7])
            except ValueError:
                continue
            if prev:
                out[c] = ((last - prev) * 100.0 / prev, last)
            continue
        if len(f) < 4 or not f[1]:
            continue
        try:
            out[c] = (float(f[3]), float(f[1]))     # (涨跌幅%, 点位)
        except (ValueError, IndexError):
            pass
    return out


def _em_batch():
    raw = http_get(EM_BATCH, 8)
    if not raw:
        return {}
    try:
        diff = (json.loads(raw.decode()).get("data") or {}).get("diff") or []
    except Exception:
        return {}
    out = {}
    for it in diff:
        tk = it.get("f12")
        if tk not in EM_WANT:
            continue
        try:
            out["100." + tk] = (float(it["f3"]), float(it.get("f2") or 0))
        except (TypeError, ValueError, KeyError):
            pass
    return out


def quotes():
    """直连公网：腾讯 1 次（明文，7 个）+ 新浪 1 次（明文，日经/韩股 + 3 个美股期货）
    + 东财批量兜底（TLS，仅在新浪取不到时）"""
    out = {}
    amt = 0.0
    amt_seg = 0
    try:
        req = []
        for c in TX_CODES:                        # 实时码与延时码同发，多占约 400B
            rt = TX_RT.get(c)
            if rt:
                req.append(rt)
            req.append(c)
        req += [c for c in AMT_CODES if c not in TX_CODES]
        body = http_get("http://qt.gtimg.cn/q=" + ",".join(req), 8, 8192)
        raw = body or b""

        def _take(code):
            """按腾讯代码切一段，返回 (涨跌幅%, 最新价)；没有该段或字段不全 → None"""
            key = ('v_%s="' % code).encode()
            i = raw.find(key)
            if i < 0:
                return None
            s = raw[i + len(key):raw.find(b'"', i + len(key))]
            if b"~" in s:
                f = s.split(b"~")
                if len(f) > 32:
                    return (float(f[32]), float(f[3]))
            else:
                f = s.split(b",")
                if len(f) > 1:
                    return (float(f[1]), float(f[0]))
            return None

        # 腾讯返回 GBK，固件无 gbk 解码器 -> 直接在 bytes 上切分数值段（数字是 ASCII）
        for c in TX_CODES:
            v = _take(TX_RT.get(c, c))
            if v is None and c in TX_RT:
                v = _take(c)                      # 实时码没给，退回 15 分钟延时码
            if v is not None:
                out[c] = v
        for c in AMT_CODES:                       # 顺路把两市成交额取出来，不多发请求
            key = ('v_%s="' % c).encode()
            i = raw.find(key)
            if i < 0:
                continue
            f = raw[i + len(key):raw.find(b'"', i + len(key))].split(b"~")
            v = 0.0
            if len(f) > 37:                       # 首选 f[37]：成交额，单位万元
                try:
                    v = float(f[37])
                except ValueError:
                    v = 0.0
            if v <= 0 and len(f) > 35:
                # 兜底：f[35] 形如「现价/成交量/成交额(元)」，第三段即成交额，
                # 除 1e4 换成万元。腾讯字段数偶有变动时靠这条不至于整条额丢失。
                try:
                    v = float(f[35].split(b"/")[2]) / 1e4
                except (ValueError, IndexError):
                    v = 0.0
            amt += v
            if v > 0:
                amt_seg += 1
    except Exception:
        pass
    # 只在两市都解析成功时写入：失败时 raw 为空、amt=0，无条件写入会把上一次
    # 的有效成交额经 pull() 的 _last.update() 覆盖成 0，屏上顶栏只剩横杠
    # （12 格因「失败码不进 out」保留旧值），表现为「只有成交量不显示了」。
    if amt > 0 and amt_seg == len(AMT_CODES):
        out["@amt"] = (amt, amt)                  # 万元。draw() 只按 ITEMS 取，多了不会被画
    out.update(_sina())
    have = 0
    for _s, _c, _t in ITEMS:
        if _c in out:
            have += 1
    if have < len(ITEMS):                         # 新浪没拿全，才试东财兜底
        em = _em_batch()
        if em:
            for secid, v in em.items():
                tk = secid.split(".")[-1]
                for src, code, ticker in ITEMS:
                    if src == "em" or ticker != tk or code in out:
                        continue
                    out[code] = v
                    break
    url = (cfg.get("quote_url") or "").strip()
    if url and have < len(ITEMS):             # 缺格才回家问一次
        j = fetch_json(url, 5)
        q = (j or {}).get("q") or {}
        for src, code, ticker in ITEMS:
            if code in out:
                continue
            v = q.get(ticker)
            if v and len(v) >= 2:
                try:
                    out[code] = (float(v[0]), float(v[1]))
                except (TypeError, ValueError):
                    pass
    return out


# ---------------- 顶栏附加指标：涨跌停家数 + 预测全天成交额 ----------------
# 日内成交额分布（16 档 × 15 分钟，A 股典型 U 型：开盘半小时最猛、午间最淡、尾盘拉起）。
# 只用来算「已走完的时间占全天的多少」，数组本身不必等于 100，运行时按总和归一化。
AMT_W = (9.0, 6.5, 5.5, 5.0, 4.5, 4.0, 4.0, 4.0,
         5.5, 4.5, 4.0, 4.0, 4.0, 4.5, 6.5, 12.0)


def _bj_now():
    """北京时间的 localtime 元组（RTC 存 UTC，显示端要 +tz）。"""
    return time.localtime(time.time() + cfg["tz"] * 3600)


def trade_min():
    """当日已交易分钟数 0..240（9:30-11:30 + 13:00-15:00）。"""
    t = _bj_now()
    cur = t[3] * 60 + t[4]
    if cur <= 570:          # 09:30 之前
        return 0
    if cur <= 690:          # 上午
        return cur - 570
    if cur <= 780:          # 午休
        return 120
    if cur <= 900:          # 下午
        return 120 + (cur - 780)
    return 240              # 15:00 之后


def est_amount(cum):
    """按 U 型分布把「当前累计成交额」外推到全天收盘（单位与 cum 一致）。

    累计进度 = 已走完那些档的权重和（当前档按分钟折算），预测 = 累计 / 进度。
    这只是估算：早盘打得越猛，外推越接近线性；成交结构突变（如午后跳水）会偏。
    """
    if not cum:
        return 0.0
    mins = trade_min()
    if mins <= 0 or mins >= 240:
        return cum
    total = 0.0
    for w in AMT_W:
        total += w
    done = 0.0
    for i in range(16):
        s = i * 15
        if mins >= s + 15:
            done += AMT_W[i]
        elif mins > s:
            done += AMT_W[i] * (mins - s) / 15.0
            break
        else:
            break
    if done <= 0:
        return cum
    return cum * total / done


def _pool_total(path):
    """同花顺涨/跌停池，只读 data.page.total。

    limit=1 让响应只有 ~300 字节（全量池有几十条，板子吃不下也没必要）。
    东财的 getTopicDTPool 在本网络返回 rc=206 空数据，不可用，所以两个都走同花顺。
    """
    t = _bj_now()
    u = ("https://data.10jqka.com.cn/dataapi/limit_up/" + path +
         "?page=1&limit=1&field=199112&filter=HS%%2CGEM2STAR"
         "&order_field=199112&order_type=1&date=%04d%02d%02d" % (t[0], t[1], t[2]))
    j = fetch_json(u, 6, 2048)              # 单次 6s；market() 调两次，最坏 12s
    if not j:
        return None
    try:
        return int(((j.get("data") or {}).get("page") or {}).get("total"))
    except (TypeError, ValueError, AttributeError):
        return None


def market():
    """涨跌停家数。两次 TLS（各约 0.8s），只在交易时段调用，收盘后不白发请求。"""
    mins = trade_min()
    # 午休（trade_min 返回 120）家数不会变，沿用上次的值即可，省掉 2 次 TLS；
    # 开盘前 0 / 收盘后 240 同理。
    if mins <= 0 or mins >= 240 or mins == 120:
        return None
    zt = _pool_total("limit_up_pool")
    dt = _pool_total("lower_limit_pool")
    if zt is None and dt is None:
        return None
    return {"zt": zt, "dt": dt}


# ---------------- 页 ----------------
GRID_Y0, ROW_H = 28, 66
COL_X, CELL_W = (0, 133, 266), 133


class Page:
    name = "?"
    ttl = 180
    RETRY_SEC = 30                   # 取数失败后的重试间隔，别每轮主循环都硬打

    def __init__(self):
        self.data = None
        self.ts = 0                  # 内容上次「变动」的时刻（屏上数字的变化时间）
        self.poll_at = 0             # 上次「取数」的时刻，与 ts 分开：取到了但数字没变时只更新这个
        self.next_try = 0            # 失败退避：下一次允许重试的时刻

    def pull(self):
        raise NotImplementedError

    def sync_cfg(self):
        """配置改动后同步本页参数（子类按需覆盖）。"""
        pass

    def auto(self):
        """该页现在是否参与自动展示（子类按需覆盖）。"""
        return True

    def draw(self):
        raise NotImplementedError

    def sig(self):
        """内容指纹：用来判断这一页到底有没有变，没变就不刷屏。
        只看数据、不看时间，避免顶栏分钟数一跳就白刷 5 秒。"""
        return repr(self.data)

    def due(self):
        """该不该取数了。刚失败过就等退避时间，别死循环重试。"""
        now = time.time()
        if now < self.next_try:
            return False
        # 必须用 poll_at 而不是 ts：内容没变时 ts 不前进，用 ts 判断会导致
        # due() 永远为真 —— 主循环 200ms 一轮，等于每秒打 4-5 次行情接口。
        return self.data is None or now - self.poll_at > self.ttl

    def refresh(self):
        """取数；成功 True，失败 False（失败后退避 RETRY_SEC 秒）。
        取到了但内容和上次一样：只更新 poll_at，不动 ts —— 屏上时间照走，
        但「数据已静止」靠空心小方块表达，不会因为数字没变就假装整页是新的。"""
        try:
            old = self.sig()
            d = self.pull()
            if d:
                self.next_try = 0
                self.poll_at = time.time()
                if self.sig_of(d) == old:
                    return True                 # 取到了，内容没变
                self.data, self.ts = d, time.time()
                return True
        except Exception:
            pass
        self.poll_at = time.time()              # 失败也算试过，否则会被当成本轮还没取
        self.next_try = time.time() + self.RETRY_SEC
        return False

    def sig_of(self, d):
        """按指定数据算指纹（refresh 里要和旧值比，不能直接读 self.data）。"""
        return repr(d)


class QuotePage(Page):
    """12 格：代码 + 无符号涨跌幅；跌=加框/括号/反白（可配）"""
    name = "quote"
    _last = {}
    mkt = {}                    # {"zt": 涨停家数, "dt": 跌停家数}，取不到就沿用上次

    def __init__(self):
        Page.__init__(self)
        self.mkt = {}
        self.sync_cfg()

    def sync_cfg(self):
        """网页改了配置后立即套用——ttl 是构造时拷进来的，不同步就要重启才生效。"""
        self.ttl = self.ttl_now()

    def ttl_now(self):
        """盘中间隔 / 非盘中间隔，按当前时刻取。

        这块屏差分只驱动变化像素（不闪），全刷只剩换页和每 8 次一次的
        清理，所以间隔按交易时段分两档：盘中 120s（默认），早晚 600s。
        """
        return cfg["quote_sec"] if in_session() else cfg["quote_sec_off"]

    def due(self):
        # 每次判 due 都重算：跨过 09:00 / 15:30 时立刻切档，不用等重启
        self.ttl = self.ttl_now()
        return Page.due(self)

    def pull(self):
        out = quotes()
        if not out:
            return None
        self._last.update(out)          # 偶发失败沿用上一次，别让格子变 --
        try:
            m = market()                # 涨跌停家数；非交易时段返回 None，保留上次的
        except Exception:
            m = None
        if m:
            self.mkt = m
        return self._last

    def sig(self):
        """带上涨跌停家数：家数变了顶栏就得重画，不能只比 12 格。"""
        return repr((self.data, self.mkt))

    def sig_of(self, d):
        return repr((d, self.mkt))

    def stats(self):
        """顶栏 extra：涨停/跌停家数，例 `58/11`（无数据→`--/--`）。成交额单独在最右绘制。"""
        m = self.mkt or {}
        zt, dt = m.get("zt"), m.get("dt")
        return "%s/%s" % ("--" if zt is None else zt, "--" if dt is None else dt)

    def amt_txt(self):
        """成交额诊断串：`13692亿` / `-`（开市但没取到）/ `closed`（未开盘或周末）。
        网页 /api/state 直接透出，屏上看不到时先看这里。"""
        if not market_show_data("SSE"):
            return "closed"
        v = (self.data or {}).get("@amt")
        if not v or v[0] <= 0:
            return "-"
        return "%.0f亿" % (v[0] / 1e4)

    def draw(self):
        fb.fill(WHITE)
        bar(self.poll_at)
        # 顶栏右侧：涨停/跌停 + 成交额（中间一个空格）；成交额无数据 → 横杠
        st = self.stats()                          # "28/9" 或 "--/--"
        v = (self.data or {}).get("@amt")
        if market_show_data("SSE") and v and v[0] > 0:
            s = "%s %.0f" % (st, v[0] / 1e4)       # "28/9 785"（一个空格）
            text(s, SCR_W - 20 - text_w(s, 2), 4, 2)   # 右对齐到最右
        elif market_show_data("SSE"):
            # 已开盘但成交额没取到 → 显式 "--"。以前这里和「未开盘」共用一条黑杠，
            # 屏上根本分不清是没开盘还是取数失败，这次把两种状态拆开。
            s = st + " --"
            text(s, SCR_W - 20 - text_w(s, 2), 4, 2)
        else:
            text(st, 340 - text_w(st, 2), 4, 2)    # 涨跌停右对齐，给横杠留位
            fb.fill_rect(348, 6, 32, 4, BLACK)     # 成交额开盘前/周末 → 横杠（收盘后保留）
        mode = cfg["mode"]
        for i, (src, code, tk) in enumerate(ITEMS):
            cx = COL_X[i % 3]
            cy = GRID_Y0 + (i // 3) * ROW_H
            v = (self.data or {}).get(code)
            if not market_show_data(tk):         # 开盘前/周末 → 横杠（收盘后当天仍保留数据）
                text(tk[0], cx + 6, cy + 6, 2)
                fb.fill_rect(cx + 49, cy + 38, 34, 4, BLACK)
                continue
            if v is None:
                text(tk[0], cx + 6, cy + 6, 2)
                center("--", cy + 30, 3, cx, CELL_W)
                continue
            p = float(v[0])
            down = p < 0
            for f in ("%.2f", "%.1f", "%.0f"):
                t = f % abs(p)
                if len(t) <= 5:
                    break
            shown = ("(" + t + ")") if (mode == "paren" and down) else t
            text(tk[0], cx + 6, cy + 6, 2)              # 标的代号只取首字母，左置成小标签
            sc = 3                                     # 字太大就降一档，保证不越出格子
            while sc > 2 and text_w(shown, sc) > CELL_W - 10:
                sc -= 1
            w = text_w(shown, sc)
            x = cx + max(0, (CELL_W - w) // 2)
            y = cy + 28
            if mode == "inv" and down:                 # 反白：白字黑底，墨水屏上最醒目
                fb.fill_rect(x - 3, y - 3, w + 6, 8 * sc + 6, BLACK)
                text(shown, x, y, sc, WHITE)
            else:
                text(shown, x, y, sc, BLACK)
                if mode == "box" and down:             # 加框
                    fb.rect(x - 3, y - 3, w + 6, 8 * sc + 6, BLACK)
        for x in (133, 266):
            vline(x, GRID_Y0, GRID_Y0 + ROW_H * 4)
        for r in range(5):
            hline(GRID_Y0 + r * ROW_H)





def bar(clock):
    """顶栏：左上「上次取数」时间（每分钟跟着走，证明它还活着）。
    顶栏右侧的「涨停/跌停 + 成交额」由 QuotePage.draw 绘制；旧版 10x10 方块已移除。"""
    text(hhmm(clock), 3, 4, 2)
    hline(24)


PAGES = [QuotePage()]


def page_dots(i):
    for k in range(len(PAGES)):
        x = SCR_W // 2 - len(PAGES) * 8 + k * 16
        if k == i:
            fb.fill_rect(x, 294, 10, 4, BLACK)
        else:
            fb.rect(x, 294, 10, 4, BLACK)


# ---------------- 屏驱动封装 ----------------
# 2026-11-03 真机结论（差分刷新已跑通，推翻"没有局刷"的旧结论）：
#   · 全刷 display()：0x24 新帧 + 0x26 清零 + 0xF7，2.9s，整屏闪一下；
#   · 差分 display_diff(buf, prev)：0x24 新帧 + 0x26 旧帧 + 70 字节 LUT + 0xCF，
#     只驱动变化像素，不闪，约 1.0s（TP=20）。
#   · 0x26 当旧帧的前提是 0x21=0x00；init() 发的 0x21=0x40 会把红平面旁路成 0，
#     display_diff() 内部会自己切换、刷完再恢复 0x40。
# 策略：内容没变不刷；数字跳动走差分；换页/手动刷新/攒够
# cfg["diff_full_every"] 次差分后走一次全刷清理残影（控制台可调，1=每次全刷）。
_full_ok = False
_updates = 0                  # 保留给网页控制台显示，现在等于"总刷新次数"
_prev = None                  # 上一帧画面，用来判断"内容到底变没变"


def show(full=False):
    """把 BUF 刷上屏。

    首次进来先 clean() 驱回纯白；之后：`full=True`（换页、手动刷新、首帧）
    走全刷；内容变化走差分（不闪）。内容与上一帧完全一致时直接跳过。
    """
    global _full_ok, _updates, _prev
    cur = bytes(BUF)
    if not _full_ok:
        e = epd_init()
        e.clean()            # 冷启动先把屏驱回纯白，盖掉上一轮的灰底
        _full_ok = True
        _prev = None
    if (not full) and _prev == cur:
        return
    epd_init()               # 上一轮刷完已经进深睡，唤醒必须重走 init（内部带 RST）
    if full or _prev is None:
        epd.display(BUF)             # 全刷：2.9s，整屏闪一下
    else:
        epd.display_diff(BUF, _prev)  # 差分：只驱动变化像素，不闪
    epd.sleep()              # 刷完立刻进深睡：厂家 FAQ 明确「刷新后长时间挂高压会损坏屏」
    _prev = cur
    _updates += 1


def _blank():
    for i in range(len(BUF)):
        BUF[i] = 0xFF


def selftest():
    """刷屏自检：同一张大字母，四种刷法各试一次，肉眼确认哪种真能上屏。

    A = display_Partial 无 prev -> 全刷（0xF7）
    B = display_Partial 带 prev -> 差分（0xCF + 70 字节 LUT，只刷变化像素）
    C = init_fast() + display_Fast -> 全刷（本屏没有独立的快刷波形）
    D = display() 0xF7 全刷      （对照组，一定成功）

    每种停 6 秒，跑完自动把当前页面全刷回来。
    """
    global _prev, _full_ok
    e = epd_init()
    for i, ch in enumerate("ABCD"):
        _blank()
        center(ch, 60, 10)                 # 80x80 大字
        center("TEST " + ch, 220, 4)
        try:
            if i == 0:
                e.display_Partial(BUF, None)
            elif i == 1:
                # 造一个「只有字母那几行不同」的上一帧，窗口才会收窄
                prev = bytearray(BUF)
                for y in range(60, 140):
                    o = y * epd42.LINE_BYTES
                    prev[o:o + epd42.LINE_BYTES] = b"\xff" * epd42.LINE_BYTES
                e.display_Partial(BUF, prev)
            elif i == 2:
                e.init_fast()
                e.display_Fast(BUF)
            else:
                e.display(BUF)
        except Exception as ex:
            print("selftest", ch, "err", ex)
        _prev = bytes(BUF)
        time.sleep(6)
    _full_ok = True
    panel.dirty = True
    panel.force_full = True


# ---------------- 网络 ----------------
sta = network.WLAN(network.STA_IF)


def connect():
    sta.active(True)
    try:
        sta.config(pm=network.WLAN.PM_NONE)   # 关 WiFi 省电：ping 315ms -> 32ms
    except Exception:
        pass
    if sta.isconnected():
        return True
    if not cfg["ssid"]:
        return False
    try:
        sta.connect(cfg["ssid"], cfg["pwd"])
        for _ in range(40):
            if sta.isconnected():
                return True
            time.sleep_ms(500)
    except Exception:
        pass
    return False


MON = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
       "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}


def _days(y, m, d):
    """公历日期 -> 距 1970-01-01 的天数"""
    y -= m <= 2
    era = (y if y >= 0 else y - 399) // 400
    yoe = y - era * 400
    doy = (153 * (m + (-3 if m > 2 else 9)) + 2) // 5 + d - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def _epoch_offset():
    """MicroPython 的 time 纪元可能是 2000-01-01（这块板就是），返回需要减掉的秒数"""
    return 946684800 if time.localtime(0)[0] == 2000 else 0


def sync_http():
    """公司网络可能封 NTP(UDP 123)，改用 HTTP 响应头里的 Date 对时"""
    s = None
    try:
        ai = socket.getaddrinfo("qt.gtimg.cn", 80)[0][-1]
        s = socket.socket()
        s.settimeout(6)
        s.connect(ai)
        s.send(b"GET /q=sh000001 HTTP/1.0\r\nHost: qt.gtimg.cn\r\n"
               b"Connection: close\r\n\r\n")
        buf = s.recv(512)
        i = buf.find(b"Date: ")
        if i < 0:
            return False
        p = buf[i + 6:buf.find(b"\r\n", i)].decode().split()
        d, mo, y = int(p[1]), MON[p[2]], int(p[3])
        t = p[4].split(":")
        # 与 NTP 口径保持一致：RTC 存 UTC，显示时再按 tz 偏移（见 hhmm/now_str）
        ep = (_days(y, mo, d) * 86400 + int(t[0]) * 3600 + int(t[1]) * 60
              + int(t[2]) - _epoch_offset())
        tm = time.localtime(ep)
        machine.RTC().datetime((tm[0], tm[1], tm[2], tm[6],
                                tm[3], tm[4], tm[5], 0))
        return True
    except Exception:
        return False
    finally:
        try:
            if s:
                s.close()
        except Exception:
            pass


def sync_time():
    if ntptime:
        try:
            ntptime.settime()
            return True
        except Exception:
            pass
    return sync_http()


# ---------------- 网页控制台 ----------------
# 布局：桌面宽幅两栏（左操作 / 右配置 3 列网格），窄屏折叠回单列
HTML = """<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>墨水屏面板</title>
<style>
*{box-sizing:border-box}
body{font:15px -apple-system,"PingFang SC";margin:0;padding:24px 20px;color:#222;background:#f4f5f7}
.wrap{max-width:1080px;margin:0 auto}
header{display:flex;align-items:baseline;gap:14px;margin-bottom:16px;flex-wrap:wrap}
h1{font-size:20px;margin:0}
#st{font-size:13px;color:#666}
.cols{display:grid;grid-template-columns:260px 1fr;gap:18px;align-items:start}
.card{background:#fff;border:1px solid #e4e6ea;border-radius:12px;padding:18px 20px}
h2{font-size:14px;margin:0 0 12px;color:#555}
.b{display:block;width:100%;margin:8px 0;padding:12px 0;border:0;border-radius:9px;
   font:600 15px/1.2 -apple-system,"PingFang SC","Helvetica Neue",sans-serif;
   color:#fff;cursor:pointer;transition:filter .12s;letter-spacing:.01em}
.b:first-of-type{margin-top:0}
.b:hover{background:#f0f1f3}
.b:active{background:#e6e8eb;transform:translateY(1px)}
.f{background:#fff;color:#111;box-shadow:inset 0 0 0 1.5px #111}
.s{background:#fff;color:#333;box-shadow:inset 0 0 0 1px #c9ccd2}
.warn{background:#fff;color:#b3392c;box-shadow:inset 0 0 0 1px #e2a39d}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px 18px}
.grid.g2{grid-template-columns:repeat(2,1fr)}
.grid.g1{grid-template-columns:1fr}
.grp{margin:0 0 20px}
.grp:last-child{margin-bottom:0}
.grp h3{display:flex;align-items:baseline;gap:9px;font-size:12px;margin:0 0 10px;color:#3d434b;font-weight:600;letter-spacing:.03em}
.grp h3 em{font-style:normal;font-weight:400;color:#a6acb6;font-size:11px;letter-spacing:0}
label{display:block;color:#666;font-size:12px}
input,select{width:100%;padding:7px 9px;margin-top:4px;border:1px solid #d8dbe0;border-radius:7px;font-size:14px;background:#fff}
input[type=checkbox]{width:auto;margin-top:9px}
.hint{font-size:11px;color:#999;margin-top:3px}
#msg{font-size:13px;color:#1a9d54;min-height:18px;margin-top:8px}
@media(max-width:860px){.grid,.grid.g2{grid-template-columns:1fr 1fr}}
@media(max-width:680px){.cols{grid-template-columns:1fr}.grid,.grid.g2{grid-template-columns:1fr 1fr}}
@media(max-width:440px){.grid,.grid.g2{grid-template-columns:1fr}}
</style>
<div class=wrap>
<header><h1>墨水屏面板</h1><div id=st>页 {page} &middot; 刷屏 {time} &middot; 数据 {dtime} &middot; 档 {rate} &middot; {mode}</div></header>
<div class=cols>
<div class=card><h2>操作</h2>
<button class="b f" onclick=g('page=0')>行情页</button>
<button class="b s" onclick=g('next=1')>下一页</button>
<button class="b s" onclick=g('refresh=1')>立即刷新</button>
<button class="b s" onclick="fetch('/cmd?led=offline').then(()=>{document.getElementById('st').textContent='灯自检：红色呼吸 30 秒'})">灯自检（红呼吸）</button>
<button class="b s" onclick="fetch('/cmd?selftest=1').then(()=>{document.getElementById('st').textContent='刷屏自检：A/B/C/D 各停 6 秒，盯着屏幕'})">刷屏自检</button>
<button class="b warn" onclick="rb(this)">重启板子</button>
</div>
<div class=card><h2>配置 <span style="font-weight:400;color:#9aa0a8;font-size:12px">改动即自动保存</span></h2>
<form onsubmit="return post()"></form>
<div id=msg></div>
</div>
</div>
<script>
const f=document.querySelector('form');
const G=[
["刷新节奏","数字变化走差分（约 1s 不闪）；全刷只剩换页和下面这个节奏的清理",[
 ["quote_sec","盘中刷新间隔（秒）","num","09:00-15:30 用这个；不低于 60"],
 ["quote_sec_off","非盘中间隔（秒）","num","早晚时段用这个；不低于 180，宁慢勿闪"],
 ["diff_full_every","每几次刷新全刷一次","num","清残影用：1=每次全刷，8=默认，上限 500"]]],
["静默时段","时段外不取数不刷屏，但网页控制台和按键照常可用",[
 ["page_sec","手动翻页后停留（秒）","num","到点自动回当前时段该显示的页"],
 ["active_from","起（时）","num","含本小时"],
 ["active_to","止（时）","num","不含本小时，20 = 晚 8 点后静默"],
 ["weekend_off","周末全天静默","chk",""]]],
["显示与灯","",[
 ["mode","跌的表示","sel","box=加框 paren=括号 inv=反白 plain=无"],
 ["tz","时区（小时）","num",""],
 ["led_bright","状态灯亮度（0-255）","num","0=全灭；在线蓝 / 离线红呼吸 / 绿闪刷屏 / 橙闪取数失败"]]],
["高级","",[
 ["quote_url","聚合服务地址","txt","留空=板子直连公网"]]]
];
function mkfield(a){const[k,lab,t,hint]=a;let i;
if(t=='sel'){i=document.createElement('select');i.name=k;
['box','paren','inv','plain'].forEach(o=>{const x=document.createElement('option');x.value=o;x.textContent=o;i.appendChild(x)});}
else{i=document.createElement('input');i.type=t=='num'?'number':t=='chk'?'checkbox':'text';i.name=k;}
const l=document.createElement('label');l.textContent=lab;l.appendChild(i);
if(hint){const s=document.createElement('div');s.className='hint';s.textContent=hint;l.appendChild(s);}
return l;}
G.forEach(g=>{const[title,sub,items]=g;
const box=document.createElement('div');box.className='grp';
const h=document.createElement('h3');h.textContent=title;
if(sub){const e=document.createElement('em');e.textContent=sub;h.appendChild(e);}
box.appendChild(h);
const gr=document.createElement('div');
gr.className='grid'+(items.length==4?' g2':(items.length<3?' g1':''));
items.forEach(a=>gr.appendChild(mkfield(a)));
box.appendChild(gr);f.appendChild(box);});
fetch('/api/config',{cache:'no-store'}).then(r=>r.json()).then(c=>{
f.querySelectorAll('[name]').forEach(el=>{const v=c[el.name];
if(el.type=='checkbox')el.checked=!!v;else if(v!==undefined)el.value=v;});});
// 改完即存：字段失焦/下拉切换/勾选都会触发 change，不用再点保存
function save(){const m=document.getElementById('msg');
const b=new URLSearchParams(new FormData(f)).toString();
m.style.color='#999';m.textContent='保存中…';
fetch('/api/config',{method:'POST',cache:'no-store',body:b}).then(r=>r.text()).then(t=>{
m.style.color='#1a9d54';
m.textContent=t+' · '+new Date().toLocaleTimeString('zh-CN',{hour12:false});}).catch(()=>{
m.style.color='#c00';m.textContent='保存失败，板子没响应（可能正在刷屏，稍后自动重试）';
setTimeout(save,1500);});}
function post(){save();return false}
f.addEventListener('change',save);        // 失焦/下拉/勾选
f.addEventListener('keydown',e=>{if(e.key=='Enter'){e.preventDefault();save()}});
function g(q){fetch('/cmd?'+q).then(()=>location.reload())}
function rb(b){b.disabled=true;b.style.opacity=.5;
fetch('/cmd?reboot=1').catch(()=>{});
const st=document.getElementById('st'),t0=Date.now();
const iv=setInterval(()=>{
  st.textContent='重启中… '+(Math.round((Date.now()-t0)/1000))+'s';
  fetch('/api/config',{cache:'no-store'}).then(r=>{if(r.ok){clearInterval(iv);location.reload()}}).catch(()=>{});
},2000)}
</script>"""


_srv = None


_pending_fail = False             # 网页"立即刷新"取数失败，留给主循环橙闪


def _force_pull():
    """立即抓一次当前页数据（网页"立即刷新"，静默期也生效）"""
    global _pending_fail
    p = panel.cur()
    _pending_fail = not p.refresh()
    panel.dirty = True


# ---------- 配置表单 ----------
_CFG_NUM = ("quote_sec", "quote_sec_off", "diff_full_every", "page_sec",
            "active_from", "active_to", "tz", "led_bright")
_CFG_TXT = ("mode", "quote_url")
_CFG_MODES = ("box", "paren", "inv", "plain")


def _unquote(s):
    out, i = "", 0
    while i < len(s):
        if s[i] == "%" and i + 2 < len(s):
            try:
                out += chr(int(s[i + 1:i + 3], 16))
                i += 3
                continue
            except ValueError:
                pass
        out += s[i]
        i += 1
    return out


def _apply_cfg_form(body):
    """解析表单 -> 更新内存配置（立即生效）-> 写盘。带屏寿命护栏。"""
    got = {}
    for kv in body.split("&"):
        if "=" in kv:
            k, v = kv.split("=", 1)
            got[k] = _unquote(v.replace("+", " "))
    # 改动前快照：只有真变了的字段才触发重画/重抓，别每次保存都白刷一次屏
    before = {k: cfg.get(k) for k in _CFG_WATCH}
    for k in _CFG_NUM:
        if k in got:
            try:
                cfg[k] = int(float(got[k]))
            except ValueError:
                pass
    for k in _CFG_TXT:
        if k in got:
            cfg[k] = got[k]
    cfg["weekend_off"] = "weekend_off" in got
    # 护栏：刷新寿命按 update 命令计数，差分和全刷都记 1 次，所以间隔仍由次数决定。
    # 厂家 FAQ：非局刷产品刷新间隔建议 >= 180 秒。盘中下限放宽到 60（用户 2026-11-05 要求
    # 1 分钟，主动接受约 2x 寿命损耗）；非盘中守厂家口径 180，且不得比盘中间隔更密。
    if cfg["quote_sec"] < 60:
        cfg["quote_sec"] = 60
    if cfg["quote_sec_off"] < 180:
        cfg["quote_sec_off"] = 180
    if cfg["quote_sec_off"] < cfg["quote_sec"]:
        cfg["quote_sec_off"] = cfg["quote_sec"]
    cfg["full_every"] = 1                # 已废弃键，恒 1；真节奏看 diff_full_every
    # diff_full_every：每 N 次刷新全刷一次清残影。1=每次全刷（最干净最闪），
    # 8=默认，上限 500（≈盘中一整天都不触发周期全刷）。再大也有兜底：
    # 每天 07:00 时段边界 active 切换时会 force 一次全刷，见 main()。
    cfg["diff_full_every"] = max(1, min(500, int(cfg["diff_full_every"])))
    if cfg["mode"] not in _CFG_MODES:
        cfg["mode"] = "box"
    cfg["led_bright"] = max(0, min(255, cfg["led_bright"]))
    cfg["active_from"] = max(0, min(23, cfg["active_from"]))
    cfg["active_to"] = max(1, min(24, cfg["active_to"]))
    for _p in PAGES:                     # ttl 是构造时拷的，这里同步，做到真的立即生效
        _p.sync_cfg()
    save_cfg(cfg)
    _after_cfg_change(before)
    return "已保存并生效"


# 保存后要善后的字段分两类：
#   显示类：只被 draw() 读取，而 render() 按内容指纹去重——数据不变就不重画，
#           静默期可能几小时看不到效果，用户以为「改了没生效」；
#   取数类：只被取数函数读取，要等该页下一次 due 才拉，
#           且主循环只给当前页取数。
_CFG_WATCH = ("mode", "tz", "quote_url")
_CFG_DISP = ("mode", "tz")                  # 只影响画法
_CFG_LOC = (("quote", ("quote_url",)),)                    # 只影响行情页取数


def _after_cfg_change(before):
    """配置保存后的善后：该重画的重画，该重抓的重抓。

    没有这一步，mode/tz 要等下一次数据变化才上屏（静默期等于
    「改了没反应」）；quote_url 要等该页下一个取数周期才生效。
    """
    disp = any(cfg.get(k) != before.get(k) for k in _CFG_DISP)
    loc = any(cfg.get(k) != before.get(k) for _, ks in _CFG_LOC for k in ks)
    if loc:
        for p in PAGES:                  # 受影响页清零取数计时，下次显示立即拉新数据
            for name, ks in _CFG_LOC:
                if p.name == name and any(cfg.get(k) != before.get(k) for k in ks):
                    p.next_try = 0
                    p.poll_at = 0
        _force_pull()                    # 当前页立即重抓（静默期也生效，失败有退避重试）
    if disp or loc:
        panel.key = None                 # 绕开 render() 的内容指纹去重
        panel.dirty = True               # 下一轮主循环立即重画（走差分，不闪）


_reboot = False                    # 网页点了"重启板子"后置位，主循环里安全复位
_selftest = False                  # 网页点了"刷屏自检"，主循环里跑一遍四种刷法


def web_once():
    """监听 socket 常驻；每轮只做一次非阻塞 accept。
    原来每轮重新 bind/close，浏览器连接经常撞在 close 瞬间被 RST，网页打不开。"""
    global _srv
    if _srv is None:
        try:
            s = socket.socket()
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("0.0.0.0", 80))
            s.listen(8)
            s.settimeout(0.05)
            _srv = s
        except Exception:
            return
    try:
        cl, _ = _srv.accept()
    except Exception:
        return
    try:
        cl.settimeout(1)
        # 一次 recv 不保证收全：按 Content-Length 补齐，否则表单会偶尔「保存失败」
        raw = b""
        while len(raw) < 2048:
            try:
                chunk = cl.recv(1024)
            except Exception:
                break
            if not chunk:
                break
            raw += chunk
            if b"\r\n\r\n" in raw:
                head, bd = raw.split(b"\r\n\r\n", 1)
                clen = 0
                for ln in head.split(b"\r\n"):
                    if ln.lower().startswith(b"content-length:"):
                        try:
                            clen = int(ln.split(b":", 1)[1])
                        except Exception:
                            clen = 0
                if len(bd) >= clen:
                    break
        req = raw.decode("utf-8", "ignore")
        line = req.split("\r\n", 1)[0] if req else ""
        parts = line.split(" ")
        method = parts[0] if parts else "GET"
        path = parts[1] if len(parts) > 1 else "/"
        cmd = path[5:] if path.startswith("/cmd?") else ""
        body = req.split("\r\n\r\n", 1)[1] if "\r\n\r\n" in req else ""
        resp = None
        if path == "/api/state":
            # 诊断用：一眼看出「多久取一次数、多久刷一次屏、数字多久没动」
            c = panel.cur()
            resp = "application/json", json.dumps({
                "page": panel.i, "name": c.name,
                "paint": hhmm(panel.last_ts),                 # 上次刷屏
                "clock": hhmm(c.poll_at),                     # 上次取数
                "data": hhmm(c.ts),                           # 数字上次变
                "data_age": int(time.time() - c.ts) if c.ts else -1,
                "updates": _updates, "diff_full_every": cfg["diff_full_every"],
                "diff_count": panel.diff_count,
                "amt": getattr(c, "amt_txt", lambda: "n/a")(),   # 顶栏成交额是不是真拿到了
                "active": is_active()})
        elif path == "/api/config":
            if method == "POST" and body:
                try:
                    resp = "text/plain", _apply_cfg_form(body)
                except Exception as e:
                    print("cfg save err:", e)
                    resp = "text/plain", "保存失败: %s" % e
            else:
                # 表单回填用；ssid/pwd 不下发——这个页面局域网内谁都能打开
                safe = {k: v for k, v in cfg.items() if k not in ("ssid", "pwd")}
                resp = "application/json", json.dumps(safe)
        else:
            if "page=0" in cmd or "page=1" in cmd:
                panel.jump(1 if "page=1" in cmd else 0)
            elif "next=1" in cmd:
                panel.next()
            elif "refresh=1" in cmd:
                _force_pull()               # 静默期也能手动抓一次
                panel.force()
            elif cmd.startswith("led="):
                # 自检灯态：led=offline 红呼吸 30 秒，led=idle 回暗蓝
                led_state("offline" if "offline" in cmd else "idle", manual=True)
            elif "reboot=1" in cmd:
                global _reboot
                _reboot = True              # 响应先发出去，主循环里再复位
            elif "selftest=1" in cmd:
                global _selftest
                _selftest = True
        if resp is None:
            resp = ("text/html; charset=utf-8",
                    HTML.replace("{page}", str(panel.i))
                        .replace("{time}", hhmm(panel.last_ts))
                        .replace("{dtime}", hhmm(panel.cur().ts))
                        .replace("{rate}", rate_str())
                        .replace("{mode}", mode_str()))
        # no-store：浏览器不许缓存配置 JSON/页面，否则刷新会看到旧值
        cl.send(b"HTTP/1.0 200 OK\r\nContent-Type: " + resp[0].encode() +
                b"\r\nCache-Control: no-store, no-cache, must-revalidate\r\n"
                b"Pragma: no-cache\r\n\r\n" + resp[1].encode())
    except Exception as e:
        print("web err:", e)          # 别静默吞掉，串口能看到
    finally:
        try:
            cl.close()
        except Exception:
            pass


# ---------------- 主循环 ----------------
class Panel:
    def __init__(self):
        self.i = 0
        self.page_at = 0
        self.last_ts = 0
        self.dirty = True
        self.force_full = False
        self.hold_until = 0              # 手动翻页后的保护期截止时刻
        self.key = None                  # 上一次真正刷到屏上的内容指纹
        self.diff_count = 0              # 连续差分次数，攒够 cfg["diff_full_every"] 全刷一次

    def cur(self):
        return PAGES[self.i]

    def goto(self, i, full=True):
        self.i = i % len(PAGES)
        self.page_at = time.time()
        self.dirty = True
        self.force_full = full

    def jump(self, i):
        """手动跳页：给一段保护期，别被自动调度立刻拉回行情页"""
        self.goto(i)
        self.hold_until = time.time() + max(20, cfg["page_sec"])

    def next(self):
        self.jump(self.i + 1)

    def auto_page(self):
        """当前自动显示的页号（只有行情页）"""
        return 0

    def tick(self):
        """主循环每轮调用：跨过时段边界时自动换页（手动保护期内不动）"""
        if time.time() < self.hold_until:
            return
        want = self.auto_page()
        if want != self.i:
            self.goto(want)

    def force(self):
        self.dirty = True
        self.force_full = True

    def render(self):
        """内容变了才刷。

        数字/内容变化走差分（不闪，约 1s）；换页、手动刷新、以及连续差分
        攒够 cfg["diff_full_every"] 次后走一次全刷（2.9s）清理残影。
        （旧结论"这块屏没有局刷、每次刷新都是全屏闪"已被 2026-11-03 的
        差分实验推翻，见 epd42.display_diff。）
        """
        cur = self.cur()
        key = (self.i, cur.sig())
        if key == self.key and not self.force_full:
            self.dirty = False
            return
        self.cur().draw()
        page_dots(self.i)
        # 差分连续攒够 cfg["diff_full_every"] 次，强制全刷一次清理残影（控制台可调）
        full = self.force_full or self.diff_count >= cfg["diff_full_every"]
        show(full=full)
        if full:
            self.diff_count = 0
        else:
            self.diff_count += 1
        self.key = key
        self.dirty = False
        self.force_full = False
        self.last_ts = time.time()
        led_evt((0, 1, 0), 90)            # 绿闪：这一页刚刷完


panel = Panel()


def btn_pressed():
    if not btn:
        return False
    if btn.value() == 0:
        time.sleep_ms(60)
        if btn.value() == 0:
            while btn.value() == 0:
                time.sleep_ms(20)
            return True
    return False


def start_webrepl():
    """WiFi 通道：WS8266 端口，拔了 USB 线也能传文件/跑代码。
    密码在 webrepl_cfg.py；必须连上网之后才启动。"""
    try:
        import webrepl
        webrepl.start()
        print("WebREPL: ws://%s:8266" % sta.ifconfig()[0])
    except Exception as e:
        print("webrepl err:", e)


# ---------------- 看门狗：主循环卡死自动重启 ----------------
# ESP32 上 MicroPython 网络栈 / e-ink SPI 偶发假死（HTTPS 卡住、或刷屏被并发打断），
# 主循环会被钉死、控制台只接不发。这里用软件定时器周期检测主循环心跳：
# 主循环每轮更新 _last_beat；若超过 WDTO 秒没心跳（正常取数最坏约 39s），判定卡死并复位。
# 阈值取 50s，远高于最坏取数，避免慢取数被误杀；真正的死循环/阻塞才会触发。
WDTO = 50
_last_beat = 0
_wd_timer = None


def _wdog(_t):
    if _last_beat and time.time() - _last_beat > WDTO:
        machine.reset()


def main():
    global _pending_fail, _last_beat, _wd_timer
    if not connect():
        fb.fill(WHITE)
        center("NO WIFI", 130, 3)
        show(full=True)
        led_state("offline")             # 红呼吸常亮：一眼看出没网，不再闪一下就没
    else:
        led_state("idle")
        start_webrepl()
    start_led_anim()                     # 状态灯动画独立线程（主循环卡住也不掉帧）
    sync_time()
    panel.goto(panel.auto_page())        # 按当前时段决定开局显示哪页
    # 启动软件看门狗：周期检测主循环心跳，卡死自动复位（开机联网延迟不计入，避免误杀）
    if _wd_timer is None:
        try:
            _wd_timer = machine.Timer(0)
            _wd_timer.init(mode=machine.Timer.PERIODIC, period=2000, callback=_wdog)
        except Exception as e:
            print("wdog err:", e)
            _wd_timer = None
    _last_beat = time.time()
    net_at = time.time()
    was_active = is_active()
    while True:
        _last_beat = time.time()          # 看门狗心跳：每轮刷新，卡死超 WDTO 秒由 _wdog 复位
        # 掉线：立刻转红呼吸；每 60s 重连一次
        if not sta.isconnected():
            led_state("offline")
            if time.time() - net_at > 60:
                net_at = time.time()
                if connect():
                    sync_time()
                    panel.force()
                    led_state("idle")            # 先复位状态
                    led_evt((0, 1, 0), 200)      # 绿：重新连上
        else:
            led_state("idle")
        # 工作时段切换：静默结束 -> 重新对时、丢掉隔夜旧数据、全刷一次
        active = is_active()
        if active != was_active:
            was_active = active
            if active:
                sync_time()
                for p in PAGES:
                    p.data = None
                panel.force()
                led_evt((0, 1, 0), 200)
        web_once()
        if _reboot:
            print("网页请求重启")
            time.sleep_ms(300)               # 给 HTTP 响应留时间发出去
            machine.reset()
        if _selftest:
            globals()["_selftest"] = False
            print("刷屏自检：A/B/C/D 四种刷法各 6 秒")
            selftest()
        if btn_pressed():                # 静默期也响应按键（本地翻页，不取数）
            panel.next()
        panel.tick()                     # 时段边界：跨过静默/工作时段时自动换页
        fail = False
        if active:
            p = panel.cur()
            if p.due():
                fail = not p.refresh()
                panel.dirty = True
        if panel.dirty:
            panel.render()                   # 内含绿闪（刷屏完成）
        if fail or _pending_fail:
            led_fail()                       # 橙闪：数据没取到，屏上是旧数
            _pending_fail = False
        led_tick()                           # 驱动离线红呼吸
        gc.collect()
        # 离线时要保持呼吸流畅，静默期也别把循环拖到 1s
        time.sleep_ms(200 if (active or _LED_STATE == "offline") else 1000)


# 只有直接跑这个文件才自动启动。被 main.py import 时由 main.py 显式调 main()，
# 免得「import 即死循环」——之前 main.py 是整份复制，改 epaper_main.py 根本不生效。
if __name__ == "__main__":
    main()
