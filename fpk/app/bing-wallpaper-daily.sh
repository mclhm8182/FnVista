#!/bin/bash
# ============================================================
#  bing-wallpaper-daily.sh
#  抓取 Bing 每日壁纸，维护「每日精选」（保留最近 7 张，自动删除更早的）。
#
#  两种模式（由 /var/lib/fnos-wallpaper/bing_auto 控制，二选一）：
#    · 每日自动更新开启（默认）：把当日 Bing 设为桌面壁纸（零断连通道）。
#    · 每日自动更新关闭（用户在「我的照片」模式）：仅刷新「每日精选」
#      缓存，不抢占桌面；用户可在「每日精选」里点选某张固定显示。
#
#  用法:
#    bing-wallpaper-daily.sh            抓当日壁纸（同一天不重复应用）
#    bing-wallpaper-daily.sh --force    强制重新下载并应用（同时开启每日自动更新）
#    bing-wallpaper-daily.sh --dry-run  只下载，不修改系统
#    bing-wallpaper-daily.sh --backfill 补齐最近 7 天历史到「每日精选」（不应用）
#
#  环境变量: BING_MARKET（默认 zh-CN）
#  依赖: curl + ffmpeg + 同目录的 fnos-wallpaper-set.sh。建议由 root 的 cron 调用。
# ============================================================

set -uo pipefail

DATA_DIR="/var/lib/fnos-wallpaper"
WALL_DIR="$DATA_DIR/wallpapers"
THUMB_DIR="$DATA_DIR/thumbs"
STATE_FILE="$DATA_DIR/last_startdate"
BING_AUTO_FILE="$DATA_DIR/bing_auto"
LOG_FILE="/var/log/fnos-bing-wallpaper.log"
KEEP=7   # 「每日精选」保留最近 7 张

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -x "$SELF_DIR/fnos-wallpaper-set.sh" ]; then
  CHANGER="$SELF_DIR/fnos-wallpaper-set.sh"
elif [ -x "/usr/local/sbin/fnos-wallpaper-set.sh" ]; then
  CHANGER="/usr/local/sbin/fnos-wallpaper-set.sh"
else
  CHANGER="$SELF_DIR/fnos-wallpaper-set.sh"
fi

MARKET="${BING_MARKET:-zh-CN}"
UA="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"

FORCE=0
DRY=0
BACKFILL=0
for a in "$@"; do
  case "$a" in
    --force)    FORCE=1 ;;
    --dry-run)  DRY=1 ;;
    --backfill) BACKFILL=1 ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "未知参数: $a（可用 --force / --backfill / --dry-run / --help）" >&2; exit 2 ;;
  esac
done

log() { printf '[%s] %s\n' "$(date '+%F %T')" "$*" >> "$LOG_FILE"; }
if [ -f "$LOG_FILE" ] && [ "$(stat -c%s "$LOG_FILE" 2>/dev/null || echo 0)" -gt 524288 ]; then
  tail -n 300 "$LOG_FILE" > "$LOG_FILE.tmp" 2>/dev/null && mv "$LOG_FILE.tmp" "$LOG_FILE"
fi

# 每日自动更新：文件缺失视为开启（默认行为）
bing_auto_on() {
  [ ! -f "$BING_AUTO_FILE" ] && return 0
  [ "$(cat "$BING_AUTO_FILE" 2>/dev/null)" = "1" ]
}

download() {
  local url="$1" sz
  curl -fsSL --max-time 120 -A "$UA" --retry 2 --retry-delay 3 -o "$TMP" "$url" 2>/dev/null || return 1
  sz=$(stat -c%s "$TMP" 2>/dev/null || echo 0)
  [ "$sz" -gt 100000 ] || { rm -f "$TMP"; return 1; }
  return 0
}

# 保留窗口 / 上限（可在 $DATA_DIR/retention_days、retention_max 覆盖，缺省 7 天 / 30 张）
retention_days() { local d; d="$(cat "$DATA_DIR/retention_days" 2>/dev/null || echo 7)"; case "$d" in ''|*[!0-9]*) d=7 ;; esac; [ "$d" -ge 1 ] || d=7; printf '%s' "$d"; }
retention_max()  { local m; m="$(cat "$DATA_DIR/retention_max" 2>/dev/null || echo 30)"; case "$m" in ''|*[!0-9]*) m=30 ;; esac; printf '%s' "$m"; }

# 孤儿缩略图清扫：缩略图对应的壁纸已不存在则删除（可重建缓存）。
# 专门处理历史遗留的「只增不减」缩略图膨胀——旧版 prune_old 未清理 _350 / saver 缩略图。
sweep_orphan_thumbs() {
  [ -d "$THUMB_DIR" ] || return 0
  local b w
  for th in "$THUMB_DIR"/bing-*_350.jpg "$THUMB_DIR"/saver-bing-*.jpg; do
    [ -f "$th" ] || continue
    b="$(basename "$th")"
    case "$b" in
      bing-*_350.jpg)  w="bing-${b#bing-}"; w="${w%_350.jpg}.jpg" ;;
      saver-bing-*.jpg) w="${b#saver-}.jpg" ;;
      *) continue ;;
    esac
    [ -f "$WALL_DIR/$w" ] || { rm -f "$th"; log "清理孤儿缩略图: $b"; }
  done
}

# 取某壁纸文件的 urlbase（meta 第 3 字段；旧版 2 字段文件返回空）
ub_of() { awk -F'\t' 'NR==1{print $3}' "$1" 2>/dev/null; }

# 去重：同一张真实 Bing 图（相同内容）绝不在「每日精选」占两个日期槽。
# 按文件内容 md5 分组，每组仅保留日期最早的那张（最可能是真实发布日），删除其余；
# 同时清理历史遗留重复：backfill 同日同图复制、当日下载失败兜底 cp 旧图等。
dedup_picks() {
  [ -d "$WALL_DIR" ] || return 0
  local f d h tmp
  tmp="$(mktemp)"
  for f in "$WALL_DIR"/bing-*.jpg; do
    [ -f "$f" ] || continue
    h="$(md5sum "$f" 2>/dev/null | awk '{print $1}')"
    [ -n "$h" ] || continue
    d="$(basename "$f")"; d="${d#bing-}"; d="${d%.jpg}"
    printf '%s\t%s\n' "$h" "$d" >> "$tmp"
  done
  [ -s "$tmp" ] || { rm -f "$tmp"; return 0; }
  sort -k1,1 -k2,2 "$tmp" | awk -F'\t' '
    {
      if ($1 != prev_h) { prev_h=$1 }
      else { print $2 }
      prev_h=$1
    }' | while read -r d; do
    f="$WALL_DIR/bing-$d.jpg"
    rm -f "$f" "$f.meta" "$THUMB_DIR/bing-${d}_350.jpg" "$THUMB_DIR/saver-bing-$d.jpg" 2>/dev/null
    log "去重删除重复图: bing-$d.jpg"
  done
  rm -f "$tmp"
}

# 维护检修：每日精选不足 7 张时，仅在确实存在「本地缺失且不与现有图重复的真实 Bing 条目」时才补齐；
# 若真实可达数量本就 <7（如 Bing 归档某日复用同一张图 / 接口缺口），则不强行为凑数而复制重复图。
self_heal() {
  local n
  n="$(ls -1 "$WALL_DIR"/bing-*.jpg 2>/dev/null | wc -l | tr -d ' ')"
  [ "${n:-0}" -lt 7 ] || return 0
  local json miss=0 ed ub EXIST_UB
  json=$(curl -fsSL --max-time 30 -A "$UA" \
    "https://cn.bing.com/HPImageArchive.aspx?format=js&idx=0&n=8&mkt=$MARKET" 2>/dev/null \
   || curl -fsSL --max-time 30 -A "$UA" \
    "https://www.bing.com/HPImageArchive.aspx?format=js&idx=0&n=8&mkt=$MARKET" 2>/dev/null)
  if [ -n "${json:-}" ] && printf '%s' "$json" | grep -q '"images"'; then
    EXIST_UB="$(for ef in "$WALL_DIR"/bing-*.jpg; do ub_of "$ef"; done 2>/dev/null | grep -v '^$' | sort -u)"
    while IFS=$'\t' read -r ed ub _; do
      [ -n "$ed" ] && [ -n "$ub" ] || continue
      [ -f "$WALL_DIR/bing-$ed.jpg" ] && continue
      if printf '%s\n' "$EXIST_UB" | grep -qxF "$ub"; then continue; fi
      miss=$((miss + 1))
    done < <(printf '%s' "$json" | python3 -c '
import json,sys
for i in json.load(sys.stdin).get("images",[]):
    print("\t".join([i.get("enddate",""), i.get("urlbase","")]))' 2>/dev/null)
  fi
  if [ "$miss" -le 0 ]; then
    log "每日精选当前 ${n} 张（已无更多真实缺口，不强行凑数）"
    return 0
  fi
  log "每日精选 ${n} 张，发现 ${miss} 个真实缺口，触发补齐"
  bash "$0" --backfill >> "$LOG_FILE" 2>&1 || true
}

# 保留最近 N 张（按文件名日期，最新在前），删除更早的及对应 .meta / 缩略图(_350) / 屏保缩略图(saver-)。
# 用「保留最新 N 张」取代脆弱的日历 cutoff——避免 Bing 归档窗口偏移（idx=0 有时含今天有时到昨天）
# 导致边界日被误删，从而稳定保留恰好 N 张最新的真实 Bing 图。
prune_old() {
  local keep kept f base max
  keep="$(retention_days)"; max="$(retention_max)"
  [ "$keep" -ge 1 ] || keep=7
  [ "$keep" -gt "$max" ] && keep="$max"
  kept=0
  while read -r f; do
    [ -f "$f" ] || continue
    kept=$((kept + 1))
    if [ "$kept" -gt "$keep" ]; then
      # 删除壁纸本体、meta、每日精选缩略图(_350)、屏保缩略图(saver-)
      base="$(basename "$f")"
      rm -f "$f" "$f.meta" "$THUMB_DIR/${base%.jpg}_350.jpg" "$THUMB_DIR/saver-${base%.jpg}.jpg"
      log "清理过期壁纸: $base"
    fi
  done < <(ls -1 "$WALL_DIR"/bing-*.jpg 2>/dev/null | sort -r)
  sweep_orphan_thumbs
}

# 兜底补齐「今日」一张：backfill 的归档窗口有时不含当天，确保「每日精选」始终含当日图。
# 统一按 enddate（=本地当日）命名，与 backfill / 主流程一致。
ensure_today() {
  local e ub ti co j
  j="$(fetch_meta 2>/dev/null)" || return 0
  e="$(printf '%s' "$j" | sed -n 's/.*"enddate":"\([0-9]\{8\}\)".*/\1/p' | head -1)"
  ub="$(printf '%s' "$j" | sed -n 's/.*"urlbase":"\([^"]*\)".*/\1/p' | head -1)"
  ti="$(printf '%s' "$j" | sed -n 's/.*"title":"\([^"]*\)".*/\1/p' | head -1)"
  co="$(printf '%s' "$j" | sed -n 's/.*"copyright":"\([^"]*\)".*/\1/p' | head -1)"
  [ -n "$e" ] && [ -n "$ub" ] || return 0
  [ -f "$WALL_DIR/bing-$e.jpg" ] && return 0
  URLBASE="$ub"; TITLE="$ti"; COPYRIGHT="$co"
  TARGET="$WALL_DIR/bing-$e.jpg"
  if download_today; then
    log "ensure_today 补齐当日壁纸: bing-$e.jpg"
  else
    log "ensure_today 当日壁纸下载失败（稍后由每日/照片模式任务重试）"
  fi
}

# 下载当日 Bing 到 TARGET 并写 meta（title / copyright，用 tab 分隔）。
# 失败自动重试 + 指数退避（2/4/8/16s，最长 60s），应对网络抖动 / 限流。
download_today() {
  [ -n "$URLBASE" ] || return 1
  local attempt max_attempt delay
  max_attempt=4
  attempt=0
  while [ "$attempt" -lt "$max_attempt" ]; do
    attempt=$((attempt + 1))
    TMP="$TARGET.part"; rm -f "$TMP"
    if download "https://cn.bing.com${URLBASE}_UHD.jpg"; then
      :
    elif download "https://cn.bing.com${URLBASE}_1920x1080.jpg"; then
      :
    else
      delay=$((1 << attempt)); [ "$delay" -gt 60 ] && delay=60
      log "当日壁纸下载失败（第 ${attempt}/${max_attempt} 次），${delay}s 后重试（指数退避）"
      [ "$attempt" -lt "$max_attempt" ] && sleep "$delay"
      continue
    fi
    mv -f "$TMP" "$TARGET" || return 1
    printf '%s\t%s\t%s\n' "${TITLE:-}" "${COPYRIGHT:-}" "${URLBASE:-}" > "$TARGET.meta"
    return 0
  done
  return 1
}

# 补齐已有 bing-*.jpg 缺失的标题/说明（取自 Bing 归档 n=8，覆盖保留窗口内的 7 张）
repair_meta(){
  local tmp
  tmp="$(mktemp)"
  curl -fsSL --max-time 30 -A "$UA" \
    "https://cn.bing.com/HPImageArchive.aspx?format=js&idx=0&n=8&mkt=$MARKET" 2>/dev/null > "$tmp" \
   || curl -fsSL --max-time 30 -A "$UA" \
    "https://www.bing.com/HPImageArchive.aspx?format=js&idx=0&n=8&mkt=$MARKET" 2>/dev/null > "$tmp"
  if [ ! -s "$tmp" ] || ! grep -q '"images"' "$tmp"; then rm -f "$tmp"; return 0; fi
  python3 - "$tmp" "$WALL_DIR" <<'PY' 2>/dev/null || true
import json,sys,os
p=sys.argv[1]; wd=sys.argv[2]
try: d=json.load(open(p,encoding="utf-8"))
except Exception: sys.exit(0)
m={}
for i in d.get("images",[]):
    ed=(i.get("enddate","") or "").strip()
    if ed: m[ed]=((i.get("title","") or ""), (i.get("copyright","") or ""), (i.get("urlbase","") or ""))
for f in os.listdir(wd):
    if not (f.startswith("bing-") and f.endswith(".jpg")): continue
    ed=f[5:-4]
    meta=os.path.join(wd,f+".meta")
    need=True
    if os.path.exists(meta):
        try:
            parts=open(meta,encoding="utf-8").read().rstrip("\n").split("\t")
            # 已有完整 3 字段（含 urlbase）且标题非空则跳过；否则补齐 urlbase
            if len(parts)>=3 and parts[0].strip(): need=False
        except Exception: pass
    if need and ed in m:
        try: open(meta,"w",encoding="utf-8").write("%s\t%s\t%s\n"%(m[ed][0],m[ed][1],m[ed][2]))
        except Exception: pass
PY
  rm -f "$tmp"
}

# ---------- 回填模式：补齐最近 7 天历史到每日精选（不应用） ----------
if [ "$BACKFILL" -eq 1 ]; then
  mkdir -p "$WALL_DIR" 2>/dev/null
  json=$(curl -fsSL --max-time 30 -A "$UA" \
    "https://cn.bing.com/HPImageArchive.aspx?format=js&idx=0&n=8&mkt=$MARKET" 2>/dev/null \
   || curl -fsSL --max-time 30 -A "$UA" \
    "https://www.bing.com/HPImageArchive.aspx?format=js&idx=0&n=8&mkt=$MARKET" 2>/dev/null)
  if [ -z "${json:-}" ] || ! printf '%s' "$json" | grep -q '"images"'; then
    printf 'done\n' > "$DATA_DIR/backfill.status" 2>/dev/null
    echo "回填失败：无法获取 Bing 归档" >&2; exit 1
  fi
  LIST=$(printf '%s' "$json" | python3 -c '
import json,sys
d=json.load(sys.stdin)
for i in d.get("images",[]):
    print("\t".join([i.get("enddate",""), i.get("urlbase",""), i.get("title",""), i.get("copyright","")]))
' 2>/dev/null)
  [ -n "$LIST" ] || { echo "回填失败：归档解析失败" >&2; exit 1; }
  EXIST_UB="$(for ef in "$WALL_DIR"/bing-*.jpg; do ub_of "$ef"; done 2>/dev/null | grep -v '^$' | sort -u)"
  DL_UBF="$(mktemp)"
  printf '%s\n' "$LIST" | while IFS=$'\t' read -r ed ub ti co; do
    [ -n "$ed" ] && [ -n "$ub" ] || continue
    T="$WALL_DIR/bing-$ed.jpg"
    [ -f "$T" ] && continue
    # 去重：同一张真实 Bing 图（urlbase 相同）已存在于「每日精选」或本次已下载，则跳过，不占第二个日期槽
    if printf '%s\n%s\n' "$EXIST_UB" "$(cat "$DL_UBF" 2>/dev/null)" | grep -qxF "$ub"; then
      log "回填跳过重复图（urlbase 已存在）: enddate=$ed"
      continue
    fi
    TMP="$T.part"; rm -f "$TMP"
    if curl -fsSL --max-time 120 -A "$UA" --retry 2 -o "$TMP" "https://cn.bing.com${ub}_UHD.jpg" 2>/dev/null \
       && [ "$(stat -c%s "$TMP" 2>/dev/null || echo 0)" -gt 100000 ]; then
      :
    elif curl -fsSL --max-time 120 -A "$UA" --retry 2 -o "$TMP" "https://cn.bing.com${ub}_1920x1080.jpg" 2>/dev/null \
       && [ "$(stat -c%s "$TMP" 2>/dev/null || echo 0)" -gt 100000 ]; then
      :
    else
      rm -f "$TMP"; continue
    fi
    mv -f "$TMP" "$T"
    printf '%s\t%s\t%s\n' "$ti" "$co" "$ub" > "$T.meta"
    printf '%s\n' "$ub" >> "$DL_UBF"
    log "回填成功: bing-$ed.jpg ($(printf '%s' "$ti" | head -c 40))"
  done
  rm -f "$DL_UBF"
  repair_meta
  dedup_picks
  ensure_today
  prune_old
  printf 'done\n' > "$DATA_DIR/backfill.status" 2>/dev/null
  n="$(ls -1 "$WALL_DIR"/bing-*.jpg 2>/dev/null | wc -l | tr -d ' ')"
  echo "回填完成（缺失的已从 Bing 归档补齐，当前每日精选 ${n} 张真实 Bing 图，无重复）"
  exit 0
fi

mkdir -p "$WALL_DIR" 2>/dev/null || { echo "无法创建 $WALL_DIR（需要 root）" >&2; exit 1; }

# ---------- 1. 取 Bing 元数据 ----------
fetch_meta() {
  local host json i
  for host in cn.bing.com www.bing.com; do
    for i in 1 2 3; do
      json=$(curl -fsSL --max-time 20 -A "$UA" \
        "https://$host/HPImageArchive.aspx?format=js&idx=0&n=1&mkt=$MARKET" 2>/dev/null)
      if [ -n "${json:-}" ] && printf '%s' "$json" | grep -q '"urlbase"'; then
        printf '%s' "$json"; return 0
      fi
      log "取元数据失败 ($host 第 ${i} 次)，5 秒后重试"; sleep 5
    done
  done
  return 1
}

JSON=$(fetch_meta) || { log "ERROR 无法获取 Bing 壁纸信息，本次跳过"; echo "无法获取 Bing 壁纸信息" >&2; exit 1; }

STARTDATE=$(printf '%s' "$JSON"  | sed -n 's/.*"startdate":"\([0-9]\{8\}\)".*/\1/p' | head -1)
# 每日精选按 Bing 的 enddate 命名（enddate 即本地当日日期，startdate 比本地晚一天）。
# 此前主流程误用 startdate 命名，而 backfill 用 enddate 命名，导致同一张当天图被两个
# 路径起了不同文件名，今日瓷砖永远落不到本地当天——这是「今天没更新」的根因之一。
ENDDATE=$(printf '%s' "$JSON"    | sed -n 's/.*"enddate":"\([0-9]\{8\}\)".*/\1/p'   | head -1)
URLBASE=$(printf '%s' "$JSON"    | sed -n 's/.*"urlbase":"\([^"]*\)".*/\1/p'                | head -1)
COPYRIGHT=$(printf '%s' "$JSON"  | sed -n 's/.*"copyright":"\([^"]*\)".*/\1/p'              | head -1)
TITLE=$(printf '%s' "$JSON"      | sed -n 's/.*"title":"\([^"]*\)".*/\1/p'                  | head -1)

if [ -z "$ENDDATE" ] || [ -z "$URLBASE" ]; then
  log "ERROR 元数据解析失败 enddate='$ENDDATE' urlbase='$URLBASE'"; exit 1
fi

TARGET="$WALL_DIR/bing-$ENDDATE.jpg"

# ---------- 2. 照片模式：仅刷新缓存，不抢占桌面 ----------
if ! bing_auto_on; then
  if [ ! -f "$TARGET" ]; then
    download_today || log "ERROR 照片模式下今日 Bing 下载失败"
  fi
  repair_meta
  prune_old
  dedup_picks
  if [ -f "$TARGET" ]; then
    echo "（照片模式）已刷新每日精选缓存，未改动桌面"
  else
    echo "（照片模式）今日 Bing 暂未取到，未改动桌面"
  fi
  self_heal
  exit 0
fi

# ---------- 3. 每日自动更新模式 ----------
ALREADY=0
if [ "$FORCE" -eq 0 ] && [ -f "$STATE_FILE" ] && [ "$(cat "$STATE_FILE" 2>/dev/null)" = "$ENDDATE" ]; then
  ALREADY=1
fi
if [ "$ALREADY" -eq 1 ] && [ -f "$TARGET" ]; then
  log "今日壁纸 ($ENDDATE) 已应用，跳过"
  echo "今日壁纸已应用（用 --force 可强制重设）"
  exit 0
fi

USED_FALLBACK=0
if ! download_today; then
  # 末次成功图兜底：当日抓取失败时复用最近一张已存在的 Bing 图，避免空屏 / 卡死
  LASTGOOD="$(ls -1t "$WALL_DIR"/bing-*.jpg 2>/dev/null | grep -vxF "$TARGET" | head -1)"
  if [ -n "$LASTGOOD" ]; then
    cp -f "$LASTGOOD" "$TARGET" 2>/dev/null
    [ -f "$LASTGOOD.meta" ] && cp -f "$LASTGOOD.meta" "$TARGET.meta" 2>/dev/null
    USED_FALLBACK=1
    log "WARN 当日 Bing 下载失败，使用末次成功图兜底: $(basename "$LASTGOOD")"
  else
    log "ERROR 图片下载失败且无末次成功图可用，本次跳过"; echo "图片下载失败" >&2; exit 1
  fi
fi

if [ "$DRY" -eq 1 ]; then
  log "DRY-RUN 已下载到 $TARGET，未修改系统"
  echo "已下载到 $TARGET（仅试运行，未修改系统）"
  exit 0
fi

[ -f "$CHANGER" ] || { log "ERROR 找不到 $CHANGER"; echo "找不到 fnos-wallpaper-set.sh" >&2; exit 1; }

# --force 同时恢复每日自动更新
if [ "$FORCE" -eq 1 ]; then
  echo "1" > "$BING_AUTO_FILE" 2>/dev/null
fi

if bash "$CHANGER" "$TARGET" >> "$LOG_FILE" 2>&1; then
  # 兜底情况下不写 last_startdate，确保次日仍会重试当日真实壁纸
  [ "$USED_FALLBACK" -eq 0 ] && echo "$ENDDATE" > "$STATE_FILE"
  log "OK 壁纸已更新为 $ENDDATE${USED_FALLBACK:+'（末次成功图兜底）'}"
  echo "壁纸已更新：${TITLE:-$COPYRIGHT}"
else
  log "ERROR fnos-wallpaper-set.sh 执行失败，详见日志 $LOG_FILE"
  echo "应用失败，详见日志 $LOG_FILE" >&2
  exit 1
fi

repair_meta
prune_old
dedup_picks
self_heal
exit 0
