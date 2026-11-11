#!/usr/bin/env python3
"""软重启板子并不打断地听串口 N 秒，抓 main.py 启动/崩溃 traceback。

用法: python3 tools/watch_boot.py [秒数]
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "esptool-lib"))
import serial  # noqa: E402

PORT = "/dev/cu.usbmodal1101"
secs = int(sys.argv[1]) if len(sys.argv) > 1 else 75

s = None
for _ in range(20):                      # 端口可能正在重新枚举，重试打开
    try:
        s = serial.Serial(PORT, 115200, timeout=0.2)
        break
    except Exception:
        time.sleep(0.5)
if s is None:
    print("打不开串口", PORT)
    sys.exit(1)
s.write(b"\x03\x03")          # 确保回到 friendly REPL
time.sleep(0.3)
s.write(b"\x04")              # Ctrl+D 软重启
s.flush()
print("=== 开始监听 %d 秒 ===" % secs)
t0 = time.time()
while time.time() - t0 < secs:
    if s.in_waiting:
        try:
            print(s.read(s.in_waiting).decode(errors="replace"), end="", flush=True)
        except Exception:
            pass
    else:
        time.sleep(0.05)
print("\n=== 监听结束 ===")
s.close()
