#!/usr/bin/env python3
"""天气页版式离线预览：把 firmware/epaper_main.py 里的 WEATHER_LAYOUT 区块
原样抽出来，在电脑上用一个假 framebuf 按 400x300 单色渲染成 PNG。

不复制版式代码 —— 预览的就是板上跑的那份，避免"预览挺好上板越界"。

用法：
    python3 tools/preview_weather.py              # 无雨场景
    python3 tools/preview_weather.py rain         # 有降水
    python3 tools/preview_weather.py edge         # 极端值（负温、长天气名）
输出：/tmp/weather_preview[_场景].png（2 倍放大，便于查看）
"""
import re
import os
import sys
import zlib
import struct

HERE = os.path.dirname(os.path.abspath(__file__))
FW = os.path.join(HERE, "..", "firmware", "epaper_main.py")
FONT = os.path.join(HERE, "font8x8.h")

SCR_W, SCR_H = 400, 300
WHITE, BLACK = 0, 1


# ---------------- 8x8 字模（和 MicroPython framebuf 内置的完全同一套） ----------------
def load_font(path=FONT):
    txt = open(path, encoding="utf-8", errors="ignore").read()
    body = txt[txt.index("{"):]
    vals = [int(v, 16) for v in re.findall(r"0x([0-9a-fA-F]{2})", body)]
    # 这套字模只含可打印 ASCII 32..127，共 96 字形 x 8 字节
    assert len(vals) >= 96 * 8, "字体数据不完整：%d 字节" % len(vals)
    return vals[:96 * 8]


def _gbase(ch):
    return (ord(ch) - 32) * 8 if 32 <= ord(ch) < 128 else 0


GLYPHS = load_font()


class FB:
    """够用就好的 framebuf 替身：只实现 firmware 里用到的那几个方法"""

    def __init__(self, w=SCR_W, h=SCR_H):
        self.w, self.h = w, h
        self.buf = bytearray(w * h)          # 1 = 黑

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
            self.pixel(xx, y, c)
            self.pixel(xx, y + h - 1, c)
        for yy in range(y, y + h):
            self.pixel(x, yy, c)
            self.pixel(x + w - 1, yy, c)

    def hline(self, x, y, w, c=1):
        for xx in range(x, x + w):
            self.pixel(xx, y, c)

    def vline(self, x, y, h, c=1):
        for yy in range(y, y + h):
            self.pixel(x, yy, c)

    def text(self, s, x, y, c=1):
        """8x8 字模，逐字符右移 8px（framebuf 原语义）"""
        for ch in s:
            base = _gbase(ch)
            for px in range(8):
                col = GLYPHS[base + px]
                for py in range(8):
                    if col & (1 << py):
                        self.pixel(x + px, y + py, c)
            x += 8


# ---------------- 从固件里抽出真实版式代码 ----------------
def load_layout():
    src = open(FW, encoding="utf-8").read()
    a = src.index("# >>> WEATHER_LAYOUT")
    b = src.index("# <<< WEATHER_LAYOUT")
    block = src[a:b]
    return block[block.index("def draw_weather"):]


class Stub:
    """给版式代码提供运行环境（和固件同名同语义）"""

    def __init__(self, cfg):
        self.fb = FB()
        self.cfg = cfg

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

    def hline(self, y, x0=0, x1=SCR_W):
        self.fb.hline(x0, y, x1 - x0, BLACK)

    def hhmm(self, ts=None):
        return self.now

    def bar(self, ts, ttl=0):
        """顶栏（固件里这段没变，这里照抄一份用于预览）"""
        self.text(self.now, 3, 4, 2)
        self.fb.fill_rect(SCR_W - 14, 6, 10, 10, BLACK)
        self.hline(24)


def png_gray1(buf, w, h, scale=2):
    """单色 bit 图 -> 灰度 PNG（不依赖 PIL）"""
    W, H = w * scale, h * scale
    raw = bytearray()
    for y in range(H):
        raw.append(0)                                  # filter: none
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


# ---------------- 场景数据（取自 2026-10-30 海淀真实响应） ----------------
NOW = "13:31"
BASE = {
    "temp": "19", "rh": "74%", "aqi": 52, "quality": "GOOD",
    "type": "CLEAR", "high": "20", "low": "5", "fx": "NW", "fl": "1",
    "sunrise": "06:41", "sunset": "17:15",
    "t1": {"type": "CLOUDY", "high": "13", "low": "5", "fx": "SE", "fl": "1", "wk": "SAT"},
    "rain": [["13:30", 0, 0.0], ["13:45", 3, 0.0], ["14:00", 8, 0.0], ["14:15", 12, 0.0],
             ["14:30", 9, 0.0], ["14:45", 5, 0.0], ["15:00", 2, 0.0], ["15:15", 0, 0.0]],
}

SCENES = {
    "dry": (BASE, "no_rain"),
    "rain": (dict(BASE, rain=[
        ["13:30", 30, 0.0], ["13:45", 65, 0.4], ["14:00", 88, 1.2], ["14:15", 95, 2.0],
        ["14:30", 72, 0.6], ["14:45", 40, 0.0], ["15:00", 15, 0.0], ["15:15", 5, 0.0]
    ]), "rain"),
    "edge": (dict(BASE, temp="-7", type="T-STORM", high="-2", low="-11", rh="100%",
                  quality="SEVERE", fx="-", fl="-"), "edge"),
}


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else "dry"
    data, tag = SCENES.get(name, SCENES["dry"])
    st = Stub({"city_label": "HAIDIAN", "quote_sec": 60, "weather_sec": 1800})
    st.now = NOW

    src = load_layout()
    g = {
        "text": st.text, "center": st.center, "text_w": st.text_w, "hline": st.hline,
        "bar": st.bar, "fb": st.fb, "cfg": st.cfg,
        "SCR_W": SCR_W, "SCR_H": SCR_H, "WHITE": WHITE, "BLACK": BLACK,
        "time": __import__("time"),
    }
    exec(src, g)
    g["draw_weather"](data, 1)

    out = "/tmp/weather_preview%s.png" % ("" if tag == "no_rain" else "_" + tag)
    open(out, "wb").write(png_gray1(st.fb.buf, SCR_W, SCR_H))
    print("已生成", out)

    # 粗查越界：统计最右/最下非白像素，接近边界就提醒
    fbm = st.fb
    maxx = maxy = -1
    for y in range(SCR_H):
        row = fbm.buf[y * SCR_W:(y + 1) * SCR_W]
        if 1 in row:
            maxy = y
            rx = SCR_W - 1 - row[::-1].index(1)
            if rx > maxx:
                maxx = rx
    print("内容边界: 右 %d/%d, 下 %d/%d" % (maxx, SCR_W - 1, maxy, SCR_H - 1))
    if maxx >= SCR_W - 1 or maxy >= SCR_H - 2:
        print("!! 贴边/可能越界")


if __name__ == "__main__":
    main()
