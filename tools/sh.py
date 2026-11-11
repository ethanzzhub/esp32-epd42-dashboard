#!/usr/bin/env python3
"""在板端 raw REPL 里执行一段代码，打印 stdout / stderr。

⚠ 进 raw REPL 会 Ctrl+C 打断正在跑的 main.py，所以脚本结束时默认 Ctrl+D 软重启，
   让 main.py 自己跑回去。要停在 REPL 调试就加 --keep。

用法：
  python3 tools/sh.py "import os; print(os.listdir())"
  cat snippet.py | python3 tools/sh.py        # 从 stdin 读多行
  python3 tools/sh.py --keep "..."            # 执行完停在 REPL，不重启
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import up  # noqa: E402
import serial  # noqa: E402


def main():
    args = sys.argv[1:]
    keep = "--keep" in args
    args = [a for a in args if a != "--keep"]
    timeout = 40
    for a in list(args):
        if a.startswith("--timeout="):
            timeout = int(a.split("=", 1)[1])
            args.remove(a)
    if args:
        code = " ".join(args)
    else:
        code = sys.stdin.read()
    code = code.strip()
    if not code:
        print("没有代码")
        return 1
    if not code.endswith("\n"):
        code += "\n"

    print("串口: %s" % up.PORT)
    s = serial.Serial(up.PORT, up.BAUD, timeout=0.5)
    try:
        up.enter_raw(s)
        out, err = up.exec_raw(s, code, timeout=timeout)
        if keep:
            up.exit_raw(s)             # 停在 REPL
        else:
            up.exit_raw(s)
            time.sleep(0.2)
            s.reset_input_buffer()
            s.write(b"\x04")           # Ctrl+D 软重启，main.py 自动跑回来
    finally:
        s.close()

    if out:
        print(out, end="" if out.endswith("\n") else "\n")
    if err.strip():
        print("---- 错误 ----")
        print(err)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
