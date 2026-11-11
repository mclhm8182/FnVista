#!/bin/bash
# ============================================================
#  fnos-wallpaper-watchdog.sh
#  壁纸看门狗：每分钟校验壁纸文件是否被飞牛守护进程还原，
#  被还原则立刻用已应用的副本换回来并重新加锁。
#
#  由 fnos-bing-wallpaper 的 /etc/cron.d/fnos-bing-wallpaper 每分钟调用。
#  「恢复原始壁纸」（--restore / 应用中心停止 / 卸载）会清空 applied 目录，
#  看门狗随之自动失效，不会跟用户抢壁纸。
# ============================================================
set -uo pipefail

DATA_DIR="/var/lib/fnos-wallpaper"
APPLIED="$DATA_DIR/applied"
BG="/usr/trim/www/static/bg/wallpaper-1.webp"
TH="/usr/trim/www/static/thumbnail_bg/wallpaper-1.webp"
LOG_FILE="/var/log/fnos-bing-wallpaper.log"

CHATTR=""
for c in /usr/bin/chattr /usr/bin/chattrx /usr/sbin/chattr /sbin/chattr; do
  [ -x "$c" ] && { CHATTR="$c"; break; }
done

# 未加锁标记（防并发重入）
LOCK="$DATA_DIR/.watchdog.lock"
[ -f "$LOCK" ] && exit 0
touch "$LOCK" 2>/dev/null || exit 0
trap 'rm -f "$LOCK"' EXIT

log() { printf '[%s] [watchdog] %s\n' "$(date '+%F %T')" "$*" >> "$LOG_FILE"; }

# 没有已应用副本 = 当前处于"已还原/未启用"状态，不做任何事
[ -f "$APPLIED/wallpaper-1.webp" ] || exit 0
[ -f "$APPLIED/thumb-1.webp" ] || exit 0

changed=0

# ---------- 大图 ----------
if [ ! -f "$BG" ] || ! cmp -s "$BG" "$APPLIED/wallpaper-1.webp"; then
  [ -n "$CHATTR" ] && "$CHATTR" -ai "$BG" 2>/dev/null
  if cp -f "$APPLIED/wallpaper-1.webp" "$BG" 2>/dev/null; then
    chmod 644 "$BG" 2>/dev/null
    [ -n "$CHATTR" ] && "$CHATTR" +ai "$BG" 2>/dev/null
    log "壁纸被系统还原，已自动换回（大图）"
    changed=1
  else
    log "警告：壁纸被还原且换回失败（目录可能被重建，1 秒后重试一次）"
    sleep 1
    if [ -d "$(dirname "$BG")" ] && cp -f "$APPLIED/wallpaper-1.webp" "$BG" 2>/dev/null; then
      chmod 644 "$BG" 2>/dev/null
      [ -n "$CHATTR" ] && "$CHATTR" +ai "$BG" 2>/dev/null
      log "重试成功，壁纸已换回（大图）"
      changed=1
    else
      log "错误：换回失败，下一分钟继续尝试"
    fi
  fi
fi

# ---------- 缩略图 ----------
TH_DIR="$(dirname "$TH" 2>/dev/null)"
if [ -n "$TH_DIR" ] && [ -d "$TH_DIR" ]; then
  if [ ! -f "$TH" ] || ! cmp -s "$TH" "$APPLIED/thumb-1.webp"; then
    [ -n "$CHATTR" ] && "$CHATTR" -ai "$TH" 2>/dev/null
    if cp -f "$APPLIED/thumb-1.webp" "$TH" 2>/dev/null; then
      chmod 644 "$TH" 2>/dev/null
      [ -n "$CHATTR" ] && "$CHATTR" +ai "$TH" 2>/dev/null
      log "壁纸被系统还原，已自动换回（缩略图）"
      changed=1
    fi
  fi
fi

# ---------- 用户自定义壁纸文件（jpg 副本同步过的那些） ----------
CUSTOM_LIST="$DATA_DIR/applied/custom.list"
# v1.5.5: 自定义壁纸行的「当前应显示图片」由 set.sh 记录。
# 点选/轮播走 --desktop-only 时来源是 desktop.jpg，不能再固定比对 bing.jpg，
# 否则看门狗会在一分钟内把用户刚选的图刷回 Bing 图。
CUSTOM_SRC="$(cat "$APPLIED/custom.source" 2>/dev/null || true)"
[ -n "$CUSTOM_SRC" ] && [ -f "$CUSTOM_SRC" ] || CUSTOM_SRC="$APPLIED/bing.jpg"
if [ -f "$CUSTOM_LIST" ] && [ -f "$CUSTOM_SRC" ]; then
  while read -r f; do
    [ -f "$f" ] || continue
    if ! cmp -s "$f" "$CUSTOM_SRC"; then
      cp -f "$CUSTOM_SRC" "$f" 2>/dev/null && chmod 644 "$f" && changed=1
      log "自定义壁纸被还原，已自动换回: $f"
    fi
  done < "$CUSTOM_LIST"
fi

# ---------- 内容正确但锁可能被目录重建带走：补锁 ----------
ensure_lock() {
  local f="$1"
  [ -f "$f" ] || return 0
  if ! lsattr "$f" 2>/dev/null | grep -q -- '-a'; then
    [ -n "$CHATTR" ] && "$CHATTR" +ai "$f" 2>/dev/null
  fi
}
ensure_lock "$BG"
[ -f "$TH" ] && ensure_lock "$TH"

# ---------- 前端还原源 www.zip 守护（登录页/远程壁纸） ----------
# 系统更新会还原 www.zip，这里按需重新修补（内部有快速检查，开销极小）
ZIP_PATCH_BIN="/usr/local/sbin/fnos-www-zip-patch.sh"
[ -x "$ZIP_PATCH_BIN" ] && "$ZIP_PATCH_BIN" --check || true

# 不 reload nginx：壁纸换回只是恢复文件内容，nginx 直接服务磁盘文件；
# reload 会瞬间断开所有前端连接（表现为网页「无法访问」），没有必要。
exit 0
