#!/usr/bin/env python3
"""MicroPython 文件上传器（raw REPL 协议，无第三方依赖，只用 pyserial）

用法：
  python3 tools/up.py 本地文件 [远端文件名]   # 省略远端名则与本地同名
  python3 tools/up.py --run 本地文件          # 传完立刻执行
"""
import sys
import os
import time

BAUD = 115200


def find_port():
    import glob
    pats = ["/dev/cu.usbmodem*", "/dev/cu.usbserial*", "/dev/ttyUSB*", "/dev/ttyACM*"]
    for p in pats:
        hit = sorted(glob.glob(p))
        if hit:
            return hit[0]
    return None


PORT = os.environ.get("ESP_PORT") or find_port() or "/dev/cu.usbserial-110"
LIB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "esptool-lib")
sys.path.insert(0, LIB)

import serial  # noqa: E402  （来自 tools/esptool-lib）


def enter_raw(s):
    s.write(b"\x03\x03\x01")          # Ctrl+C 打断，Ctrl+A 进 raw REPL
    time.sleep(0.2)
    for _ in range(40):
        line = s.readline()
        if line and b"raw REPL" in line:
            break
        time.sleep(0.05)
    else:
        raise RuntimeError("进 raw REPL 失败，先手动 Ctrl+C 试试")


def exit_raw(s):
    s.write(b"\x02")                  # Ctrl+B 回到普通 REPL
    time.sleep(0.1)


def exec_raw(s, code, timeout=30):
    """在 raw REPL 执行代码，返回输出文本"""
    s.write(code.encode() + b"\x04")  # Ctrl+D 执行
    time.sleep(0.01)
    out = b""
    err = b""
    t0 = time.time()
    while True:
        if s.in_waiting:
            c = s.read(1)
            if c in (b"\r", b"\n"):   # 杂散换行直接吞掉，别混进输出
                continue
            if c == b"\x04":          # stdout 结束，紧跟的是 stderr
                err = s.read_until(b"\x04")[:-1]
                break
            if c == b"\x01":          # 开始输出
                c = s.read(1)
                if c == b"\x01":
                    err += s.read_until(b"\x04")[:-1]
                    break
            out += c
        elif time.time() - t0 > timeout:
            break
        else:
            time.sleep(0.002)         # 细粒度轮询，别用粗 sleep 拖慢每一块
    # 非阻塞吃掉残余字节：以前这里是 s.readline()，串口 timeout=0.5s，
    # 而 raw REPL 回完 \x04 后通常没有换行可读，等于每个块白等 0.5 秒
    # （515 块的 main.py 就这么拖到 5 分半）。
    if s.in_waiting:
        s.read(s.in_waiting)
    return out.decode(errors="replace"), err.decode(errors="replace")


def upload(s, local, remote, chunk=128):
    data = open(local, "rb").read()
    enter_raw(s)
    exec_raw(s, "import ubinascii\nf=open('%s','wb')\n" % remote, timeout=10)
    n = 0
    for i in range(0, len(data), chunk):
        part = data[i:i + chunk]
        # base64 传输，避免转义/控制字节带来的歧义
        b64 = ub64(part)
        exec_raw(s, "f.write(ubinascii.a2b_base64(%r))\n" % b64, timeout=10)
        n += len(part)
        # 每 16 块确认一次，保证同步
        if (i // chunk) % 16 == 15:
            out, _ = exec_raw(s, "print('K%d')\n" % n, timeout=10)
            if ("K%d" % n) not in out:
                raise RuntimeError("第 %d 字节处失去同步，重跑即可" % n)
    exec_raw(s, "f.close()\n", timeout=10)
    exit_raw(s)
    print("已上传 %-16s -> %-16s  %d 字节" % (os.path.basename(local), remote, len(data)))


def ub64(b):
    import base64
    return base64.b64encode(b).decode()


def verify(s, local, remote):
    """读回板端文件哈希，与本地比对"""
    import hashlib
    want = hashlib.sha1(open(local, "rb").read()).digest()
    enter_raw(s)
    out, err = exec_raw(s, (
        "import hashlib,ubinascii\n"
        "print(ubinascii.hexlify(hashlib.sha1(open('%s','rb').read()).digest()))\n" % remote),
        timeout=20)
    exit_raw(s)
    # 板端 print(ubinascii.hexlify(...)) 会带 b'...' 前缀，直接看本地哈希是否出现在输出里
    return want.hex() in out, (out.strip()[:20] or "空")


def main():
    args = [a for a in sys.argv[1:] if a != "--run"]
    run = "--run" in sys.argv[1:]
    if not args:
        print(__doc__)
        return 1
    local = args[0]
    if not os.path.exists(local):
        print("❌ 找不到文件:", local)
        return 1
    remote = args[1] if len(args) > 1 else os.path.basename(local)
    print("串口: %s" % PORT)
    s = serial.Serial(PORT, BAUD, timeout=0.5)
    try:
        upload(s, local, remote)
        ok, got = verify(s, local, remote)
        if not ok:
            print("  ⚠ 校验不一致(板端 %s)，重传…" % got[:12])
            upload(s, local, remote)
            ok, got = verify(s, local, remote)
            print("  校验:", "一致" if ok else "仍不一致 %s" % got[:12])
        else:
            print("  校验: 一致")
        if run:
            enter_raw(s)
            out, err = exec_raw(s, "import %s\n" % remote.replace(".py", ""), timeout=60)
            exit_raw(s)
            print("---- 运行输出 ----")
            print(out)
            if err.strip():
                print("---- 错误 ----")
                print(err)
    finally:
        s.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
