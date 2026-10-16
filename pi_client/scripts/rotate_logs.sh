#!/bin/sh
# ============================================
# 摸鱼遥控车 - 日志轮转脚本
# ============================================
#
# 服务是用 shell 的 >> 重定向写日志的（$LOG_DIR/moyurobot_web.log 等），
# Python 侧的 RotatingFileHandler 管不到这个 fd，所以需要一个外部轮转。
#
# 策略：文件超过 --max-mb 时，只保留最后 --keep-lines 行，其余丢弃。
# 采用「原地截断」（copytruncate 风格）而不是 mv/rm：
# 服务进程正持有该文件的 fd，mv 会让它继续写已删除的 inode（丢日志 + 白占空间）；
# 原地写入保持 inode 不变，服务无需重启、也不会丢新日志。
#
# 默认是 dry-run（只报告会做什么，不改任何文件），确认无误再加 --apply。
#
# 用法：
#   ./rotate_logs.sh                              # 预览
#   ./rotate_logs.sh --apply                      # 执行
#   ./rotate_logs.sh --max-mb 20 --keep-lines 5000 --apply
#   ./rotate_logs.sh --dir /home/bobo/logs --apply
#
# 挂 cron（每天凌晨 4 点）：
#   0 4 * * * /path/to/pi_client/scripts/rotate_logs.sh --apply >/dev/null 2>&1
#

LOG_DIR="${LOG_DIR:-$HOME/logs}"
MAX_MB="${LOG_MAX_MB:-8}"
KEEP_LINES="${LOG_KEEP_LINES:-3000}"
APPLY=0

usage() {
    cat <<'EOF'
日志轮转脚本

选项：
  --dir <path>        日志目录（默认 $LOG_DIR 或 ~/logs）
  --max-mb <n>        超过多少 MB 触发轮转（默认 $LOG_MAX_MB 或 8）
  --keep-lines <n>    轮转后保留最后多少行（默认 $LOG_KEEP_LINES 或 3000）
  --apply             真正执行轮转（不加则只预览）
  --dry-run           只预览，不改文件（默认行为）
  -h, --help          显示帮助

只处理日志目录下直接匹配 *.log 的文件，不会递归、不会删除任何文件。
EOF
}

is_uint() {
    case "$1" in
        ''|*[!0-9]*) return 1 ;;
        *) return 0 ;;
    esac
}

# 精确字节数（不用 du -m：文件系统块大小会让几百字节的文件也报 1MB）
file_bytes() {
    wc -c < "$1" 2>/dev/null | tr -d ' '
}

# 字节数 -> 人类可读 MB
human_mb() {
    awk -v b="${1:-0}" 'BEGIN { printf "%.1f", b / 1048576 }'
}

while [ $# -gt 0 ]; do
    case "$1" in
        --apply) APPLY=1 ;;
        --dry-run) APPLY=0 ;;
        --dir)
            shift
            LOG_DIR="${1:-}"
            ;;
        --max-mb)
            shift
            MAX_MB="${1:-}"
            ;;
        --keep-lines)
            shift
            KEEP_LINES="${1:-}"
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            printf "[ERROR] 未知参数: %s\n" "$1"
            usage
            exit 1
            ;;
    esac
    shift
done

if ! is_uint "$MAX_MB" || ! is_uint "$KEEP_LINES"; then
    printf "[ERROR] --max-mb / --keep-lines 必须是整数\n"
    exit 1
fi

if [ ! -d "$LOG_DIR" ]; then
    printf "[WARN] 日志目录不存在: %s\n" "$LOG_DIR"
    exit 0
fi

if [ "$APPLY" -eq 1 ]; then
    printf "[INFO] 日志轮转（实际执行）: dir=%s max=%sMB keep=%s行\n" "$LOG_DIR" "$MAX_MB" "$KEEP_LINES"
else
    printf "[INFO] 日志轮转（dry-run，不改文件）: dir=%s max=%sMB keep=%s行\n" "$LOG_DIR" "$MAX_MB" "$KEEP_LINES"
    printf "       确认后加 --apply 执行\n"
fi

scanned=0
rotated=0
max_bytes=$((MAX_MB * 1024 * 1024))

for file in "$LOG_DIR"/*.log; do
    [ -f "$file" ] || continue
    scanned=$((scanned + 1))

    size_bytes=$(file_bytes "$file")
    [ -n "$size_bytes" ] || continue
    lines=$(wc -l < "$file" 2>/dev/null | tr -d ' ')
    [ -n "$lines" ] || lines="?"

    if [ "$size_bytes" -lt "$max_bytes" ]; then
        printf "  [SKIP] %-32s %sMB / %s 行\n" "$(basename "$file")" "$(human_mb "$size_bytes")" "$lines"
        continue
    fi

    rotated=$((rotated + 1))

    if [ "$APPLY" -ne 1 ]; then
        printf "  [PLAN] %-32s %sMB / %s 行 -> 保留最后 %s 行\n" \
            "$(basename "$file")" "$(human_mb "$size_bytes")" "$lines" "$KEEP_LINES"
        continue
    fi

    tmp="$file.rotate.tmp"
    if tail -n "$KEEP_LINES" "$file" > "$tmp" 2>/dev/null && cat "$tmp" > "$file" 2>/dev/null; then
        rm -f "$tmp"
        new_bytes=$(file_bytes "$file")
        printf "  [DONE] %-32s %sMB -> %sMB\n" \
            "$(basename "$file")" "$(human_mb "$size_bytes")" "$(human_mb "$new_bytes")"
    else
        rm -f "$tmp"
        printf "  [FAIL] %-32s 轮转失败，文件未改动\n" "$(basename "$file")"
    fi
done

printf "[INFO] 扫描 %s 个日志文件，需轮转 %s 个\n" "$scanned" "$rotated"

if [ "$rotated" -eq 0 ]; then
    exit 0
fi

if [ "$APPLY" -eq 1 ]; then
    printf "[INFO] 完成。提示：如磁盘仍紧张，可检查 Python 侧文件日志上限（LOG_MAX_BYTES / LOG_BACKUP_COUNT）\n"
else
    printf "[INFO] 以上为预览，未改动任何文件\n"
fi
