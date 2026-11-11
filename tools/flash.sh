#!/bin/zsh
# ESP32-S3 N16R8 一键烧录 MicroPython（含擦除）
# 用法：
#   ./flash.sh            # 擦除 + 烧录 + 验证
#   ./flash.sh chipinfo   # 只看芯片信息（不烧录）
set -e
HERE="${0:a:h}"
PY=/opt/homebrew/bin/python3
export PYTHONPATH="$HERE/esptool-lib"
FW="$HERE/../bin/ESP32_GENERIC_S3-SPIRAM_OCT-20260824-v1.29.0.bin"
[ -f "$FW" ] || FW=$(ls "$HERE"/../bin/*.bin 2>/dev/null | head -1)
if [ -z "$FW" ] || [ ! -f "$FW" ]; then
  echo "❌ 找不到固件：请把 .bin 放到 $(dirname "$HERE")/bin/"
  exit 1
fi

# 自动找串口
PORT=$(ls /dev/cu.usb* 2>/dev/null | head -1)
if [ -z "$PORT" ]; then
  echo "❌ 没找到串口。请用 Type-C 数据线连接板子（注意：很多 C-C 线只能充电，换根数据线），再试。"
  echo "   如果插了还是找不到：按住板上 BOOT 键不放 → 短按 RST → 松开 BOOT，再试一次。"
  exit 1
fi
echo "串口: $PORT"

if [ "$1" = "chipinfo" ]; then
  $PY -m esptool --port "$PORT" --baud 460800 chip_id
  exit 0
fi

echo "== 1/3 擦除 Flash（首次烧录必做，清掉出厂固件）=="
$PY -m esptool --port "$PORT" --baud 460800 --chip esp32s3 erase_flash

echo "== 2/3 写入 MicroPython 固件（v1.29.0 Octal PSRAM，N16R8 适用）=="
$PY -m esptool --port "$PORT" --baud 460800 --chip esp32s3 write_flash 0 "$FW"

echo "== 3/3 校验 =="
$PY -m esptool --port "$PORT" --baud 460800 --chip esp32s3 verify_flash 0 "$FW" 2>&1 | tail -3

echo
echo "✅ 烧录完成。用 Thonny / mpremote 上传 epd42.py、epaper_main.py 后即可运行。"
