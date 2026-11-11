#!/usr/bin/env python3
"""通过 WiFi 操作板子（WebREPL 官方协议，无第三方依赖，拔掉 USB 线也能用）

用法：
  python3 tools/webrepl.py "print(os.listdir())"   # 板端跑一段代码
  python3 tools/webrepl.py --put 本地文件 [远端名]  # 上传文件（WebREPL 原生协议 + SHA1 校验）

密码在板端 webrepl_cfg.py / 本机 firmware/webrepl_cfg.py。
实现对照官方 micropython/webrepl 仓库的 webrepl_cli.py：
  - 帧：0x82 二进制 / 0x81 文本，无掩码（MicroPython 服务端不要求 RFC 掩码）
  - 登录：等 "Password: " 提示帧后发 密码+b"\\r"
"""
import sys
import os
import time
import struct
import socket

HOST = os.environ.get("ESP_IP") or "172.16.48.181"
PORT = 8266
PASS = os.environ.get("ESP_PASS") or "144591"   # 改了板上 webrepl_cfg 但还没重启时，用 ESP_PASS 覆盖旧密码
CHUNK = 1024


class WS:
    """MicroPython WebREPL 的 websocket（官方 cli 同款极简实现）"""

    def __init__(self, host, port):
        # 主循环跑着时 WebREPL 只在 REPL 空闲的瞬间 accept，连接经常要等一两轮，
        # 超时给 6 秒；外层还会重试。
        self.s = socket.create_connection((host, port), timeout=6)
        # 官方握手请求（服务端不校验 Key，极简）
        self.s.send(b"GET / HTTP/1.1\r\nHost: x\r\nConnection: Upgrade\r\n"
                    b"Upgrade: websocket\r\nSec-WebSocket-Key: foo\r\n\r\n")
        buf = b""
        self.s.settimeout(5)
        while b"\r\n\r\n" not in buf:
            chunk = self.s.recv(1024)
            if not chunk:
                raise RuntimeError("握手无响应")
            buf += chunk
        if b"101" not in buf.split(b"\r\n")[0]:
            raise RuntimeError("握手失败: " + buf.split(b"\r\n")[0].decode(errors="replace"))
        self.buf = buf.split(b"\r\n\r\n", 1)[1]   # 残留字节（可能已含 Password 提示帧）
        self._login(PASS)

    # ---- 帧 ----
    def write(self, data, frame=0x82):
        n = len(data)
        hdr = struct.pack(">BB", frame, n) if n < 126 else struct.pack(">BBH", frame, 126, n)
        self.s.send(hdr)
        self.s.send(data)

    def read(self, size, text_ok=False):
        """读 payload 字节；自动跳过未知类型帧（与官方 cli 一致）"""
        if not self.buf:
            while True:
                hdr = self._exact(2)
                fl, sz = struct.unpack(">BB", hdr)
                if sz == 126:
                    (sz,) = struct.unpack(">H", self._exact(2))
                if fl == 0x82 or (text_ok and fl == 0x81):
                    break
                while sz:                      # 跳过不认识的帧
                    sz -= len(self._exact(sz))
            self.buf = self._exact(sz)         # 读入整个帧 payload
        d, self.buf = self.buf[:size], self.buf[size:]
        return d

    def _exact(self, n, timeout=10):
        self.s.settimeout(timeout)
        buf = b""
        while len(buf) < n:
            chunk = self.s.recv(n - len(buf))
            if not chunk:
                raise RuntimeError("连接中断")
            buf += chunk
        return buf

    # ---- 登录（官方 login：等 ":" 提示，发 密码+\r）----
    def _login(self, passwd):
        while True:
            c = self.read(1, text_ok=True)
            if c == b"":
                raise RuntimeError("服务端未发登录提示")
            if c == b":":
                self.read(1, text_ok=True)     # 提示末尾的空格
                break
        self.write(passwd.encode() + b"\r")
        # 等回应（connected / incorrect password）
        out = b""
        t0 = time.time()
        while time.time() - t0 < 10:
            try:
                self.s.settimeout(2)
                chunk = self.s.recv(512)
                if not chunk:
                    raise RuntimeError("密码错误，服务端断开")
                out += chunk
            except socket.timeout:
                continue
            if b"connected" in out:
                return
            if b"incorrect" in out.lower():
                raise RuntimeError("WebREPL 密码错误")
        raise RuntimeError("登录超时: %r" % out[-100:])

    # ---- REPL ----
    def _frame(self, timeout=1.5):
        """读一个完整的 WebSocket 帧，返回 payload。

        WebREPL 服务端把 REPL 输出按帧发送，帧头（0x81/0x82）和 payload 混在
        同一个 TCP 流里。直接 recv 原始字节会把帧头当成内容 —— 这是「输出全是
        乱码」的根因。必须按帧切。"""
        hdr = self._exact(2, timeout)
        fl, sz = struct.unpack(">BB", hdr)
        if sz == 126:
            (sz,) = struct.unpack(">H", self._exact(2, timeout))
        elif sz == 127:
            (sz,) = struct.unpack(">Q", self._exact(8, timeout))
        if not sz:
            return b""
        return self._exact(sz, timeout)

    def _read_until_prompt(self, timeout):
        """收帧直到出现 '>>>' 提示符（或超时），返回拼接后的 payload。

        注意：刷屏类命令要 3 秒以上才吐输出，收到第一个字节前不能因为单次超时
        就放弃，否则「命令跑了但结果没读到」。"""
        out = b""
        idle = 0
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                p = self._frame(1.5)
            except socket.timeout:
                if out:
                    break             # 已经收到内容，再超时视为输出结束
                idle += 1
                if idle >= 5:         # 一直没有输出（纯赋值语句），7.5s 后放弃
                    break
                continue
            except Exception:
                break
            if not p:
                continue
            idle = 0
            out += p
            if out.endswith(b">>> ") or out.endswith(b">>>"):
                break
        return out

    def _drain(self):
        """读掉接收缓冲里已有的帧（启动横幅、上一条命令的提示符等）。

        不清的话，第一次 exec 会把开机横幅当成命令输出返回，真正结果反而
        被后面的超时截掉。"""
        try:
            while True:
                self._frame(0.2)
        except Exception:
            pass

    def wake(self):
        """打断主循环并顺手停掉看门狗。

        主循环是 `while True`，不打断的话 WebREPL 注入的代码只会排在输入缓冲里
        永远不执行（表现为「连上了但没输出」）；而打断之后主循环的软件看门狗
        （50s 无心跳就复位）必须同时停掉，否则两次查询的间隔里板子就被重启了
        —— 表现为「WiFi 时通时不通」。"""
        for _ in range(3):
            self.write(b"\x03", 0x81)
            time.sleep(0.15)
        self._read_until_prompt(3)
        self._drain()
        self.write(b"_t=__import__('app')._wd_timer; _t and _t.deinit()\r\n", 0x81)
        self._read_until_prompt(4)
        self._drain()

    def exec_line(self, line, timeout=30):
        """发一行代码（TEXT 帧），收输出直到 '>>>' 提示符。"""
        self.wake()                       # 先抢回提示符，否则主循环占着 REPL
        time.sleep(0.5)                   # Ctrl-C 之后 REPL 要重新就位，立刻发会被吞
        out = b""
        for _ in range(2):                # 第一发偶尔被 Ctrl-C 的余波吞掉，补一发
            self.write(line.encode() + b"\r\n", 0x81)
            out = self._read_until_prompt(timeout)
            if out.replace(b">>>", b"").strip():
                break
            time.sleep(0.6)
        if os.environ.get("ESP_DEBUG"):
            print("RAW:", repr(out))
        txt = out.decode(errors="replace")
        txt = txt.replace("\r", "")
        # payload 里还夹着 REPL 自己的流控字节（\x01 包首、\x02 包尾、\x04 EOT），
        # 只留可打印字符和换行。
        txt = "".join(ch for ch in txt if ch in "\n\t" or " " <= ch)
        txt = txt.replace(">>>", "")
        # 命令回显和输出粘在一起，按行去掉与输入完全一致的那一行
        want = line.strip()
        lines = [ln for ln in txt.split("\n") if ln.strip() and ln.strip() != want]
        return "\n".join(lines) + "\n"

    # ---- 文件传输（WebREPL 原生协议，官方 put_file 同款）----
    def put_file(self, local, remote):
        data = open(local, "rb").read()
        sz = len(data)
        fname = remote.encode()
        rec = struct.pack("<2sBBQLH64s", b"WA", 1, 0, 0, sz, len(fname), fname)
        self.write(rec[:10])
        self.write(rec[10:])
        if self._resp() != 0:
            raise RuntimeError("板端拒绝接收（存储空间不足？）")
        t0 = time.time()
        for i in range(0, sz, CHUNK):
            self.write(data[i:i + CHUNK])
        if self._resp() != 0:
            raise RuntimeError("板端报告写入失败")
        print("已上传 %s -> %s  %d 字节 (%.1fs)" %
              (os.path.basename(local), remote, sz, time.time() - t0))
        return self.verify(local, remote)

    def _resp(self):
        d = self.read(4)
        sig, code = struct.unpack("<2sH", d)
        if sig != b"WB":
            raise RuntimeError("协议错位: %r" % d)
        return code

    def get_file(self, remote):
        """WebREPL GET_FILE 协议读回文件（官方 get_file 同款）"""
        fname = remote.encode()
        rec = struct.pack("<2sBBQLH64s", b"WA", 2, 0, 0, 0, len(fname), fname)
        self.write(rec)
        if self._resp() != 0:
            raise RuntimeError("板端拒绝读取（文件不存在？）")
        data = b""
        while True:
            self.write(b"\0")
            (sz,) = struct.unpack("<H", self.read(2))
            if sz == 0:
                break
            data += self.read(sz)
        if self._resp() != 0:
            raise RuntimeError("读取中断")
        return data

    def verify(self, local, remote):
        """回读板端文件，SHA1 比对（协议层完成，不依赖 REPL）"""
        import hashlib
        got = self.get_file(remote)
        want = hashlib.sha1(open(local, "rb").read()).hexdigest()
        ok = hashlib.sha1(got).hexdigest() == want and len(got) == os.stat(local).st_size
        print("  SHA1 回读校验:", "一致" if ok else "不一致! (板端 %d 字节)" % len(got))
        return ok


def _retry(fn, tries=4, wait=3):
    """连不上就重试：主循环占着 REPL 时，accept 只在个别瞬间放行。"""
    last = None
    for i in range(tries):
        try:
            return fn()
        except Exception as e:
            last = e
            if i < tries - 1:
                print("  连接未就绪（%s），%ds 后重试…" % (type(e).__name__, wait))
                time.sleep(wait)
    raise last


def main():
    flags = ("--put", "--reset")
    args = [a for a in sys.argv[1:] if a not in flags]
    put = "--put" in sys.argv[1:]
    reset = "--reset" in sys.argv[1:]
    if not args:
        print(__doc__)
        return 1
    print("WebREPL: %s:%d" % (HOST, PORT))

    def work():
        ws = WS(HOST, PORT)
        try:
            if put:
                local = args[0]
                remote = args[1] if len(args) > 1 else os.path.basename(local)
                return ws.put_file(local, remote)
            print(ws.exec_line(args[0]))
            if reset:
                print("软重启，main.py 即将恢复运行")
                try:
                    ws.write(b"\x04", 0x81)   # Ctrl-D；连接随即失效，忽略后续错误
                    time.sleep(0.3)
                except Exception:
                    pass
            else:
                print("（主循环已暂停在 REPL；要恢复自动刷新，加 --reset 或用串口重启）")
            return True
        finally:
            try:
                ws.s.close()
            except Exception:
                pass

    return 0 if _retry(work) else 1


if __name__ == "__main__":
    sys.exit(main())
