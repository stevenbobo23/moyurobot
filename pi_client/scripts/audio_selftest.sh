#!/usr/bin/env bash
# ============================================================================
# 摸鱼遥控车 · 语音对讲硬件自检
#
# 在树莓派上运行，检查声卡、录音、回放是否就绪：
#     ./scripts/audio_selftest.sh
#     ./scripts/audio_selftest.sh plughw:1,0      # 指定要测试的设备
# ============================================================================
set -u

DEVICE="${1:-default}"
PASS=0
FAIL=0
WARN=0

ok()   { echo "  ✅ $1"; PASS=$((PASS + 1)); }
bad()  { echo "  ❌ $1"; FAIL=$((FAIL + 1)); }
warn() { echo "  ⚠️  $1"; WARN=$((WARN + 1)); }

need() {
    if command -v "$1" >/dev/null 2>&1; then
        ok "命令存在: $1 -> $(command -v "$1")"
    else
        bad "缺少命令: $1${2:+（$2）}"
    fi
}

echo "============================================================"
echo " 摸鱼遥控车 · 语音对讲自检   设备: $DEVICE"
echo "============================================================"

echo
echo "[1/5] 音频工具链"
need arecord "sudo apt install -y alsa-utils"
need aplay   "sudo apt install -y alsa-utils"
command -v parec  >/dev/null 2>&1 && ok "命令存在: parec（PulseAudio 可用）" || warn "无 parec：使用 ALSA 后端即可，忽略"
command -v ffmpeg >/dev/null 2>&1 && ok "命令存在: ffmpeg" || warn "无 ffmpeg：RTMP 推流与 ffmpeg 音频后端不可用"

echo
echo "[2/5] 声卡枚举"
if [ -f /proc/asound/cards ]; then
    echo "----- /proc/asound/cards -----"
    cat /proc/asound/cards
else
    warn "没有 /proc/asound/cards（非 Linux 或没装 ALSA）"
fi
if command -v arecord >/dev/null 2>&1; then
    echo "----- arecord -l（麦克风）-----"
    arecord -l 2>&1 || true
fi
if command -v aplay >/dev/null 2>&1; then
    echo "----- aplay -l（扬声器）-----"
    aplay -l 2>&1 || true
fi
if command -v arecord >/dev/null 2>&1 && arecord -l 2>/dev/null | grep -q '^card'; then
    ok "检测到录音设备"
else
    bad "没有检测到录音设备：插上 USB 麦克风后重试（dmesg | tail 看内核日志）"
fi
if command -v aplay >/dev/null 2>&1 && aplay -l 2>/dev/null | grep -q '^card'; then
    ok "检测到播放设备"
else
    bad "没有检测到播放设备：插上 USB 音箱/耳机后重试"
fi

echo
echo "[3/5] 端口占用（语音对讲默认 8081）"
PORT="${AUDIO_WS_PORT:-8081}"
if command -v ss >/dev/null 2>&1; then
    if ss -ltn 2>/dev/null | grep -q ":$PORT "; then
        warn "端口 $PORT 已被占用：改 AUDIO_WS_PORT 或停掉占用进程"
    else
        ok "端口 $PORT 空闲"
    fi
else
    warn "没有 ss 命令，跳过端口检查"
fi

echo
echo "[4/5] 录音测试（3 秒，请对着麦克风说话）"
TMPWAV="$(mktemp -t moyu_mic_XXXX.wav)"
if command -v arecord >/dev/null 2>&1; then
    if arecord -q -D "$DEVICE" -f S16_LE -r 16000 -c 1 -d 3 "$TMPWAV" 2>/tmp/moyu_rec_err; then
        SIZE=$(wc -c <"$TMPWAV" | tr -d ' ')
        if [ "$SIZE" -gt 40000 ]; then
            ok "录音成功：$SIZE 字节（约 3 秒 16kHz 单声道）"
            if command -v python3 >/dev/null 2>&1; then
                RMS=$(python3 - "$TMPWAV" <<'PY'
import array, math, sys, wave
try:
    with wave.open(sys.argv[1], "rb") as w:
        data = w.readframes(w.getnframes())
    samples = array.array("h")
    samples.frombytes(data)
    if not samples:
        print(0)
    else:
        total = sum(v * v for v in samples)
        print(round(math.sqrt(total / len(samples))))
except Exception:
    print(-1)
PY
)
                if [ "$RMS" = "-1" ]; then
                    warn "无法计算电平（缺 audioop 或文件格式异常）"
                elif [ "$RMS" -lt 30 ]; then
                    warn "录音电平极低（RMS=$RMS）：麦克风静音/增益太低，可调 AUDIO_MONITOR_GAIN"
                else
                    ok "录音电平正常（RMS=$RMS）"
                fi
            fi
        else
            bad "录音文件过小（$SIZE 字节）：设备 $DEVICE 可能打不开"
            sed 's/^/      /' /tmp/moyu_rec_err 2>/dev/null | head -5
        fi
    else
        bad "arecord 执行失败（设备 $DEVICE）"
        sed 's/^/      /' /tmp/moyu_rec_err 2>/dev/null | head -5
    fi
fi

echo
echo "[5/5] 回放测试（刚才录的 3 秒会播出来）"
if command -v aplay >/dev/null 2>&1; then
    if aplay -q -D "$DEVICE" "$TMPWAV" 2>/tmp/moyu_play_err; then
        ok "回放成功（如果没有声音：检查音箱供电/音量 alsamixer）"
    else
        bad "aplay 执行失败（设备 $DEVICE）"
        sed 's/^/      /' /tmp/moyu_play_err 2>/dev/null | head -5
    fi
fi
rm -f "$TMPWAV" /tmp/moyu_rec_err /tmp/moyu_play_err

echo
echo "============================================================"
echo " 结果: 通过 $PASS / 警告 $WARN / 失败 $FAIL"
echo "============================================================"
if [ "$FAIL" -gt 0 ]; then
    echo "有失败项，先解决硬件/依赖，再启动服务："
else
    echo "硬件就绪，可以启动服务了："
fi
cat <<'TIP'
  cd pi_client
  AUDIO_CAPTURE_DEVICE="" AUDIO_PLAYBACK_DEVICE="" python -m moyurobot.web.controller --port 8080
  # 无硬件时先用模拟后端验证网页端：
  AUDIO_BACKEND=sim python -m moyurobot.web.controller --port 8080
TIP
exit "$FAIL"
