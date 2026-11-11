#!/usr/bin/env python3
"""WebREPL 客户端：文件上传/下载 + REPL 执行。协议照 micropython/webrepl 参考实现。

要点（2026-11-03 踩坑记录）：
  · 服务端【不解 mask】——客户端帧必须不加掩码（官方参考实现就不加）。
    加了掩码长度字节会被置上 0x80，服务端按 128+n 解长度，全程死锁/错位。
  · 登录：等服务端 text 帧出现 ":" + " " 的密码提示，再发 密码+\r（binary 帧）。
  · 传文件：82 字节记录 <2sBBQLH64s = "WA"+op+0+0+size+len(fname)+fname，
    op=1 上传 / 2 下载 / 3 取版本；回包 4 字节 "WB"+code(0=OK)。

用法：
  python3 tools/wrepl.py --put 本地文件 远端文件名
  python3 tools/wrepl.py --get 远端文件名 本地文件
  python3 tools/wrepl.py --repl "print(1+1)"
"""
import base64
import os
import socket
import struct
import sys
import time

HOST = "172.16.48.206"
PORT = 8266
PASS = b"144591"

WEBREPL_PUT_FILE = 1
WEBREPL_GET_FILE = 2
WEBREPL_GET_VER = 3
FRAME_TXT = 0x81
FRAME_BIN = 0x82
REQ_S = "<2sBBQLH64s"


def send_frame(s, data, opcode=FRAME_BIN):
    """客户端帧：不加掩码（服务端不解 mask，见文件头）。"""
    n = len(data)
    if n < 126:
        hdr = struct.pack(">BB", opcode, n)
    elif n < 65536:
        hdr = struct.pack(">BBH", opcode, 126, n)
    else:
        hdr = struct.pack(">BBQ", opcode, 127, n)
    s.send(hdr)
    s.send(data)


def recv_exact(s, n):
    buf = b""
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("连接中断（还需要 %d 字节）" % (n - len(buf)))
        buf += chunk
    return buf


class WS:
    """极简 WebSocket：读帧 + 按类型分发，非目标帧跳过（照参考实现）。

    raw 里保留握手后的残余字节——握手响应和 "Password: " 提示帧
    经常在同一个 TCP 段到达，直接丢掉提示帧就会永远等不到密码提示。
    """

    def __init__(self, s, pre=b""):
        self.s = s
        self.raw = pre
        self.buf = b""

    def _recv(self, n):
        while len(self.raw) < n:
            chunk = self.s.recv(4096)
            if not chunk:
                raise ConnectionError("连接中断")
            self.raw += chunk
        d, self.raw = self.raw[:n], self.raw[n:]
        return d

    def _read_frame(self, text_ok=False):
        hdr = self._recv(2)
        fl, sz = struct.unpack(">BB", hdr)
        if sz == 126:
            (sz,) = struct.unpack(">H", self._recv(2))
        elif sz == 127:
            (sz,) = struct.unpack(">Q", self._recv(8))
        data = self._recv(sz)
        if fl == FRAME_BIN or (text_ok and fl == FRAME_TXT):
            return data
        return self._read_frame(text_ok)     # 其他类型跳过

    def read(self, size, text_ok=False):
        if not self.buf:
            self.buf = self._read_frame(text_ok)
        d, self.buf = self.buf[:size], self.buf[size:]
        return d

    def write(self, data, opcode=FRAME_BIN):
        send_frame(self.s, data, opcode)


def connect():
    s = socket.socket()
    s.settimeout(20)
    s.connect((HOST, PORT))
    key = base64.b64encode(os.urandom(16)).decode()
    req = ("GET / HTTP/1.1\r\nHost: %s:%d\r\nConnection: Upgrade\r\n"
           "Upgrade: websocket\r\nSec-WebSocket-Key: %s\r\n"
           "Sec-WebSocket-Version: 13\r\n\r\n" % (HOST, PORT, key))
    s.send(req.encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = s.recv(1024)
        if not chunk:
            raise ConnectionError("握手被断开")
        resp += chunk
    return s, resp.split(b"\r\n\r\n", 1)[1]   # 握手头之后的残余字节


def login(ws):
    """等密码提示（':' 后跟 ' '），发密码+\r。"""
    while True:
        c = ws.read(1, text_ok=True)
        if c == b":":
            assert ws.read(1, text_ok=True) == b" "
            break
    ws.write(PASS + b"\r")
    time.sleep(0.3)
    try:                                  # 登录后的回显/提示读干
        while True:
            d = ws.s.recv(4096)
            if not d:
                break
    except socket.timeout:
        pass


def read_resp(ws):
    sig, code = struct.unpack("<2sH", ws.read(4))
    assert sig == b"WB", "回包签名错误: %r" % sig
    return code


def send_req(ws, op, sz=0, fname=b""):
    rec = struct.pack(REQ_S, b"WA", op, 0, 0, sz, len(fname), fname)
    ws.write(rec[:10])
    ws.write(rec[10:])


def get_ver(ws):
    send_req(ws, WEBREPL_GET_VER)
    return struct.unpack("<BBB", ws.read(3))


def put_file(ws, local, remote):
    sz = os.stat(local)[6]
    fname = remote.encode()
    send_req(ws, WEBREPL_PUT_FILE, sz, fname)
    assert read_resp(ws) == 0, "服务端拒绝上传"
    sent = 0
    with open(local, "rb") as f:
        while True:
            buf = f.read(1024)
            if not buf:
                break
            ws.write(buf)
            sent += len(buf)
            if sent % 16384 < 1024:
                print("已发送 %d / %d 字节" % (sent, sz), flush=True)
    print("上传完成: %s -> %s (%d 字节)" % (local, remote, sz))
    assert read_resp(ws) == 0, "写入失败"
    print("服务端确认写入")


def get_file(ws, remote, local):
    send_req(ws, WEBREPL_GET_FILE, 0, remote.encode())
    assert read_resp(ws) == 0, "服务端拒绝下载"
    with open(local, "wb") as f:
        cnt = 0
        while True:
            ws.write(b"\0")
            (sz,) = struct.unpack("<H", ws.read(2))
            if sz == 0:
                break
            data = ws.read(sz)
            f.write(data)
            cnt += len(data)
            if cnt % 16384 < 1024:
                print("已接收 %d 字节" % cnt, flush=True)
    print("下载完成: %s -> %s (%d 字节)" % (remote, local, cnt))
    assert read_resp(ws) == 0
    print("服务端确认读完")


def repl(ws, code):
    """进 raw REPL 执行代码（text 帧），打印输出。

    板子正跑着 main.py 时 REPL 被占，输入全排在队列里不执行、也不回显——
    必须先 Ctrl+C 打断（等 traceback/提示符回来）再 Ctrl+A，否则像没响应。
    """
    ws.write(b"\x03", FRAME_TXT)          # Ctrl+C：打断正在运行的程序
    time.sleep(0.8)                       # 等 traceback 和 >>> 提示符刷完
    ws.write(b"\x01", FRAME_TXT)          # Ctrl+A
    time.sleep(0.3)
    ws.write(code.encode() + b"\x04", FRAME_TXT)
    out = b""
    t0 = time.time()
    while time.time() - t0 < 12:
        try:
            ws.s.settimeout(2)
            out += ws.read(4096, text_ok=True)   # raw REPL 回显是文本帧，不能只收 bin
        except socket.timeout:
            break
    print(out.decode(errors="replace"))


def main():
    args = sys.argv[1:]
    s, pre = connect()
    print("握手: 101 Switching Protocols")
    ws = WS(s, pre)
    login(ws)
    print("已登录，版本:", get_ver(ws))
    if args and args[0] == "--put":
        put_file(ws, args[1], args[2])
    elif args and args[0] == "--get":
        get_file(ws, args[1], args[2])
    elif args and args[0] == "--repl":
        repl(ws, args[1])
    else:
        print("需要 --put/--get/--repl 参数")
    s.close()


if __name__ == "__main__":
    main()
