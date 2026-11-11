#!/usr/bin/env python3
"""等串口出现，抓住的第一秒起就一直录，直到超时。

用途：真正断电后重新插上，从 USB 枚举那一刻开始录启动日志。
（板子断电期间串口节点会消失，普通 open 会直接报错，
 这里每 80ms 轮询一次，端口一出现立刻接管。）

用法：python3 tools/wait_port.py <超时秒> <输出文件>
"""
import glob
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "esptool-lib"))
import serial

BAUD = 115200


def find_port():
    for pat in ("/dev/cu.usbmodem*", "/dev/cu.usbserial-*", "/dev/cu.wchusbserial*"):
        hits = glob.glob(pat)
        if hits:
            return hits[0]
    return None


def main():
    limit = float(sys.argv[1]) if len(sys.argv) > 1 else 300
    out_path = sys.argv[2] if len(sys.argv) > 2 else "/tmp/cold_boot.bin"
    out = open(out_path, "wb")
    t0 = time.time()

    print("等待串口出现（最多 %ds）..." % limit, flush=True)
    port = None
    while time.time() - t0 < limit:
        port = find_port()
        if port:
            break
        time.sleep(0.08)

    if not port:
        print("超时：串口始终没出现 —— 板子没插到电脑上，或线是纯充电线", flush=True)
        return 1

    print("串口出现: %s  t+%.2fs" % (port, time.time() - t0), flush=True)
    out.write(("--- PORT %s at t+%.2fs ---\n" % (port, time.time() - t0)).encode())
    out.flush()

    s = None
    got = 0
    while time.time() - t0 < limit:
        if s is None:
            try:
                s = serial.Serial(port, BAUD, timeout=0.2)
                s.reset_input_buffer()
                print("已接管串口", flush=True)
            except Exception as e:
                s = None
                time.sleep(0.1)
                continue
        try:
            d = s.read(2048)
            if d:
                got += len(d)
                out.write(d)
                out.flush()
                if b"WebREPL ws://" in d:
                    print(">>> 看到 WebREPL 启动", flush=True)
        except Exception:
            try:
                s.close()
            except Exception:
                pass
            s = None
            time.sleep(0.1)

    try:
        if s:
            s.close()
    except Exception:
        pass
    out.close()
    print("结束，共录到 %d 字节 -> %s" % (got, out_path), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
