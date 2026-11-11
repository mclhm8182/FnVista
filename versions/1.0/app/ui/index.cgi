#!/bin/bash
# ============================================================
#  index.cgi - 飞牛视野 设置页（aerial-tv 风格 · 单页滚动 · AJAX）
#  由飞牛统一入口调用: /cgi/ThirdParty/fnos-bing-wallpaper/index.cgi/
#  系统已校验 NAS 登录态；所有参数均严格校验，不拼接执行用户输入。
#
#  信息架构（v1.6.6）:
#    大标题「当前壁纸」+ 动态主机名副标题
#    ├─ 当前壁纸卡片（显示正在显示的壁纸 + 状态）
#    ├─ 每日精选（Bing）：每日自动更新语义卡 + 最近 7 天网格（点选即固定该图）
#    ├─ 我的照片：添加照片（× 可移除），已选照片网格（点选即设为桌面）
#    └─ 其他：屏幕保护程序（开关 + 屏保时间 + 开始屏保）/ 自动轮换（开关 + 轮播间隔）/
#           查看运行日志 / 恢复系统原始壁纸
#
#  「每日精选」与「我的照片」是两个独立来源；「自动轮换」开启时按 applied_src
#   决定轮播哪个模块：
#    · 每日自动更新开  → 桌面跟随每日 Bing（同时关闭自动轮换）
#    · 自动轮换开      → 轮播「当前选中的模块」（applied_src=photo 轮我的照片，否则轮每日精选）
#
#  action:
#    save          保存 每日自动更新 / 自动轮换 / 轮播间隔 / 屏保开关 / 屏保时间
#    force         后台抓取今日 Bing 并应用（开启每日自动更新）
#    force_status  查询后台抓取状态
#    restore       恢复系统原始壁纸
#    apply&file=   固定显示某张 Bing 壁纸（暂停自动更新，零断连）
#    applysrc&p=   选用某张我的照片（零断连）
#    addfiles      勾选的本地图片加入「我的照片」（files= 换行分隔）
#    removefile    从「我的照片」移除一个本地文件（p=）
#    listdir&dir=  浏览本地目录（仅图片）
#    thumb         缩略图：file= 为 Bing，p= 为本地图片
#    curthumb      当前桌面壁纸缩略图（applied/desktop.jpg）
#    backfill      后台补齐最近 7 天 Bing 历史
#    logs          最近 200 行运行日志
#    ajax=1        任意动作附加此参数时返回 JSON（供前端 fetch）
# ============================================================
set -uo pipefail

APP="fnos-bing-wallpaper"
DATA_DIR="/var/lib/fnos-wallpaper"
WALL_DIR="$DATA_DIR/wallpapers"
THUMB_DIR="$DATA_DIR/thumbs"
APPLIED_DIR="$DATA_DIR/applied"
DAILY_BIN="/usr/local/sbin/bing-wallpaper-daily.sh"
ROTATE_BIN="/usr/local/sbin/fnos-wallpaper-rotate.sh"
SET_BIN="/usr/local/sbin/fnos-wallpaper-set.sh"
CRON_UPDATE_BIN="/usr/local/sbin/fnos-cron-update.sh"
LOG_FILE="/var/log/fnos-bing-wallpaper.log"
FFMPEG="$(command -v ffmpeg 2>/dev/null || true)"
FFPROBE="$(command -v ffprobe 2>/dev/null || true)"

# ---------- 输出辅助 ----------
json_escape() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g' | tr -d '\n\r\t'; }

json_out() {  # $1=ok(true/false) $2=msg
  printf 'Content-Type: application/json; charset=utf-8\r\n\r\n'
  printf '{"ok":%s,"msg":"%s"}\n' "$1" "$(json_escape "$2")"
}

json_raw() {
  printf 'Content-Type: application/json; charset=utf-8\r\n\r\n'
  printf '%s\n' "$1"
}

# ---------- 解析参数 ----------
sanitize() { printf '%s' "$1" | tr -cd 'a-zA-Z0-9=._:-'; }
QS="${QUERY_STRING:-}"
get_param() {
  local raw
  raw="$(printf '%s' "$QS" | tr '&' '\n' | grep "^$1=" | head -1 | cut -d= -f2-)"
  printf '%s' "$raw" | python3 -c 'import sys,urllib.parse;print(urllib.parse.unquote_plus(sys.stdin.read().strip("\r\n")))'
}

# 本地路径校验：get_param 已完成 URL 解码；此处校验 /volN/ 前缀或本应用数据目录（$DATA_DIR，用于本机上传） + 拒绝 ..
check_path() {
  local p="$1"
  [ -n "$p" ] || return 1
  case "$p" in
    /vol[0-9]/*) ;;
    "$DATA_DIR"/*) ;;
    *) return 1 ;;
  esac
  case "$p" in *".."*) return 1 ;; esac
  printf '%s' "$p"
}

# 媒体文件判断（仅图片；视频模块已移除）
media_type() {
  local le="${1##*.}"; le="$(printf '%s' "$le" | tr 'A-Z' 'a-z')"
  case "$le" in jpg|jpeg|png|webp|bmp) echo img ;; esac
}

# 从图片生成可应用 JPG：$1=源路径 $2=输出 jpg
render_jpg() {
  case "$(media_type "$1")" in
    img)
      if [ "${1##*.}" = "jpg" ] || [ "${1##*.}" = "jpeg" ]; then
        cp -f "$1" "$2" 2>/dev/null
      else
        [ -n "$FFMPEG" ] && "$FFMPEG" -y -hide_banner -loglevel error -i "$1" -frames:v 1 "$2" >/dev/null 2>&1
      fi
      ;;
  esac
  [ -s "$2" ]
}
# 屏保大图：把壁纸缩放到最长边 <=1920 并重新编码为 JPEG（远端 NAS 传输更快，避免屏保壁纸因体积过大加载不出导致黑屏）。
# 只缩不放（最长边>1920 才缩放，否则按原尺寸重编码），故小图不会被放大、也不会被无谓放大模糊。
# 注意：本机 ffmpeg（Rockchip 硬解版）无法解析 scale 里的 if()/min()/逗号表达式（实测报 "No option name near 'if(gt(ih'"），
# 故改用 ffprobe 取维度，在 shell 里判断横/竖向后套用无逗号的安全 scale 形式（scale=1920:-2 或 scale=-2:1920）。
# ffmpeg/ffprobe 不可用时返回非 0，由调用方直出原图兜底。
simg_gen() {
  local IN="$1" OUT="$2" dims W H VF
  [ -n "$FFMPEG" ] || return 1
  [ -n "$FFPROBE" ] || return 1
  mkdir -p "$(dirname "$OUT")" 2>/dev/null
  dims="$("$FFPROBE" -v error -select_streams v:0 -show_entries stream=width,height -of csv=p=0 "$IN" 2>/dev/null)"
  W="${dims%%,*}"; H="${dims##*,}"
  if { [ -n "$W" ] && [ "$W" -gt 1920 ]; } || { [ -n "$H" ] && [ "$H" -gt 1920 ]; }; then
    if [ "${W:-0}" -ge "${H:-0}" ]; then
      VF="scale=1920:-2"
    else
      VF="scale=-2:1920"
    fi
    timeout 30 "$FFMPEG" -y -hide_banner -loglevel error -i "$IN" -vf "$VF" -q:v 4 "$OUT" >/dev/null 2>&1
  else
    # 小图：仅重编码为 jpeg，不缩放（避免被放大模糊）
    timeout 30 "$FFMPEG" -y -hide_banner -loglevel error -i "$IN" -q:v 4 "$OUT" >/dev/null 2>&1
  fi
  [ -s "$OUT" ] && return 0 || return 1
}

urlquote() { python3 -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1],safe=""))' "$1" 2>/dev/null; }
html_escape() { python3 -c 'import sys,html;print(html.escape(sys.argv[1],quote=True))' "$1" 2>/dev/null; }

# ---------- 一次性迁移：旧版「自定义目录」→ 新版「勾选文件」 ----------
if [ ! -f "$DATA_DIR/.migrated_v154" ] && [ ! -s "$DATA_DIR/custom_files" ] \
   && [ -s "$DATA_DIR/custom_dir" ]; then
  CD="$(cat "$DATA_DIR/custom_dir" 2>/dev/null)"
  case "$CD" in
    /vol[0-9]/*)
      if [ -d "$CD" ]; then
        find "$CD" -maxdepth 2 -type f \( -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' \
             -o -iname '*.webp' -o -iname '*.bmp' \) -size +50k 2>/dev/null \
          | head -200 > "$DATA_DIR/custom_files" 2>/dev/null
      fi
      ;;
  esac
  : > "$DATA_DIR/.migrated_v154" 2>/dev/null
fi

IS_AJAX=0
case "$(sanitize "$(get_param ajax)")" in 1) IS_AJAX=1 ;; esac

OK="true"
MSG=""
if [ -n "$QS" ]; then
  ACTION="$(sanitize "$(get_param action)")"

  case "$ACTION" in
    save)
      BA="$(sanitize "$(get_param bing_auto)")"
      RO="$(sanitize "$(get_param rotate)")"
      RI="$(sanitize "$(get_param rotate_interval)")"
      case "$BA" in 1|0) ;; *) BA=0 ;; esac
      case "$RO" in 1|0) ;; *) RO=0 ;; esac
      case "$RI" in 0.5|1|5|15|30|60|120|240) ;; *) RI=30 ;; esac
      MERGE="$(sanitize "$(get_param merge)")"; case "$MERGE" in 1) ;; *) MERGE=0 ;; esac
      SS_ON="$(sanitize "$(get_param ss_on)")"; case "$SS_ON" in 1) ;; *) SS_ON=0 ;; esac
      SS_DELAY="$(sanitize "$(get_param ss_delay)")"; case "$SS_DELAY" in 1|5|10|20|30) ;; *) SS_DELAY=5 ;; esac
      WO="$(get_param weather_on)"; case "$WO" in 1) ;; *) WO=0 ;; esac
      WC="$(get_param weather_city)"; WC="$(printf '%s' "$WC" | tr -d '\n\r\t' | python3 -c 'import sys;print(sys.stdin.read()[:40])')"
      # 二选一互斥：开启「轮流换」即关闭每日自动更新，反之亦然
      [ "$RO" = "1" ] && BA=0
      [ "$BA" = "1" ] && RO=0
      # 合并轮播仅在「每日自动更新关闭」时生效；开启每日更新时强制关闭合并
      [ "$BA" = "1" ] && MERGE=0
      PREV_BA="$(cat "$DATA_DIR/bing_auto" 2>/dev/null || echo 0)"
      mkdir -p "$DATA_DIR" 2>/dev/null
      printf '%s\n' "$BA" > "$DATA_DIR/bing_auto"
      printf '%s\n' "$RO" > "$DATA_DIR/rotate"
      printf '%s\n' "$RI" > "$DATA_DIR/rotate_interval"
      printf '%s\n' "$MERGE" > "$DATA_DIR/merge"
      printf '%s\n' "$SS_ON" > "$DATA_DIR/ss_on"
      printf '%s\n' "$SS_DELAY" > "$DATA_DIR/ss_delay"
      printf '%s\n' "$WO" > "$DATA_DIR/weather_on"
      printf '%s\n' "$WC" > "$DATA_DIR/weather_city"
      [ "$BA" = "1" ] && rm -f "$DATA_DIR/bing_selected" 2>/dev/null
      # 记录「当前选中的模块」，供自动轮换（源感知）判断轮播每日精选还是我的照片
      [ "$BA" = "1" ] && printf 'bing\n' > "$DATA_DIR/applied_src" 2>/dev/null
      [ "$RO" = "1" ] && [ ! -s "$DATA_DIR/applied_src" ] && printf 'bing\n' > "$DATA_DIR/applied_src" 2>/dev/null
      if [ -x "$CRON_UPDATE_BIN" ] && bash "$CRON_UPDATE_BIN" >/dev/null 2>&1; then
        if [ "$BA" = "1" ]; then
          if [ "$PREV_BA" != "1" ]; then
            # 由「未开启」切到「开启」时强制应用当日 Bing：每日脚本有「同一天不重复处理」
            # 逻辑，今天早些时候若已应用过会被判跳过，导致恢复系统壁纸后重开每日精选
            # 不立即生效（要等到次日）。--force 绕过同日去重，桌面壁纸即时切回。
            nohup bash "$DAILY_BIN" --force >/dev/null 2>&1 &
            MSG="已开启每日自动更新，正在应用今日壁纸"
          else
            MSG="每日自动更新已开启"
          fi
        elif [ "$RO" = "1" ]; then
          setsid nohup bash "$ROTATE_BIN" --ensure >/dev/null 2>&1 < /dev/null &
          MSG="已开启自动轮换，正在切换当前模块壁纸"
        else
          MSG="设置已保存"
        fi
      else
        MSG="保存失败：无法重建定时任务（需要 root 权限）"; OK="false"
      fi
      # ---- 维护检修：开启「每日自动更新」时确保「每日精选」凑够 7 张（不够则后台补齐）----
      if [ "$OK" = "true" ] && [ "$BA" = "1" ]; then
        CUR_N="$(ls -1 "$WALL_DIR"/bing-*.jpg 2>/dev/null | wc -l | tr -d ' ')"
        if [ "${CUR_N:-0}" -lt 7 ]; then
          BF_ST="$(cat "$DATA_DIR/backfill.status" 2>/dev/null || echo idle)"
          if [ "$BF_ST" != "running" ]; then
            printf 'running\n' > "$DATA_DIR/backfill.status" 2>/dev/null
            nohup bash "$DAILY_BIN" --backfill >> "$LOG_FILE" 2>&1 &
            MSG="${MSG}；正在补齐每日精选至 7 张"
          fi
        fi
      fi
      ;;

    force)
      if [ ! -x "$DAILY_BIN" ]; then
        MSG="找不到 $DAILY_BIN"; OK="false"
      else
        printf '1\n' > "$DATA_DIR/bing_auto"
        printf '0\n' > "$DATA_DIR/rotate"
        rm -f "$DATA_DIR/bing_selected" 2>/dev/null
        ST="$(cat "$DATA_DIR/force.status" 2>/dev/null || echo idle)"
        ST_T="$(stat -c %Y "$DATA_DIR/force.status" 2>/dev/null || echo 0)"
        if [ "$ST" = "running" ] && [ $(( $(date +%s) - ST_T )) -lt 120 ]; then
          MSG="上一次抓取还在进行中，请稍候"; OK="false"
        else
          printf 'running\n' > "$DATA_DIR/force.status" 2>/dev/null
          : > "$DATA_DIR/force.out" 2>/dev/null
          setsid nohup bash -c 'bash "$1" --force > "$2" 2>&1; printf "done\n" > "$3"' _ "$DAILY_BIN" "$DATA_DIR/force.out" "$DATA_DIR/force.status" >/dev/null 2>&1 < /dev/null &
          MSG="已开始更新今日壁纸"
        fi
      fi
      ;;

    force_status)
      FST="$(cat "$DATA_DIR/force.status" 2>/dev/null || echo idle)"
      FOUT="$(tail -1 "$DATA_DIR/force.out" 2>/dev/null || true)"
      printf 'Content-Type: application/json; charset=utf-8\r\n\r\n'
      printf '{"ok":true,"state":"%s","msg":"%s"}\n' \
        "$(json_escape "$FST")" "$(json_escape "$FOUT")"
      exit 0
      ;;

    restore)
      if [ -x "$SET_BIN" ]; then
        if bash "$SET_BIN" --restore >/dev/null 2>&1; then
          # 回归系统默认壁纸状态：移除本项目接管痕迹（不删存储的壁纸文件），
          # 并关闭关联开关（轮流换 / 每日自动更新 / 已选图片高亮），直至用户手动重开。
          rm -f "$DATA_DIR/applied_src" "$DATA_DIR/applied_detail" "$DATA_DIR/bing_selected" 2>/dev/null
          printf '0\n' > "$DATA_DIR/bing_auto" 2>/dev/null
          printf '0\n' > "$DATA_DIR/rotate" 2>/dev/null
          # 重建定时任务：关闭后每日任务仅在 bing_auto=1 时运行（看门狗在 applied 目录
          # 被清空后也会自动失效），确保系统壁纸不会被再次覆盖回来。
          if [ -x "$CRON_UPDATE_BIN" ] && ! bash "$CRON_UPDATE_BIN" >/dev/null 2>&1; then
            MSG="已恢复系统默认壁纸（定时任务清理失败，可重启应用兜底）"
          else
            MSG="已恢复系统默认壁纸，并关闭每日更换与轮播（屏保不受影响，可随时重开）"
          fi
        else
          MSG="恢复失败：可能没有原始壁纸备份"; OK="false"
        fi
      else
        MSG="找不到 $SET_BIN"; OK="false"
      fi
      ;;

    apply)
      F="$(sanitize "$(get_param file)")"
      if printf '%s' "$F" | grep -qE '^bing-[0-9]{8}\.jpg$' && [ -f "$WALL_DIR/$F" ]; then
        if [ -x "$SET_BIN" ]; then
          # 点选某张 Bing 壁纸：固定显示它，停止每日自动更新，并停掉我的照片轮播（互斥）
          printf '0\n' > "$DATA_DIR/bing_auto"
          printf '%s\n' "$F" > "$DATA_DIR/bing_selected"
          printf '0\n' > "$DATA_DIR/rotate"
          if bash "$SET_BIN" --desktop-only "$WALL_DIR/$F" >/dev/null 2>&1; then
            MSG="已固定显示 ${F#bing-} 的壁纸（每日自动更新已暂停）"
          else
            MSG="应用失败：壁纸设置脚本执行出错"; OK="false"
          fi
        else
          MSG="找不到 $SET_BIN"; OK="false"
        fi
      else
        MSG="文件不存在或名称不合法"; OK="false"
      fi
      ;;

    applysrc)
      P="$(check_path "$(get_param p)")" || P=""
      if [ -z "$P" ] || [ ! -f "$P" ]; then
        MSG="文件不存在或路径不合法"; OK="false"
      elif [ ! -x "$SET_BIN" ]; then
        MSG="找不到 $SET_BIN"; OK="false"
      else
        printf '0\n' > "$DATA_DIR/bing_auto"
      TMPJ="$DATA_DIR/.apply_tmp.jpg"
        if [ "$(media_type "$P")" = "img" ] && render_jpg "$P" "$TMPJ"; then
          if bash "$SET_BIN" --desktop-only "$TMPJ" >/dev/null 2>&1; then
            # 记录真实来源（SET_BIN 只拿到临时文件，会记成 .apply_tmp.jpg，
            # 导致「我的照片」高亮绿框对不上）。这里用原始路径覆盖，供前端比对高亮。
            printf 'photo\n' > "$DATA_DIR/applied_src"
            printf '%s\n' "$P" > "$DATA_DIR/applied_detail"
            MSG="已应用：${P##*/}"
          else
            MSG="应用失败：壁纸设置脚本执行出错"; OK="false"
          fi
          rm -f "$TMPJ" 2>/dev/null
        else
          MSG="无法读取该文件或格式不支持"; OK="false"
        fi
      fi
      ;;

    addfiles)
      RAW="$(get_param files)"
      TMP="$DATA_DIR/.custom_files.add"
      : > "$TMP" 2>/dev/null
      ADDED=0
      if [ -n "$RAW" ]; then
        printf '%s\n' "$RAW" | while IFS= read -r line; do
          [ -n "$line" ] || continue
          P="$(check_path "$line")" || continue
          [ -f "$P" ] || continue
          [ "$(media_type "$P")" = "img" ] || continue
          printf '%s\n' "$P"
        done > "$TMP" 2>/dev/null
        ADDED="$(wc -l < "$TMP" 2>/dev/null || echo 0)"
      fi
      if [ "$ADDED" -eq 0 ]; then
        MSG="没有可添加的图片文件"; OK="false"; rm -f "$TMP" 2>/dev/null
      else
        EXIST_N=$( [ -s "$DATA_DIR/custom_files" ] && wc -l < "$DATA_DIR/custom_files" || echo 0 )
        if [ $((EXIST_N + ADDED)) -gt 8 ]; then
          MSG="最多保留 8 张照片用于轮换（当前 $EXIST_N 张，本次多选 $ADDED 张）"; OK="false"; rm -f "$TMP" 2>/dev/null
        else
          mkdir -p "$DATA_DIR" 2>/dev/null
          { [ -f "$DATA_DIR/custom_files" ] && cat "$DATA_DIR/custom_files"; cat "$TMP"; } \
            | awk '!seen[$0]++' > "$DATA_DIR/custom_files.new" 2>/dev/null
          mv -f "$DATA_DIR/custom_files.new" "$DATA_DIR/custom_files" 2>/dev/null
          rm -f "$TMP" 2>/dev/null
          MSG="已加入 $ADDED 张照片"
        fi
      fi
      ;;

    upload)
      # 本机（电脑/手机）上传图片：multipart/form-data，字段 file(二进制) + filename(原文件名)
      # 服务端解析后转码为 JPEG 存入 $DATA_DIR/uploads/，并追加到 custom_files（与 NAS 照片同等对待）
      # 关键修复：绝不能把脚本用 heredoc 喂给 python 的 stdin（python3 - <<'PY'）——CGI 的 POST 正文同样在 stdin 上，
      # 会被 heredoc 吃掉，导致解析到的 body 永远为空、上传永远失败。改为先把脚本写到临时文件再执行，POST 正文留在 stdin 上。
      TMPPY="$(mktemp /tmp/fnv_up.XXXXXX.py)"
      cat > "$TMPPY" <<'PY'
import os,sys,json,subprocess,tempfile,re
data_dir=sys.argv[1]
cl=int(os.environ.get('CONTENT_LENGTH','0') or 0)
ct=os.environ.get('CONTENT_TYPE','') or ''
m=re.search(r'boundary=([^;]+)',ct)
if not m or not cl:
    print(json.dumps({'ok':False,'msg':'无上传数据'})); sys.exit(0)
boundary=m.group(1).strip().strip('"').encode()
try:
    raw=sys.stdin.buffer.read(cl)
except Exception:
    print(json.dumps({'ok':False,'msg':'读取上传失败'})); sys.exit(0)
updir=os.path.join(data_dir,'uploads')
os.makedirs(updir,exist_ok=True)
custom=os.path.join(data_dir,'custom_files')
existing=[]; seen=set()
if os.path.isfile(custom):
    for l in open(custom,encoding='utf-8',errors='replace'):
        l=l.strip()
        if l and l not in seen:
            seen.add(l); existing.append(l)
added=[]; errs=[]
for part in raw.split(b'--'+boundary):
    if not part or part in (b'--',b'\r\n'): continue
    if part.startswith(b'--'): break
    if b'\r\n\r\n' not in part: continue
    head,_,bdy=part.partition(b'\r\n\r\n')
    bdy=bdy.rstrip(b'\r\n')
    if b'filename=' not in head: continue
    fm=re.search(r'filename="?([^";]+)"?',head.decode('utf-8','replace'))
    name=fm.group(1) if fm else 'upload'
    base=re.sub(r'[^A-Za-z0-9._-]','_',os.path.basename(name)) or 'upload'
    ext=os.path.splitext(base)[1].lower() or '.jpg'
    if len(bdy) > 30*1024*1024:  # 单张上限 30MB，过大直接跳过避免占用带宽/拖慢缩略图
        errs.append(name+'(超过30MB)'); continue
    tmp=None
    try:
        tf=tempfile.NamedTemporaryFile(suffix=ext,delete=False); tmp=tf.name; tf.write(bdy); tf.close()
        out=os.path.join(updir,os.path.splitext(base)[0]+'.jpg')
        i=1
        while os.path.exists(out):
            out=os.path.join(updir,'%s_%d.jpg'%(os.path.splitext(base)[0],i)); i+=1
        r=subprocess.run(['ffmpeg','-y','-hide_banner','-loglevel','error','-i',tmp,'-q:v','4',out],capture_output=True)
        if os.path.isfile(out) and os.path.getsize(out)>0:
            ap=os.path.abspath(out)
            if ap not in seen:
                seen.add(ap); existing.append(ap); added.append(ap)
        else:
            errs.append(name)
    except Exception:
        errs.append(name)
    finally:
        if tmp and os.path.exists(tmp): os.remove(tmp)
with open(custom,'w',encoding='utf-8') as f:
    for p in existing: f.write(p+'\n')
msg=('已添加 %d 张本机照片'%len(added)) if added else '未添加（格式不支持或重复）'
if errs: msg+='，%d 张失败'%len(errs)
print(json.dumps({'ok':bool(added),'msg':msg,'added':added},ensure_ascii=False))
PY
      UP_OUT="$(WALL_DIR="$WALL_DIR" DATA_DIR="$DATA_DIR" CONTENT_LENGTH="${CONTENT_LENGTH:-0}" CONTENT_TYPE="$CONTENT_TYPE" python3 "$TMPPY" "$DATA_DIR")"
      rm -f "$TMPPY"
      printf 'Content-Type: application/json; charset=utf-8\r\n\r\n'
      printf '%s\n' "$UP_OUT"
      exit 0
      ;;

    removefile)
      P="$(check_path "$(get_param p)")" || P=""
      if [ -z "$P" ]; then
        MSG="路径不合法"; OK="false"
      elif [ ! -s "$DATA_DIR/custom_files" ]; then
        MSG="我的照片里没有本地文件"; OK="false"
      else
        grep -vxF -- "$P" "$DATA_DIR/custom_files" > "$DATA_DIR/.custom_files.rm" 2>/dev/null
        mv -f "$DATA_DIR/.custom_files.rm" "$DATA_DIR/custom_files" 2>/dev/null
        MSG="已从「我的照片」移除：${P##*/}"
      fi
      ;;

    listdir)
      D="$(check_path "$(get_param dir)")" || D=""
      if [ -z "$D" ] || [ ! -d "$D" ]; then
        json_raw '{"ok":false,"msg":"目录不存在或路径不合法"}'
        exit 0
      fi
      LJ="$(python3 -c '
import os,sys,json
p=sys.argv[1]
IMG=(".jpg",".jpeg",".png",".webp",".bmp")
dirs=[];files=[]
try:
    for e in sorted(os.listdir(p), key=str.lower):
        if e.startswith("."): continue
        fp=os.path.join(p,e)
        if os.path.isdir(fp): dirs.append(e)
        elif e.lower().endswith(IMG): files.append(e)
except Exception: pass
print(json.dumps({"ok":True,"dir":p,"kind":"img","dirs":dirs,"files":files},ensure_ascii=False))
' "$D" 2>/dev/null)" || LJ='{"ok":false,"msg":"读取目录失败"}'
      json_raw "$LJ"
      exit 0
      ;;

    thumb)
      F="$(sanitize "$(get_param file)")"
      if printf '%s' "$F" | grep -qE '^bing-[0-9]{8}\.jpg$' && [ -f "$WALL_DIR/$F" ]; then
        mkdir -p "$THUMB_DIR" 2>/dev/null
        TH="$THUMB_DIR/${F%.jpg}_350.jpg"
        if [ -n "$FFMPEG" ] && { [ ! -s "$TH" ] || [ "$WALL_DIR/$F" -nt "$TH" ]; }; then
          "$FFMPEG" -y -hide_banner -loglevel error -i "$WALL_DIR/$F" -vf "scale=350:-2" -q:v 4 "$TH" >/dev/null 2>&1
        fi
        if [ -s "$TH" ]; then
          printf 'Content-Type: image/jpeg\r\nCache-Control: max-age=86400\r\n\r\n'; cat "$TH"; exit 0
        fi
        # 兜底：直接出 4K 原图（体积可控），不要因缩略图失败报错
        printf 'Content-Type: image/jpeg\r\nCache-Control: max-age=86400\r\n\r\n'; cat "$WALL_DIR/$F"; exit 0
      fi
      P="$(check_path "$(get_param p)")" || P=""
      if [ -z "$P" ] || [ ! -f "$P" ]; then
        printf 'Status: 404\r\nContent-Type: text/plain\r\n\r\nnot found'; exit 0
      fi
      mkdir -p "$THUMB_DIR" 2>/dev/null
      KEY="$(printf '%s|%s' "$P" "$(stat -c '%Y' "$P" 2>/dev/null)" | md5sum | cut -d" " -f1)"
      TH="$THUMB_DIR/src-$KEY.jpg"
      # 始终用 ffmpeg 重新缩放到 350px（绝不 copy 原图当缩略图）；大图解码慢时放宽超时
      if [ -n "$FFMPEG" ] && { [ ! -s "$TH" ] || [ "$P" -nt "$TH" ]; }; then
        timeout 30 "$FFMPEG" -y -hide_banner -loglevel error -i "$P" -vf "scale=350:-2" -q:v 4 "$TH" >/dev/null 2>&1
      fi
      if [ -s "$TH" ]; then
        printf 'Content-Type: image/jpeg\r\nCache-Control: max-age=86400\r\n\r\n'; cat "$TH"; exit 0
      fi
      # 生成失败（ffmpeg 不可用 / 解码超时 / 非图片）：用占位图，绝不回退原图（避免大图当缩略图拖慢页面、甚至撑爆请求）
      printf 'Content-Type: image/svg+xml; charset=utf-8\r\nCache-Control: max-age=60\r\n\r\n'
      printf '%s' '<svg xmlns="http://www.w3.org/2000/svg" width="320" height="180" viewBox="0 0 320 180"><rect width="320" height="180" fill="#e5e5ea"/><circle cx="160" cy="90" r="26" fill="#8e8e93"/><path d="M153 77l23 13-23 13z" fill="#fff"/></svg>'
      exit 0
      ;;

    curthumb)
      if [ -s "$APPLIED_DIR/desktop.jpg" ]; then
        printf 'Content-Type: image/jpeg\r\nCache-Control: no-cache\r\n\r\n'; cat "$APPLIED_DIR/desktop.jpg"; exit 0
      fi
      # 每日自动更新走完整换图通道，落盘为 applied/bing.jpg（而非 desktop.jpg）
      if [ -s "$APPLIED_DIR/bing.jpg" ]; then
        printf 'Content-Type: image/jpeg\r\nCache-Control: no-cache\r\n\r\n'; cat "$APPLIED_DIR/bing.jpg"; exit 0
      fi
      # 未接管时（如刚恢复系统壁纸）回退显示系统壁纸原图，避免卡片空白
      SYS_ORIG="$DATA_DIR/orig/wallpaper-1.webp.orig"
      SYS_BG="/usr/trim/www/static/bg/wallpaper-1.webp"
      if [ -s "$SYS_ORIG" ]; then
        printf 'Content-Type: image/webp\r\nCache-Control: no-cache\r\n\r\n'; cat "$SYS_ORIG"; exit 0
      elif [ -s "$SYS_BG" ]; then
        printf 'Content-Type: image/webp\r\nCache-Control: no-cache\r\n\r\n'; cat "$SYS_BG"; exit 0
      fi
      printf 'Status: 404\r\nContent-Type: text/plain\r\n\r\nnone'; exit 0
      ;;
    full)
      F="$(sanitize "$(get_param file)")"
      if printf '%s' "$F" | grep -qE '^bing-[0-9]{8}\.jpg$' && [ -f "$WALL_DIR/$F" ]; then
        printf 'Content-Type: image/jpeg\r\nCache-Control: max-age=86400\r\n\r\n'; cat "$WALL_DIR/$F"; exit 0
      fi
      P="$(check_path "$(get_param p)")" || P=""
      if [ -n "$P" ] && [ -f "$P" ]; then
        printf 'Content-Type: image/jpeg\r\nCache-Control: max-age=86400\r\n\r\n'; cat "$P"; exit 0
      fi
      printf 'Status: 404\r\nContent-Type: text/plain\r\n\r\nnot found'; exit 0
      ;;
    saverimg)
      # 屏保专用：返回压缩后的壁纸（最长边<=1920 的 JPEG），远端 NAS 传输更快，避免黑屏
      F="$(sanitize "$(get_param file)")"
      if printf '%s' "$F" | grep -qE '^bing-[0-9]{8}\.jpg$' && [ -f "$WALL_DIR/$F" ]; then
        mkdir -p "$THUMB_DIR" 2>/dev/null
        TH="$THUMB_DIR/saver-${F%.jpg}.jpg"
        [ -s "$TH" ] || simg_gen "$WALL_DIR/$F" "$TH" || true
        if [ -s "$TH" ]; then
          printf 'Content-Type: image/jpeg\r\nCache-Control: max-age=86400\r\n\r\n'; cat "$TH"; exit 0
        fi
        printf 'Content-Type: image/jpeg\r\nCache-Control: max-age=86400\r\n\r\n'; cat "$WALL_DIR/$F"; exit 0
      fi
      P="$(check_path "$(get_param p)")" || P=""
      if [ -n "$P" ] && [ -f "$P" ]; then
        mkdir -p "$THUMB_DIR" 2>/dev/null
        KEY="$(printf '%s|%s' "$P" "$(stat -c '%Y' "$P" 2>/dev/null)" | md5sum | cut -d" " -f1)"
        TH="$THUMB_DIR/saver-src-$KEY.jpg"
        [ -s "$TH" ] || simg_gen "$P" "$TH" || true
        if [ -s "$TH" ]; then
          printf 'Content-Type: image/jpeg\r\nCache-Control: max-age=86400\r\n\r\n'; cat "$TH"; exit 0
        fi
        printf 'Content-Type: image/jpeg\r\nCache-Control: max-age=86400\r\n\r\n'; cat "$P"; exit 0
      fi
      printf 'Status: 404\r\nContent-Type: text/plain\r\n\r\nnot found'; exit 0
      ;;

    logs)
      LOGJSON="$(python3 -c '
import json
t=""
try:
    with open("/var/log/fnos-bing-wallpaper.log", errors="replace") as f:
        t="".join(f.read().splitlines(True)[-200:])
except Exception:
    t=""
if not t.strip(): t="(暂无日志)"
print(json.dumps({"ok":True,"logs":t},ensure_ascii=False))
' 2>/dev/null)" || LOGJSON=""
      printf 'Content-Type: application/json; charset=utf-8\r\n\r\n'
      if [ -n "$LOGJSON" ]; then printf '%s\n' "$LOGJSON"
      else printf '{"ok":false,"logs":"读取失败"}\n'; fi
      exit 0
      ;;

    backfill)
      if [ ! -x "$DAILY_BIN" ]; then
        MSG="找不到 $DAILY_BIN"; OK="false"
      else
        printf 'running\n' > "$DATA_DIR/backfill.status" 2>/dev/null
        nohup bash "$DAILY_BIN" --backfill >> "$LOG_FILE" 2>&1 &
        MSG="已开始补齐最近 7 天每日精选"
      fi
      ;;
    backfill_status)
      ST="$(cat "$DATA_DIR/backfill.status" 2>/dev/null || echo idle)"
      printf 'Content-Type: application/json; charset=utf-8\r\n\r\n'
      printf '{"ok":true,"state":"%s"}\n' "$(json_escape "$ST")"
      exit 0
      ;;
    saverjson)
      # 屏保轮播所需的「每日精选 + 我的照片」图片列表（实时读取，避免缓存过期）
      # 每项含 url/title/desc/kind：bing 项带标题与说明（用于屏保下方字幕），photo 项不带（用户图片不需要字幕）
      # url 统一走压缩端点 ?action=saverimg，远端 NAS 传输更快、避免屏保黑屏
      SJ="$(WALL_DIR="$WALL_DIR" DATA_DIR="$DATA_DIR" python3 - <<'PY'
import json,os,urllib.parse
wall=os.environ.get('WALL_DIR',''); data=os.environ.get('DATA_DIR','')
bing=[]; photo=[]
try:
    for f in sorted(os.listdir(wall)):
        if f.startswith('bing-') and f.endswith('.jpg'):
            title=''; desc=''
            mf=os.path.join(wall,f+'.meta')
            if os.path.isfile(mf):
                try:
                    parts=open(mf,encoding='utf-8',errors='replace').read().split('\t')
                    title=(parts[0] if len(parts)>0 else '').strip()
                    desc=(parts[1] if len(parts)>1 else '').strip()
                except Exception: pass
            if not title: title=f[5:len(f)-4]
            if not desc: desc='Bing 每日壁纸'
            bing.append({'url':'?action=saverimg&file='+urllib.parse.quote(f),'title':title,'desc':desc,'kind':'bing'})
except Exception: pass
try:
    for line in open(os.path.join(data,'custom_files')):
        p=line.strip()
        if p and os.path.isfile(p):
            # 路径含空格/中文/& 等字符时必须编码，否则 ?action=saverimg&p= 会被截断成 404（屏保照片间歇性黑屏）
            photo.append({'url':'?action=saverimg&p='+urllib.parse.quote(p, safe=''),'title':'','desc':'','kind':'photo'})
except Exception: pass
print(json.dumps({'bing':bing,'photo':photo},ensure_ascii=False))
PY
)"
      printf 'Content-Type: application/json; charset=utf-8\r\n\r\n'
      printf '%s\n' "$SJ"
      exit 0
      ;;
  esac
fi

if [ "$IS_AJAX" -eq 1 ]; then
  json_out "$OK" "$MSG"
  exit 0
fi

# ---------- HTML 页头 ----------
header() { echo "Content-Type: text/html; charset=utf-8"; echo ""; }

# ---------- 读取当前状态 ----------
NAS="$(hostname 2>/dev/null || echo NAS)"
BA="1";  [ -f "$DATA_DIR/bing_auto" ]       && BA="$(cat "$DATA_DIR/bing_auto" 2>/dev/null)";       case "$BA" in 1) ;; *) BA=0 ;; esac
ROTATE="0"; [ -f "$DATA_DIR/rotate" ]        && ROTATE="$(cat "$DATA_DIR/rotate" 2>/dev/null)";      case "$ROTATE" in 1) ;; *) ROTATE=0 ;; esac
RINT="30"; [ -f "$DATA_DIR/rotate_interval" ] && RINT="$(cat "$DATA_DIR/rotate_interval" 2>/dev/null)"; case "$RINT" in 0.5|1|5|15|30|60|120|240) ;; *) RINT=30 ;; esac
MERGE="0"; [ -f "$DATA_DIR/merge" ] && MERGE="$(cat "$DATA_DIR/merge" 2>/dev/null)"; case "$MERGE" in 1) ;; *) MERGE=0 ;; esac
SS_ON="0";   [ -f "$DATA_DIR/ss_on" ]    && SS_ON="$(cat "$DATA_DIR/ss_on" 2>/dev/null)";    case "$SS_ON" in 1) ;; *) SS_ON=0 ;; esac
SS_DELAY="5";[ -f "$DATA_DIR/ss_delay" ] && SS_DELAY="$(cat "$DATA_DIR/ss_delay" 2>/dev/null)"; case "$SS_DELAY" in 1|5|10|20|30) ;; *) SS_DELAY=5 ;; esac
WEATHER_ON="1"; [ -f "$DATA_DIR/weather_on" ] && WEATHER_ON="$(cat "$DATA_DIR/weather_on" 2>/dev/null)"; case "$WEATHER_ON" in 1) ;; *) WEATHER_ON=0 ;; esac
WEATHER_CITY=""; [ -f "$DATA_DIR/weather_city" ] && WEATHER_CITY="$(cat "$DATA_DIR/weather_city" 2>/dev/null | tr -d '\n\r')"
WEATHER_CITY_ESC="$(printf '%s' "$WEATHER_CITY" | python3 -c 'import sys,html;print(html.escape(sys.stdin.read(),quote=True))')"
WEATHER_CITY_JS="$(printf '%s' "$WEATHER_CITY" | python3 -c 'import sys,json;print(json.dumps(sys.stdin.read())[1:-1])')"
CUR_SRC="";  [ -f "$DATA_DIR/applied_src" ]    && CUR_SRC="$(cat "$DATA_DIR/applied_src" 2>/dev/null)"
CUR_DETAIL=""; [ -f "$DATA_DIR/applied_detail" ] && CUR_DETAIL="$(cat "$DATA_DIR/applied_detail" 2>/dev/null)"
BSEL=""; [ -f "$DATA_DIR/bing_selected" ] && BSEL="$(cat "$DATA_DIR/bing_selected" 2>/dev/null)"

# ---------- 构建每日精选网格（第一张=每日自动更新语义卡片，其后为固定某张） ----------
LATEST_BING_F="$(ls -1 "$WALL_DIR"/bing-*.jpg 2>/dev/null | sort | tail -1 | xargs -r basename 2>/dev/null)"
BING_AUTO_HTML=""
AUTO_CUR=""; [ "$BA" = "1" ] && AUTO_CUR=" cur"
if [ -n "$LATEST_BING_F" ]; then
  BING_AUTO_HTML="<div class=\"bcard auto$AUTO_CUR\" onclick=\"setAuto()\">
      <img src=\"?action=thumb&file=$LATEST_BING_F\" alt=\"每日自动更新\" loading=\"lazy\">
      <div class=\"badge\">自动</div>
      <div class=\"bcap\"><div class=\"bt\">每日自动更新</div><div class=\"bd\">每天自动换上最新 Bing 壁纸</div></div>
    </div>"
fi

BING_HTML=""
if [ -d "$WALL_DIR" ]; then
  for f in $(ls "$WALL_DIR" 2>/dev/null | grep -E '^bing-[0-9]{8}\.jpg$' | sort -r); do
    title=""; [ -f "$WALL_DIR/$f.meta" ] && title="$(cut -f1 "$WALL_DIR/$f.meta" 2>/dev/null | head -1)"
    desc="";  [ -f "$WALL_DIR/$f.meta" ] && desc="$(cut -f2 "$WALL_DIR/$f.meta" 2>/dev/null | head -1)"
    [ -z "$title" ] && title="${f#bing-}"; title="${title%.jpg}"
    [ -z "$desc" ] && desc="Bing 每日壁纸"
    cur=""; [ "$CUR_SRC" = "bing" ] && [ "$CUR_DETAIL" = "$f" ] && [ "$BA" = "0" ] && cur=" cur"
    BING_HTML="$BING_HTML
    <div class=\"bcard$cur\" onclick=\"chooseBing('$f')\">
      <img src=\"?action=thumb&file=$f\" alt=\"$title\" loading=\"lazy\">
      <div class=\"bcap\"><div class=\"bt\">$title</div><div class=\"bd\">$desc</div></div>
    </div>"
  done
fi
[ -z "$BING_HTML" ] && BING_HTML="<div class=\"empty\">每日精选还是空的，首次安装或重启后会自动填充最近 7 天，也可稍后在设置里手动补齐。</div>"

PHOTO_HTML=""
PHOTO_N=0
if [ -s "$DATA_DIR/custom_files" ]; then
  while IFS= read -r p; do
    [ -n "$p" ] || continue
    [ -f "$p" ] || continue
    [ "$(media_type "$p")" = "img" ] || continue
    PHOTO_N=$((PHOTO_N+1))
    ENC="$(urlquote "$p")"
    NM="$(html_escape "${p##*/}")"
    cur=""; [ "$CUR_SRC" = "photo" ] && [ "$CUR_DETAIL" = "$p" ] && cur=" cur"
    PHOTO_HTML="$PHOTO_HTML
    <div class=\"pcard$cur\" data-p=\"$ENC\" onclick=\"choosePhoto(this.getAttribute('data-p'))\">
      <img src=\"?action=thumb&p=$ENC\" alt=\"$NM\" loading=\"lazy\">
      <button class=\"del\" data-p=\"$ENC\" title=\"移除\" onclick=\"event.stopPropagation();removePhoto(this.getAttribute('data-p'))\">×</button>
      <div class=\"pn\">$NM</div>
    </div>"
  done < "$DATA_DIR/custom_files"
fi
[ -z "$PHOTO_HTML" ] && PHOTO_HTML="<div class=\"empty\">还没有添加照片，点上方按钮从 NAS 里选最多 8 张用于轮换。</div>"

# ---------- 屏保所需图片列表（压缩 URL + Bing 标题/说明） ----------
SAVER_JSON="$(WALL_DIR="$WALL_DIR" DATA_DIR="$DATA_DIR" python3 - <<'PY'
import json,os,urllib.parse
wall=os.environ.get('WALL_DIR',''); data=os.environ.get('DATA_DIR','')
bing=[]; photo=[]
try:
    for f in sorted(os.listdir(wall)):
        if f.startswith('bing-') and f.endswith('.jpg'):
            title=''; desc=''
            mf=os.path.join(wall,f+'.meta')
            if os.path.isfile(mf):
                try:
                    parts=open(mf,encoding='utf-8',errors='replace').read().split('\t')
                    title=(parts[0] if len(parts)>0 else '').strip()
                    desc=(parts[1] if len(parts)>1 else '').strip()
                except Exception: pass
            if not title: title=f[5:len(f)-4]
            if not desc: desc='Bing 每日壁纸'
            bing.append({'url':'?action=saverimg&file='+urllib.parse.quote(f),'title':title,'desc':desc,'kind':'bing'})
except Exception: pass
try:
    for line in open(os.path.join(data,'custom_files')):
        p=line.strip()
        if p and os.path.isfile(p):
            photo.append({'url':'?action=saverimg&p='+urllib.parse.quote(p, safe=''),'title':'','desc':'','kind':'photo'})
except Exception: pass
print(json.dumps({'bing':bing,'photo':photo},ensure_ascii=False))
PY
)"
[ -z "$SAVER_JSON" ] && SAVER_JSON='{"bing":[],"photo":[]}'
# JS 安全转义：把单引号转义，避免写入 <script> 时破坏字符串
SAVER_JSON_JS="$(printf '%s' "$SAVER_JSON" | sed "s/'/\\\\'/g")"

# ---------- 当前壁纸状态文案 ----------
if [ "$ROTATE" = "1" ]; then
  CUR_STATUS="自动轮换"
elif [ "$CUR_SRC" = "photo" ]; then
  CUR_STATUS="我的图片"
elif [ "$CUR_SRC" = "bing" ]; then
  if [ "$BA" = "1" ]; then
    CUR_STATUS="每日自动更新"
  else
    BTITLE=""; [ -n "$BSEL" ] && [ -f "$WALL_DIR/$BSEL.meta" ] && BTITLE="$(cut -f1 "$WALL_DIR/$BSEL.meta" 2>/dev/null | head -1)"
    CUR_STATUS="${BTITLE:-每日精选}"
  fi
else
  CUR_STATUS="系统原始壁纸"
fi

BA_CHK=""; [ "$BA" = "1" ] && BA_CHK="checked"
ROT_CHK=""; [ "$ROTATE" = "1" ] && ROT_CHK="checked"
MERGE_CHK=""; [ "$MERGE" = "1" ] && MERGE_CHK="checked"
INTERVAL_OPTIONS=""
for iv in 0.5 1 5 15 30 60 120 240; do
  sel=""; [ "$iv" = "$RINT" ] && sel=" selected"
  case "$iv" in 0.5) lab="30 秒" ;; 1) lab="1 分钟" ;; 60) lab="1 小时" ;; 120) lab="2 小时" ;; 240) lab="4 小时" ;; *) lab="$iv 分钟" ;; esac
  INTERVAL_OPTIONS="$INTERVAL_OPTIONS<option value=\"$iv\"$sel>$lab</option>"
done
SS_CHK=""; [ "$SS_ON" = "1" ] && SS_CHK="checked"
SS_DELAY_OPTIONS=""
for sd in 1 5 10 20 30; do
  sel=""; [ "$sd" = "$SS_DELAY" ] && sel=" selected"
  lab="$sd 分钟"; [ "$sd" = "1" ] && lab="1 分钟"
  SS_DELAY_OPTIONS="$SS_DELAY_OPTIONS<option value=\"$sd\"$sel>$lab</option>"
done
WEATHER_CHK=""; [ "$WEATHER_ON" = "1" ] && WEATHER_CHK="checked"
CUR_IMG="?action=curthumb&t=$(date +%s)"

# ---------- 页面 ----------
header
cat <<HTMLPAGE
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>飞牛视野 - 壁纸</title>
<style>
  :root{ --blue:#007aff; --green:#34c759; --red:#ff3b30;
         --bg:#f2f2f7; --card:#fff; --text:#000; --sec:#6d6d72; --val:#8e8e93;
         --sep:rgba(60,60,67,.29); --fill:#e9e9eb; --orange:#e08a2b; }
  @media (prefers-color-scheme: dark){
    :root{ --bg:#000; --card:#1c1c1e; --text:#fff; --sec:#8e8e93; --val:#98989d;
           --sep:rgba(84,84,88,.65); --fill:#2c2c2e; }
  }
  *{box-sizing:border-box; -webkit-tap-highlight-color:transparent}
  body{margin:0;padding:20px 16px 48px;background:var(--bg);color:var(--text);
       font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","PingFang SC",
       "Helvetica Neue","Microsoft YaHei",sans-serif;
       -webkit-font-smoothing:antialiased; font-size:17px; line-height:1.36}
  .wrap{max-width:600px;margin:0 auto}
  h1{font-size:20px;font-weight:700;letter-spacing:-.3px;margin:2px 16px 2px}
  .sub{color:var(--sec);font-size:14px;margin:0 16px 18px}
  .sec-title{margin:24px 16px 8px;font-size:20px;font-weight:700;letter-spacing:-.3px}
  .cap{font-size:13px;color:var(--sec);padding:2px 16px 12px;line-height:1.45}
  .card{background:var(--card);border-radius:12px;overflow:hidden;margin-bottom:8px}
  .row{display:flex;align-items:center;justify-content:space-between;gap:12px;
       min-height:48px;padding:9px 16px;position:relative}
  .row+.row::after{content:"";position:absolute;left:16px;right:0;top:0;
       height:1px;background:var(--sep);transform:scaleY(.5)}
  .row .lbl{flex-shrink:0}
  .row .val{color:var(--val);font-size:16px;text-align:right;word-break:break-all}
  .row .chev{color:var(--val);font-size:20px;line-height:1;flex-shrink:0}
  .row.tappable{cursor:pointer;color:var(--blue)}
  .row.tappable:active{background:var(--fill)}
  .row.tappable.disabled{pointer-events:none;opacity:.45}
  .row.destructive{color:var(--red)}
  .msg{border-radius:12px;padding:11px 16px;font-size:15px;margin:0 0 14px}
  .msg.ok{background:rgba(52,199,89,.14);color:#1e8e3e}
  .msg.err{background:rgba(255,59,48,.12);color:var(--red)}
  select{appearance:none;-webkit-appearance:none;border:0;background:transparent;
       color:var(--val);font-size:17px;font-family:inherit;text-align:right;
       outline:none;padding:0 18px 0 0;cursor:pointer;max-width:200px;
       background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='11' height='7' viewBox='0 0 11 7'><path d='M1 1l4.5 4.5L10 1' stroke='%238e8e93' stroke-width='1.6' fill='none' stroke-linecap='round'/></svg>");
       background-repeat:no-repeat;background-position:right center}
  select option{color:#000}
  input.txtin{appearance:none;-webkit-appearance:none;border:0;background:var(--fill);color:var(--text);
       font-size:15px;font-family:inherit;text-align:right;outline:none;border-radius:8px;
       padding:6px 10px;max-width:180px;box-sizing:border-box}
  input.txtin::placeholder{color:var(--sec)}
  /* 天气城市：整行左对齐输入框（不再右对齐+限宽，消除框内左侧大片空白）+ 候选下拉 */
  input.txtin.cityin{width:100%;max-width:none;text-align:left;display:block}
  .citywrap{position:relative;padding:0 16px 10px}
  .citydd{display:none;z-index:50;background:var(--card);border:1px solid var(--sep);
       border-radius:10px;overflow:hidden;box-shadow:0 10px 28px rgba(0,0,0,.28);
       max-height:240px;overflow-y:auto}
  .citydd .it{display:flex;align-items:center;gap:8px;padding:9px 12px;font-size:14px;cursor:pointer;border-bottom:1px solid var(--sep)}
  .citydd .it:last-child{border-bottom:0}
  .citydd .it.empty{padding:12px;color:var(--sec);font-size:13px;cursor:default}
  .citydd .it .nm{font-size:15px;font-weight:500}
  .citydd .it .sub{font-size:12px;color:var(--sec);margin:0;flex-shrink:0;white-space:nowrap}
  .citydd .it:active{background:var(--fill)}
  /* 开关 */
  .switch{position:relative;display:inline-block;width:51px;height:31px;flex-shrink:0}
  .switch input{opacity:0;width:0;height:0}
  .switch .knob{position:absolute;inset:0;border-radius:31px;background:var(--fill);
       transition:background .22s;cursor:pointer}
  .switch .knob::before{content:"";position:absolute;width:27px;height:27px;left:2px;top:2px;
       border-radius:50%;background:#fff;box-shadow:0 2px 4px rgba(0,0,0,.25);
       transition:transform .22s}
  .switch input:checked + .knob{background:var(--green)}
  .switch input:checked + .knob::before{transform:translateX(20px)}
  /* 当前壁纸卡：与下方的「每日精选 / 我的照片」网格同宽对齐（不再全屏出血），圆角与卡片一致 */
  .card.current{position:relative;width:100%;border-radius:12px;overflow:hidden;aspect-ratio:16/9;background:var(--fill);padding:0}
  .cur-img{width:100%;height:100%;object-fit:cover;display:block}
  .cur-ov{position:absolute;left:0;right:0;bottom:0;padding:16px 18px;color:#fff;
       background:linear-gradient(to top,rgba(0,0,0,.62),rgba(0,0,0,0));pointer-events:none}
  .cur-label{font-size:12px;opacity:.85;text-shadow:0 1px 3px rgba(0,0,0,.6)}
  .cur-status{font-size:19px;font-weight:700;margin-top:2px;text-shadow:0 1px 5px rgba(0,0,0,.65)}
  /* 网格缩略图 */
  .thumbs{display:flex;gap:12px;padding:14px 16px;overflow-x:auto;scrollbar-width:thin;
       scrollbar-color:var(--fill) transparent}
  .thumbs::-webkit-scrollbar{height:7px}
  .thumbs::-webkit-scrollbar-thumb{background:var(--fill);border-radius:4px}
  .thumb{flex:0 0 auto;width:140px;cursor:pointer;position:relative}
  .thumb img{width:140px;height:84px;object-fit:cover;border-radius:10px;display:block;background:var(--fill)}
  .thumb:active img{opacity:.7}
  .thumb.cur img{box-shadow:0 0 0 2px var(--card),0 0 0 4px var(--green)}
  .thumb .t{display:block;font-size:12px;color:var(--text);margin-top:6px;
       max-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .thumb .d{display:block;font-size:11px;color:var(--sec);margin-top:2px}
  .thumb .del{position:absolute;top:5px;right:6px;width:20px;height:20px;border:0;
       border-radius:50%;background:rgba(0,0,0,.6);color:#fff;font-size:15px;line-height:18px;
       text-align:center;cursor:pointer;padding:0}
  .thumb .del:active{background:var(--red)}
  .empty{padding:20px 16px;color:var(--sec);font-size:14px;grid-column:1/-1;width:100%;box-sizing:border-box}
  /* 弹窗 */
  .mask{position:fixed;inset:0;background:rgba(0,0,0,.4);display:none;
       align-items:center;justify-content:center;z-index:9;padding:32px}
  .mask.show{display:flex}
  .alert{width:280px;background:var(--card);border-radius:14px;overflow:hidden;
       text-align:center;box-shadow:0 10px 40px rgba(0,0,0,.3)}
  .alert .t{padding:18px 16px 4px;font-size:17px;font-weight:600}
  .alert .d{padding:2px 16px 16px;font-size:13px;color:var(--sec)}
  .alert .btns{display:flex;border-top:.5px solid var(--sep)}
  .alert .ab{flex:1;border:0;background:transparent;font-family:inherit;font-size:16px;
       min-height:44px;cursor:pointer;color:var(--blue);padding:0}
  .alert .ab:active{background:var(--fill)}
  .alert .ab + .ab{border-left:.5px solid var(--sep)}
  .alert .ab.strong{font-weight:600}
  .alert .ab.danger{color:var(--red)}
  #dirMask{z-index:10}
  .dirPanel{width:min(560px,94vw);max-height:74vh;background:var(--card);border-radius:14px;
       overflow:hidden;display:flex;flex-direction:column;box-shadow:0 10px 40px rgba(0,0,0,.3)}
  .dirPanel .t{padding:14px 16px 6px;font-size:16px;font-weight:600}
  .dirPanel .path{padding:0 16px 8px;font-size:12px;color:var(--sec);word-break:break-all}
  .dirList{flex:1;overflow-y:auto;border-top:.5px solid var(--sep);min-height:220px}
  .dirList .diritem{display:flex;align-items:center;gap:10px;padding:11px 16px;cursor:pointer;
       border-bottom:.5px solid var(--sep);font-size:15px}
  .dirList .diritem:active{background:var(--fill)}
  .dirList .diritem .ico{flex-shrink:0;width:20px;text-align:center}
  .dirList .diritem .nm{flex:1;word-break:break-all;color:var(--blue)}
  .filegrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(104px,1fr));gap:12px;padding:12px 14px}
  .fitem{cursor:pointer;position:relative}
  .fitem .fimg{width:100%;aspect-ratio:16/9;object-fit:cover;border-radius:10px;display:block;background:var(--fill)}
  .fitem:active .fimg{opacity:.7}
  .fitem .fnm{font-size:11.5px;color:var(--sec);margin-top:5px;text-align:center;
       overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .fitem .chk{position:absolute;top:6px;left:8px;width:18px;height:18px;border-radius:50%;
       background:rgba(255,255,255,.85);color:transparent;font-size:12px;line-height:18px;
       text-align:center;box-shadow:0 1px 3px rgba(0,0,0,.3);transition:all .15s}
  .fitem.sel .fimg{box-shadow:0 0 0 2px var(--card),0 0 0 3px var(--green)}
  .fitem.sel .chk{background:var(--green);color:#fff}
  .dirList .empty{padding:32px 16px;text-align:center;color:var(--sec);font-size:14px}
  .dirBtns{display:flex;border-top:.5px solid var(--sep)}
  .dirBtns .ab{flex:1;border:0;background:transparent;font-family:inherit;font-size:16px;
       min-height:46px;cursor:pointer;color:var(--blue);padding:0}
  .dirBtns .ab:active{background:var(--fill)}
  .dirBtns .ab + .ab{border-left:.5px solid var(--sep)}
  .dirBtns .ab.strong{font-weight:600}
  .dirBtns .ab.disabled{pointer-events:none;opacity:.45}
  /* 日志覆盖层 */
  #logLayer{position:fixed;inset:0;background:var(--bg);z-index:20;display:none;
       flex-direction:column;padding:16px}
  #logLayer.show{display:flex}
  .logTop{display:flex;align-items:center;gap:12px;padding-bottom:12px}
  .logBack{color:var(--blue);font-size:17px;cursor:pointer}
  .logTitle{flex:1;font-size:17px;font-weight:700;letter-spacing:-.3px}
  .logRefresh,.logCopy{color:var(--blue);font-size:15px;cursor:pointer}
  #logLayer .logbox{flex:1;background:var(--card);border-radius:12px;margin:0;max-height:none;
       padding:10px 16px;font:11.5px/1.55 "SF Mono",ui-monospace,Menlo,Consolas,monospace;
       color:var(--sec);white-space:pre-wrap;word-break:break-all;overflow-y:auto}
  /* Toast */
  #toast{position:fixed;top:18px;left:50%;transform:translateX(-50%) translateY(-8px);
       background:rgba(30,30,32,.92);color:#fff;font-size:14px;padding:10px 18px;
       border-radius:20px;max-width:80%;text-align:center;opacity:0;z-index:99;
       transition:opacity .25s,transform .25s;pointer-events:none}
  #toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
  /* 忙碌层 */
  #busyLayer{display:none;position:fixed;inset:0;background:rgba(0,0,0,.58);z-index:99999;
       align-items:center;justify-content:center;padding:24px}
  #busyLayer.show{display:flex}
  #busyLayer .box{background:#1c1c1e;color:#fff;border-radius:16px;padding:26px 22px;max-width:360px;text-align:center}
  #busyLayer .bt{font-size:17px;font-weight:600;margin-bottom:10px}
  #busyLayer .bd{font-size:14px;color:#b0b0b5;line-height:1.55}
  #busyLayer .bs{margin-top:18px;font-size:13px;color:#8e8e93}
  /* 每日精选 / 我的照片：纵向每行 2 张 */
  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px;padding:4px 0 8px}
  .bcard{position:relative;border-radius:12px;overflow:hidden;background:var(--fill);cursor:pointer;aspect-ratio:16/9}
  .bcard img{width:100%;height:100%;object-fit:cover;display:block}
  .bcard:active img{opacity:.82}
  .bcard.cur{box-shadow:0 0 0 2px var(--card),0 0 0 4px var(--green)}
  .bcard .bcap{position:absolute;left:0;right:0;bottom:0;padding:8px 10px;color:#fff;
       background:linear-gradient(to top,rgba(0,0,0,.64),rgba(0,0,0,0))}
  .bcard .bt{font-size:13px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;text-shadow:0 1px 3px rgba(0,0,0,.6)}
  .bcard .bd{font-size:11px;opacity:.9;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:1px;text-shadow:0 1px 3px rgba(0,0,0,.6)}
  .bcard.auto .bt{font-size:15px}
  .bcard .badge{position:absolute;top:6px;left:6px;background:rgba(0,0,0,.55);color:#fff;font-size:11px;
       padding:2px 7px;border-radius:8px;font-weight:600}
  .pcard{position:relative;border-radius:12px;overflow:hidden;background:var(--fill);cursor:pointer;aspect-ratio:16/9}
  .pcard img{width:100%;height:100%;object-fit:cover;display:block}
  .pcard:active img{opacity:.82}
  .pcard.cur{box-shadow:0 0 0 2px var(--card),0 0 0 4px var(--green)}
  .pcard .pn{position:absolute;left:0;right:0;bottom:0;padding:6px 8px;color:#fff;font-size:11px;
       white-space:nowrap;overflow:hidden;text-overflow:ellipsis;background:linear-gradient(to top,rgba(0,0,0,.62),rgba(0,0,0,0))}
  .pcard .del{position:absolute;top:5px;right:6px;width:20px;height:20px;border:0;border-radius:50%;
       background:rgba(0,0,0,.6);color:#fff;font-size:15px;line-height:18px;text-align:center;cursor:pointer;padding:0}
  .pcard .del:active{background:var(--red)}
</style>
</head>
<body>
<div class="wrap" id="wrap" data-ba="$BA">
  <h1>当前壁纸</h1>
  <div class="sub">NAS：<span id="nasName">$NAS</span></div>

  <div class="card current">
    <img class="cur-img" id="curImg" src="$CUR_IMG" alt="当前壁纸" onerror="this.onerror=null;this.src='?action=curthumb&t='+Date.now();">
    <div class="cur-ov">
      <div class="cur-status" id="curStatus">$CUR_STATUS</div>
    </div>
  </div>

  <div class="sec-title">每日精选</div>
  <div class="grid2" id="bingThumbs">$BING_AUTO_HTML$BING_HTML</div>

  <div class="sec-title">我的照片</div>
  <div class="card">
    <div class="row tappable" onclick="showSrc()">
      <span class="lbl">添加照片…</span><span class="val" id="cntImg">$PHOTO_N 张</span>
    </div>
  </div>
  <div class="grid2" id="photoThumbs">$PHOTO_HTML</div>

  <div class="sec-title">壁纸轮换</div>
  <div class="card">
    <div class="row">
      <span class="lbl">自动轮换</span>
      <label class="switch"><input type="checkbox" id="rotateSw" $ROT_CHK onchange="onRotateToggle(this)"><span class="knob"></span></label>
    </div>
    <div class="cap">后台自动切换桌面壁纸（每日精选 / 我的照片）</div>
    <div class="row" id="rotOptsRow" $([ "$ROTATE" = "0" ] && echo 'style="display:none"')>
      <span class="lbl">轮播间隔</span>
      <select id="intervalSel" onchange="onIntervalChange(this)">$INTERVAL_OPTIONS</select>
    </div>
    <div class="row" id="mergeRow" $([ "$ROTATE" = "0" ] || [ "$BA" = "1" ] && echo 'style="display:none"')>
      <span class="lbl">合并轮播</span>
      <label class="switch"><input type="checkbox" id="mergeSw" $MERGE_CHK onchange="onMergeToggle(this)"><span class="knob"></span></label>
    </div>
    <div class="cap" id="mergeCap" $([ "$ROTATE" = "0" ] || [ "$BA" = "1" ] && echo 'style="display:none"')>把每日精选与我的照片放进同一个池子轮换（仅每日自动更新关闭时可用）</div>
  </div>

  <div class="sec-title">屏幕保护</div>
  <div class="card">
    <div class="row">
      <span class="lbl">屏幕保护程序</span>
      <label class="switch"><input type="checkbox" id="ssSw" $SS_CHK onchange="onSsToggle(this)"><span class="knob"></span></label>
    </div>
    <div class="cap">轮播每日精选和你的照片</div>
    <div class="row" id="ssDelayRow" $([ "$SS_ON" = "0" ] && echo 'style="display:none"')>
      <span class="lbl">屏保时间</span>
      <select id="ssDelaySel" onchange="onSsDelayChange(this)">$SS_DELAY_OPTIONS</select>
    </div>
    <div class="row">
      <span class="lbl">显示天气</span>
      <label class="switch"><input type="checkbox" id="weatherSw" $WEATHER_CHK onchange="onWeatherToggle(this)"><span class="knob"></span></label>
    </div>
    <div class="cap">屏保展示当地天气；留空则按访问设备 IP 自动定位（可能不准）</div>
    <div id="weatherCityRow" $([ "$WEATHER_ON" = "0" ] && echo 'style="display:none"')>
      <div class="row"><span class="lbl">天气城市</span></div>
      <div class="citywrap">
        <input id="weatherCity" class="txtin cityin" type="text" placeholder="留空则IP自动定位" autocomplete="off" value="$WEATHER_CITY_ESC" oninput="onCityInput()" onfocus="onCityFocus()" onblur="onCityBlur()">
        <div class="citydd" id="cityDD"></div>
      </div>
    </div>
    <div class="row tappable" onclick="startSaverNow()"><span class="lbl">开始屏保</span></div>
  </div>

  <div class="sec-title">系统</div>
  <div class="card">
    <div class="row tappable" onclick="showLogs()"><span class="lbl">查看运行日志</span><span class="chev">›</span></div>
    <div class="row tappable destructive" onclick="confirmRestore()"><span class="lbl">恢复系统原始壁纸</span></div>
  </div>

  <div class="sec-title">关于</div>
  <div class="card">
    <a class="row tappable" href="https://github.com/mclhm8182/FnVista" target="_blank" rel="noopener" style="text-decoration:none;color:inherit">
      <span style="display:flex;align-items:center;gap:10px">
        <svg viewBox="0 0 16 16" width="22" height="22" fill="currentColor" aria-hidden="true"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"></path></svg>
        <span style="font-size:16px;font-weight:500">mclhm8182</span>
      </span>
      <span class="val">GitHub ›</span>
    </a>
  </div>
</div>

<div class="mask" id="mask">
  <div class="alert">
    <div class="t" id="alertT"></div>
    <div class="d" id="alertD"></div>
    <div class="btns">
      <button class="ab strong" onclick="hideConfirm()">取消</button>
      <button class="ab danger" id="alertOk">确定</button>
    </div>
  </div>
</div>

<div class="mask" id="dirMask">
  <div class="dirPanel">
    <div class="t" id="dirTitle">添加照片</div>
    <div class="path" id="dirPath"></div>
    <div class="dirList" id="dirList"><div class="empty">加载中…</div></div>
    <div class="dirBtns">
      <button class="ab" onclick="hideBrowse()">取消</button>
      <button class="ab strong" id="dirUseBtn" onclick="dirConfirm()">加入</button>
    </div>
  </div>
</div>

<div class="mask" id="srcMask">
  <div style="width:min(320px,90vw);background:var(--card);border-radius:14px;overflow:hidden">
    <div onclick="hideSrc();openBrowse()" style="text-align:center;padding:15px 12px;font-size:16px;color:var(--blue);cursor:pointer">NAS里的图片</div>
    <div onclick="hideSrc();pickLocal()" style="text-align:center;padding:15px 12px;font-size:16px;color:var(--blue);cursor:pointer;border-top:.5px solid var(--sep)">电脑/手机的图片</div>
  </div>
</div>
<input type="file" id="localFiles" accept="image/*" multiple style="display:none" onchange="uploadLocal(this)">

<div id="logLayer">
  <div class="logTop">
    <span class="logBack" onclick="hideLogs()">‹ 返回</span>
    <span class="logTitle">运行日志</span>
    <span class="logCopy" onclick="copyLogs()">复制</span>
    <span class="logRefresh" onclick="loadLogs()">刷新</span>
  </div>
  <pre class="logbox" id="logContent">加载中…</pre>
</div>
<div id="toast"></div>
<div id="busyLayer">
  <div class="box">
    <div class="bt" id="busyT">正在更新壁纸</div>
    <div class="bd" id="busyD">飞牛正在重启网页服务，连接会短暂中断，请勿关闭页面。</div>
    <div class="bs" id="busyS">请稍候…</div>
  </div>
</div>

<script>
var SAVER_JSON=$SAVER_JSON_JS;
var CUR_BA="$BA";
var CUR_SS_ON="$SS_ON";
var CUR_SS_DELAY="$SS_DELAY";
var CUR_WEATHER_ON="$WEATHER_ON";
var CUR_WEATHER_CITY="$WEATHER_CITY_JS";
var pendingOk=null;
function api(qs){return fetch('?'+qs).then(function(r){return r.json();});}
var toastTimer=null;
function toast(msg){
  var el=document.getElementById('toast');
  el.textContent=msg; el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer=setTimeout(function(){el.classList.remove('show');},3000);
}
function showConfirm(t,d,btn,fn){
  document.getElementById('alertT').textContent=t;
  document.getElementById('alertD').textContent=d;
  document.getElementById('alertOk').textContent=btn||'确定';
  pendingOk=fn;
  document.getElementById('mask').classList.add('show');
}
function hideConfirm(){document.getElementById('mask').classList.remove('show');pendingOk=null;}
document.getElementById('alertOk').addEventListener('click',function(){
  var f=pendingOk;
  document.getElementById('mask').classList.remove('show');
  pendingOk=null;
  if(f) f();
});
document.getElementById('mask').addEventListener('click',function(e){
  if(e.target===this)hideConfirm();
});
async function refreshPage(){
  try{
    var html=await (await fetch('?t='+Date.now())).text();
    var doc=new DOMParser().parseFromString(html,'text/html');
    var nw=doc.getElementById('wrap');
    if(nw){
      document.getElementById('wrap').replaceWith(nw);
      var w2=document.getElementById('wrap');
      if(w2&&w2.getAttribute('data-ba')!=null)CUR_BA=w2.getAttribute('data-ba');
    }
    // 整页重绘后立刻刷新顶部「当前壁纸」预览，避免手动点选后卡片长时间不更新（无需强制刷新网页）
    var ci=document.getElementById('curImg');
    if(ci){ ci.style.visibility='visible'; ci.src='?action=curthumb&t='+Date.now(); }
  }catch(e){}
}
/* 保存轮流换与间隔（每日自动更新由自动卡 setAuto 控制；二者互斥由后端保证） */
async function saveAll(){
  var ba=CUR_BA;
  var ro=document.getElementById('rotateSw').checked?1:0;
  var mg=document.getElementById('mergeSw')?(document.getElementById('mergeSw').checked?1:0):0;
  var ri=document.getElementById('intervalSel').value;
  var sso=document.getElementById('ssSw')?(document.getElementById('ssSw').checked?1:0):0;
  var ssd=document.getElementById('ssDelaySel')?document.getElementById('ssDelaySel').value:5;
  var woe=document.getElementById('weatherSw')?(document.getElementById('weatherSw').checked?1:0):0;
  var wcity=document.getElementById('weatherCity')?encodeURIComponent((document.getElementById('weatherCity').value||'').trim()):'';
  toast('正在保存…');
  try{
    var d=await api('action=save&ajax=1&bing_auto='+ba+'&rotate='+ro+'&rotate_interval='+ri+'&merge='+mg+'&ss_on='+sso+'&ss_delay='+ssd+'&weather_on='+woe+'&weather_city='+wcity);
    toast(d.msg||'已保存');
  }catch(e){toast('保存失败，请重试');}
  refreshPage();
}
function onRotateToggle(el){
  document.getElementById('rotOptsRow').style.display=el.checked?'':'none';
  var showMerge=el.checked && CUR_BA!==1;
  var mr=document.getElementById('mergeRow'); if(mr) mr.style.display=showMerge?'':'none';
  var mcap=document.getElementById('mergeCap'); if(mcap) mcap.style.display=showMerge?'':'none';
  saveAll();
}
function onMergeToggle(el){saveAll();}
function onIntervalChange(el){saveAll();}
/* 点选某张 Bing 壁纸：固定该图并暂停自动更新 */
async function chooseBing(file){
  toast('正在应用…');
  try{ var d=await api('action=apply&file='+file+'&ajax=1'); toast(d.msg||'已应用'); }
  catch(e){ toast('应用失败，请重试'); }
  refreshPage();
}
/* 点选某张我的照片：设为桌面 */
async function choosePhoto(enc){
  toast('正在应用…');
  try{ var d=await api('action=applysrc&ajax=1&p='+enc); toast(d.msg||'已应用'); }
  catch(e){ toast('应用失败，请重试'); }
  refreshPage();
}
async function removePhoto(enc){
  try{ var d=await api('action=removefile&ajax=1&p='+enc); toast(d.msg||'已移除'); }
  catch(e){ toast('移除失败'); }
  refreshPage();
}
/* 每日自动更新：点自动卡即开启，并关闭轮流换（互斥） */
async function setAuto(){
  CUR_BA=1;
  toast('正在开启每日自动更新…');
  try{
    var d=await api('action=save&ajax=1&bing_auto=1&rotate=0');
    toast(d.msg||'已开启每日自动更新');
  }catch(e){toast('操作失败，请重试');}
  refreshPage();
}
/* 补齐最近 7 天每日精选（后台执行，轮询状态） */
async function doBackfill(){
  toast('正在补齐最近 7 天每日精选…');
  try{
    var d=await api('action=backfill&ajax=1');
    if(d.ok===false){toast(d.msg||'无法开始');return;}
  }catch(e){toast('启动失败，请重试');return;}
  var t0=Date.now(),done=false;
  var timer=setInterval(async function(){
    var el=Date.now()-t0;
    try{
      var r=await fetch('?action=backfill_status&ajax=1',{cache:'no-store'});
      var j=await r.json();
      if(j.state==='done'){done=true;clearInterval(timer);toast('已补齐最近 7 天每日精选');setTimeout(refreshPage,600);return;}
    }catch(e){}
    if(!done&&el>120000){clearInterval(timer);toast('补齐超时，请手动刷新');}
  },2500);
}
/* ============ 屏保：覆盖整页，自动轮播「每日精选 + 我的照片」，叠加时钟（含农历） ============ */
/* 优先挂到最外层文档（window.top）——飞牛把第三方应用放在子窗口/iframe 里，
   普通 position:fixed 只能盖住子窗口；同域时把覆盖层插到顶层文档即可盖住整个飞牛页面。 */
function getRootDoc(){
  try{
    if(window.top && window.top!==window && window.top.document && window.top.document.body) return window.top.document;
  }catch(e){}
  return document;
}
var SSRoot=getRootDoc();
var SS_ON=(CUR_SS_ON==='1');
var SS_DELAY=parseInt(CUR_SS_DELAY||'5',10)||5;
var WEATHER_ON=(CUR_WEATHER_ON==='1');
var WEATHER_CITY=CUR_WEATHER_CITY||'';
var ssState={active:false,timer:null,rotTimer:null,clockTimer:null,weatherTimer:null,jitterTimer:null,imgIdx:0,list:[],layer:null,imgA:null,imgB:null,activeImg:null,clock:null,date:null,time:null,weather:null,caption:null,capT:null,capD:null};

function ssEnsureLayer(){
  if(ssState.layer) return;
  var d=SSRoot, b=d.body, hd=d.head||b;
  if(!d.getElementById('ssStyle')){
    var st=d.createElement('style'); st.id='ssStyle';
    st.textContent=
      '.ss-layer{position:fixed;inset:0;z-index:2147483647;background:#000;display:none;'
      +'align-items:center;justify-content:center;flex-direction:column;cursor:default;overflow:hidden;'
      +'font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}'
      +'.ss-layer.show{display:flex}'
      +'.ss-img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;opacity:0;transition:opacity 1.2s ease;'
      +'will-change:transform;animation:kb 30s ease-in-out infinite alternate}'
      +'.ss-img.show{opacity:1}'
      +'#ssImgB{animation-delay:-15s}'
      +'@keyframes kb{0%{transform:scale(1.05) translate(0,0)}100%{transform:scale(1.14) translate(-2.5%,-1.8%)}}'
      +'.ss-clock{position:absolute;top:max(28px,env(safe-area-inset-top));left:max(28px,env(safe-area-inset-left));right:auto;z-index:2;color:#fff;text-align:left;max-width:62vw;'
      +'text-shadow:0 2px 14px rgba(0,0,0,.7);margin:0;animation:clockfloat 38s ease-in-out infinite}'
      +'.ss-clock-inner{transition:transform 4s ease}'
      +'@keyframes clockfloat{0%{transform:translate(0,0)}25%{transform:translate(9px,7px)}50%{transform:translate(-7px,11px)}75%{transform:translate(-11px,-5px)}100%{transform:translate(0,0)}}'
      +'.ss-weather{font-size:clamp(14px,3vw,22px);opacity:.92;line-height:1.32;margin:10px 0 0;white-space:normal}'
      +'.ss-date{font-size:clamp(14px,3vw,22px);opacity:.92;line-height:1.32;margin:0 auto 8px;white-space:normal}'
      +'.ss-time{font-size:clamp(40px,12vw,108px);font-weight:700;line-height:1.04;letter-spacing:2px;'
      +'font-variant-numeric:tabular-nums;white-space:nowrap;margin:0 auto}'
      +'.ss-hint{position:absolute;bottom:26px;left:0;right:0;text-align:center;color:rgba(255,255,255,.55);'
      +'font-size:13px;z-index:2;text-shadow:0 1px 4px rgba(0,0,0,.6)}'
      +'.ss-caption{position:absolute;left:0;right:0;bottom:62px;z-index:2;text-align:center;color:#fff;padding:0 26px;pointer-events:none;'
      +'text-shadow:0 2px 14px rgba(0,0,0,.75);opacity:0;transition:opacity .9s ease}'
      +'.ss-caption.show{opacity:1}'
      +'.ss-cap-t{font-size:clamp(18px,3.4vw,30px);font-weight:600;line-height:1.32;margin:0 0 5px;white-space:normal;word-break:break-word}'
      +'.ss-cap-d{font-size:clamp(13px,2.2vw,17px);opacity:.92;line-height:1.32;margin:0;white-space:normal;word-break:break-word}';
    hd.appendChild(st);
  }
  var L=d.createElement('div'); L.className='ss-layer'; L.id='ssLayer';
  L.innerHTML='<img class="ss-img" id="ssImgA" alt=""><img class="ss-img" id="ssImgB" alt=""><div class="ss-clock"><div class="ss-clock-inner"><div class="ss-date"></div><div class="ss-time"></div><div class="ss-weather"></div></div></div><div class="ss-caption"><div class="ss-cap-t"></div><div class="ss-cap-d"></div></div><div class="ss-hint">轻触任意处退出屏保</div>';
  b.appendChild(L);
  ssState.layer=L; ssState.imgA=L.querySelector('#ssImgA'); ssState.imgB=L.querySelector('#ssImgB');
  ssState.activeImg=ssState.imgA; ssState.clock=L.querySelector('.ss-clock-inner');
  ssState.caption=L.querySelector('.ss-caption'); ssState.capT=L.querySelector('.ss-cap-t'); ssState.capD=L.querySelector('.ss-cap-d');  ssState.date=L.querySelector('.ss-date'); ssState.time=L.querySelector('.ss-time'); ssState.weather=L.querySelector('.ss-weather');
  L.addEventListener('click', ssStop);
  /* 退出提示随系统语言：中文「轻触任意处退出屏保」，其余回退英文 */
  var ssHint=L.querySelector('.ss-hint');
  if(ssHint) ssHint.textContent=ssIsZh()?'轻触任意处退出屏保':'Tap anywhere to exit';
}
/* 把相对 URL 转成绝对地址（覆盖层在顶层文档时，相对地址会以顶层页为基准而失效） */
function ssAbs(u){
  if(!u) return u;
  if(u.indexOf('http')===0 || u.indexOf('//')===0) return u;
  return location.href.split('?')[0] + (u.charAt(0)==='?'?u:('?'+u));
}
/* 农历（1900–2100） */
var LUNAR_INFO=[0x04bd8,0x04ae0,0x0a570,0x054d5,0x0d260,0x0d950,0x16554,0x056a0,0x09ad0,0x055d2,
0x04ae0,0x0a5b6,0x0a4d0,0x0d250,0x1d255,0x0b540,0x0d6a0,0x0ada2,0x095b0,0x14977,
0x04970,0x0a4b0,0x0b4b5,0x06a50,0x06d40,0x1ab54,0x02b60,0x09570,0x052f2,0x04970,
0x06566,0x0d4a0,0x0ea50,0x06e95,0x05ad0,0x02b60,0x186e3,0x092e0,0x1c8d7,0x0c950,
0x0d4a0,0x1d8a6,0x0b550,0x056a0,0x1a5b4,0x025d0,0x092d0,0x0d2b2,0x0a950,0x0b557,
0x06ca0,0x0b550,0x15355,0x04da0,0x0a5b0,0x14573,0x052b0,0x0a9a8,0x0e950,0x06aa0,
0x0aea6,0x0ab50,0x04b60,0x0aae4,0x0a570,0x05260,0x0f263,0x0d950,0x05b57,0x056a0,
0x096d0,0x04dd5,0x04ad0,0x0a4d0,0x0d4d4,0x0d250,0x0d558,0x0b540,0x0b6a0,0x195a6,
0x095b0,0x049b0,0x0a974,0x0a4b0,0x0b27a,0x06a50,0x06d40,0x0af46,0x0ab60,0x09570,
0x04af5,0x04970,0x064b0,0x074a3,0x0ea50,0x06b58,0x055c0,0x0ab60,0x096d5,0x092e0,
0x0c960,0x0d954,0x0d4a0,0x0da50,0x07552,0x056a0,0x0abb7,0x025d0,0x092d0,0x0cab5,
0x0a950,0x0b4a0,0x0baa4,0x0ad50,0x055d9,0x04ba0,0x0a5b0,0x15176,0x052b0,0x0a930,
0x07954,0x06aa0,0x0ad50,0x05b52,0x04b60,0x0a6e6,0x0a4e0,0x0d260,0x0ea65,0x0d530,
0x05aa0,0x076a0,0x096d5,0x04af5,0x04ab0,0x0a930,0x0a4a8,0x0d250,0x0d252,0x0b540,
0x0d6a0,0x0ada0,0x095b0,0x04a93,0x04b50,0x0b555,0x06a50,0x06d40,0x0ab54,0x095aa,
0x04ba0,0x0a5b0,0x052b0,0x04b55,0x0a4a0,0x0d250,0x0d252,0x0b540];
var LUNAR_MONTH=["正月","二月","三月","四月","五月","六月","七月","八月","九月","十月","冬月","腊月"];
var LUNAR_DAY=["初一","初二","初三","初四","初五","初六","初七","初八","初九","初十","十一","十二","十三","十四","十五","十六","十七","十八","十九","二十","廿一","廿二","廿三","廿四","廿五","廿六","廿七","廿八","廿九","三十"];
function ssLunarYDays(y){var s=348,i;for(i=0x8000;i>0x8;i>>=1)s+=(LUNAR_INFO[y-1900]&i)?1:0;return s+ssLeapDays(y);}
function ssLeapDays(y){if(ssLeapMonth(y))return (LUNAR_INFO[y-1900]&0x10000)?30:29;return 0;}
function ssLeapMonth(y){return LUNAR_INFO[y-1900]&0xf;}
function ssMonthDays(y,m){if(m>12||m<1)return -1;return (LUNAR_INFO[y-1900]&(0x10000>>m))?30:29;}
function ssLunarStr(y,m,d){
  var base=new Date(1900,0,31), obj=new Date(y,m-1,d);
  var offset=Math.round((obj-base)/86400000);
  var temp=0,ly=1900,leap=0,isLeap=false,lm=1,ld=0;
  for(ly=1900;ly<2101&&offset>0;ly++){temp=ssLunarYDays(ly);offset-=temp;}
  if(offset<0){offset+=temp;ly--;}
  leap=ssLeapMonth(ly);
  for(lm=1;lm<13&&offset>0;lm++){
    if(leap>0&&lm===(leap+1)&&!isLeap){lm--;isLeap=true;temp=ssLeapDays(ly);}
    else{temp=ssMonthDays(ly,lm);}
    if(isLeap&&lm===(leap+1))isLeap=false;
    offset-=temp;
  }
  if(offset===0&&leap>0&&lm===leap+1){if(isLeap){isLeap=false;}else{isLeap=true;lm--;}}
  if(offset<0){offset+=temp;lm--;}
  ld=offset+1;
  var ms=(isLeap?"闰":"")+LUNAR_MONTH[lm-1];
  return "农历"+ms+LUNAR_DAY[ld-1];
}
/* i18n：屏保跟随系统语言；农历为中文特有概念，仅中文环境展示，非中文回退英文且不显示农历 */
function ssIsZh(){
  var l=(navigator.language||'zh').toLowerCase();
  return l.indexOf('zh')===0;
}
function ssTick(){
  var now=new Date();
  if(ssIsZh()){
    ssState.date.textContent=now.toLocaleDateString('zh-CN',{year:'numeric',month:'long',day:'numeric',weekday:'long'})
      +' '+ssLunarStr(now.getFullYear(),now.getMonth()+1,now.getDate());
    ssState.time.textContent=now.toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',hour12:false});
  } else {
    /* 非中文：仅公历日期 + 星期（英文回退），不显示农历 */
    ssState.date.textContent=now.toLocaleDateString('en',{year:'numeric',month:'long',day:'numeric',weekday:'long'});
    ssState.time.textContent=now.toLocaleTimeString('en',{hour:'2-digit',minute:'2-digit',hour12:false});
  }
  /* 约束日期行宽度不超过时间宽度（窄屏自动换行，仍不超出时间两侧） */
  if(ssState.time.offsetWidth) ssState.date.style.maxWidth=ssState.time.offsetWidth+'px';
}
function ssSetCaption(item){
  // 仅「每日精选（Bing）」显示说明文字，我的照片不显示字幕；标题（capT）按用户要求不再显示
  if(!ssState.caption) return;
  ssState.capT.textContent='';
  if(item && item.kind==='bing' && item.title){
    ssState.capD.textContent=item.desc||'';
    ssState.caption.classList.add('show');
  } else {
    ssState.caption.classList.remove('show');
  }
}
function ssRotate(){
  if(ssState.rotTimer) clearInterval(ssState.rotTimer);
  var list=ssState.list;
  if(!list.length){ return; }
  ssState.imgIdx=0; ssState.errStreak=0;
  ssState.activeImg=ssState.imgA; ssState.imgA.classList.remove('show'); ssState.imgB.classList.remove('show');
  function showNext(){
    if(!ssState.list.length) return;
    var item=ssState.list[ssState.imgIdx%ssState.list.length];
    var url=ssAbs(item.url);
    var next=(ssState.activeImg===ssState.imgA)?ssState.imgB:ssState.imgA;
    function onready(){ ssState.errStreak=0; ssSetCaption(item); next.classList.add('show'); if(ssState.activeImg&&ssState.activeImg!==next) ssState.activeImg.classList.remove('show'); ssState.activeImg=next; }
    next.onload=onready;
    // 某张图加载失败（如路径含特殊字符）时不要显示黑屏，直接跳过这一张
    next.onerror=function(){
      ssSetCaption(null);
      if(ssState.errStreak>ssState.list.length) return; // 全部失败则停止，避免死循环
      ssState.errStreak++;
      if(ssState.activeImg!==next) next.classList.remove('show');
      ssState.imgIdx++;
      showNext();
    };
    // saverimg 的 URL 已按文件名稳定，浏览器可缓存，故不再追加时间戳（远端 NAS 重复轮播不重复下载）
    next.src=url;
    ssState.imgIdx++;
  }
  showNext();
  ssState.rotTimer=setInterval(showNext,30000);
}
/* 屏保天气：查看者浏览器所在公网 IP 定位 → Open-Meteo 取温度与天气状况 → 反向地理编码取城市名 */
/* 显示形如「☀️晴 26°C 武汉 14～20℃」；中文环境取中文天气与城市，其余回退英文 */
function ssWMO(code, zh){
  var ZH={0:['☀️','晴'],1:['🌤️','晴'],2:['⛅','多云'],3:['☁️','阴'],
    45:['🌫️','雾'],48:['🌫️','雾凇'],
    51:['🌦️','毛毛雨'],53:['🌦️','毛毛雨'],55:['🌦️','毛毛雨'],
    56:['🌧️','冻雨'],57:['🌧️','冻雨'],
    61:['🌧️','小雨'],63:['🌧️','中雨'],65:['🌧️','大雨'],
    66:['🌧️','冻雨'],67:['🌧️','冻雨'],
    71:['🌨️','小雪'],73:['🌨️','中雪'],75:['🌨️','大雪'],77:['🌨️','雪粒'],
    80:['🌦️','阵雨'],81:['🌦️','阵雨'],82:['⛈️','强阵雨'],
    85:['🌨️','阵雪'],86:['🌨️','阵雪'],
    95:['⛈️','雷阵雨'],96:['⛈️','雷阵雨伴冰雹'],99:['⛈️','雷阵雨伴冰雹']};
  var EN={0:['☀️','Clear'],1:['🌤️','Clear'],2:['⛅','Partly cloudy'],3:['☁️','Overcast'],
    45:['🌫️','Fog'],48:['🌫️','Rime fog'],
    51:['🌦️','Light drizzle'],53:['🌦️','Drizzle'],55:['🌦️','Heavy drizzle'],
    56:['🌧️','Freezing drizzle'],57:['🌧️','Freezing drizzle'],
    61:['🌧️','Light rain'],63:['🌧️','Rain'],65:['🌧️','Heavy rain'],
    66:['🌧️','Freezing rain'],67:['🌧️','Freezing rain'],
    71:['🌨️','Light snow'],73:['🌨️','Snow'],75:['🌨️','Heavy snow'],77:['🌨️','Snow grains'],
    80:['🌦️','Showers'],81:['🌦️','Showers'],82:['⛈️','Heavy showers'],
    85:['🌨️','Snow showers'],86:['🌨️','Snow showers'],
    95:['⛈️','Thunderstorm'],96:['⛈️','Thunderstorm w/ hail'],99:['⛈️','Thunderstorm w/ hail']};
  if(zh===undefined) zh=ssIsZh();
  var r=ZH[code]||['🌡️','未知']; var e=EN[code]||['🌡️','Unknown'];
  return [r[0], zh?r[1]:e[1]];
}
/* 免密钥反向地理编码取地名（BigDataCloud client 接口支持 CORS；中文环境取 zh，其余取 en） */
function ssCityName(lat,lon,fallback){
  var lang=ssIsZh()?'zh':'en';
  var u='https://api.bigdatacloud.net/data/reverse-geocode-client?latitude='+lat+'&longitude='+lon+'&localityLanguage='+lang;
  return fetch(u).then(function(r){return r.json();}).then(function(j){
    var c=(j.city||j.locality||j.principalSubdivision||'').replace(/市$/,'');
    return c||fallback||'';
  }).catch(function(){ return fallback||''; });
}
/* 城市名 → 经纬度（Open-Meteo 地理编码，免密钥、支持中文、可 CORS） */
function ssGeocodeCity(city){
  var u='https://geocoding-api.open-meteo.com/v1/search?name='+encodeURIComponent(city)+'&count=1&language=zh&format=json';
  return fetch(u).then(function(r){return r.json();}).then(function(j){
    var f=j&&j.results&&j.results[0];
    if(!f) return null;
    return {lat:f.latitude, lon:f.longitude};
  }).catch(function(){ return null; });
}
function ssFetchWeather(){
  if(!ssState.weather) return;
  // 读「当前」开关与城市，而非页面加载时缓存的全局变量：save 后用 refreshPage() 做的是
  // 局部 AJAX 重绘（只替换 #wrap），并不会重新执行顶层脚本，全局 WEATHER_ON/WEATHER_CITY
  // 仍是旧值。若此处仍用全局，则「清除自定义城市后重开屏保」仍会显示旧城市（需手动刷新网页）。
  var won=document.getElementById('weatherSw')?document.getElementById('weatherSw').checked:WEATHER_ON;
  if(!won){ ssState.weather.textContent=''; ssState.weather.style.display='none'; return; }
  ssState.weather.style.display='';
  ssState.weather.textContent='';
  function setText(s){ ssState.weather.textContent=s||'天气获取失败'; }
  var wc=document.getElementById('weatherCity')?((document.getElementById('weatherCity').value||'').trim()):'';
  if(wc){
    ssGeocodeCity(wc).then(function(g){
      if(!g){ setText(T('未找到「')+wc+T('」，请检查城市名')); return; }
      var wurl='https://api.open-meteo.com/v1/forecast?latitude='+g.lat+'&longitude='+g.lon
        +'&current=temperature_2m,weather_code&daily=temperature_2m_min,temperature_2m_max&timezone=auto';
      fetch(wurl).then(function(r){return r.json();}).then(function(w){
        var cur=w.current&&w.current.temperature_2m;
        var code=w.current&&w.current.weather_code;
        var wx=ssWMO(code);
        var min=w.daily&&w.daily.temperature_2m_min&&w.daily.temperature_2m_min[0];
        var max=w.daily&&w.daily.temperature_2m_max&&w.daily.temperature_2m_max[0];
        var s=wx[0]+wx[1]+' ';
        if(cur!=null) s+=Math.round(cur)+'°C ';
        s+=wc+' ';
        if(max!=null&&min!=null) s+=Math.round(min)+'～'+Math.round(max)+'°C';
        else if(cur!=null) s+=T('当前 ')+Math.round(cur)+'°C';
        setText(s);
      }).catch(function(){ setText('天气获取失败'); });
    });
    return;
  }
  var geoUrls=['https://ipwho.is/','https://ipapi.co/json/'];
  function tryGeo(i){
    if(i>=geoUrls.length){ setText('天气获取失败'); return; }
    fetch(geoUrls[i]).then(function(r){return r.json();}).then(function(g){
      var lat=null,lon=null,en='';
      if(g){ lat=(g.latitude!=null)?g.latitude:null; lon=(g.longitude!=null)?g.longitude:null; en=g.city||g.region||''; }
      if(lat==null||lon==null) return tryGeo(i+1);
      var wurl='https://api.open-meteo.com/v1/forecast?latitude='+lat+'&longitude='+lon
        +'&current=temperature_2m,weather_code&daily=temperature_2m_min,temperature_2m_max&timezone=auto';
      Promise.all([
        fetch(wurl).then(function(r){return r.json();}),
        ssCityName(lat,lon,en)
      ]).then(function(arr){
        var w=arr[0], city=arr[1];
        var cur=w.current&&w.current.temperature_2m;
        var code=w.current&&w.current.weather_code;
        var wx=ssWMO(code);
        var min=w.daily&&w.daily.temperature_2m_min&&w.daily.temperature_2m_min[0];
        var max=w.daily&&w.daily.temperature_2m_max&&w.daily.temperature_2m_max[0];
        var s=wx[0]+wx[1]+' ';
        if(cur!=null) s+=Math.round(cur)+'°C ';
        if(city) s+=city+' ';
        if(max!=null&&min!=null) s+=Math.round(min)+'～'+Math.round(max)+'°C';
        else if(cur!=null) s+=T('当前 ')+Math.round(cur)+'°C';
        setText(s);
      }).catch(function(){ setText('天气获取失败'); });
    }).catch(function(){ tryGeo(i+1); });
  }
  tryGeo(0);
}
/* 防烧屏：时钟（含日期/天气）不时整体挪动一点位置 */
function ssJitter(){
  if(!ssState.clock) return;
  var dx=(Math.random()*26-13)|0, dy=(Math.random()*18-9)|0;
  ssState.clock.style.transform='translate('+dx+'px,'+dy+'px)';
}
/* 立即启动屏保（供「其他 → 开始屏保」按钮）：用 setTimeout 延后一帧，
   避免按钮点击本身触发的 activity 监听立刻把刚启动的屏保关掉 */
function startSaverNow(){ setTimeout(ssStart,60); }
function ssStart(){
  ssEnsureLayer();
  if(ssState.list.length){ doStart(); } else {
    fetch('?action=saverjson&t='+Date.now()).then(function(r){return r.json();}).then(function(j){
      ssState.list=(j.bing||[]).concat(j.photo||[]);
    }).catch(function(){
      ssState.list=(SAVER_JSON.bing||[]).concat(SAVER_JSON.photo||[]);
    }).then(doStart);
  }
  function doStart(){
    ssState.active=true;
    ssState.layer.classList.add('show');
    ssTick();
    if(ssState.clockTimer) clearInterval(ssState.clockTimer);
    ssState.clockTimer=setInterval(ssTick,10000);
    ssRotate();
    ssFetchWeather();
    if(ssState.weatherTimer) clearInterval(ssState.weatherTimer);
    ssState.weatherTimer=setInterval(ssFetchWeather,30*60*1000);
    ssJitter();
    if(ssState.jitterTimer) clearInterval(ssState.jitterTimer);
    ssState.jitterTimer=setInterval(ssJitter,60000);
  }
}
function ssStop(){
  if(!ssState.active) return;
  ssState.active=false;
  ssState.layer.classList.remove('show');
  if(ssState.clockTimer){clearInterval(ssState.clockTimer);ssState.clockTimer=null;}
  if(ssState.rotTimer){clearInterval(ssState.rotTimer);ssState.rotTimer=null;}
  if(ssState.weatherTimer){clearInterval(ssState.weatherTimer);ssState.weatherTimer=null;}
  if(ssState.jitterTimer){clearInterval(ssState.jitterTimer);ssState.jitterTimer=null;}
  ssResetTimer();
}
function ssResetTimer(){
  if(ssState.timer) clearTimeout(ssState.timer);
  if(!SS_ON || document.hidden) return;
  ssState.timer=setTimeout(function(){ if(!document.hidden) ssStart(); }, SS_DELAY*60*1000);
}
function ssActivity(){
  if(ssState.active) ssStop(); else ssResetTimer();
}
['mousemove','keydown','click','touchstart','wheel','scroll'].forEach(function(ev){
  document.addEventListener(ev,ssActivity,{passive:true});
  if(SSRoot!==document) SSRoot.addEventListener(ev,ssActivity,{passive:true});
});
ssResetTimer();
function onSsToggle(el){
  SS_ON=el.checked?true:false;
  var row=document.getElementById('ssDelayRow'); if(row) row.style.display=SS_ON?'':'none';
  if(!SS_ON && ssState.active) ssStop(); else ssResetTimer();
  saveAll();
}
function onSsDelayChange(el){
  SS_DELAY=parseInt(el.value,10)||1;
  ssResetTimer();
  saveAll();
}
function onWeatherToggle(el){
  WEATHER_ON=el.checked?true:false;
  var row=document.getElementById('weatherCityRow'); if(row) row.style.display=WEATHER_ON?'':'none';
  saveAll();
}
function onWeatherCityChange(el){
  WEATHER_CITY=(el.value||'').trim();
  saveAll();
}
/* 天气城市：实时搜索候选（Open-Meteo 地理编码），输入首字/首字母即弹候选，点选填入 */
function onCityInput(){
  var v=(document.getElementById('weatherCity').value||'').trim();
  clearTimeout(window.__cityTimer);
  if(!v){ hideCityDD(); return; }
  window.__cityTimer=setTimeout(function(){ searchCity(v); }, 300);
}
function onCityFocus(){ var v=(document.getElementById('weatherCity').value||'').trim(); if(v) searchCity(v); }
function onCityBlur(){ window.__cityHideT=setTimeout(hideCityDD, 180); }
function hideCityDD(){ var d=document.getElementById('cityDD'); if(d) d.style.display='none'; }
async function searchCity(q){
  try{
    var url='https://geocoding-api.open-meteo.com/v1/search?name='+encodeURIComponent(q)+'&count=8&language=zh&format=json';
    var r=await fetch(url,{cache:'no-store'});
    var j=await r.json();
    renderCityDD(j.results||[]);
  }catch(e){ hideCityDD(); }
}
function renderCityDD(list){
  var inp=document.getElementById('weatherCity'); var d=document.getElementById('cityDD');
  if(!inp||!d) return;
  if(document.activeElement!==inp){ hideCityDD(); return; }
  var rect=inp.getBoundingClientRect();
  d.style.position='fixed'; d.style.left=rect.left+'px'; d.style.top=(rect.bottom+4)+'px'; d.style.width=rect.width+'px';
  d.textContent='';
  if(!list.length){
    var e=document.createElement('div'); e.className='it empty'; e.textContent='无匹配城市，将按输入内容尝试定位';
    d.appendChild(e); d.style.display='block'; return;
  }
  list.forEach(function(c){
    var name=c.name||''; var sub=[c.admin1,c.country].filter(Boolean).join(' · ');
    var it=document.createElement('div'); it.className='it';
    var n=document.createElement('span'); n.className='nm'; n.textContent=name; it.appendChild(n);
    if(sub){ var s=document.createElement('span'); s.className='sub'; s.textContent=sub; it.appendChild(s); }
    it.onmousedown=function(ev){ ev.preventDefault(); pickCity(name); };
    d.appendChild(it);
  });
  d.style.display='block';
}
function pickCity(name){
  var inp=document.getElementById('weatherCity'); if(inp) inp.value=name;
  hideCityDD();
  if(window.__cityHideT) clearTimeout(window.__cityHideT);
  onWeatherCityChange(inp);
}
async function confirmRestore(){
  showConfirm('恢复系统原始壁纸？','当前壁纸将恢复为飞牛默认图，并关闭每日更换与轮播（屏保不受影响，可随时在设置里重开）。','恢复',async function(){
    try{ var d=await api('action=restore&ajax=1'); toast(d.msg||'已恢复'); }
    catch(e){ toast('恢复失败'); }
    refreshPage();
  });
}
/* 文件浏览（仅图片） */
var browseDir='';var browseHome='/vol1/';var pickSel={};
function joinPath(dir,name){return dir.replace(/\/+$/,'')+'/'+name;}
function openBrowse(){
  browseDir=browseHome;
  pickSel={};
  document.getElementById('dirTitle').textContent='添加照片（可多选）';
  document.getElementById('dirUseBtn').textContent='加入';
  document.getElementById('dirUseBtn').classList.remove('disabled');
  document.getElementById('dirMask').classList.add('show');
  loadDir();
}
function hideBrowse(){document.getElementById('dirMask').classList.remove('show');}
document.getElementById('dirMask').addEventListener('click',function(e){if(e.target===this)hideBrowse();});
/* 添加照片：来源选择（NAS 里的图片 / 本机电脑手机上传） */
function showSrc(){document.getElementById('srcMask').classList.add('show');}
function hideSrc(){document.getElementById('srcMask').classList.remove('show');}
document.getElementById('srcMask').addEventListener('click',function(e){if(e.target===this)hideSrc();});
var UP_LIMIT=30*1024*1024; // 本机上传单张上限 30MB（避免大图拖慢缩略图与占用带宽）
function pickLocal(){var i=document.getElementById('localFiles'); i.value=''; i.click();}
async function uploadLocal(input){
  var files=input.files; if(!files||!files.length) return;
  var ok=0, fail=0, skip=0;
  for(var i=0;i<files.length;i++){
    var f=files[i];
    if(f.size>UP_LIMIT){ skip++; continue; }
    var fd=new FormData(); fd.append('file',f,f.name);
    try{
      var r=await fetch('?action=upload&ajax=1',{method:'POST',body:fd});
      var d=await r.json();
      if(d&&d.ok) ok++; else fail++;
    }catch(e){ fail++; }
  }
  if(ok||skip||fail){
    var msg='';
    if(ok) msg+=T('已添加 ')+ok+T(' 张本机照片');
    if(skip) msg+=(msg?T('，'):'')+skip+T(' 张超过 30MB 已跳过');
    if(fail) msg+=(msg?T('，'):'')+fail+T(' 张失败');
    toast(msg||'上传完成');
    if(ok) refreshPage();
  }
}
async function loadDir(){
  var list=document.getElementById('dirList');
  list.innerHTML='<div class="empty">加载中…</div>';
  document.getElementById('dirPath').textContent=browseDir;
  var d;
  try{ d=await api('action=listdir&ajax=1&dir='+encodeURIComponent(browseDir)); }
  catch(e){list.innerHTML='<div class="empty">加载失败</div>';return;}
  if(!d.ok){list.innerHTML='<div class="empty">'+(d.msg||'读取失败')+'</div>';return;}
  var html='';
  if(browseDir!=='/vol1/' && browseDir.replace(/\/[^/]*\/?$/,'')!==''){
    var up=browseDir.replace(/\/[^/]+\/?$/,'')||'/';
    html+='<div class="diritem" data-up="'+encodeURIComponent(up)+'" onclick="goDir(decodeURIComponent(this.getAttribute(\'data-up\')))"><span class="ico">↩︎</span><span class="nm">返回上级</span></div>';
  }
  (d.dirs||[]).forEach(function(nm){
    html+='<div class="diritem" data-d="'+encodeURIComponent(nm)+'" onclick="goDir(joinPath(browseDir,decodeURIComponent(this.getAttribute(\'data-d\'))))"><span class="ico">📁</span><span class="nm">'+escHtml(nm)+'</span></div>';
  });
  var files=d.files||[];
  var capped=files.length>80;
  var shown=capped?files.slice(0,80):files;
  if(shown.length){
    html+='<div class="filegrid">';
    shown.forEach(function(nm){
      var full=joinPath(browseDir,nm);var enc=encodeURIComponent(full);
      var sel=pickSel[full]?' sel':'';
      html+='<div class="fitem'+sel+'" data-p="'+enc+'" onclick="toggleSel(this.getAttribute(\'data-p\'))">'
        +'<span class="chk">✓</span>'
        +'<img class="fimg" src="?action=thumb&ajax=1&p='+enc+'" alt="">'
        +'<div class="fnm">'+escHtml(nm)+'</div></div>';
    });
    html+='</div>';
    if(capped)html+='<div class="empty">此目录文件较多，仅显示前 80 个，请进入子目录查找</div>';
  }
  list.innerHTML=html||'<div class="empty">此目录下没有图片或子目录</div>';
}
function escHtml(s){return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
function goDir(p){browseDir=p;loadDir();}
function toggleSel(encFull){
  var full=decodeURIComponent(encFull);
  if(pickSel[full]){delete pickSel[full];}else{pickSel[full]=true;}
  document.querySelectorAll('.fitem[data-p="'+encFull+'"]').forEach(function(el){el.classList.toggle('sel',!!pickSel[full]);});
}
async function dirConfirm(){
  var add=Object.keys(pickSel).filter(function(k){return pickSel[k];});
  if(!add.length){toast('请先勾选照片');return;}
  var btn=document.getElementById('dirUseBtn');
  btn.classList.add('disabled');
  try{
    var d=await api('action=addfiles&ajax=1&kind=img&files='+encodeURIComponent(add.join('\n')));
    toast(d.msg||'已加入');
    if(d.ok){hideBrowse();refreshPage();}
  }catch(e){toast('添加失败，请重试');}
  btn.classList.remove('disabled');
}
/* 运行日志 */
async function loadLogs(){
  var el=document.getElementById('logContent');
  el.textContent='加载中…';
  try{ var r=await fetch('?action=logs&ajax=1'); var d=await r.json(); el.textContent=d.logs||'(暂无日志)'; }
  catch(e){el.textContent='读取失败';}
}
function showLogs(){document.getElementById('logLayer').classList.add('show');loadLogs();}
function hideLogs(){document.getElementById('logLayer').classList.remove('show');}
function copyLogs(){
  var txt=document.getElementById('logContent').textContent||'';
  if(!txt.trim()){toast('日志为空');return;}
  function fallback(){
    var ta=document.createElement('textarea');ta.value=txt;ta.style.position='fixed';ta.style.opacity='0';
    document.body.appendChild(ta);ta.select();
    try{document.execCommand('copy');toast('日志已复制');}catch(e){toast('复制失败，请手动长按选择');}
    document.body.removeChild(ta);
  }
  if(navigator.clipboard&&window.isSecureContext){navigator.clipboard.writeText(txt).then(function(){toast('日志已复制');},fallback);}
  else fallback();
}
/* 初始化 */
(function(){
  var roots=['/vol1/','/vol2/','/vol3/','/vol00/'];
  (function tryNext(i){
    if(i>=roots.length)return;
    api('action=listdir&ajax=1&dir='+encodeURIComponent(roots[i])).then(function(d){
      if(d.ok){browseHome=roots[i];}else tryNext(i+1);
    }).catch(function(){tryNext(i+1);});
  })(0);
})();
/* ============ i18n：设置页语言自适应（#44，zh 安全——非中文才英文化，中文零改动） ============ */
var I18N_EN={
  '当前壁纸':'Current Wallpaper','每日精选':'Daily Picks','我的照片':'My Photos',
  '壁纸轮换':'Wallpaper Rotation','屏幕保护':'Screen Saver','系统':'System','关于':'About',
  '添加照片…':'Add Photos…','添加照片':'Add Photos','添加照片（可多选）':'Add Photos (multiple)',
  '自动轮换':'Auto Rotate','轮播间隔':'Rotation Interval','合并轮播':'Merge Rotation',
  '屏幕保护程序':'Screen Saver','屏保时间':'Saver Delay','显示天气':'Show Weather',
  '天气城市':'Weather City','开始屏保':'Start Screen Saver','查看运行日志':'View Logs',
  '恢复系统原始壁纸':'Restore Default Wallpaper',
  '后台自动切换桌面壁纸（每日精选 / 我的照片）':'Auto-switch desktop wallpaper (Daily Picks / My Photos)',
  '轮播每日精选和你的照片':'Rotate Daily Picks and your photos',
  '把每日精选与我的照片放进同一个池子轮换（仅每日自动更新关闭时可用）':'Pool Daily Picks and My Photos into one rotation (only when daily auto-update is off)',
  '屏保展示当地天气；留空则按访问设备 IP 自动定位（可能不准）':'Show local weather on the saver; blank = auto-locate by device IP (may be inaccurate)',
  '留空则IP自动定位':'Blank = auto-locate by IP',
  'NAS：':'NAS: ','GitHub ›':'GitHub ›','取消':'Cancel','确定':'OK','加入':'Add',
  '复制':'Copy','刷新':'Refresh','‹ 返回':'‹ Back','运行日志':'Run Logs',
  '加载中…':'Loading…','加载失败':'Load failed','读取失败':'Read failed','返回上级':'Up',
  '此目录文件较多，仅显示前 80 个，请进入子目录查找':'This folder has many files; only the first 80 are shown. Enter a subfolder to find more.',
  '此目录下没有图片或子目录':'No images or subfolders in this folder',
  '无匹配城市，将按输入内容尝试定位':'No matching city; will try to locate using your input',
  'NAS里的图片':'Images on NAS','电脑/手机的图片':'Images from this device',
  '恢复系统原始壁纸？':'Restore default wallpaper?',
  '当前壁纸将恢复为飞牛默认图，并关闭每日更换与轮播（屏保不受影响，可随时在设置里重开）。':'Current wallpaper will revert to the fnOS default, and daily update & rotation will be turned off (screen saver is unaffected; you can re-enable anytime).',
  '恢复':'Restore','正在更新壁纸':'Updating wallpaper',
  '飞牛正在重启网页服务，连接会短暂中断，请勿关闭页面。':'fnOS is restarting the web service; the connection may briefly drop. Please don\'t close this page.',
  '请稍候…':'Please wait…','每日自动更新':'Daily Auto-Update',
  '每日精选还是空的，首次安装或重启后会自动填充最近 7 天，也可稍后在设置里手动补齐。':'Daily Picks is empty; it fills with the last 7 days automatically after first install or reboot, or you can top it up manually in settings later.',
  '正在保存…':'Saving…','已保存':'Saved','保存失败，请重试':'Save failed, please retry',
  '正在应用…':'Applying…','已应用':'Applied','应用失败，请重试':'Apply failed, please retry',
  '已移除':'Removed','移除失败':'Remove failed','正在开启每日自动更新…':'Enabling daily auto-update…',
  '已开启每日自动更新':'Daily auto-update enabled','操作失败，请重试':'Operation failed, please retry',
  '正在补齐最近 7 天每日精选…':'Backfilling last 7 days of Daily Picks…',
  '已补齐最近 7 天每日精选':'Backfilled last 7 days of Daily Picks',
  '补齐超时，请手动刷新':'Backfill timed out, please refresh manually','无法开始':'Cannot start',
  '启动失败，请重试':'Start failed, please retry','已恢复':'Restored','恢复失败':'Restore failed',
  '上传完成':'Upload complete','请先勾选照片':'Please select photos first','已加入':'Added',
  '添加失败，请重试':'Add failed, please retry','(暂无日志)':'(No logs yet)','日志为空':'Logs empty',
  '日志已复制':'Logs copied','复制失败，请手动长按选择':'Copy failed; please long-press to select',
  '天气获取失败':'Weather unavailable',
  '未找到「':'City not found: ','」，请检查城市名':'',
  ' 张本机照片':' local photos',' 张超过 30MB 已跳过':' over 30MB skipped',' 张失败':' failed',
  ' 张':' photos','当前 ':'Now ','，':',','飞牛视野 - 壁纸':'FnVista - Wallpaper'
};
function T(s){ return (s==null)?s:(ssIsZh()? s : (I18N_EN[s]!==undefined? I18N_EN[s] : s)); }
function i18nUnits(s){
  if(s==null) return s;
  s=s.replace(/(\d+)\s*秒/g,function(m,d){return d+'s';})
     .replace(/(\d+)\s*小时/g,function(m,d){return d+' h';})
     .replace(/(\d+)\s*分钟/g,function(m,d){return d+' min';})
     .replace(/(\d+)\s*张/g,function(m,d){return d+' photos';});
  return s;
}
function i18nText(s){ var t=T(s); if(t===s) t=i18nUnits(s); return t; }
function i18nNode(n){
  if(!n) return;
  var tag=(n.parentNode&&n.parentNode.tagName)?n.parentNode.tagName.toUpperCase():'';
  if(tag==='STYLE'||tag==='SCRIPT'||tag==='SVG') return;
  if(n.nodeType===3){
    var t=i18nText(n.nodeValue);
    if(t!==n.nodeValue) n.nodeValue=t;
  } else if(n.nodeType===1){
    if(n.placeholder){ var p=i18nText(n.placeholder); if(p!==n.placeholder) n.placeholder=p; }
    if(n.alt){ var a=i18nText(n.alt); if(a!==n.alt) n.alt=a; }
    if(n.title){ var ti=i18nText(n.title); if(ti!==n.title) n.title=ti; }
  }
}
function i18nWalk(root){
  if(!root) return;
  var wl=[root];
  while(wl.length){
    var el=wl.shift();
    i18nNode(el);
    var ch=el.childNodes, i;
    for(i=0;i<ch.length;i++){ if(ch[i].nodeType===1||ch[i].nodeType===3) wl.push(ch[i]); }
  }
}
function i18nInit(){
  if(ssIsZh()) return; /* 中文：完全不改 */
  document.documentElement.lang='en';
  if(document.title) document.title=i18nText(document.title);
  i18nWalk(document.body);
  if(window.MutationObserver){
    var mo=new MutationObserver(function(muts){
      for(var k=0;k<muts.length;k++){
        var m=muts[k];
        if(m.type==='childList'){
          for(var j=0;j<m.addedNodes.length;j++){
            var an=m.addedNodes[j];
            if(an.nodeType===1) i18nWalk(an); else i18nNode(an);
          }
        } else if(m.type==='characterData'){ i18nNode(m.target); }
      }
    });
    mo.observe(document.body,{childList:true,subtree:true,characterData:true});
  }
}
i18nInit();
</script>
</body>
</html>
HTMLPAGE
exit 0
