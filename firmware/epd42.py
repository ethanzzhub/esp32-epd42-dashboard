"""SSD1619A 4.2" 墨水屏驱动（400x300，黑白）—— 2026-11-03 真机结论版

====== 这块屏的真机行为（反复实测，与网上多数教程不同，别照抄教程）======

1. **只有 0x22=0xF7 能真正驱动像素**。0xC7 / 0xCF 在这块屏上是"空转"：
   波形会跑（BUSY 800ms 左右），但屏幕一个像素都不动。

2. **单独发过 0x04（Power ON）之后，后续 0xF7 就退化成"有波形无高压"**
   （≈840ms，屏不动）。原因未查清（推测 0x04 与 0xF7 内部的高压序列冲突），
   但可稳定复现 6 次以上。**本文件任何地方都不发 0x04/0x01/0x06**，
   高压由 0xF7 自己的 ENABLE_ANALOG→DISABLE_ANALOG 序列负责。
   旧版 init_part() 里那句 0x04 就是"局刷一直不生效"的元凶。

3. **0x44/0x45 窗口在能驱动的模式下被忽略**：一律整屏 300 行扫描。
   所以不存在"只刷变化的那几十行"，全刷 5.2s 不会因为窗口变窄而变快。
   窄窗口只影响 RAM 写入，不影响驱动范围。

4. **差分刷新成立（2026-11-03 实测，推翻旧结论）**：把旧帧发 0x26、新帧发
   0x24，配 **70 字节**自定义 LUT（规格书格式，不是 SSD1683 的 233 字节）
   + 0x22=0xCF + 0x21=0x00（让红平面透传当"旧帧"用），只有真正变化的像素
   被驱动，不整屏翻转。TP=40 约 1.8s，TP=10 约 0.6s。见 display_diff()。
   旧结论"0x26 只是红平面、差分不成立"的错因：init() 一直发 0x21=0x40
   （红平面旁路成 0），旧帧从未启用过。

5. **自定义 233 字节波形表能被 0xC7 执行**（耗时随帧数线性变化，说明真的进
   了波形引擎），但它是**整屏统一驱动、与 RAM 数据无关**：8 帧没反应，
   129 帧能把整屏连同黑带一起漂白。也就是说只靠改 LUT 画不出图案。
   相关函数（make_lut / _lut_drive / write_lut）保留在文件里供后续实验，
   但**不要**用在正式刷新路径上。

6. **0x1A=0x1E00（30°C 档）可把全刷从 5.2s 压到 2.9s**，出图正常。
   这是目前唯一可靠的提速手段，见 init(short=True)。

7. **0x22=0xFF 与 0xF7 完全等价**（2026-11-03 实测：3166ms vs 3169ms，
   BUSY 都是 0ms 拉高、3.17s 回落）——但那是"全刷"内部的事，不影响结论 4
   的差分路径。差分的开关是 0x22=0xCF（bit4=1 用寄存器 LUT + bit3=1 黑白
   差分模式），前提是 LUT 按规格书写 **70 字节**；写成 233 字节时多出的
   字节被当成后续命令吞掉，波形静默空转 195ms ——这是 2026-11-03 之前
   所有差分实验失败的根因。

====== 结论 ======
两条可靠路径：
  · 全刷：display()，0x24 新帧 + 0x26 清零 + 0xF7，2.9s（短波形）/
    5.2s（长波形），整屏闪一下。换页/冷启动/定期清理用。
  · 差分：display_diff(buf, prev)，0x24 新帧 + 0x26 旧帧 + 70 字节 LUT
    + 0xCF，只驱动变化像素，不闪，0.6~1.8s（TP 可调）。行情页数字跳动走这条。
上层策略：内容没变不刷；变了优先差分；每攒够 N 次差分（或换页）全刷一次防残影。

引脚（驱动板 8pin 排针）：3V3 / GND / SCK / SDA / RST / DC / CS1 / BUSY（CS2 空着）
BUSY：高电平=忙，别反接。

用法：
    from machine import Pin, SPI
    spi = SPI(1, baudrate=20_000_000, sck=Pin(SCK), mosi=Pin(SDA))
    epd = EPD(spi, cs=Pin(CS), dc=Pin(DC), rst=Pin(RST), busy=Pin(BUSY))
    epd.init()                 # 含软复位与短波形设置
    epd.display(buf)           # 全刷：buf = bytearray(15000)，1=白 0=黑（MONO_HLSB）
    epd.display_diff(buf)      # 差分：自动用 last_frame 当旧帧，只刷变化像素
    epd.sleep()
"""
import time
from machine import Pin

EPD_WIDTH = 400
EPD_HEIGHT = 300
LINE_BYTES = EPD_WIDTH // 8          # 50
FRAME_BYTES = LINE_BYTES * EPD_HEIGHT  # 15000

# 官方 4 灰阶 LUT（233 字节），仅 Init_4Gray/Lut() 用，正常刷屏不碰它
LUT_ALL = bytes((
    0x01, 0x0A, 0x1B, 0x0F, 0x03, 0x01, 0x01, 0x05, 0x0A, 0x01, 0x0A, 0x01,
    0x01, 0x01, 0x05, 0x08, 0x03, 0x02, 0x04, 0x01, 0x01, 0x01, 0x04, 0x04,
    0x02, 0x00, 0x01, 0x01, 0x01, 0x00, 0x00, 0x00, 0x00, 0x01, 0x01, 0x01,
    0x00, 0x00, 0x00, 0x00, 0x01, 0x01, 0x01, 0x0A, 0x1B, 0x0F, 0x03, 0x01,
    0x01, 0x05, 0x4A, 0x01, 0x8A, 0x01, 0x01, 0x01, 0x05, 0x48, 0x03, 0x82,
    0x84, 0x01, 0x01, 0x01, 0x84, 0x84, 0x82, 0x00, 0x01, 0x01, 0x01, 0x00,
    0x00, 0x00, 0x00, 0x01, 0x01, 0x01, 0x00, 0x00, 0x00, 0x00, 0x01, 0x01,
    0x01, 0x0A, 0x1B, 0x8F, 0x03, 0x01, 0x01, 0x05, 0x4A, 0x01, 0x8A, 0x01,
    0x01, 0x01, 0x05, 0x48, 0x83, 0x82, 0x04, 0x01, 0x01, 0x01, 0x04, 0x04,
    0x02, 0x00, 0x01, 0x01, 0x01, 0x00, 0x00, 0x00, 0x00, 0x01, 0x01, 0x01,
    0x00, 0x00, 0x00, 0x00, 0x01, 0x01, 0x01, 0x8A, 0x1B, 0x8F, 0x03, 0x01,
    0x01, 0x05, 0x4A, 0x01, 0x8A, 0x01, 0x01, 0x01, 0x05, 0x48, 0x83, 0x02,
    0x04, 0x01, 0x01, 0x01, 0x04, 0x04, 0x02, 0x00, 0x01, 0x01, 0x01, 0x00,
    0x00, 0x00, 0x00, 0x01, 0x01, 0x01, 0x00, 0x00, 0x00, 0x00, 0x01, 0x01,
    0x01, 0x8A, 0x9B, 0x8F, 0x03, 0x01, 0x01, 0x05, 0x4A, 0x01, 0x8A, 0x01,
    0x01, 0x01, 0x05, 0x48, 0x03, 0x42, 0x04, 0x01, 0x01, 0x01, 0x04, 0x04,
    0x42, 0x00, 0x01, 0x01, 0x01, 0x00, 0x00, 0x00, 0x00, 0x01, 0x01, 0x01,
    0x00, 0x00, 0x00, 0x00, 0x01, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x02, 0x00, 0x00, 0x07,
    0x17, 0x41, 0xA8, 0x32, 0x30,
))

# ---------------- 以下为实验工具，正式刷屏路径不使用 ----------------
# 233 字节表的段偏移（三色布局，实测芯片只执行 168 那段，且与像素数据无关）
LUT_OFS = {"C": 0, "W": 112, "B": 168}


def _lut_drive(t, base, frames, vs):
    """往 base 起的 42 字节写「连续 frames 帧单极性驱动 + 3 帧泄放」。"""
    g = 0
    n = frames
    while n > 0 and g < 6:
        step = 63 if n > 63 else n
        o = base + g * 7
        t[o] = 1
        t[o + 1] = (vs << 6) | step     # VS 高 2 位：01=VSH1 10=VSL
        t[o + 2] = 0x03
        t[o + 5] = 1
        t[o + 6] = 1
        n -= step
        g += 1


def make_lut(frames=8, fr=0x02, vs=0x01, ofs=168):
    """构造一张实验用波形表。注意：它整屏统一驱动，画不出图案（见文件头结论 5）。"""
    t = bytearray(233)
    t[224] = fr
    t[227] = 0x07
    t[228] = 0x17
    t[229] = 0x41
    t[230] = 0xA8
    t[231] = 0x32
    t[232] = 0x30
    _lut_drive(t, ofs, frames, vs)
    return t


# ---------------- 差分刷新波形（2026-11-03 真机验证，_diff9.py 照片确认） ----------------
# SSD1619A 规格书格式：0x32 收 70 字节 = VS 5组x7字节 + TP/RP 5组x7字节。
# 槽位按 (旧位, 新位) 选波形：槽0=黑->黑 槽1=黑->白 槽2=白->黑 槽3=白->白 槽4=备用。
# 每组第 0 字节高 2 位是 VS：00=VSS(保持) 01=VSH1(驱动成黑) 10=VSL(驱动成白)。
def make_diff_lut(tp=20):
    """70 字节差分 LUT。tp=TP0 档位（0~63），总耗时约 193+40.3*tp 毫秒：
    tp=40→1.8s  20→1.0s  10→0.6s。tp 越小越快、驱动越弱，太小会留残影。"""
    VS = (0, 2, 1, 0, 0)      # VSS, VSL(白), VSH1(黑), VSS, VSS
    t = bytearray(70)
    for s in range(5):
        t[7 * s + 0] = (VS[s] & 3) << 6
    t[35] = tp & 0x3F         # TP0：波形时长
    t[39] = 1                 # RP0：波形跑一遍
    return bytes(t)


class EPD:
    def __init__(self, spi, cs, dc, rst, busy):
        self.spi = spi
        self.cs = cs
        self.dc = dc
        self.rst = rst
        self.busy = busy
        self.width = EPD_WIDTH
        self.height = EPD_HEIGHT
        self.on_wait = None          # 忙等期间的回调（主程序挂 web 轮询，刷屏时网页照样能点）
        self.last_frame = None       # 驱动自己记的上一帧
        self.short_wave = True       # True=用 30°C 短波形（2.9s），False=5.2s 长波形
        self.diff_tp = 20            # 差分波形时长档位 0~63：40≈1.8s 20≈1.0s 10≈0.6s
        cs.value(1)
        dc.value(0)
        rst.init(Pin.OUT)
        cs.init(Pin.OUT)
        dc.init(Pin.OUT)
        busy.init(Pin.IN)

    # ---------- 底层 ----------
    def reset(self):
        self.rst.value(1); time.sleep_ms(100)
        self.rst.value(0); time.sleep_ms(2)
        self.rst.value(1); time.sleep_ms(100)

    def send_command(self, cmd):
        self.dc.value(0)
        self.cs.value(0)
        self.spi.write(bytes((cmd,)))
        self.cs.value(1)

    def send_data(self, data):
        self.dc.value(1)
        self.cs.value(0)
        self.spi.write(bytes((data,)) if isinstance(data, int) else bytes(data))
        self.cs.value(1)

    def send_data2(self, buf):
        """整帧写入：一次传完，比分片快得多"""
        self.dc.value(1)
        self.cs.value(0)
        self.spi.write(buf)
        self.cs.value(1)

    def ReadBusy(self, timeout_ms=15000):
        t0 = time.ticks_ms()
        while self.busy.value() == 1:                  # 1 = busy
            if time.ticks_diff(time.ticks_ms(), t0) > timeout_ms:
                return False
            if self.on_wait:
                self.on_wait()                         # 等屏的同时处理网页请求
            else:
                time.sleep_ms(20)
        return True

    # ---------- 刷屏指令 ----------
    def wait_assert(self, ms=200):
        """等 BUSY 真正拉高（波形启动），最多 ms 毫秒。

        ⚠ 必需：0x20 之后 BUSY 不是立刻拉高的（真机实测有几十毫秒的延迟，
        刷新被中断后更明显）。ReadBusy() 只判"高->低"，如果进得太早会看到
        低电平直接返回，上层就会在波形还在跑的时候发 sleep() ——2026-11-03
        实测到 display() 25ms 就返回（正常 3.1s），屏被留在半死状态。
        """
        t0 = time.ticks_ms()
        while self.busy.value() == 0:
            if time.ticks_diff(time.ticks_ms(), t0) > ms:
                return False
            time.sleep_ms(2)
        return True

    def TurnOnDisplay(self):
        """唯一能真正驱动这块屏的更新命令：LOAD_LUT + 显示。

        0xC7（不加载 LUT）在本屏是空转，别用。
        """
        self.send_command(0x22); self.send_data(0xF7)
        self.send_command(0x20)
        self.wait_assert()           # 先等波形启动，再等它跑完
        return self.ReadBusy()

    def TurnOnDisplay_Fast(self):
        """保留接口：实测不驱动，内部已改走 0xF7。"""
        return self.TurnOnDisplay()

    def TurnOnDisplay_Partial(self):
        """保留接口：实测不驱动，内部已改走 0xF7。"""
        return self.TurnOnDisplay()

    # ---------- 初始化 ----------
    def set_temp(self, hi=0x1E, lo=0x00):
        """0x1A = 12bit 温度寄存器（两字节），决定 OTP 选哪套波形。
        0x1E00(30°C)  -> 2.9s 短波形
        0x07FF(POR)   -> 5.2s 长波形
        """
        self.send_command(0x1A); self.send_data(hi); self.send_data(lo)

    def init(self, short=None):
        """复位 + 初始化。short=True 用短波形（2.9s），False 用长波形（5.2s）。

        **不要在这里发 0x04/0x01/0x06**：那会让后续 0xF7 变成空转，
        这是之前"局刷不生效、快刷不出图"的根因。
        """
        if short is not None:
            self.short_wave = short
        self.reset(); self.ReadBusy()
        self.send_command(0x12); self.ReadBusy()        # SW RESET
        self.send_command(0x21); self.send_data(0x40); self.send_data(0x00)  # 复用红平面、只走黑白
        self.send_command(0x3C); self.send_data(0x05)   # Border waveform
        self.set_temp(0x1E if self.short_wave else 0x07, 0x00 if self.short_wave else 0xFF)
        self.send_command(0x11); self.send_data(0x03)   # 数据入口：X 增、Y 增
        self._window()
        return 0

    def init_fast(self, *a, **kw):
        """兼容旧接口 = init(short=True)。"""
        return self.init(short=True)

    def init_part(self, *a, **kw):
        """兼容旧接口。

        真正要做的只是"把边框波形设成跟随 VCOM"，**不能复位、不能上电**：
        复位会丢 RAM，单独发 0x04 会让后续驱动失效。
        """
        self.send_command(0x3C); self.send_data(0x80)
        return 0

    def _window(self, y0=0, y1=EPD_HEIGHT - 1):
        self.send_command(0x44); self.send_data(0x00); self.send_data(0x31)   # RAM X 0..49 (400px)
        self.send_command(0x45)
        self.send_data(y0 & 0xFF); self.send_data((y0 >> 8) & 0xFF)
        self.send_data(y1 & 0xFF); self.send_data((y1 >> 8) & 0xFF)           # RAM Y y0..y1
        self.send_command(0x4E); self.send_data(0x00)
        self.send_command(0x4F); self.send_data(y0 & 0xFF); self.send_data((y0 >> 8) & 0xFF)
        self.ReadBusy()

    def Lut(self):
        """写官方 4 灰阶波形表（仅 Init_4Gray 用）。"""
        self.send_command(0x32)
        for i in range(227):
            self.send_data(LUT_ALL[i])
        self.send_command(0x3F); self.send_data(LUT_ALL[227])
        self.send_command(0x03); self.send_data(LUT_ALL[228])
        self.send_command(0x04)
        for i in range(229, 232):
            self.send_data(LUT_ALL[i])
        self.send_command(0x2C); self.send_data(LUT_ALL[232])

    def write_lut(self, tab):
        """写入一张 233 字节自定义波形表（实验用）。

        ⚠ 本屏的自定义表是整屏统一驱动、与 RAM 数据无关，画不出图案；
        仅用于研究波形时序，不要接入正式刷新路径。
        """
        self.send_command(0x32); self.send_data2(tab[0:227])
        self.send_command(0x3F); self.send_data(tab[227])
        self.send_command(0x03); self.send_data(tab[228])
        self.send_command(0x04); self.send_data2(tab[229:232])
        self.send_command(0x2C); self.send_data(tab[232])

    def Init_4Gray(self):
        self.reset(); self.ReadBusy()
        self.send_command(0x12); self.ReadBusy()
        self.send_command(0x21); self.send_data(0x00); self.send_data(0x00)
        self.send_command(0x3C); self.send_data(0x03)
        self.send_command(0x0C)
        for b in (0x8B, 0x9C, 0xA4, 0x0F):
            self.send_data(b)
        self.Lut()
        self.send_command(0x11); self.send_data(0x03)
        self._window()
        return 0

    # ---------- 出图 ----------
    def _write_frame(self, buf):
        self._window()
        self.send_command(0x24); self.send_data2(buf)
        self.send_command(0x26); self.send_data2(bytes(FRAME_BYTES))   # 红平面清零

    def Clear(self):
        self._write_frame(b"\xff" * FRAME_BYTES)
        return self.TurnOnDisplay()

    def clean(self):
        """冷启动把屏面驱回纯白：先黑后白各一帧，用长波形（拉得最干净）。约 11 秒。"""
        old = self.short_wave
        self.set_temp(0x07, 0xFF)                    # 长波形
        self._write_frame(bytes(FRAME_BYTES))
        self.TurnOnDisplay()
        self._write_frame(b"\xff" * FRAME_BYTES)
        self.TurnOnDisplay()
        self.short_wave = old
        self.set_temp(0x1E if old else 0x07, 0x00 if old else 0xFF)
        self.last_frame = b"\xff" * FRAME_BYTES      # 屏面已是纯白，旧帧记账为全白
        return 0

    def display(self, buf):
        """全刷（唯一可靠路径）：0x24 写黑白平面、0x26 清零、0xF7 驱动。

        RAM 约定「白=(0x24=1, 0x26=0)、黑=(0,0)」；0x26 写 0 即不点亮红。
        """
        self._write_frame(buf)
        self.last_frame = bytes(buf)
        return self.TurnOnDisplay()

    def display_inv(self, buf):
        """全刷变体：0x26 写新图反相，每个像素都有一次黑白跳变，白底最干净（更闪）。"""
        self._window()
        self.send_command(0x24); self.send_data2(buf)
        inv = bytearray(len(buf))
        for i in range(len(buf)):
            inv[i] = buf[i] ^ 0xFF
        self.send_command(0x26); self.send_data2(inv)
        self.last_frame = bytes(buf)
        return self.TurnOnDisplay()

    def display_flash(self, buf):
        """全刷变体：先整屏黑再上图（两次全刷，约 12s，最干净）"""
        black = bytes(FRAME_BYTES)
        white = b"\xff" * FRAME_BYTES
        self.send_command(0x24); self.send_data2(black)
        self.send_command(0x26); self.send_data2(white)
        self.TurnOnDisplay()
        self.send_command(0x24); self.send_data2(buf)
        self.send_command(0x26); self.send_data2(black)
        self.last_frame = bytes(buf)
        return self.TurnOnDisplay()

    def display_Fast(self, buf):
        """兼容旧接口 = display()。本屏没有可用的快刷波形。"""
        return self.display(buf)

    def display_diff(self, buf, prev=None):
        """差分刷新（2026-11-03 真机验证）：只驱动真正变化的像素，不整屏闪。

        配方（_diff9.py 照片验证过，字节布局别改）：
          0x21=0x00  让红平面透传当"旧帧"（init() 默认发的 0x40 会把它旁路成 0）
          0x32       70 字节差分 LUT（规格书格式；写 233 字节会被吞、静默空转）
          0x24       新帧    0x26 旧帧
          0x22=0xCF  寄存器 LUT + 黑白差分模式
        耗时约 193+40.3*TP 毫秒（TP=self.diff_tp）。刷完把 0x21 恢复成 0x40，
        否则后续 0xF7 全刷会冒红。prev 默认取 last_frame（驱动自己记的上一帧）。
        """
        if prev is None:
            prev = self.last_frame
        if prev is None:
            return self.display(buf)          # 没有旧帧可差分，退回全刷
        if bytes(prev) == bytes(buf):
            return True                       # 没变化，什么都不驱动
        self.send_command(0x21); self.send_data(0x00); self.send_data(0x00)
        self.send_command(0x32); self.send_data2(make_diff_lut(self.diff_tp))
        self._window()
        self.send_command(0x24); self.send_data2(buf)
        self.send_command(0x26); self.send_data2(prev)
        self.send_command(0x22); self.send_data(0xCF)
        self.send_command(0x20)
        self.wait_assert(500)                 # 0xCF 的 BUSY 启动延迟比 0xF7 长，别进太早
        ok = self.ReadBusy()
        self.send_command(0x21); self.send_data(0x40); self.send_data(0x00)
        self.last_frame = bytes(buf)
        return ok

    def display_Partial(self, buf, prev=None):
        """兼容旧接口，现在走真正的差分刷新（2026-11-03 起）。

        以前这里退化成全刷（当时结论"没有局刷"）；差分跑通后改为：
        prev 给了就差分，没给就退回全刷。
        """
        if prev is not None and prev == buf:
            return True
        if prev is not None:
            return self.display_diff(buf, prev)
        return self.display(buf)

    def sleep(self):
        self.send_command(0x10); self.send_data(0x01)
        time.sleep_ms(200)
