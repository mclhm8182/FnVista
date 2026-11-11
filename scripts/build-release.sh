#!/bin/bash
# build-release.sh — 构建新版本 fpk，并永久保留所有历史版本
#
# 规则（用户明确要求）：只增不删。任何时候都不要删除 dist/ 下已有的旧 fpk，
# 也不要删除 versions/ 下的旧版本源码快照。版本号一旦发布就不再覆盖。
#
# 用法： ./scripts/build-release.sh
# 依赖： fnpack（默认取工作区 ../../fnpack，可用 FNPACK 环境变量覆盖）

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

FNPACK="${FNPACK:-$ROOT/../../fnpack}"
[ -x "$FNPACK" ] || { echo "找不到 fnpack: $FNPACK" >&2; exit 1; }

VER="$(grep -E '^version=' fpk/manifest | head -1 | cut -d= -f2 | tr -d '\r\n ')"
[ -n "$VER" ] || { echo "manifest 缺少 version" >&2; exit 1; }

OUT="dist/FnVista-${VER}.fpk"
SNAP="versions/${VER}"

# 硬性保护：已发布的版本默认不允许覆盖。
# 仅当该版本仍在开发迭代、需要重打包时用 REBUILD=1 显式覆盖同版本产物；
# 其他历史版本（dist/ 与 versions/）始终不动。
if [ -f "$OUT" ]; then
  if [ "${REBUILD:-0}" != "1" ]; then
    echo "版本 ${VER} 的包已存在：${OUT}" >&2
    echo "按「只增不删」规则拒绝覆盖。请递增 manifest 版本号，或用 REBUILD=1 显式重打包该版本。" >&2
    exit 1
  fi
  echo ">>> REBUILD 模式：覆盖同版本 ${VER} 的产物（其他历史版本保持不变）"
fi

mkdir -p dist "$SNAP"

# 记录打包前已有的 fpk，便于定位 fnpack 新生成的产物
BEFORE="$(mktemp)"
trap 'rm -f "$BEFORE"' EXIT
find "$ROOT" -name '*.fpk' -type f | sort > "$BEFORE"

echo ">>> 打包 v${VER} ..."
"$FNPACK" build -d fpk

NEW="$(find "$ROOT" -name '*.fpk' -type f | sort | comm -13 "$BEFORE" - | head -1)"
if [ -z "$NEW" ]; then
  echo "未检测到新生成的 fpk，请检查 fnpack 输出" >&2
  exit 1
fi

mv "$NEW" "$OUT"
shasum -a 256 "$OUT" > "${OUT}.sha256"

# 自动复制安装包到工作目录根（用户要求：每次完成新版本都要把安装包留在工作目录供取用）
WS_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
if [ -d "$WS_ROOT" ]; then
  cp -f "$OUT" "$WS_ROOT/FnVista-${VER}.fpk"
  echo "已复制安装包到工作目录: $WS_ROOT/FnVista-${VER}.fpk"
fi

# 源码快照：保留本版本发布时的完整 fpk/ 工程，日后可回溯或重新打包
cp -R fpk/. "$SNAP/"
echo "源码快照: ${SNAP}/ ($(find "$SNAP" -type f | wc -l | tr -d ' ') 个文件)"

echo
echo "=== 构建完成（历史版本完整保留） ==="
echo "包: ${OUT}"
echo "SHA256: $(cut -d' ' -f1 "${OUT}.sha256")"
echo
echo "dist/ 现有版本："
ls -1 dist/*.fpk | sed 's|.*/|  |'
echo
echo "versions/ 现有源码快照："
ls -1d versions/*/ | sed 's|.*/\([^/]*\)/$|  \1|'
