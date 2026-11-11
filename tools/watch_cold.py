#!/usr/bin/env python3
"""冷启动黑匣子：被动监听串口，不主动发任何东西。

拔 USB -> 插回 的整个过程中，串口设备会消失再重新枚举，普通监听会直接
报错退出。这个脚本会一直重连，并且把每次断开/重连都打上时间戳，方便
事后区分「旧进程的输出」和「冷启动的输出」。

用法:
  python3 tools/watch_cold.py [秒数] [输出文件]
默认 240 秒，写 /tmp/cold_boot.bin

看完用 tools/../ 随便什么看，例如:
  cat -v /tmp/cold_boot.bin | tail -60
"""
import glob
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "esptool-lib"))
import serial  # noqa: E402

PATS = ("/dev/cu.usbmodem*", "/dev/cu.usbserial*", "/dev/ttyUSB*", "/dev/ttyACM*")


def find_port():
    for p in PATS:
        hit = sorted(glob.glob(p))
        if hit:
            return hit[0]
    return None


def main():
    secs = int(sys.argv[1]) if len(sys.argv) > 1 else 240
    out_path = sys.argv[2] if len(sys.argv) > 2 else "/tmp/cold_boot.bin"
    t_end = time.time() + secs
    out = open(out_path, "wb")
    s = None
    total = 0
    print("监听 %d 秒 -> %s" % (secs, out_path), flush=True)
    while time.time() < t_end:
        if s is None:
            p = find_port()
            if not p:
                time.sleep(0.15)
                continue
            try:
                s = serial.Serial(p, 115200, timeout=0.2)
            except Exception:
                s = None
                time.sleep(0.15)
                continue
            stamp = "\n\n##### 串口就绪 %s %s #####\n" % (time.strftime("%H:%M:%S"), p)
            print(stamp.strip(), flush=True)
            out.write(stamp.encode())
        try:
            d = s.read(1024)
            if d:
                out.write(d)
                out.flush()
                total += len(d)
        except Exception:
            stamp = "\n##### 串口断开 %s #####\n" % time.strftime("%H:%M:%S")
            print(stamp.strip(), flush=True)
            out.write(stamp.encode())
            out.flush()
            try:
                s.close()
            except Exception:
                pass
            s = None
    try:
        if s:
            s.close()
    except Exception:
        pass
    out.close()
    print("结束 共 %d 字节 -> %s" % (total, out_path), flush=True)


main()
