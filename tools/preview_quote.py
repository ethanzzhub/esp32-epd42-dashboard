#!/usr/bin/env python3
"""行情面板 对比预览：左=07:00（开盘前：A股/港股/日经/韩股→横杠；美股期货/贵金油 24h→显示）
右=20:00（收盘后：所有市场数据保留、不显示横杠）。复用 preview_v5 的 8x8 点阵与合成器。

用法：python3 tools/preview_quote.py
输出：_shots/quote_nodata.png
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import preview_v5 as P

GRID_Y0, ROW_H = 28, 66
COL_X, CELL_W = (0, 133, 266), 133

ITEMS = [
    ("tx", "sh000001", "SSE"), ("tx", "sz399006", "CHINEXT"), ("tx", "sh000680", "STAR"),
    ("tx", "hkHSTECH", "HSTECH"), ("sn", "znb_NKY", "N225"), ("sn", "znb_KOSPI", "KS11"),
    ("shf", "hf_NQ", "NQ"), ("shf", "hf_ES", "ES"), ("shf", "hf_YM", "YM"),
    ("tx", "hf_XAU", "GOLD"), ("tx", "hf_CL", "OIL"), ("tx", "hf_SI", "SILVER"),
]
VALS = [0.42, 1.15, 0.83, -0.76, 1.92, -0.34, 0.83, 0.43, 0.39, 0.55, -1.20, 0.90]
DATA = {code: (v, 0) for (_, code, _), v in zip(ITEMS, VALS)}

MARKET_SESSION = {
    "SSE": (570, 900, False), "CHINEXT": (570, 900, False), "STAR": (570, 900, False),
    "HSTECH": (570, 960, False),
    "N225": (480, 840, False), "KS11": (480, 870, False),
    "NQ": (0, 1440, True), "ES": (0, 1440, True), "YM": (0, 1440, True),
    "GOLD": (0, 1440, True), "OIL": (0, 1440, True), "SILVER": (0, 1440, True),
}


def market_show_data(tk, t):
    """开盘前→横杠(False)；开盘起至当天收盘后→显示数据(True)；周末→横杠。"""
    a, b, wknd = MARKET_SESSION.get(tk, (0, 1440, True))
    if wknd:
        return True
    if t[6] >= 5:
        return False
    m = t[3] * 60 + t[4]
    return m >= a


def draw_panel(sim_min, wd=1):
    t = (0, 0, 0, sim_min // 60, sim_min % 60, 0, wd, 0)
    S = P.Stub({"quote_sec": 120})
    S.now = "%02d:%02d" % (sim_min // 60, sim_min % 60)
    fb = S.fb
    fb.fill(P.WHITE)
    S.bar(1)
    # 顶栏右侧：涨停/跌停 + 成交额（一个空格）；开盘前→横杠，收盘后保留
    show_amt = market_show_data("SSE", t)
    st = "58/11"
    if show_amt:
        s = "%s %s" % (st, "10030")
        S.text(s, 400 - 20 - S.text_w(s, 2), 4, 2)
    else:
        S.text(st, 340 - S.text_w(st, 2), 4, 2)
        fb.fill_rect(348, 6, 32, 4, P.BLACK)
    mode = "box"
    for i, (src, code, tk) in enumerate(ITEMS):
        cx = COL_X[i % 3]
        cy = GRID_Y0 + (i // 3) * ROW_H
        S.text(tk[0], cx + 6, cy + 6, 2)              # 首字母左置小标签
        if not market_show_data(tk, t):                # 开盘前/周末 → 横杠（收盘后保留）
            fb.fill_rect(cx + 49, cy + 38, 34, 4, P.BLACK)
            continue
        v = DATA.get(code)
        if v is None:                                  # 开市但暂取不到 → --
            S.center("--", cy + 30, 3, cx, CELL_W)
            continue
        p = float(v[0])
        down = p < 0
        tt = None
        for f in ("%.2f", "%.1f", "%.0f"):
            tt = f % abs(p)
            if len(tt) <= 5:
                break
        shown = ("(" + tt + ")") if (mode == "paren" and down) else tt
        sc = 3
        while sc > 2 and S.text_w(shown, sc) > CELL_W - 10:
            sc -= 1
        w = S.text_w(shown, sc)
        x = cx + max(0, (CELL_W - w) // 2)
        y = cy + 28
        if mode == "inv" and down:
            fb.fill_rect(x - 3, y - 3, w + 6, 8 * sc + 6, P.BLACK)
            S.text(shown, x, y, sc, P.WHITE)
        else:
            S.text(shown, x, y, sc, P.BLACK)
            if mode == "box" and down:
                fb.rect(x - 3, y - 3, w + 6, 8 * sc + 6, P.BLACK)
    for x in (133, 266):
        fb.fill_rect(x, GRID_Y0, 1, ROW_H * 4, P.BLACK)
    for r in range(5):
        fb.hline(0, GRID_Y0 + r * ROW_H, 400)
    return fb.buf


if __name__ == "__main__":
    left = draw_panel(7 * 60)         # 07:00 开盘前 → 日间市场横杠
    right = draw_panel(20 * 60)       # 20:00 收盘后 → 数据保留（不横杠）
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "_shots", "quote_nodata.png")
    open(out, "wb").write(P.composite(left, right))
    print("已生成", os.path.abspath(out))
