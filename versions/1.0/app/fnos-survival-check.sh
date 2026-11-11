#!/bin/bash
# ============================================================
#  fnos-survival-check.sh   升级 / 开机 / 运行期存活自检
#
#  目标：防止「某次 fnOS 升级 / 异常后，本应用的定时任务或看门狗被清掉，
#        导致壁纸不再更新」这种静默失效。
#
#  自检项：
#    1) /etc/cron.d/fnos-bing-wallpaper 是否存在且含 daily / watchdog 行；
#       缺失或残损则重建（fnos-cron-update.sh）。
#    2) cron 服务是否在运行；未运行则 enable --now。
#    3) 轮播开关开启但守护进程未运行 -> 重新拉起。
#
#  仅在确实发现异常并修复时才写日志，正常时零开销。
#  由 install / upgrade / main 及看门狗（每分钟）调用。
# ============================================================
set -uo pipefail

DATA_DIR="/var/lib/fnos-wallpaper"
CRON_FILE="/etc/cron.d/fnos-bing-wallpaper"
CRON_UPDATE_BIN="/usr/local/sbin/fnos-cron-update.sh"
ROTATE_BIN="/usr/local/sbin/fnos-wallpaper-rotate.sh"
ROTATE_FLAG="$DATA_DIR/rotate"
LOG_FILE="/var/log/fnos-bing-wallpaper.log"

# 非 root 直接退出（cron 以 root 调用，正常不会走到这里）
[ "$(id -u)" -eq 0 ] || exit 0

log() { printf '[%s] [survival] %s\n' "$(date '+%F %T')" "$*" >> "$LOG_FILE"; }

rebuild=0
# 1) 定时任务文件完整性：必须同时存在 daily 与 watchdog 两行
if [ ! -f "$CRON_FILE" ] \
   || ! grep -q 'bing-wallpaper-daily.sh' "$CRON_FILE" \
   || ! grep -q 'fnos-wallpaper-watchdog.sh' "$CRON_FILE"; then
  rebuild=1
fi
# 2) cron 服务活跃
if ! systemctl is-active cron >/dev/null 2>&1; then
  rebuild=1
fi

if [ "$rebuild" -eq 1 ]; then
  [ -x "$CRON_UPDATE_BIN" ] && bash "$CRON_UPDATE_BIN" || true
  systemctl enable --now cron >/dev/null 2>&1 || true
  log "存活自检：已重建定时任务并确认 cron 服务运行"
fi

# 3) 轮播守护进程存活（仅在开启时检查）
if [ -f "$ROTATE_FLAG" ] && [ "$(cat "$ROTATE_FLAG" 2>/dev/null)" = "1" ]; then
  if ! pgrep -f 'fnos-wallpaper-rotate.sh --daemon' >/dev/null 2>&1; then
    setsid nohup bash "$ROTATE_BIN" --ensure >/dev/null 2>&1 < /dev/null &
    log "存活自检：轮播守护进程未运行，已重新拉起"
  fi
fi

exit 0
