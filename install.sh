#!/bin/bash
# ============================================================
#  飞牛 fnOS Bing 每日壁纸 - 安装脚本
#
#  用法:
#    sudo ./install.sh                 安装（默认每天 07:00 自动更换）
#    sudo ./install.sh --time 06:30    指定每天的更换时间
#    sudo ./install.sh --no-run        安装但不立即更换壁纸
#    sudo ./install.sh --uninstall     卸载并恢复系统原始壁纸
#
#  远程一键安装:
#    curl -fsSL https://raw.githubusercontent.com/pmxwxhn/fnos-bing-wallpaper/main/install.sh | sudo bash
# ============================================================

set -uo pipefail

REPO="mclhm8182/fnos-bing-wallpaper"
RAW_BASE="https://raw.githubusercontent.com/${REPO}/main"

SBIN_DIR="/usr/local/sbin"
CRON_FILE="/etc/cron.d/fnos-bing-wallpaper"
DAILY_BIN="$SBIN_DIR/bing-wallpaper-daily.sh"
SET_BIN="$SBIN_DIR/fnos-wallpaper-set.sh"
DATA_DIR="/var/lib/fnos-wallpaper"
LOG_FILE="/var/log/fnos-bing-wallpaper.log"

RUN_TIME="07:00"
DO_RUN=1
DO_UNINSTALL=0

die()  { echo "错误: $*" >&2; exit 1; }
info() { echo "  $*"; }
ok()   { echo "  ✓ $*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --time)      RUN_TIME="${2:-}"; shift 2 ;;
    --time=*)    RUN_TIME="${1#*=}"; shift ;;
    --no-run)    DO_RUN=0; shift ;;
    --uninstall) DO_UNINSTALL=1; shift ;;
    -h|--help)   sed -n '2,15p' "$0"; exit 0 ;;
    *) die "未知参数: $1（可用 --help 查看）" ;;
  esac
done

[ "$(id -u)" -eq 0 ] || die "请用 root 运行：sudo ./install.sh"

# ---------- 卸载 ----------
if [ "$DO_UNINSTALL" -eq 1 ]; then
  echo "== 卸载 =="
  if [ -x "$SET_BIN" ]; then
    bash "$SET_BIN" --restore || echo "  恢复原始壁纸失败，可手动检查 $DATA_DIR/orig"
  fi
  rm -f "$CRON_FILE" "$DAILY_BIN" "$SET_BIN"
  ok "已删除脚本与定时任务"
  echo
  echo "数据目录 $DATA_DIR 保留了壁纸缓存和原始备份，如确认不需要可手动删除："
  echo "  sudo rm -rf $DATA_DIR $LOG_FILE"
  exit 0
fi

# ---------- 环境检查 ----------
echo "== 检查环境 =="
[ -d /usr/trim/www/static/bg ] || die "没找到 /usr/trim/www/static/bg，这台机器看起来不是飞牛 fnOS"

command -v curl >/dev/null 2>&1 || die "缺少 curl"
ok "curl 可用"

if ! command -v ffmpeg >/dev/null 2>&1; then
  die "缺少 ffmpeg。请先执行：sudo apt update && sudo apt install -y ffmpeg"
fi
command -v ffmpeg >/dev/null && ok "ffmpeg 可用 ($(ffmpeg -version 2>/dev/null | head -1 | cut -d' ' -f1-3))"

if command -v chattr >/dev/null 2>&1 || [ -x /usr/bin/chattrx ]; then
  ok "chattr 可用（用于锁定壁纸文件，防止被系统还原）"
else
  echo "  ! 未找到 chattr，壁纸可能被系统自动还原"
fi

# ---------- 准备脚本文件 ----------
echo
echo "== 准备脚本 =="

# 判断是本地安装（压缩包解压后）还是远程安装（curl | bash）
SELF_PATH="${BASH_SOURCE[0]:-}"
SRC_DIR=""

if [ -n "$SELF_PATH" ] && [ -f "$SELF_PATH" ]; then
  CANDIDATE="$(cd "$(dirname "$SELF_PATH")" && pwd)"
  if [ -f "$CANDIDATE/scripts/fnos-wallpaper-set.sh" ]; then
    SRC_DIR="$CANDIDATE/scripts"
    ok "使用本地脚本: $SRC_DIR"
  fi
fi

TMP_DL=""
if [ -z "$SRC_DIR" ]; then
  info "未找到本地脚本，从 GitHub 下载..."
  TMP_DL="$(mktemp -d)"
  for f in fnos-wallpaper-set.sh bing-wallpaper-daily.sh; do
    curl -fsSL --max-time 60 -o "$TMP_DL/$f" "$RAW_BASE/scripts/$f" \
      || die "下载 $f 失败，请检查网络或改用压缩包安装"
    info "已下载 $f"
  done
  SRC_DIR="$TMP_DL"
fi

# ---------- 安装 ----------
echo
echo "== 安装 =="
install -m 755 "$SRC_DIR/fnos-wallpaper-set.sh"  "$SET_BIN"   || die "安装失败"
install -m 755 "$SRC_DIR/bing-wallpaper-daily.sh" "$DAILY_BIN" || die "安装失败"
ok "脚本已安装到 $SBIN_DIR"

# 校验时间格式
if ! printf '%s' "$RUN_TIME" | grep -qE '^([01][0-9]|2[0-3]):[0-5][0-9]$'; then
  die "时间格式不对：$RUN_TIME（应为 HH:MM，例如 07:00）"
fi
CRON_H="${RUN_TIME%%:*}"
CRON_M="${RUN_TIME##*:}"
# cron 会把前导 0 当八进制，去掉前导零更稳妥
CRON_H=$((10#$CRON_H))
CRON_M=$((10#$CRON_M))

cat > "$CRON_FILE" <<EOF
# 飞牛 fnOS Bing 每日壁纸
# 由 install.sh 生成，卸载请执行 install.sh --uninstall
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
$CRON_M $CRON_H * * * root $DAILY_BIN >/dev/null 2>&1
EOF
chmod 644 "$CRON_FILE"
chown root:root "$CRON_FILE"
ok "定时任务已安装：每天 $(printf '%02d:%02d' "$CRON_H" "$CRON_M")"

if ! systemctl is-active cron >/dev/null 2>&1; then
  systemctl enable --now cron >/dev/null 2>&1 || echo "  ! cron 服务未运行，请手动启动：systemctl enable --now cron"
fi
ok "cron 服务运行中"

[ -n "$TMP_DL" ] && rm -rf "$TMP_DL"

# ---------- 立即执行一次 ----------
echo
if [ "$DO_RUN" -eq 1 ]; then
  echo "== 立即应用今日壁纸 =="
  if bash "$DAILY_BIN" --force; then
    ok "壁纸已更新"
  else
    echo "  ! 首次应用失败，可稍后手动重试：sudo $DAILY_BIN --force"
  fi
else
  echo "已跳过立即执行，可手动运行：sudo $DAILY_BIN --force"
fi

echo
echo "============================================"
echo " 安装完成"
echo "============================================"
echo " 刷新浏览器（F5）即可看到新壁纸。"
echo
echo " 常用命令："
echo "   sudo $DAILY_BIN --force      立即换一张"
echo "   sudo $SET_BIN --status       查看当前状态"
echo "   sudo $SET_BIN --restore      恢复系统原始壁纸"
echo "   sudo $SET_BIN 图片路径        换成本地图片"
echo "   tail -f $LOG_FILE            查看日志"
echo
echo " 卸载：sudo ./install.sh --uninstall"
echo "============================================"
