# 真机自测：连 WiFi -> 对时 -> 取行情 -> 取天气（不需要屏幕）
import time
import network

SSID = "hxq-office"
PWD = "Office@321"


def wifi():
    w = network.WLAN(network.STA_IF)
    w.active(True)
    if not w.isconnected():
        w.connect(SSID, PWD)
        t0 = time.ticks_ms()
        while not w.isconnected():
            if time.ticks_diff(time.ticks_ms(), t0) > 25000:
                return None
            time.sleep_ms(300)
    return w.ifconfig()[0]


def load():
    """加载 epaper_main.py 但不执行最后的 main()（屏还没到）"""
    src = open("epaper_main.py").read()
    src = src[:src.rfind("main()")]
    g = {"__name__": "pm"}
    exec(src, g)
    return g


def run():
    ip = wifi()
    if not ip:
        print("WiFi 连不上")
        return
    print("WiFi OK:", ip)
    g = load()

    ok = g["sync_time"]()
    tm = time.localtime(time.time())
    print("对时:", "OK" if ok else "FAIL",
          "%04d-%02d-%02d %02d:%02d:%02d" % (tm[0], tm[1], tm[2], tm[3], tm[4], tm[5]))

    t0 = time.ticks_ms()
    q = g["quotes"]()
    ms = time.ticks_diff(time.ticks_ms(), t0)
    print("行情: %d/12  (%dms)" % (len(q), ms))
    for src_, code, tk in g["ITEMS"]:
        v = q.get(code)
        print("   %-13s %-8s %s" % (code, tk,
              ("%+.2f%%  %.2f" % (v[0], v[1])) if v else "-- 缺"))

    t0 = time.ticks_ms()
    w = g["weather"]()
    ms = time.ticks_diff(time.ticks_ms(), t0)
    print("天气 (%dms):" % ms, w)
