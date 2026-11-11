#!/bin/bash
# ============================================================
#  fnos-wallpaper-set.sh
#  设置飞牛 fnOS 登录页 / 桌面壁纸（webp 转码版）
#
#  用法:
#    fnos-wallpaper-set.sh <图片路径>              # 完整换图：登录页 + 桌面
#    fnos-wallpaper-set.sh --desktop-only <图片>   # 只换桌面（零断连，v1.5.5）
#    fnos-wallpaper-set.sh --restore               # 恢复系统原始壁纸
#    fnos-wallpaper-set.sh --status                # 查看当前状态
#
#  环境变量:
#    WALLPAPER_WIDTH    大图最大宽度，默认 2560（不会放大原图）
#    WALLPAPER_QUALITY  webp 质量 0-100，默认 85
#
#  说明:
#    登录页壁纸 = /usr/trim/www/static/bg/wallpaper-1.webp；
#    桌面壁纸   = trim_sac 自定义壁纸行指向的 /usr/trim/var/trim_sac/wallpaper/
#                 <uid>/<uuid>.jpg（另外还受 wallpaper_type 控制，见下文）。
#    改前者会触发 fnOS nginx 完整性监控重启（约 11 秒断连），改后者不会。
#    因此 v1.5.5 起把两条路径拆开：日常点选/轮播只走 --desktop-only，
#    登录页只在每日定时或手动抓取 Bing 时随完整换图一起更新。
#
#    飞牛有守护进程会在数秒内把壁纸文件还原成系统原图，必须给目标文件
#    加 append-only 属性锁住；脚本会在替换的瞬间临时解锁、写完立刻加锁。
#
#    系统原始壁纸首次替换前会备份到 /var/lib/fnos-wallpaper/orig/，
#    随时可用 --restore 还原。
# ============================================================

set -uo pipefail

BG_DIR="/usr/trim/www/static/bg"
TH_DIR="/usr/trim/www/static/thumbnail_bg"
BG="$BG_DIR/wallpaper-1.webp"
TH="$TH_DIR/wallpaper-1.webp"

WIDTH="${WALLPAPER_WIDTH:-2560}"
QUALITY="${WALLPAPER_QUALITY:-85}"
THUMB_WIDTH="${WALLPAPER_THUMB_WIDTH:-350}"
THUMB_QUALITY="${WALLPAPER_THUMB_QUALITY:-80}"

FFMPEG="$(command -v ffmpeg || true)"
FFPROBE="$(command -v ffprobe || true)"
NGINX_SVC="${FNOS_NGINX_SVC:-trim_nginx}"

DATA_DIR="/var/lib/fnos-wallpaper"
ORIG_DIR="$DATA_DIR/orig"
APPLIED_DIR="$DATA_DIR/applied"
CUSTOM_LIST="$APPLIED_DIR/custom.list"
# 用户自上传壁纸文件的存放目录（wallpaper 表 url /sac/desktop/v1/bgImg/xxx 的实际落盘位置）
CUSTOM_WP_ROOT="/usr/trim/var/trim_sac/wallpaper"
# 兼容本工具早期版本用过的备份目录
LEGACY_ORIG_DIR="/var/lib/bing-wallpaper/orig"

# chattr 路径：也被其它工具改名成 chattrx 的情况一并兼容
CHATTR=""
for c in /usr/bin/chattr /usr/bin/chattrx /usr/sbin/chattr /sbin/chattr; do
  [ -x "$c" ] && { CHATTR="$c"; break; }
done

die()  { echo "错误: $*" >&2; exit 1; }
info() { echo "  $*"; }
warn() { echo "  警告: $*" >&2; }

usage() {
  cat <<'USAGE'
用法:
  fnos-wallpaper-set.sh <图片路径>              完整换图（登录页+桌面，会触发 nginx 重启）
  fnos-wallpaper-set.sh --desktop-only <图片>   只换桌面（零断连，不碰 /usr/trim/www）
  fnos-wallpaper-set.sh --restore               恢复系统原始壁纸
  fnos-wallpaper-set.sh --status                查看当前壁纸状态

环境变量:
  WALLPAPER_WIDTH        大图最大宽度，默认 2560（不会放大原图）
  WALLPAPER_QUALITY      webp 质量 0-100，默认 85
  WALLPAPER_THUMB_WIDTH  缩略图宽度，默认 350

示例:
  sudo fnos-wallpaper-set.sh /vol1/1000/壁纸.jpg
  sudo WALLPAPER_WIDTH=3840 WALLPAPER_QUALITY=90 fnos-wallpaper-set.sh pic.png
USAGE
}

# 加锁 / 解锁：飞牛守护进程靠 append-only 属性挡住
lock_file()   { [ -n "$CHATTR" ] && "$CHATTR" +ai "$1" 2>/dev/null; return 0; }
unlock_file() { [ -n "$CHATTR" ] && "$CHATTR" -ai "$1" 2>/dev/null; return 0; }

reload_nginx() {
  if systemctl reload "$NGINX_SVC" 2>/dev/null; then
    info "nginx 已平滑重载"
  elif systemctl restart "$NGINX_SVC" 2>/dev/null; then
    info "nginx 已重启"
  else
    warn "nginx 重载失败，可手动执行: systemctl restart $NGINX_SVC"
  fi
}

# 转码为 webp。宽度超过 maxw 时等比缩小，不放大。
#
# 注意: 飞牛限制了 ffmpeg 直接写入 /usr/trim/www（会报 ENOENT），
#       而 cp 可以写入。所以先转码到 /tmp，再复制进去。
transcode() {
  local src="$1" out="$2" maxw="$3" q="$4"
  local vf="scale='min(${maxw},iw)':-2:flags=lanczos"
  "$FFMPEG" -y -hide_banner -loglevel error \
    -i "$src" -vf "$vf" \
    -c:v libwebp -quality "$q" -compression_level 4 \
    -f webp "$out" || return 1
  [ -s "$out" ] || return 1
  return 0
}

# 生成桌面自定义壁纸行用的 jpg。与 transcode() 的区别：不写 /usr/trim/www。
# 超宽屏 / 竖屏等异比例适配：按「最长边不超过 maxw」等比缩放（绝不放大），
# 因此无论 21:9 还是竖屏手机图都只会被等比裁剪而非拉伸变形；ffprobe 不可用时
# 回退到原「按宽度封顶」策略，保证至少不变形。
transcode_jpg() {
  local src="$1" out="$2" maxw="${3:-2560}"
  local dims W H long vf
  if [ -n "$FFPROBE" ]; then
    dims="$("$FFPROBE" -v error -select_streams v:0 -show_entries stream=width,height -of csv=p=0 "$src" 2>/dev/null)"
    W="${dims%%,*}"; H="${dims##*,}"
    case "$W" in ''|*[!0-9]*) W="" ;; esac
    case "$H" in ''|*[!0-9]*) H="" ;; esac
  fi
  if [ -n "$W" ] && [ -n "$H" ]; then
    long=$W; [ "$H" -gt "$long" ] && long=$H
    if [ "$long" -gt "$maxw" ]; then
      # 超出上限：横图封顶宽、竖图封顶高（均取偶数，ffmpeg 友好）
      if [ "$W" -ge "$H" ]; then vf="scale=${maxw}:-2"; else vf="scale=-2:${maxw}"; fi
    else
      vf="scale='min(${maxw},iw)':-2"
    fi
  else
    vf="scale='min(${maxw},iw)':-2"
  fi
  "$FFMPEG" -y -hide_banner -loglevel error \
    -i "$src" -vf "$vf" -q:v 2 -f image2 "$out" || return 1
  [ -s "$out" ] || return 1
  return 0
}

# 解锁 → 覆盖 → 立刻上锁。
# 无锁窗口必须尽可能短：飞牛守护进程会在这个间隙重建 static 目录。
install_file() {
  local tmp="$1" dst="$2"
  unlock_file "$dst"
  if cp -f "$tmp" "$dst" 2>/dev/null; then
    chmod 644 "$dst" 2>/dev/null
    lock_file "$dst"
    return 0
  fi
  lock_file "$dst"
  return 1
}

# 找可用的原始壁纸备份
find_orig() {
  local name="$1" f
  for f in "$ORIG_DIR/$name" "$LEGACY_ORIG_DIR/$name"; do
    [ -f "$f" ] && { echo "$f"; return 0; }
  done
  return 1
}

# ---------- 桌面壁纸指向管理（trim_sac 数据库） ----------
#
# 重要: 前端代码里，URL 以 /static/bg 开头的系统壁纸在飞牛远程访问
# (fnos.net 中转) 下会被加上 static2.fygonas.com 等 CDN 前缀，从飞牛
# 官方 CDN 取「出厂原图」，本地替换的文件根本不会被请求 —— 这就是
# 「局域网看得到 Bing、远程访问始终是默认壁纸」的根因。
#
# 而自定义壁纸（wallpaper 表 user_id<>0 的行，URL 形如
# /sac/desktop/v1/bgImg/<uuid>.jpg）不走该前缀，无论局域网还是远程
# 都从 NAS 本机取图。因此本应用把桌面统一指向一条属于自己的自定义
# 壁纸行，每天更新该行背后的文件。
#
DB_BACKUP="$DATA_DIR/db_wp_backup"
# v1.5.5: 备份格式 uid|wpid|wallpaper_type|dynamic_wp_id
#（旧版只有 uid|wpid 两列，restore 时按列数兼容）
# 本应用管理的自定义壁纸行记录: uid|wpid|created(0复用/1本脚本创建)
CUSTOM_MAP="$DATA_DIR/custom_wp_map"
# 兼容常见命名，两名都认
CUSTOM_WP_NAMES="bing-wallpaper|Bing 每日壁纸"

db_available() {
  command -v psql >/dev/null 2>&1 && id postgres >/dev/null 2>&1
}

# 记录所有用户当前指向（只记第一次，避免覆盖用户中途的选择）
# v1.5.5: 连壁纸类型一起备份。飞牛支持 100=静态照片 / 300=纯色 / 500=系统动态，
# 用户一旦手动选过「动态壁纸」，wallpaper_type 会变成 500 —— 此时应用维护的
# 静态自定义行根本不会被桌面读取（表现为「选了图片却没变」）。因此应用必须
# 临时把桌面切回 100，卸载/还原时再恢复用户原来的类型。
backup_wp_pointers() {
  db_available || return 0
  [ -s "$DB_BACKUP" ] && return 0
  su postgres -c "psql -d trim_sac -t -A -F'|' -c 'select user_id, coalesce(single_photo_wp_id,0), coalesce(wallpaper_type,100), coalesce(dynamic_wp_id,0) from user_preference;'" \
    > "$DB_BACKUP" 2>/dev/null
}

# 找到（或创建）某用户名下的 Bing 自定义壁纸行，输出 wpid
find_or_create_custom_row() {
  local uid="$1" jpg="$2" wpid url uuid fdir
  wpid="$(su postgres -c "psql -d trim_sac -t -A -c \"select id from wallpaper where user_id = $uid and type = 100 and (name = 'bing-wallpaper' or name = 'Bing 每日壁纸') order by id limit 1;\"" 2>/dev/null | tr -d '[:space:]')"
  case "$wpid" in ''|*[!0-9]*) wpid="" ;; esac

  if [ -n "$wpid" ]; then
    echo "$uid|$wpid|0" >> "$CUSTOM_MAP"
    echo "$wpid"
    return 0
  fi

  # 创建新行: 先落盘文件，再插表
  [ -f "$jpg" ] || { warn "创建自定义壁纸行失败: 缺少 $jpg"; return 1; }
  uuid="$(cat /proc/sys/kernel/random/uuid 2>/dev/null)" || uuid="$(head -c 16 /dev/urandom | md5sum | cut -c1-36)"
  fdir="$CUSTOM_WP_ROOT/$uid"
  mkdir -p "$fdir"
  cp -f "$jpg" "$fdir/$uuid.jpg" && chmod 644 "$fdir/$uuid.jpg" || { warn "写入自定义壁纸文件失败"; return 1; }
  cp -f "$jpg" "$fdir/thumb-$uuid.jpg" && chmod 644 "$fdir/thumb-$uuid.jpg"
  url="/sac/desktop/v1/bgImg/$uuid.jpg"
  wpid="$(su postgres -c "psql -d trim_sac -t -A -c \"insert into wallpaper (type, name, user_id, url, thumbnail_url, is_dynamic, created_at, updated_at) values (100, 'Bing 每日壁纸', $uid, '$url', '/sac/desktop/v1/bgImg/thumb-$uuid.jpg', false, now(), now()) returning id;\"" 2>/dev/null | tr -d '[:space:]')"
  case "$wpid" in ''|*[!0-9]*) warn "插入自定义壁纸行失败"; return 1 ;; esac
  echo "$uid|$wpid|1" >> "$CUSTOM_MAP"
  info "已创建自定义壁纸行 id=$wpid (用户 $uid)" >&2
  echo "$wpid"
}

# 把所有用户的桌面指向本应用的自定义壁纸行，并确保壁纸类型为静态照片(100)
# v1.5.5: 不再只筛 wallpaper_type=100 的用户。用户可能正在用动态壁纸(500)
# 或纯色(300)，这些情况下光改 single_photo_wp_id 桌面不会有任何变化。
point_desktop_to_bing() {
  local jpg="$1"
  db_available || { warn "数据库不可用，跳过桌面壁纸指向设置"; return 0; }
  backup_wp_pointers
  : > "$CUSTOM_MAP"
  local uids uid wpid
  uids="$(su postgres -c "psql -d trim_sac -t -A -c 'select distinct user_id from user_preference;'" 2>/dev/null)"
  for uid in $uids; do
    case "$uid" in ''|*[!0-9]*) continue ;; esac
    wpid="$(find_or_create_custom_row "$uid" "$jpg")" || continue
    [ -n "$wpid" ] || continue
    if su postgres -c "psql -d trim_sac -c 'update user_preference set wallpaper_type = 100, single_photo_wp_id = $wpid, updated_at = now() where user_id = $uid;'" >/dev/null 2>&1; then
      info "用户 $uid 桌面已指向自定义壁纸 id=$wpid（静态照片模式）"
    else
      warn "更新用户 $uid 桌面壁纸指向失败"
    fi
  done
}

# 还原各用户原来的桌面指向；删除本脚本创建的自定义壁纸行及文件
restore_wp_pointers() {
  db_available || return 0
  if [ -f "$CUSTOM_MAP" ]; then
    local uid wpid created fname
    while IFS='|' read -r uid wpid created; do
      case "$uid$wpid" in ''|*[!0-9]*) continue ;; esac
      if [ "$created" = "1" ]; then
        # 删除本脚本创建的行和文件
        local url f tf
        url="$(su postgres -c "psql -d trim_sac -t -A -c \"select url from wallpaper where id = $wpid;\"" 2>/dev/null | tr -d '[:space:]')"
        fname="$(basename "$url" 2>/dev/null)"
        if [ -n "$fname" ]; then
          rm -f "$CUSTOM_WP_ROOT/$uid/$fname" "$CUSTOM_WP_ROOT/$uid/thumb-$fname"
        fi
        su postgres -c "psql -d trim_sac -c 'delete from wallpaper where id = $wpid;'" >/dev/null 2>&1
        info "已删除自定义壁纸行 id=$wpid"
      fi
    done < "$CUSTOM_MAP"
    rm -f "$CUSTOM_MAP"
  fi
  [ -f "$DB_BACKUP" ] || return 0
  local uid wpid wptype dwpid
  while IFS='|' read -r uid wpid wptype dwpid; do
    case "$uid$wpid" in ''|*[!0-9]*) continue ;; esac
    # 兼容旧版两列备份
    case "$wptype" in ''|*[!0-9]*) wptype=100 ;; esac
    case "$dwpid"  in ''|*[!0-9]*) dwpid=0 ;; esac
    su postgres -c "psql -d trim_sac -c 'update user_preference set wallpaper_type = $wptype, single_photo_wp_id = $wpid, dynamic_wp_id = $dwpid, updated_at = now() where user_id = $uid;'" >/dev/null 2>&1
  done < "$DB_BACKUP"
  rm -f "$DB_BACKUP"
  info "已还原用户桌面壁纸指向与壁纸类型"
}

# ---------- 同步 Bing 自定义壁纸行的文件 ----------
# 无论 DB 指向是否被改，把本应用管理的自定义壁纸行背后的文件都换成
# 当前壁纸（jpg），保证桌面取到的永远是最新 Bing 图。
sync_custom_wallpaper_files() {
  # v1.5.5: 允许传入指定图片（--desktop-only 用），缺省沿用完整换图生成的 bing.jpg
  local jpg="${1:-$APPLIED_DIR/bing.jpg}"
  [ -f "$jpg" ] || return 0
  db_available || return 0

  : > "$CUSTOM_LIST"
  local uid wpid created url turl fname tfname f tf ob
  # CUSTOM_MAP 存在但为空时也必须回退按名字查 —— 1.5.4 曾因空文件直接
  # 走空循环，导致选中自定义图片后桌面文件根本没被替换。
  if [ -s "$CUSTOM_MAP" ]; then
    while IFS='|' read -r uid wpid created; do
      case "$uid$wpid" in ''|*[!0-9]*) continue ;; esac
      sync_one_custom_row "$uid" "$wpid" "$jpg"
    done < "$CUSTOM_MAP"
  else
    su postgres -c "psql -d trim_sac -t -A -F'|' -c \"select user_id, id from wallpaper where user_id is not null and user_id <> 0 and (name = 'bing-wallpaper' or name = 'Bing 每日壁纸');\"" 2>/dev/null |
    while IFS='|' read -r uid wpid; do
      case "$uid$wpid" in ''|*[!0-9]*) continue ;; esac
      sync_one_custom_row "$uid" "$wpid" "$jpg"
    done
  fi
  # 看门狗按这份来源比对自定义壁纸文件，避免把 --desktop-only 换上的图
  # 又刷回 Bing 图（v1.5.5 之前固定比对 bing.jpg，会在一分钟内覆盖轮播）
  printf '%s\n' "$jpg" > "$APPLIED_DIR/custom.source" 2>/dev/null
}

# 同步单条自定义壁纸行的大图与缩略图文件
sync_one_custom_row() {
  local uid="$1" wpid="$2" jpg="$3"
  local row url turl fname tfname f tf ob
  row="$(su postgres -c "psql -d trim_sac -t -A -F'|' -c \"select url, coalesce(thumbnail_url,'') from wallpaper where id = $wpid and user_id = $uid;\"" 2>/dev/null)"
  url="${row%%|*}"; turl="${row#*|}"
  [ "$turl" = "$row" ] && turl=""
  [ -n "$url" ] || return 0
  fname="$(basename "$url")"
  for f in "$CUSTOM_WP_ROOT/$uid/$fname"; do
    [ -f "$f" ] || continue
    ob="$ORIG_DIR/custom-$fname"
    [ -f "$ob" ] || cp -a "$f" "$ob" 2>/dev/null
    cp -f "$jpg" "$f" 2>/dev/null && chmod 644 "$f" && { echo "$f" >> "$CUSTOM_LIST"; info "已同步自定义壁纸: $f"; }
  done
  if [ -n "$turl" ]; then
    tfname="$(basename "$turl")"
    [ "$tfname" = "$fname" ] && return 0
    for tf in "$CUSTOM_WP_ROOT/$uid/$tfname"; do
      [ -f "$tf" ] || continue
      ob="$ORIG_DIR/custom-$tfname"
      [ -f "$ob" ] || cp -a "$tf" "$ob" 2>/dev/null
      cp -f "$jpg" "$tf" 2>/dev/null && chmod 644 "$tf" && { echo "$tf" >> "$CUSTOM_LIST"; info "已同步自定义壁纸缩略图: $tf"; }
    done
  fi
}

# 还原被同步过的自定义壁纸文件
restore_custom_wallpaper_files() {
  [ -f "$CUSTOM_LIST" ] || return 0
  while read -r f; do
    [ -f "$f" ] || continue
    local ob="$ORIG_DIR/custom-$(basename "$f")"
    [ -f "$ob" ] && cp -a "$ob" "$f" 2>/dev/null && chmod 644 "$f" && info "已还原自定义壁纸: $f"
  done < "$CUSTOM_LIST"
  rm -f "$CUSTOM_LIST"
}

[ "$(id -u)" -eq 0 ] || die "需要 root 权限运行，请用 sudo"

case "${1:-}" in
  ""|-h|--help)
    usage
    ;;

  --status)
    echo "大图:   $BG"
    if [ -f "$BG" ]; then ls -la "$BG"; lsattr "$BG" 2>/dev/null; else echo "  (不存在)"; fi
    echo "缩略图: $TH"
    if [ -f "$TH" ]; then ls -la "$TH"; lsattr "$TH" 2>/dev/null; else echo "  (不存在)"; fi
    echo "原始备份:"
    if [ -d "$ORIG_DIR" ] || [ -d "$LEGACY_ORIG_DIR" ]; then
      ls -la "$ORIG_DIR" "$LEGACY_ORIG_DIR" 2>/dev/null
    else
      echo "  (无)"
    fi
    echo "桌面指向:"
    if db_available; then
      su postgres -c "psql -d trim_sac -t -A -F' | ' -c 'select p.user_id, p.wallpaper_type, p.single_photo_wp_id, p.dynamic_wp_id, w.name, w.url from user_preference p left join wallpaper w on w.id = p.single_photo_wp_id;'" 2>/dev/null | sed 's/^/  /'
    else
      echo "  (数据库不可用)"
    fi
    ;;

  --restore)
    echo "恢复系统原始壁纸..."
    RB="$(find_orig wallpaper-1.webp.orig)" || die "找不到原始壁纸备份，无法恢复"
    RT="$(find_orig thumb-1.webp.orig || true)"

    # 还原前端 www.zip 补丁（登录页 CSS 与 CDN 前缀）
    [ -x /usr/local/sbin/fnos-www-zip-patch.sh ] && /usr/local/sbin/fnos-www-zip-patch.sh --revert

    # 还原用户原来的桌面壁纸指向与自定义壁纸文件，再清看门狗副本
    restore_wp_pointers
    restore_custom_wallpaper_files
    rm -rf "$DATA_DIR/applied"

    # 零断连恢复：桌面壁纸实际取自 trim_sac 的自定义壁纸行文件
    # /usr/trim/var/trim_sac/wallpaper/<uid>/<uuid>.jpg，并不读受 fnOS 监视的
    # /usr/trim/www/static/bg/wallpaper-1.webp。因此恢复桌面只需回退 DB 指针与
    # 自定义壁纸文件（见 restore_wp_pointers / restore_custom_wallpaper_files），
    # 切勿再覆盖 wallpaper-1.webp —— fnOS 自研 nginx 模块会监视该文件，一旦被改动
    # 就重启 trim_nginx（实测约 11 秒），导致网页「连接已断开」（这才是根因，而非
    # 本脚本 reload）。登录页壁纸（wallpaper-1.webp）保留上次 Bing 图不动以杜绝断连；
    # 桌面（用户实际看到的部分）已通过指针回退恢复为系统默认。
    unlock_file "$BG"; unlock_file "$TH"
    echo "完成。"
    ;;

  --desktop-only)
    # ------------------------------------------------------------
    # v1.5.5 零断连通道
    # fnOS 自研 nginx 模块会监视 /usr/trim/www 下的文件，一旦发现
    # wallpaper-1.webp 被改动就主动 "nginx will stop serve" 并重启
    # trim_nginx（实测约 11 秒），期间所有网页连接断开 —— 这就是
    # 「点一下就提示连接已断开」的根因，不是本脚本 reload 造成的。
    #
    # 桌面壁纸实际取自 trim_sac 的自定义壁纸行文件
    # /usr/trim/var/trim_sac/wallpaper/<uid>/<uuid>.jpg，该目录不受监视，
    # 实测覆盖它不会重启 nginx、也不会被系统还原。因此点选壁纸 / 秒级
    # 轮播一律走这里：只换桌面，登录页保持当天 Bing 图。
    # ------------------------------------------------------------
    SRC="${2:-}"
    [ -n "$SRC" ] && [ -f "$SRC" ] || die "源文件不存在: ${SRC:-（未指定）}"
    [ -n "$FFMPEG" ] || die "找不到 ffmpeg"
    db_available || die "数据库不可用，无法更新桌面壁纸"

    mkdir -p "$APPLIED_DIR" 2>/dev/null
    TMPJ="$APPLIED_DIR/desktop.jpg.tmp"
    transcode_jpg "$SRC" "$TMPJ" "$WIDTH" || die "桌面壁纸转码失败"
    mv -f "$TMPJ" "$APPLIED_DIR/desktop.jpg" || die "写入桌面壁纸副本失败"
    info "桌面图已生成 ($(stat -c%s "$APPLIED_DIR/desktop.jpg") 字节)"

    point_desktop_to_bing "$APPLIED_DIR/desktop.jpg"
    sync_custom_wallpaper_files "$APPLIED_DIR/desktop.jpg"
    printf '%s\n' "$(date '+%F %T')" > "$DATA_DIR/desktop_last"
    case "${SRC##*/}" in
      bing-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9].jpg) echo "bing"  > "$DATA_DIR/applied_src" ;;
      *) echo "photo" > "$DATA_DIR/applied_src" ;;
    esac
    echo "${SRC##*/}" > "$DATA_DIR/applied_detail"
    echo "完成：桌面壁纸已更新（未改动登录页文件，网页不会断连）。"
    ;;

  --live-webp)
    die "视频动态壁纸模块已移除（飞牛桌面不支持视频动态壁纸）"
    ;;

  *)
    SRC="$1"
    [ -f "$SRC" ] || die "源文件不存在: $SRC"
    [ -n "$FFMPEG" ] || die "找不到 ffmpeg，请先安装（Debian/Ubuntu: apt install ffmpeg）"
    [ -d "$BG_DIR" ] || die "目录不存在: $BG_DIR（请确认这是飞牛 fnOS 系统）"

    HAS_THUMB=1
    [ -d "$TH_DIR" ] || { HAS_THUMB=0; warn "缩略图目录不存在，跳过缩略图: $TH_DIR"; }

    echo "源图: $SRC"

    # 原始备份只做一次，且以 DATA_DIR 下这份为准。
    # 不能依赖同目录的 .orig（会被守护进程清理），也不能在 .orig 消失后
    # 重新备份 —— 那时目标文件可能已经是新图，会把新图误当原图。
    mkdir -p "$ORIG_DIR"

    # 若旧版（数据目录改名之前）已备份过，先迁移过来，
    # 否则会把当前可能已经是新图的文件误当系统原图备份。
    if [ ! -f "$ORIG_DIR/wallpaper-1.webp.orig" ] && [ -f "$LEGACY_ORIG_DIR/wallpaper-1.webp.orig" ]; then
      cp -a "$LEGACY_ORIG_DIR/wallpaper-1.webp.orig" "$ORIG_DIR/wallpaper-1.webp.orig" \
        && info "已迁移原有系统壁纸备份"
    fi
    if [ "$HAS_THUMB" -eq 1 ] && [ ! -f "$ORIG_DIR/thumb-1.webp.orig" ] && [ -f "$LEGACY_ORIG_DIR/thumb-1.webp.orig" ]; then
      cp -a "$LEGACY_ORIG_DIR/thumb-1.webp.orig" "$ORIG_DIR/thumb-1.webp.orig" \
        && info "已迁移原有缩略图备份"
    fi

    if [ ! -f "$ORIG_DIR/wallpaper-1.webp.orig" ] && [ -f "$BG" ]; then
      unlock_file "$BG"
      cp -a "$BG" "$ORIG_DIR/wallpaper-1.webp.orig" && info "已保存系统原始大图备份"
    fi
    if [ "$HAS_THUMB" -eq 1 ] && [ ! -f "$ORIG_DIR/thumb-1.webp.orig" ] && [ -f "$TH" ]; then
      unlock_file "$TH"
      cp -a "$TH" "$ORIG_DIR/thumb-1.webp.orig" && info "已保存系统原始缩略图备份"
    fi

    # 先把两张图都转码到 /tmp（耗时步骤，完全不碰系统目录）
    TMP_BG="/tmp/fnos-bg-$$-$(date +%s).webp"
    TMP_TH="/tmp/fnos-th-$$-$(date +%s).webp"
    trap 'rm -f "$TMP_BG" "$TMP_TH"' EXIT

    transcode "$SRC" "$TMP_BG" "$WIDTH" "$QUALITY" \
      || die "大图转码失败（源文件可能不是有效图片）"
    info "大图转码完成 ($(stat -c%s "$TMP_BG") 字节)"

    if [ "$HAS_THUMB" -eq 1 ]; then
      transcode "$SRC" "$TMP_TH" "$THUMB_WIDTH" "$THUMB_QUALITY" \
        || die "缩略图转码失败"
      info "缩略图转码完成 ($(stat -c%s "$TMP_TH") 字节)"
    fi

    # 再逐张「解锁 → 覆盖 → 立刻上锁」
    install_file "$TMP_BG" "$BG" || die "替换大图失败"
    info "大图已更新 ($(stat -c%s "$BG") 字节)"

    if [ "$HAS_THUMB" -eq 1 ]; then
      install_file "$TMP_TH" "$TH" || die "替换缩略图失败"
      info "缩略图已更新 ($(stat -c%s "$TH") 字节)"
    fi

    [ -n "$CHATTR" ] && info "已加文件锁" || warn "未找到 chattr，壁纸可能被系统还原"

    # 保存已应用副本，供看门狗在被系统还原后自动换回
    # （副本不能放壁纸同目录，会被守护进程清理）
    APPLIED_DIR="$DATA_DIR/applied"
    mkdir -p "$APPLIED_DIR"
    cp -f "$TMP_BG" "$APPLIED_DIR/wallpaper-1.webp"
    if [ "$HAS_THUMB" -eq 1 ]; then
      cp -f "$TMP_TH" "$APPLIED_DIR/thumb-1.webp"
    fi
    info "看门狗副本已更新"

    # 生成 jpg 副本（供自定义壁纸行与看门狗使用）
    if [ -n "$FFMPEG" ]; then
      "$FFMPEG" -y -hide_banner -loglevel error -i "$TMP_BG" -q:v 2 "$APPLIED_DIR/bing.jpg" 2>/dev/null \
        && info "jpg 副本已生成"
    fi

    # 桌面壁纸指向 Bing 自定义壁纸行（远程访问不走 CDN，见函数头注释）
    point_desktop_to_bing "$APPLIED_DIR/bing.jpg"

    # 同步自定义壁纸行背后的文件（对抗 DB/前端缓存旧指向）
    sync_custom_wallpaper_files

    case "${SRC##*/}" in
      bing-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9].jpg) echo "bing"  > "$DATA_DIR/applied_src" ;;
      *) echo "photo" > "$DATA_DIR/applied_src" ;;
    esac
    echo "${SRC##*/}" > "$DATA_DIR/applied_detail"

    # 不 reload nginx：桌面壁纸走动态接口、登录页文件 no-cache，
    # reload 会瞬间断开所有前端连接（表现为「无法访问」），没有必要。
    echo "完成。刷新页面（F5）即可看到新壁纸。"
    ;;
esac
