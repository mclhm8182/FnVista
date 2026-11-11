#!/bin/bash
# ============================================================
#  fnos-cron-update.sh
#  统一重建本应用的定时任务（每日更换 + 看门狗 + 可选轮播）
#  供 安装回调 / main start / 设置页保存 三处调用，避免不同步
# ============================================================
set -uo pipefail

DATA_DIR="/var/lib/fnos-wallpaper"
CRON_FILE="/etc/cron.d/fnos-bing-wallpaper"
DAILY_BIN="/usr/local/sbin/bing-wallpaper-daily.sh"
WATCHDOG_BIN="/usr/local/sbin/fnos-wallpaper-watchdog.sh"
ROTATE_BIN="/usr/local/sbin/fnos-wallpaper-rotate.sh"

[ "$(id -u)" -eq 0 ] || exit 1

rt="07:00"
[ -f "$DATA_DIR/run_time" ] && rt="$(cat "$DATA_DIR/run_time" 2>/dev/null)"
printf '%s' "$rt" | grep -qE '^([01][0-9]|2[0-3]):[0-5][0-9]$' || rt="07:00"
h=$((10#${rt%%:*})); m=$((10#${rt##*:}))

rotate_line=""
if [ -f "$DATA_DIR/rotate" ] && [ "$(cat "$DATA_DIR/rotate" 2>/dev/null)" = "1" ] \
   && [ -x "$ROTATE_BIN" ]; then
  # v1.5.5: cron 只负责「每分钟尝试拉起守护进程」，真正的秒级节流在守护进程里
  rotate_line="* * * * * root $ROTATE_BIN --daemon >/dev/null 2>&1"
fi

cat > "$CRON_FILE" <<EOF
# 飞牛 fnOS Bing 每日壁纸（由应用 fnos-bing-wallpaper 管理）
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
$m $h * * * root $DAILY_BIN >/dev/null 2>&1
* * * * * root $WATCHDOG_BIN >/dev/null 2>&1
${rotate_line}
EOF
sed -i '/^$/d' "$CRON_FILE" 2>/dev/null
chmod 644 "$CRON_FILE"
chown root:root "$CRON_FILE" 2>/dev/null
# cron 已经在跑时不要重复 enable --now（实测约耗 4 秒，会让「保存设置」变慢）
[ "$(systemctl is-active cron 2>/dev/null)" = "active" ] || systemctl enable --now cron >/dev/null 2>&1
# 保存设置后立刻拉起守护进程，不必等下一分钟的 cron（关闭时守护进程会自行退出）
if [ -n "$rotate_line" ] && [ -x "$ROTATE_BIN" ]; then
  setsid nohup bash "$ROTATE_BIN" --ensure >/dev/null 2>&1 < /dev/null &
fi
exit 0
