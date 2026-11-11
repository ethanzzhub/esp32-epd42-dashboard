#!/usr/bin/env python3
"""天气页 现状 vs 提案 v5 对比预览：用 8x8 点阵像素级渲染（与板上一致），
输出一张左右拼合的 PNG，规避浏览器预览打不开的问题。

现状：直接抽 firmware 里的 WEATHER_LAYOUT 区块渲染（板上真代码）。
提案 v5：draw_v5()，与 天气页方案v5_预览.html 同坐标。

用法：python3 tools/preview_v5.py
输出：_shots/weather_v5_compare.png
"""
import re, os, sys, zlib, struct

HERE = os.path.dirname(os.path.abspath(__file__))
FW = os.path.join(HERE, "..", "firmware", "epaper_main.py")
FONT = os.path.join(HERE, "font8x8.h")
OUT = os.path.join(HERE, "..", "_shots", "weather_v5_compare.png")

SCR_W, SCR_H = 400, 300
WHITE, BLACK = 0, 1


def load_font(path=FONT):
    txt = open(path, encoding="utf-8", errors="ignore").read()
    body = txt[txt.index("{"):]
    vals = [int(v, 16) for v in re.findall(r"0x([0-9a-fA-F]{2})", body)]
    return vals[:96 * 8]


GLYPHS = load_font()


def _gbase(ch):
    return (ord(ch) - 32) * 8 if 32 <= ord(ch) < 128 else 0


class FB:
    def __init__(self, w=SCR_W, h=SCR_H):
        self.w, self.h = w, h
        self.buf = bytearray(w * h)

    def fill(self, c):
        self.buf[:] = bytes([1 if c else 0]) * (self.w * self.h)

    def pixel(self, x, y, c=1):
        if 0 <= x < self.w and 0 <= y < self.h:
            self.buf[y * self.w + x] = 1 if c else 0

    def fill_rect(self, x, y, w, h, c=1):
        for yy in range(y, y + h):
            for xx in range(x, x + w):
                self.pixel(xx, yy, c)

    def rect(self, x, y, w, h, c=1):
        for xx in range(x, x + w):
            self.pixel(xx, y, c); self.pixel(xx, y + h - 1, c)
        for yy in range(y, y + h):
            self.pixel(x, yy, c); self.pixel(x + w - 1, yy, c)

    def hline(self, x, y, w, c=1):
        for xx in range(x, x + w):
            self.pixel(xx, y, c)


class Stub:
    def __init__(self, cfg):
        self.fb = FB(); self.cfg = cfg; self.now = "18:32"

    def text(self, s, x, y, sc=2, c=BLACK):
        for ch in s:
            base = _gbase(ch)
            for px in range(8):
                col = GLYPHS[base + px]
                if not col:
                    continue
                for py in range(8):
                    if col & (1 << py):
                        self.fb.fill_rect(x + px * sc, y + py * sc, sc, sc, c)
            x += 8 * sc
        return x

    def text_w(self, s, sc=2):
        return len(s) * 8 * sc

    def center(self, s, y, sc, x0=0, w=SCR_W, c=BLACK):
        self.text(s, x0 + (w - self.text_w(s, sc)) // 2, y, sc, c)

    def hhmm(self, ts=None):
        return self.now

    def hline(self, y, x0=0, x1=SCR_W):
        self.fb.hline(x0, y, x1 - x0)

    def bar(self, clock):
        self.text(self.now, 3, 4, 2)
        self.hline(24)


def draw_v5(d, ts=0, poll=None):
    fb, text, center, text_w, hline, fill_rect, rect = (
        S.fb, S.text, S.center, S.text_w, S.hline, S.fb.fill_rect, S.fb.rect)
    cfg = S.cfg
    fb.fill(WHITE)
    S.bar(poll or ts, ts, 10800)
    if cfg["city_label"]:
        center(cfg["city_label"], 4, 2, w=376)
    if not d:
        center("NO DATA", 130, 3); return
    # 今日：温度行 + 明细行，类型贴大字右侧垂直居中
    t = str(d.get("temp", "--")).split(".")[0]
    text(t, 12, 40, 6)
    text("C", 112, 64, 3)
    text(d.get("type", "N/A"), 140, 52, 3)
    text("%s  %s" % (d.get("high", "--") + "/" + d.get("low", "--"),
                     d.get("fx", "-") + " " + d.get("fl", "-")), 12, 96, 3)
    hline(120)                           # 今日 / 明天 分隔（唯一中段线）
    # 明天
    t1 = d.get("t1") or {}
    text("TMR " + t1.get("type", "N/A") + " " + t1.get("high", "--") + "/" + t1.get("low", "--"), 12, 146, 3)
    text("%s  %s" % (t1.get("fx", "-") + " " + t1.get("fl", "-"), t1.get("wk", "--")), 12, 178, 2)
    # 环境
    text("RH %s" % d.get("rh", "--"), 12, 216, 2)
    text("AQI %s %s" % (d.get("aqi", "--"), d.get("quality", "--")), 128, 216, 2)
    text("SUN %s-%s" % (d.get("sunrise", "--"), d.get("sunset", "--")), 12, 246, 2)


def png_gray1(buf, w, h, scale=2):
    W, H = w * scale, h * scale
    raw = bytearray()
    for y in range(H):
        raw.append(0)
        sy = y // scale
        for x in range(W):
            raw.append(0 if buf[sy * w + (x // scale)] else 255)
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data +
                struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    return (b"\x89PNG\r\n\x1a\n" +
            chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 0, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(bytes(raw), 9)) +
            chunk(b"IEND", b""))


def draw_old(d, ts=0, poll=None):
    """旧版固件布局（带降水柱状图 + 明天风标重叠 bug），用于前后对比"""
    fb, text, center, text_w, hline, fill_rect, rect = (
        S.fb, S.text, S.center, S.text_w, S.hline, S.fb.fill_rect, S.fb.rect)
    cfg = S.cfg
    fb.fill(WHITE)
    S.bar(poll or ts, ts, 10800)
    if cfg["city_label"]:
        center(cfg["city_label"], 4, 2, w=376)
    if not d:
        center("NO DATA", 130, 3); return
    t = str(d.get("temp", "--")).split(".")[0]
    text(t, 10, 30, 6)
    text("C", 10 + text_w(t, 6) + 4, 46, 3)
    text(d.get("type", "N/A"), 206, 30, 3)
    text("%s/%s" % (d.get("high", "--"), d.get("low", "--")), 206, 60, 3)
    text("WIND %s %s" % (d.get("fx", "-"), d.get("fl", "-")), 206, 88, 2)
    hline(114)
    rain = (d.get("rain") or [])[:8]
    first = next((s for s in rain if s[2] > 0), None)
    summ = ("RAIN " + first[0]) if first else "NO RAIN"
    text("RAIN 2H", 8, 120, 2)
    text(summ, SCR_W - 8 - text_w(summ, 2), 120, 2)
    base = 178
    for i, s in enumerate(rain):
        x = 10 + i * 48; h = s[1] * 44 // 100
        if h > 0:
            (fill_rect if s[2] > 0 else rect)(x, base - h, 36, h, BLACK)
        if s[0][3:] == "00":
            text(s[0][:2], x, 183, 1)
    hline(193)
    t1 = d.get("t1") or {}
    text("TMR", 8, 200, 2)
    text(t1.get("type", "N/A"), 64, 197, 3)
    text("%s/%s" % (t1.get("high", "--"), t1.get("low", "--")), 216, 197, 3)
    text("%s %s" % (t1.get("fx", "-"), t1.get("fl", "-")), 322, 201, 2)
    hline(224)
    text("RH %s" % d.get("rh", "--"), 8, 232, 2)
    text("AQI %s %s" % (d.get("aqi", "--"), d.get("quality", "--")), 128, 232, 2)
    text("SUN %s-%s" % (d.get("sunrise", "--"), d.get("sunset", "--")), 8, 258, 2)


def render_old(d):
    global S
    S = Stub({"city_label": "HAIDIAN", "quote_sec": 60, "weather_sec": 1800})
    draw_old(d, 1)
    return S.fb.buf


def render_v5(d):
    global S
    S = Stub({"city_label": "HAIDIAN", "quote_sec": 60, "weather_sec": 1800})
    src = open(FW, encoding="utf-8").read()
    a = src.index("# >>> WEATHER_LAYOUT"); b = src.index("# <<< WEATHER_LAYOUT")
    block = src[a:b][src[a:b].index("def draw_weather"):]
    g = {"text": S.text, "center": S.center, "text_w": S.text_w, "hline": S.hline,
         "fill_rect": S.fb.fill_rect, "rect": S.fb.rect, "bar": S.bar,
         "fb": S.fb, "cfg": S.cfg, "SCR_W": SCR_W, "SCR_H": SCR_H,
         "WHITE": WHITE, "BLACK": BLACK, "hhmm": S.hhmm, "time": __import__("time")}
    exec(block, g)
    g["draw_weather"](d, 1)
    return S.fb.buf


def render_v5(d):
    global S
    S = Stub({"city_label": "HAIDIAN", "quote_sec": 60, "weather_sec": 1800})
    draw_v5(d, 1)
    return S.fb.buf


def composite(left, right, lw=SCR_W, lh=SCR_H, gap=14, scale=2):
    W = (lw + gap + lw) * scale
    H = lh * scale
    raw = bytearray()
    for y in range(H):
        raw.append(0)
        sy = y // scale
        for x in range(W):
            bx = x // scale
            if bx < lw:
                v = left[sy * lw + bx]
            elif bx >= lw + gap:
                v = right[sy * lw + (bx - lw - gap)]
            else:
                v = 1   # 间隙留白（白）
            raw.append(0 if v else 255)
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data +
                struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    return (b"\x89PNG\r\n\x1a\n" +
            chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 0, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(bytes(raw), 9)) +
            chunk(b"IEND", b""))


DATA = {
    "temp": "23", "rh": "62%", "aqi": 52, "quality": "FAIR",
    "type": "CLEAR", "high": "31", "low": "22", "fx": "SW", "fl": "2",
    "sunrise": "06:12", "sunset": "17:38",
    "t1": {"type": "CLOUDY", "high": "30", "low": "21", "fx": "SW", "fl": "3", "wk": "WED"},
    "rain": [["18:00", 45, 0.5], ["18:15", 30, 0], ["18:30", 70, 1.2], ["18:45", 90, 2.0],
             ["19:00", 80, 1.5], ["19:15", 40, 0], ["19:30", 20, 0], ["19:45", 10, 0]],
}

if __name__ == "__main__":
    left = render_old(DATA)
    right = render_v5(DATA)
    png = composite(left, right)
    open(OUT, "wb").write(png)
    print("已生成", OUT, os.path.getsize(OUT), "bytes")
