#!/usr/bin/env python3
"""一次性：通过 webrepl 控制台软重启板子（machine.reset）。"""
import sys, time, socket
sys.path.insert(0, "tools")
import wrepl as W

s, pre = W.connect()
ws = W.WS(s, pre)
W.login(ws)
print("已登录，发送 Ctrl+C 确保在 REPL 提示符")
ws.write(b"\x03", W.FRAME_TXT)
time.sleep(1.0)
ws.write(b"\x01", W.FRAME_TXT)          # Ctrl+A 进 raw REPL
time.sleep(0.5)
ws.write(b"import machine; machine.reset()\r\n\x04", W.FRAME_TXT)
print("重启指令已发，等 5 秒让板子起来")
time.sleep(5)
s.close()
