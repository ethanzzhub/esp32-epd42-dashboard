#!/usr/bin/env python3
"""软件模拟断电：deepsleep 冷启动测试。

为什么不用 machine.reset()：
  reset() 是软复位，WiFi 驱动不重建、MAC/txpower 残留，
  测不出「断电后驱动从零初始化」这条真实路径。

deepsleep(ms) 会把数字域整体掉电（WiFi/BT/外设），
只保留 RTC 域，唤醒等同芯片级复位；过程中 USB 也会断开重枚举，
录到的日志与真拔线基本一致。唯一的差别是 LDO 一直有电。

用法：
  python3 tools/power_cycle.py [睡眠毫秒数，默认 8000] [总超时秒，默认 120]
"""

import glob
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "esptool-lib"))
import serial

BAUD = 115200
LOG = "/tmp/deep_boot.bin"


def find_port():
    for pat in ("/dev/cu.usbmodem*", "/dev/cu.usbserial-*"):
        hits = glob.glob(pat)
        if hits:
            return hits[0]
    return None


def open_port(deadline):
    while time.time() < deadline:
        p = find_port()
        if p:
            try:
                return serial.Serial(p, BAUD, timeout=0.2)
            except Exception:
                pass
        time.sleep(0.08)
    return None


def main():
    sleep_ms = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    limit = float(sys.argv[2]) if len(sys.argv) > 2 else 120
    t0 = time.time()
    deadline = t0 + limit

    s = open_port(deadline)
    if not s:
        print("串口没出现，放弃")
        return 1
    print("串口已连: %s" % s.port)

    # 抢 REPL：Ctrl-C 打断 main.py，再停看门狗（不停会在 50s 后 RTC_SW_CPU_RST）
    s.reset_input_buffer()
    s.write(b"\x03")
    time.sleep(0.6)
    s.write(b"import machine; machine.Timer(0).deinit()\r\n")
    time.sleep(0.4)
    s.reset_input_buffer()

    out = open(LOG, "wb")
    out.write(("--- deepsleep %dms 前 ---\n" % sleep_ms).encode())

    # 触发深睡。这条命令不返回，串口会立刻断开
    s.write(("import machine; print('SLEEPING'); machine.deepsleep(%d)\r\n" % sleep_ms).encode())
    print("已发出 deepsleep(%d)，等它自己醒..." % sleep_ms)
    time.sleep(0.5)
    try:
        s.close()
    except Exception:
        pass

    # 跨重枚举录日志
    s = None
    wake_at = None
    while time.time() < deadline:
        if s is None:
            s = open_port(deadline)
            if s and wake_at is None:
                wake_at = time.time() - t0
                print("t+%.1fs 串口重新出现，开始录制" % wake_at)
                out.write(b"\n--- WAKE: port back ---\n")
                out.flush()
            continue
        try:
            d = s.read(2048)
            if d:
                out.write(d)
                out.flush()
                if b"WebREPL ws://" in d:
                    print(">>> WebREPL 已启动，t+%.1fs" % (time.time() - t0))
                    out.close()
                    try:
                        s.close()
                    except Exception:
                        pass
                    return 0
        except Exception:
            try:
                s.close()
            except Exception:
                pass
            s = None
            time.sleep(0.1)

    out.close()
    print("日志 -> %s" % LOG)
    return 0


if __name__ == "__main__":
    sys.exit(main())
