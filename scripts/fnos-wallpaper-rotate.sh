#!/bin/bash
# ============================================================
#  fnos-wallpaper-rotate.sh   壁纸轮播（v1.5.5 秒级守护进程版）
#
#  过去由 cron 每分钟调用一次、脚本内部按「分钟」节流，因此最快只能
#  60 秒换一次，且用户质疑的「30 秒是否真的自动切换」根本做不到。
#  v1.5.5 改为常驻守护进程：内部每 5 秒检查一次是否到期，间隔以秒计，
#  最短 30 秒；cron 每分钟跑一次 --daemon 用 flock 做「拉起/去重」。
#
#  换图一律走 fnos-wallpaper-set.sh --desktop-only（只改 trim_sac 自定义
#  壁纸行文件，不碰 /usr/trim/www），因此轮播不会触发 fnOS nginx 完整性
#  监控重启，网页不会断连。桌面端由注入 index.html 的定时 JS 每 10 秒
#  做一次离屏指纹比对，内容变化时才以「淡入淡出」方式切换（仿 aerial-tv），
#  无需 F5 即可看到自动轮换，且静止时不闪。
#
#  配置（/var/lib/fnos-wallpaper/）:
#    rotate           1=开启
#    rotate_interval  间隔「分钟」，可为 0.5（=30 秒）/1/5/15/30/60/120/240
#                     沿用分钟单位是为了兼容旧版，无需迁移数据
#    custom_files     用户勾选加入池子的本地文件（每行一个绝对路径）
# ============================================================
set -uo pipefail

DATA_DIR="/var/lib/fnos-wallpaper"
WALL_DIR="$DATA_DIR/wallpapers"
LOG_FILE="/var/log/fnos-bing-wallpaper.log"
SET_BIN="/usr/local/sbin/fnos-wallpaper-set.sh"
ROT_TIME="$DATA_DIR/rotate.time"
ROT_LAST="$DATA_DIR/rotate_last"
DAEMON_LOCK="$DATA_DIR/.rotate-daemon.lock"
ONCE_LOCK="$DATA_DIR/.rotate.lock"

log() { printf '[%s] [rotate] %s\n' "$(date '+%F %T')" "$*" >> "$LOG_FILE"; }

interval_sec() {
  local m
  m="$(cat "$DATA_DIR/rotate_interval" 2>/dev/null || echo 60)"
  case "$m" in 0.5|1|5|15|30|60|120|240) ;; *) m=60 ;; esac
  awk "BEGIN{printf \"%d\", $m*60}"
}

rotate_enabled() {
  [ -f "$DATA_DIR/rotate" ] && [ "$(cat "$DATA_DIR/rotate" 2>/dev/null)" = "1" ]
}

# ---------- 单次换图 ----------
do_rotate() {
  rotate_enabled || return 0
  [ -x "$SET_BIN" ] || return 0
  # 防御：每日自动更新开启时绝不轮播我的照片（二者互斥）
  if [ -f "$DATA_DIR/bing_auto" ] && [ "$(cat "$DATA_DIR/bing_auto" 2>/dev/null)" = "1" ]; then
    rm -f "$ONCE_LOCK" 2>/dev/null; return 0
  fi
  [ ! -f "$ONCE_LOCK" ] || return 0
  touch "$ONCE_LOCK" 2>/dev/null || return 0

  # ---------- 组装壁纸池：按「当前选中的模块」决定轮播源 ----------
  #   applied_src=photo（且我的照片非空）→ 轮播我的照片
  #   其余（applied_src=bing / 空）→ 轮播每日精选历史图（bing-*.jpg）
  POOL_FILE="$(mktemp)"

  SRC="$( [ -f "$DATA_DIR/applied_src" ] && cat "$DATA_DIR/applied_src" 2>/dev/null )"
  USE_PHOTO=0
  if [ "$SRC" = "photo" ] && [ -s "$DATA_DIR/custom_files" ]; then
    grep -iE '\.(jpe?g|png|webp|bmp)$' "$DATA_DIR/custom_files" 2>/dev/null \
      | head -300 >> "$POOL_FILE"
    [ -s "$POOL_FILE" ] && USE_PHOTO=1
  fi
  if [ "$USE_PHOTO" -ne 1 ]; then
    # 当前模块为每日精选（或尚未选定）：轮播每日精选历史图
    : > "$POOL_FILE"
    for f in $(ls -1 "$WALL_DIR"/bing-*.jpg 2>/dev/null | sort); do
      echo "$f" >> "$POOL_FILE"
    done
    # 记录当前模块为每日精选，保证状态文案一致
    printf 'bing\n' > "$DATA_DIR/applied_src" 2>/dev/null
  fi

  # 去重但保留首次出现顺序（与设置页一致）
  awk '!seen[$0]++' "$POOL_FILE" > "$POOL_FILE.dedup"
  mv -f "$POOL_FILE.dedup" "$POOL_FILE"

  TOTAL=$(wc -l < "$POOL_FILE" 2>/dev/null || echo 0)
  if [ "$TOTAL" -lt 1 ]; then
    rm -f "$POOL_FILE" "$ONCE_LOCK"
    log "壁纸池为空，跳过轮播"
    return 0
  fi

  # ---------- 顺序轮换：记录下标，每次取「下一张」（与设置页显示顺序一致，不再随机） ----------
  ROT_IDX="$DATA_DIR/rotate_idx"
  idx=0
  if [ -f "$ROT_IDX" ]; then
    idx="$(cat "$ROT_IDX" 2>/dev/null || echo 0)"
    case "$idx" in ''|*[!0-9]*) idx=0 ;; esac
  fi
  [ "$idx" -ge "$TOTAL" ] && idx=0
  PICK="$(sed -n "$((idx + 1))p" "$POOL_FILE")"
  echo $(( (idx + 1) % TOTAL )) > "$ROT_IDX"
  rm -f "$POOL_FILE"
  [ -n "$PICK" ] || PICK="$(ls -1 "$WALL_DIR"/bing-*.jpg 2>/dev/null | sort -r | head -1)"
  [ -f "$PICK" ] || { rm -f "$ONCE_LOCK"; return 0; }

  # 仅支持静态图片轮播（飞牛桌面不支持视频动态壁纸）

  if bash "$SET_BIN" --desktop-only "$PICK" >> "$LOG_FILE" 2>&1; then
    echo "$PICK" > "$ROT_LAST"
    touch "$ROT_TIME"
    log "轮播已切换（桌面零断连）: $PICK"
  else
    # 失败也推进节流时间戳，避免坏文件让守护进程每 5 秒空转重试
    touch "$ROT_TIME"
    log "轮播应用失败: $PICK"
  fi
  rm -f "$ONCE_LOCK"
}

# ---------- 守护进程 ----------
# cron 每分钟执行一次 --daemon；flock -n 保证全场只有一个守护进程，
# 同时也让 cron 天然具备「挂了自动拉起」的能力。
start_daemon() {
  rotate_enabled || return 0
  exec 9>>"$DAEMON_LOCK" 2>/dev/null || return 0
  flock -n 9 2>/dev/null || return 0
  log "轮播守护进程启动 pid=$$"
  while true; do
    rotate_enabled || { log "轮播已关闭，守护进程退出"; break; }
    SEC="$(interval_sec)"
    now=$(date +%s)
    last=$(stat -c %Y "$ROT_TIME" 2>/dev/null || echo 0)
    if [ $((now - last)) -ge "$SEC" ]; then
      do_rotate
    fi
    # 5 秒一拍：设置里改间隔或关开关，最多 5 秒生效
    sleep 5
  done
  flock -u 9 2>/dev/null
  exec 9>&- 2>/dev/null
}

case "${1:-}" in
  --daemon) start_daemon ;;
  --ensure)
    # 供设置页保存后立刻拉起，避免等下一分钟 cron
    if rotate_enabled; then
      setsid nohup bash "$0" --daemon >/dev/null 2>&1 < /dev/null &
    fi
    ;;
  *) do_rotate ;;
esac
exit 0
