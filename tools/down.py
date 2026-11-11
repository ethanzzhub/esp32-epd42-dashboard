#!/usr/bin/env python3
"""把板上的文件拉回本地（base64 分块，raw REPL）。

用法：
  python3 tools/down.py                 # 全部 .py/.json 拉到 backup/
  python3 tools/down.py a.py b.json     # 只拉指定文件
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import up  # noqa: E402
import serial  # noqa: E402

CHUNK = 512


def run(s, code, timeout=15):
    """执行一段代码，返回 @@ 之间的内容；板端杂散字节一律丢掉"""
    out, err = up.exec_raw(s, code, timeout=timeout)
    s.reset_input_buffer()          # 清掉尾部的 \r\n / '>' 残留，否则下一块会错位
    if err.strip():
        raise RuntimeError(err.strip()[:160])
    if "@@" not in out:
        raise RuntimeError("没有找到标记，输出=%r" % out[:80])
    return out.split("@@")[1]


def ls(s):
    out = run(s, (
        "import os\n"
        "print('@@'+','.join('%s:%d' % (f[0], f[3]) for f in os.ilistdir() if f[1] == 0x8000)+'@@')\n"))
    items = {}
    for tok in out.strip().split(","):
        if ":" in tok:
            name, size = tok.rsplit(":", 1)
            items[name] = int(size)
    return items


def fetch(s, remote, size):
    data = b""
    off = 0
    import base64
    while off < size:
        n = min(CHUNK, size - off)
        # 用 @@ 包住 base64，避开 raw REPL 前后杂散字节
        line = run(s, (
            "import ubinascii\n"
            "f=open('%s','rb')\n"
            "f.seek(%d)\n"
            "print('@@'+ubinascii.b2a_base64(f.read(%d)).decode().strip()+'@@')\n"
            "f.close()\n" % (remote, off, n)))
        data += base64.b64decode(line)
        off += n
    return data


def main():
    want = sys.argv[1:]
    dst = os.path.join(os.path.dirname(HERE), "backup")
    os.makedirs(dst, exist_ok=True)
    print("串口: %s" % up.PORT)
    s = serial.Serial(up.PORT, up.BAUD, timeout=0.5)
    try:
        up.enter_raw(s)
        files = ls(s)
        targets = want or [n for n in files if n.endswith((".py", ".json"))]
        for name in targets:
            size = files.get(name)
            if size is None:
                print("  跳过（板上没有）: %s" % name)
                continue
            blob = fetch(s, name, size)
            if len(blob) != size:
                print("  ⚠ %s 长度不符 %d/%d" % (name, len(blob), size))
            with open(os.path.join(dst, name), "wb") as f:
                f.write(blob)
            print("  ✓ %-16s %6d 字节" % (name, len(blob)))
        up.exit_raw(s)
    finally:
        s.close()
    print("备份目录: %s" % dst)
    return 0


if __name__ == "__main__":
    sys.exit(main())
