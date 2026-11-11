#!/bin/bash
# ============================================================
#  fnos-www-zip-patch.sh
#  修补飞牛还原源 /usr/trim/share/.restore/www.zip
#
#  背景: fnOS 前端把「系统壁纸」的 URL 在远程访问下加上
#        static2.fnnas.com / static2.fygonas.com 等 CDN 前缀，
#        从飞牛官方 CloudFront 取出厂原图，导致本应用替换的
#        壁纸在登录页（及部分场景的桌面）不生效。
#        /usr/trim/www 与 nginx 配置受完整性守护（秒级还原），
#        但守护进程从 www.zip 还原 —— 修补 zip 本身即可一劳永逸。
#
#  动作:
#    1) assets/index-*.js  移除 static2 CDN 前缀（根修）
#    2) index.html         注入 CSS（凡内联引用 wallpaper-1.webp
#       的元素强制改用本机文件，兼容旧缓存 JS 的浏览器）
#    3) index.html         注入定时 JS（v1.6.2 改为「头部比对 + 同层交叉淡入淡出」）：
#       前端每 10 秒用 Range 请求只读响应头（content-length / content-range）
#       判断壁纸内容是否变化（彻底规避跨域染色），仅当内容真变了才在「原壁纸
#       所在层级」做旧图淡出 / 新图淡入的交叉过渡（仿 aerial-tv）；克隆的 z-index
#       仅比原元素高 1 级，绝不放大到全屏最高层级 —— 不盖桌面图标、不干扰
#       正在打开的 app 页面，静止时无任何刷新与闪烁。
#
#  注意: www.zip 内条目带密码加密、无法解压，因此补丁一律以
#        线上 /usr/trim/www 的同名文件为基础（它就是当前系统版本
#        的原始内容），打好补丁后回写 zip。飞牛系统更新会重置
#        zip 与线上文件，看门狗会自动重打补丁。
#
#  用法:
#    fnos-www-zip-patch.sh [--check]   # 检查并按需修补（供看门狗）
#    fnos-www-zip-patch.sh --revert    # 还原 zip 与线上文件（停用时）
#
#  原 zip 备份: /usr/trim/share/.restore/www.zip.orig（首次修补时创建）
# ============================================================
set -uo pipefail

DATA_DIR="/var/lib/fnos-wallpaper"
ORIG_DIR="$DATA_DIR/orig"
ZIP="/usr/trim/share/.restore/www.zip"
WROOT="/usr/trim/www"
CSS_MARK="fnos-bing-wp-css"
# v1.5.5: 同时注入桌面壁纸自动刷新脚本，让秒级轮播无需 F5 就能看见
JS_MARK="fnos-bing-wp-js"
# v1.6.1: 注入的 JS 内容会随版本演进（如新增淡入淡出），用版本令牌判断
# zip/live 里的旧 JS 是否需要被替换，避免「已打过补丁就跳过」导致升级后
# 新逻辑（淡入淡出）始终不生效。
JS_VER="fnos-bing-wp-js-1.6.2-fade"
STATE="$DATA_DIR/www-patch-entries"
FAIL_FLAG="$DATA_DIR/.zip-patch-failed"
LOG_FILE="/var/log/fnos-bing-wallpaper.log"

log() { printf '[%s] [zip-patch] %s\n' "$(date '+%F %T')" "$*" >> "$LOG_FILE"; }

[ -f "$ZIP" ] || exit 0
[ "$(id -u)" -eq 0 ] || exit 0

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
cd "$WORK"

# zip 内 index.html 是否已带补丁（补丁条目为未加密，可直接读取）
# 1.5.5 起要求 CSS 和 JS 两个标记都在，否则会给旧安装补上自动刷新脚本
zip_html_patched() {
  local h
  h="$(unzip -p "$ZIP" index.html 2>/dev/null)" || return 1
  printf '%s' "$h" | grep -q "$CSS_MARK" || return 1
  printf '%s' "$h" | grep -q "$JS_MARK" || return 1
  printf '%s' "$h" | grep -q "$JS_VER" || return 1
  return 0
}

find_js_entries() {
  zipinfo -1 "$ZIP" 2>/dev/null | grep -E '^assets/index-.*\.js$'
}

# ---------- 还原模式 ----------
if [ "${1:-}" = "--revert" ]; then
  [ -f "$STATE" ] || exit 0
  patched=0
  while IFS= read -r entry; do
    [ -n "$entry" ] || continue
    base="$(basename "$entry")"
    ob="$ORIG_DIR/www-$base.orig"
    [ -f "$ob" ] || continue
    mkdir -p "$(dirname "$entry")"
    cp -f "$ob" "$entry"
    zip -q "$ZIP" "$entry" && patched=$((patched+1))
    tgt="$WROOT/$entry"
    [ -d "$(dirname "$tgt")" ] && cp -f "$ob" "$tgt" 2>/dev/null && chmod 644 "$tgt" 2>/dev/null
  done < "$STATE"
  rm -f "$STATE"
  log "已还原 www.zip 原始条目（$patched 项）"
  exit 0
fi

# ---------- 检查是否需要修补 ----------
needs_patch=0
zip_html_patched || needs_patch=1
if [ "$needs_patch" -eq 0 ]; then
  # zip 的 index.html 有补丁；再确认 JS 补丁条目仍可读（未加密）
  # 注意：不能 unzip | head 管道判定——本脚本 set -o pipefail，
  #       大文件会让 unzip 收到 SIGPIPE(141) 而误判失败（曾导致每分钟重写 www.zip）
  for js in $(find_js_entries); do
    if [ -f "$STATE" ] && grep -qx "$js" "$STATE" 2>/dev/null; then
      unzip -p "$ZIP" "$js" > "$WORK/.jschk" 2>/dev/null
      [ -s "$WORK/.jschk" ] || { needs_patch=1; break; }
    fi
  done
fi

# 全部干净：仅确保 live 文件也是补丁版（守护进程通常会自己同步）
if [ "$needs_patch" -eq 0 ]; then
  if [ -f "$STATE" ]; then
    live_ok=1
    while IFS= read -r entry; do
      [ -n "$entry" ] || continue
      tgt="$WROOT/$entry"
      [ -f "$tgt" ] || { live_ok=0; break; }
      case "$entry" in
        index.html)  grep -q "$CSS_MARK" "$tgt" 2>/dev/null || { live_ok=0; break; }
                     grep -q "$JS_MARK"  "$tgt" 2>/dev/null || { live_ok=0; break; }
                     grep -q "$JS_VER"   "$tgt" 2>/dev/null || { live_ok=0; break; } ;;
        *.js)        grep -q "https://static2" "$tgt" 2>/dev/null && { live_ok=0; break; } ;;
      esac
    done < "$STATE"
    if [ "$live_ok" -eq 1 ]; then
      rm -f "$FAIL_FLAG"
      exit 0
    fi
    while IFS= read -r entry; do
      [ -n "$entry" ] || continue
      tgt="$WROOT/$entry"
      [ -d "$(dirname "$tgt")" ] || continue
      unzip -p "$ZIP" "$entry" > "$tgt" 2>/dev/null && chmod 644 "$tgt"
    done < "$STATE"
    log "已从补丁版 www.zip 同步 live 前端文件"
  fi
  exit 0
fi

# 需要修补：失败退避（1 小时内不重复尝试，避免日志刷屏）
if [ -f "$FAIL_FLAG" ]; then
  ft=$(stat -c %Y "$FAIL_FLAG" 2>/dev/null || echo 0)
  now=$(date +%s)
  [ $((now - ft)) -lt 3600 ] && exit 0
fi

# ---------- 全量修补（以线上文件为基础，zip 条目加密无法解压） ----------
: > "$STATE.new"

# 1) index.html
live_html="$WROOT/index.html"
if [ -f "$live_html" ]; then
  cp -f "$live_html" index.html
  if ! grep -q "$CSS_MARK" index.html; then
    cp -a index.html "$ORIG_DIR/www-index.html.orig"
  fi
  if ! grep -q "$CSS_MARK" index.html || ! grep -q "$JS_MARK" index.html || ! grep -q "$JS_VER" index.html; then
    python3 - "$CSS_MARK" "$JS_MARK" "$JS_VER" <<'PY'
import sys, re
css, js, jsver = sys.argv[1], sys.argv[2], sys.argv[3]
p = 'index.html'
s = open(p, encoding='utf-8', errors='replace').read()
# 先剥离此前注入的旧补丁（无论旧 JS 是哪个版本）：否则升级后旧逻辑
#（无淡入淡出）会残留，与 PATCH_VER 令牌缺失共同触发「跳过补丁」。
s = re.sub(r'<style id="%s">.*?</style>' % re.escape(css), '', s, flags=re.S)
s = re.sub(r'<script id="%s">.*?</script>' % re.escape(js), '', s, flags=re.S)
style = ('<style id="%s">[style*="wallpaper-1.webp"]'
         '{background-image:url("/static/bg/wallpaper-1.webp") !important}</style>') % css
code = """(function(){
  var KEY='/sac/desktop/v1/bgImg/';
  /* PATCH_VER: fnos-bing-wp-js-1.6.2-fade */
  var FADE=1200; /* 淡入淡出时长(ms)，仿 aerial-tv */
  var MARK={};   /* base -> 上次内容标记（content-length/content-range） */
  var BUSY={};   /* base -> 是否正在淡入淡出（防止重叠） */
  function stripQ(u){return (u||'').split('?')[0];}
  /* 取 8x8 缩略指纹：内容变了指纹就变，与 _wp 时间戳无关 */
  function hashOf(img){
    try{
      var c=document.createElement('canvas');c.width=8;c.height=8;
      var x=c.getContext('2d');x.drawImage(img,0,0,8,8);
      var d=x.getImageData(0,0,8,8).data,s='';
      for(var i=0;i<d.length;i+=4){s+=(d[i]>>3)+'_'+(d[i+1]>>3)+'_'+(d[i+2]>>3)+';';}
      return s;
    }catch(e){return null;} /* CORS 染色时返回 null，走降级 */
  }
  function listEls(){
    var a=[],n=document.querySelectorAll('img[src*="'+KEY+'"]'),i;
    for(i=0;i<n.length;i++)a.push(n[i]);
    var m=document.querySelectorAll('[style*="'+KEY+'"]'),j;
    for(j=0;j<m.length;j++)a.push(m[j]);
    return a;
  }
  function elBase(el){
    if(el.tagName==='IMG')return stripQ(el.getAttribute('src')||'');
    var bg=el.style.backgroundImage||'',mm=bg.match(/url\\(([^)]*)\\)/);
    if(!mm)return '';
    return stripQ(mm[1].replace(/^['"]+/,'').replace(/['"]+$/,''));
  }
  function elOld(el){
    if(el.tagName==='IMG')return el.getAttribute('src')||'';
    return el.style.backgroundImage||'';
  }
  function elSet(el,v){
    if(el.tagName==='IMG')el.setAttribute('src',v);
    else el.style.backgroundImage='url("'+v+'")';
  }
  /* 在原壁纸元素所在层级做交叉淡入淡出：克隆旧图盖在原位置，z-index 仅比原元素高 1 级，
     绝不放大到全屏最高层级 —— 因此不会盖住桌面图标 / 正在打开的 app 页面 */
  function crossfade(el,newSrc,oldSrc,isImg){
    var op=el.offsetParent||document.body;
    var er=el.getBoundingClientRect(), or=op.getBoundingClientRect();
    var clone=document.createElement(isImg?'img':'div');
    if(isImg){clone.src=oldSrc;clone.style.objectFit=(el.style.objectFit||'cover');clone.style.width='100%';clone.style.height='100%';}
    else{clone.style.backgroundImage=oldSrc;clone.style.backgroundSize=(el.style.backgroundSize||'cover');clone.style.backgroundPosition=(el.style.backgroundPosition||'center');clone.style.backgroundRepeat='no-repeat';}
    var tz=parseInt(getComputedStyle(el).zIndex); if(isNaN(tz))tz=0;
    clone.style.cssText+=';position:absolute;left:'+(er.left-or.left)+'px;top:'+(er.top-or.top)+'px;'+
      'width:'+er.width+'px;height:'+er.height+'px;pointer-events:none;opacity:1;'+
      'transition:opacity '+FADE+'ms ease;z-index:'+(tz+1)+';';
    op.appendChild(clone);
    elSet(el,newSrc);                 /* 原元素立刻换上新图（被克隆盖住，肉眼不可见） */
    void clone.offsetWidth;           /* 强制回流，让过渡生效 */
    clone.style.opacity='0';          /* 克隆淡出，露出下方已更新的原元素 */
    setTimeout(function(){if(clone.parentNode)clone.parentNode.removeChild(clone);},FADE+200);
  }
  /* 用 Range 请求只读响应头（不下载正文），以 content-length / content-range 判断壁纸
     内容是否变化；彻底规避跨域染色（只读头部、不读像素），淡入淡出一定会触发 */
  function getMarker(url){
    return fetch(url,{method:'GET',cache:'no-store',headers:{'Range':'bytes=0-0'}}).then(function(r){
      var cr=r.headers.get('content-range');
      if(cr){var m=cr.match(/\\/(\\d+)$/); if(m) return 'sz='+m[1];}
      var cl=r.headers.get('content-length');
      if(cl) return 'sz='+cl;
      throw 0;
    });
  }
  function tick(){
    try{
      var els=listEls(),seen={},bases=[],i;
      for(i=0;i<els.length;i++){var b=elBase(els[i]);if(!b)continue;if(!seen[b]){seen[b]=1;bases.push(b);}}
      for(i=0;i<bases.length;i++){
        (function(base){
          if(BUSY[base])return;
          getMarker(base+'?_wp='+Date.now()).then(function(mk){
            var st=MARK[base];
            if(st===undefined){MARK[base]=mk;return;}   /* 首拍只记录，不闪 */
            if(st===mk)return;                          /* 内容没变，保持，不刷新 */
            MARK[base]=mk;
            BUSY[base]=true;
            var a=listEls(),k;
            for(k=0;k<a.length;k++){
              var el=a[k]; if(elBase(el)!==base)continue;
              var newSrc=base+'?_wp='+Date.now();
              var oldSrc=elBase(el);
              if(el.tagName==='IMG')crossfade(el,newSrc,oldSrc,true);
              else crossfade(el,newSrc,oldSrc,false);
            }
            setTimeout(function(){BUSY[base]=false;},FADE+400);
          }).catch(function(){});
        })(bases[i]);
      }
    }catch(e){}
  }
  setInterval(tick,3000);
  tick();
})();"""
script = '<script id="%s">%s</script>' % (js, code)
# 已先剥离旧注入，这里无条件重新写入最新 style + script（含 PATCH_VER 令牌）
parts = style + script
if parts:
    s = s.replace('</head>', parts + '</head>', 1) if '</head>' in s else s + parts
    open(p, 'w', encoding='utf-8').write(s)
PY
  fi
  if grep -q "$CSS_MARK" index.html && grep -q "$JS_MARK" index.html; then
    if zip -q "$ZIP" index.html 2>/dev/null; then
      echo "index.html" >> "$STATE.new"
      cp -f index.html "$live_html" && chmod 644 "$live_html"
      log "www.zip: index.html 补丁已写入"
    else
      log "错误: zip 更新 index.html 失败"
      touch "$FAIL_FLAG"
    fi
  else
    log "错误: index.html 注入失败"
    touch "$FAIL_FLAG"
  fi
else
  log "警告: 线上 index.html 不存在，跳过"
fi

# 2) 入口 JS（移除 static2 CDN 前缀）
for js in $(find_js_entries); do
  live_js="$WROOT/$js"
  [ -f "$live_js" ] || { log "警告: 线上 $js 不存在，跳过"; continue; }
  mkdir -p "$(dirname "$js")"
  cp -f "$live_js" "$js"
  if grep -q "https://static2" "$js"; then
    base="$(basename "$js")"
    cp -a "$js" "$ORIG_DIR/www-$base.orig"
    sed -i 's|https://static2\.fygonas\.com/os-web/v1||g; s|https://static2\.\${w}/os-web/v1||g' "$js"
  fi
  if grep -q "https://static2" "$js"; then
    log "警告: $js 仍含 static2 引用（形态变化，需适配）"
    touch "$FAIL_FLAG"
    continue
  fi
  if zip -q "$ZIP" "$js" 2>/dev/null; then
    echo "$js" >> "$STATE.new"
    cp -f "$js" "$live_js" && chmod 644 "$live_js"
    log "www.zip: $js 已移除 CDN 前缀"
  else
    log "错误: zip 更新 $js 失败"
    touch "$FAIL_FLAG"
  fi
done

[ -s "$STATE.new" ] && mv "$STATE.new" "$STATE" || rm -f "$STATE.new"
exit 0
