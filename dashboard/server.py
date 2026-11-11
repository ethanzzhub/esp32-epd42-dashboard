#!/usr/bin/env python3
"""行情聚合服务 —— 跑在家里常驻的电脑上，给 ESP32 面板和网页共用一份数据。

为什么需要它（而不是让板子直连）：
  1. 东财是 HTTPS，MCU 上每次 TLS 握手要 0.5~1.5 秒、吃 30~40KB RAM。
     （MicroPython 默认不校验证书，所以时间没对上也能连——不是证书问题，是纯粹的慢。）
  2. 板子直连要发 2 个请求（腾讯 1 + 东财批量 1），出口 IP 越多越容易被限流。
     放这里抓只有 1 次出口，重试和缓存都由它扛。
  3. 板子改成一次纯 HTTP 取一段几百字节的 JSON，快、稳、省电。

启动：
  python3 server.py                  # 0.0.0.0:8788
  PORT=9000 REFRESH=2 python3 server.py
  TOKEN=某某串 python3 server.py      # 公网暴露时建议带上，访问要 /api/q?k=某某串

接口：
  GET /api/q      {"t": 时间戳, "q": {"SSE": [涨跌幅%, 现价], ...}}
  GET /healthz    健康检查
  GET /           看板页（同目录 kiosk.html）
"""

import json
import os
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8788"))
REFRESH = float(os.environ.get("REFRESH", "3"))     # 后台抓取间隔（秒）
TOKEN = os.environ.get("TOKEN", "").strip()          # 非空则 /api/* 需要 ?k=
HERE = os.path.dirname(os.path.abspath(__file__))

# ---- 标的清单（改顺序 = 改屏幕顺序，与固件 ITEMS 保持一致）----
TX_CODES = [
    "sh000001", "sh000680", "sz399006",      # A 股（sh000680=科创综指）
    "r_hkHSTECH",                            # 亚太（r_ 前缀=实时；无前缀是延时 15 分钟）
    "usNDX", "usDJI", "usINX",               # 美股
    "hf_XAU", "hf_CL", "hf_SI",              # 大宗
]
TX_TICKER = {
    "sh000001": "SSE", "sh000680": "STAR", "sz399006": "CHINEXT",
    "r_hkHSTECH": "HSTECH",
    "usNDX": "NDX", "usDJI": "DJI", "usINX": "SPX",
    "hf_XAU": "GOLD", "hf_CL": "OIL", "hf_SI": "SILVER",
}
EM_WANT = {"N225": "N225", "KS11": "KS11"}          # 东财代码 -> 固件 ticker
EM_BATCH = ("https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=100&po=1"
            "&np=1&fltt=2&fs=m:100&fields=f2,f3,f12")
# 日经/韩股：腾讯没有这两个代码。东财在公司网络下会被 IP 级限流（实测整段断连），
# 所以首选新浪（znb_NKY=日经225, znb_KOSPI=首尔综合），东财留作兜底。
SN_CODES = ["znb_NKY", "znb_KOSPI"]
SN_TICKER = {"znb_NKY": "N225", "znb_KOSPI": "KS11"}

STATE = {"t": 0, "q": {}}
LOCK = threading.Lock()
LOG = []


def note(msg):
    """保留最近 20 条日志，方便 /healthz 里看最近出了什么事"""
    LOG.append("%s %s" % (time.strftime("%H:%M:%S"), msg))
    del LOG[:-20]


def _get(url, timeout=8):
    req = urllib.request.Request(url, headers={
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"),
        "Referer": "https://gu.qq.com/",
        "Accept": "*/*",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_tencent():
    """腾讯：一次请求拿 10 个。指数为 ~ 分隔，hf_ 外盘商品为 , 分隔"""
    raw = _get("https://qt.gtimg.cn/q=" + ",".join(TX_CODES)).decode("gbk", "ignore")
    out = {}
    for line in raw.split(";"):
        line = line.strip()
        if not line.startswith("v_"):
            continue
        code, _, val = line.partition("=")
        ticker = TX_TICKER.get(code[2:].strip())
        if not ticker:
            continue
        s = val.strip().strip('"')
        if not s:
            continue
        try:
            if "~" in s:                       # 股票 / 指数：[32]=涨跌幅% [3]=现价
                f = s.split("~")
                if len(f) <= 32:
                    continue
                out[ticker] = [round(float(f[32]), 2), float(f[3])]
            else:                              # 外盘商品：[1]=涨跌幅% [0]=现价
                f = s.split(",")
                if len(f) < 2:
                    continue
                out[ticker] = [round(float(f[1]), 2), float(f[0])]
        except ValueError:
            continue
    return out


def fetch_sina():
    """新浪全球指数：明文 HTTP，需带 Referer，返回 GBK。
    字段：名称, 现价, 涨跌额, 涨跌幅%, 时间, 时间戳, 日期
    """
    req = urllib.request.Request(
        "http://hq.sinajs.cn/list=" + ",".join(SN_CODES),
        headers={"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0"),
                 "Referer": "https://finance.sina.com.cn"})
    raw = urllib.request.urlopen(req, timeout=8).read().decode("gbk", "ignore")
    out = {}
    for code in SN_CODES:
        key = 'hq_str_%s="' % code
        i = raw.find(key)
        if i < 0:
            continue
        f = raw[i + len(key):raw.find('"', i + len(key))].split(",")
        if len(f) < 4 or not f[1]:
            continue
        try:
            out[SN_TICKER[code]] = [round(float(f[3]), 2), float(f[1])]
        except (ValueError, KeyError):
            continue
    return out


def fetch_em(tries=3):
    """东财全球指数批量接口：一次拿 63 个，挑出日经/韩股。

    早期版本是每个标的单独打 stock/get，那个接口约一半概率返回空包，
    而且按 IP 限流（密集请求后整段被拒）。批量接口稳定且只发一次请求。
    fltt=2 已把 f2/f3 处理成最终值，不用再除 10^f59。
    注意：这里的 NDX 是纳斯达克综合（26169），不是纳指 100，所以美股仍走腾讯。
    """
    for _ in range(tries):
        try:
            j = json.loads(_get(EM_BATCH).decode("utf-8"))
            diff = (j.get("data") or {}).get("diff") or []
            out = {}
            for it in diff:
                tk = it.get("f12")
                if tk not in EM_WANT:
                    continue
                try:
                    out[EM_WANT[tk]] = [round(float(it["f3"]), 2), float(it["f2"])]
                except (TypeError, ValueError, KeyError):
                    continue
            if out:
                return out
        except Exception:
            continue
    return {}


def refresh_once():
    got = {}
    try:
        got.update(fetch_tencent())
    except Exception as e:
        note("腾讯失败: %r" % (e,))

    try:
        got.update(fetch_sina())
    except Exception as e:
        note("新浪失败: %r" % (e,))

    if not all(tk in got for tk in EM_WANT.values()):   # 新浪没拿全才试东财
        for ticker, v in fetch_em().items():
            got.setdefault(ticker, v)
    for tk in EM_WANT.values():
        if tk not in got:
            note("日经/韩股缺 %s（本轮回退到上次的值）" % tk)

    miss = [t for t in list(TX_TICKER.values()) + list(EM_WANT.values())
            if t not in got]
    with LOCK:
        # 只覆盖本次拿到的项：偶发失败时保留上一次的值，别把屏刷成一片 --
        STATE["q"].update(got)
        if len(got) >= 10:
            STATE["t"] = int(time.time())
    return miss


def loop():
    while True:
        t0 = time.time()
        try:
            miss = refresh_once()
            if miss:
                note("缺: " + ",".join(miss))
        except Exception as e:
            note("刷新异常: %r" % (e,))
        time.sleep(max(0.5, REFRESH - (time.time() - t0)))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "quote-relay/1.0"

    def _file(self, name, ctype):
        f = os.path.join(HERE, name)
        if os.path.exists(f):
            with open(f, "rb") as fh:
                return self._send(200, fh.read(), ctype)
        return self._send(404, name + " not found", "text/plain; charset=utf-8")

    def _send(self, code, body, ctype):
        if isinstance(body, str):
            body = body.encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        path, _, qs = self.path.partition("?")
        if TOKEN and path.startswith("/api/"):
            if urllib.parse.parse_qs(qs).get("k", [""])[0] != TOKEN:
                return self._send(403, '{"e":"forbidden"}', "application/json")

        if path in ("/api/q", "/api/quotes"):
            with LOCK:
                body = json.dumps({"t": STATE["t"], "q": STATE["q"]},
                                  separators=(",", ":"))
            return self._send(200, body, "application/json")

        if path == "/healthz":
            with LOCK:
                n, t = len(STATE["q"]), STATE["t"]
            body = json.dumps({
                "items": n,
                "t": t,
                "age": (int(time.time()) - t) if t else -1,
                "log": LOG[-5:],
            }, ensure_ascii=False)
            return self._send(200, body, "application/json")

        if path in ("/", "/index.html", "/kiosk.html"):
            return self._file("kiosk.html", "text/html; charset=utf-8")

        self._send(404, '{"e":"not found"}', "application/json")

    def log_message(self, *a):
        pass                                  # 静默：别每 5 秒刷一行日志


def main():
    threading.Thread(target=loop, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True
    print("行情服务已启动  http://0.0.0.0:%d/api/q" % PORT)
    print("  刷新间隔 %.1fs   令牌 %s" % (REFRESH, "已启用" if TOKEN else "未启用"))
    print("  板子配置：quote_url = http://<本机内网IP>:%d/api/q%s"
          % (PORT, ("?k=" + TOKEN) if TOKEN else ""))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
