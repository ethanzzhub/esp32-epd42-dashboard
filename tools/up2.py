#!/usr/bin/env python3
"""快速上传：raw REPL + 批量 base64（一次 exec 推 4KB），比 up.py 快 8 倍。

用法：python3 tools/up2.py 本地文件 [远端文件名]
"""
import os
import sys
import time
import base64
import hashlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "esptool-lib"))
import serial  # noqa: E402
import up as _up  # noqa: E402  复用 enter_raw / exec_raw

BATCH = 3072          # 每次 exec 推送的原始字节数
PIECE = 768           # 单条 a2b_base64 的原始字节数（base64 后 1024 字符，4 的倍数）


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    local = sys.argv[1]
    remote = sys.argv[2] if len(sys.argv) > 2 else os.path.basename(local)
    data = open(local, "rb").read()
    want = hashlib.sha1(data).hexdigest()
    print("串口: %s  本地 %d 字节 sha1=%s" % (_up.PORT, len(data), want[:12]), flush=True)

    s = serial.Serial(_up.PORT, _up.BAUD, timeout=0.5)
    try:
        _up.enter_raw(s)
        _up.exec_raw(s, "import ubinascii\nf=open('%s','wb')\n" % remote, timeout=10)
        sent = 0
        t0 = time.time()
        for off in range(0, len(data), BATCH):
            batch = data[off:off + BATCH]
            code = ""
            for i in range(0, len(batch), PIECE):
                b64 = base64.b64encode(batch[i:i + PIECE]).decode()
                code += "f.write(ubinascii.a2b_base64('%s'))\n" % b64
            _up.exec_raw(s, code, timeout=20)
            sent += len(batch)
            print("  %d/%d  %.1fs" % (sent, len(data), time.time() - t0), flush=True)
        _up.exec_raw(s, "f.close()\n", timeout=10)
        out, _ = _up.exec_raw(s, (
            "import hashlib,ubinascii,os\n"
            "d=open('%s','rb').read()\n"
            "print(os.stat('%s')[6])\n"
            "print(ubinascii.hexlify(hashlib.sha1(d).digest()).decode())\n"
            % (remote, remote)), timeout=30)
        _up.exit_raw(s)
    finally:
        s.close()

    size_ok = str(len(data)) in out
    hash_ok = want in out
    print("  板上 sha1: %s" % (out.strip().splitlines()[-1][-40:] if out.strip() else "空"))
    print("  结果: %s" % ("一致 ✓" if (size_ok and hash_ok) else "不一致 ✗ 需重传"))
    return 0 if (size_ok and hash_ok) else 3


if __name__ == "__main__":
    sys.exit(main())
