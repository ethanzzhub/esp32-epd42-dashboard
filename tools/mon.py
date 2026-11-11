#!/usr/bin/env python3
"""只看不动：软重启板子，然后被动抓 20 秒串口输出（含 main.py 的 traceback）。

用法：python3 tools/mon.py [秒数]
不会发 Ctrl+C，所以 main.py 能一直跑，能看到它启动全过程和崩溃栈。
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "esptool-lib"))
import serial  # noqa: E402
import up  # noqa: E402

sec = float(sys.argv[1]) if len(sys.argv) > 1 else 20.0

s = serial.Serial(up.PORT, up.BAUD, timeout=0.2)
print("串口: %s  监听 %.0fs（软重启后被动读取）" % (up.PORT, sec), flush=True)
s.reset_input_buffer()
s.write(b"\x04")          # Ctrl+D 软重启
t0 = time.time()
buf = b""
while time.time() - t0 < sec:
    d = s.read(4096)
    if d:
        buf += d
        sys.stdout.write(d.decode("utf-8", "replace"))
        sys.stdout.flush()
s.close()
print("\n---- 共 %d 字节 ----" % len(buf))
