"""脑图页面：Three.js 3D 渲染 + 自动降级的 2D 简化视图。

视觉编码：节点=记忆，边=关联；颜色=主导情绪，大小=权重，亮度=最近被想起的程度，外圈光晕=受保护的重要记忆。
交互：拖动旋转、滚轮/双指缩放、点击节点（高亮它和相邻的关联边，并显示详情）、可开关自动旋转、复位视角。

可靠性设计（浏览器里的事，插件侧无法保证 CDN 可达，所以层层兜底）：
1. Three.js 依次尝试：插件数据目录里的本地 three.min.js → jsDelivr → cdnjs → unpkg
2. 全部失败 / 浏览器不支持 WebGL / 渲染中途报错 → 自动退回 2D 简化视图，并在页面上写明原因
3. 数据契约（export_graph 的 nodes/edges/meta）保持不变；页面只读，不提供任何写操作

两种模式：
- 服务模式：render_page(None)，页面自己用 ?key= 去拉 /api/graph
- 自包含模式：render_page(graph)，数据内联进 HTML，可作为文件通过聊天发送（兜底方案；Three.js 仍从 CDN 加载）
"""
from __future__ import annotations

import json
from typing import Any

# 固定版本的非模块构建（r128 起稳定提供 three.min.js），不依赖 OrbitControls 等第二个脚本——交互自己实现
THREE_CDN_URLS: list[str] = [
    "https://cdn.jsdelivr.net/npm/three@0.128.0/build/three.min.js",
    "https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js",
    "https://unpkg.com/three@0.128.0/build/three.min.js",
]
CDN_HOSTS = "https://cdn.jsdelivr.net https://cdnjs.cloudflare.com https://unpkg.com"
LOCAL_THREE_PATH = "/vendor/three.min.js"      # 服务模式下，若数据目录里有 three.min.js 就优先用它

_TEMPLATE = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>她的脑图</title>
<style>
:root{--bg:#0f1218;--fg:#e8ecf2;--mut:#8b95a5;--card:#171c25;--btn:#232a36;--line:rgba(128,128,128,.22)}
@media (prefers-color-scheme: light){:root{--bg:#f6f7f9;--fg:#1d2330;--mut:#667085;--card:#fff;--btn:#eef0f4}}
html,body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:1040px;margin:0 auto;padding:16px}
h1{font-size:18px;margin:4px 0 2px}.sub{color:var(--mut);margin:0 0 12px}
.card{background:var(--card);border-radius:12px;padding:12px;margin-bottom:12px}
.stagewrap{position:relative}
#stage{height:min(64vh,540px);border-radius:8px;overflow:hidden;display:flex;align-items:center;justify-content:center;color:var(--mut)}
canvas.gl,canvas.flat{width:100%;height:100%;display:block;touch-action:none;cursor:grab}
canvas.gl:active{cursor:grabbing}
.tools{position:absolute;right:8px;top:8px;display:flex;gap:6px}
.tools button{background:var(--btn);color:var(--fg);border:0;border-radius:6px;padding:4px 10px;font:inherit;font-size:12px;cursor:pointer}
.legend{margin-top:8px}.legend span{display:inline-flex;align-items:center;margin-right:12px;color:var(--mut)}
.legend i{width:10px;height:10px;border-radius:50%;display:inline-block;margin-right:4px}
#hint{color:var(--mut);font-size:12px;margin-top:4px}
#note{color:#e5a13c;font-size:12px;margin-top:6px;min-height:0}
#detail{margin-top:8px;color:var(--mut);min-height:40px}
table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:4px 6px;border-bottom:1px solid var(--line);font-size:13px}
</style></head><body><div class="wrap">
<h1>她的脑图 <small style="color:var(--mut);font-weight:400">只读</small></h1>
<p class="sub" id="stat">加载中…</p>
<div class="card">
  <div class="stagewrap"><div id="stage">加载中…</div>
    <div class="tools"><button id="btnRot" type="button">自动旋转：开</button><button id="btnReset" type="button">复位视角</button></div></div>
  <div class="legend" id="legend"></div>
  <div id="hint">拖动旋转 · 滚轮/双指缩放 · 点击节点查看记忆 ｜ 颜色=情绪　大小=权重　亮度=最近被想起　光圈=受保护的重要记忆</div>
  <div id="note"></div>
  <div id="detail">点击一个节点查看这条记忆。</div>
</div>
<div class="card"><table id="tbl"><thead><tr><th>#</th><th>类型</th><th>摘要</th><th>权重</th><th>情绪</th><th>召回</th></tr></thead><tbody></tbody></table></div>
</div>
<script>
(function(){
'use strict';
const EMBEDDED = __EMBEDDED__;
const THREE_URLS = __THREE_URLS__;
const LOCAL_THREE = '__LOCAL_THREE__';
const KEY = new URLSearchParams(location.search).get('key') || '';
const $ = id => document.getElementById(id);
const dark = !!(window.matchMedia && matchMedia('(prefers-color-scheme: dark)').matches);
let current = null;

function setNote(t){ $('note').textContent = t || ''; }
function describe(d){
  return '#'+d.id+' ['+d.type+'] '+d.summary+' ｜权重 '+d.weight.toFixed(2)+' ｜'+d.dominant_emotion_cn+' ｜召回 '+d.recall_count+' 次 ｜清晰度 '+d.clarity.toFixed(2)
    +(d.compressed?' ｜已压缩':'')+(d.protected?' ｜受保护':'')+(d.dormant?' ｜沉睡':'')+'　（/记忆详情 '+d.id+' 看全文）';
}

/* ---------- 元信息 / 图例 / 表格（所有文本一律 textContent，不拼 HTML） ---------- */
function renderMeta(g){
  const m = g.meta;
  $('stat').textContent = '记忆节点 '+m.node_count+' / 共 '+m.total_memories+' 条 · 关联边 '+m.edge_count+(m.truncated?'（已按权重截取）':'');
  const lg = $('legend'); lg.textContent = '';
  for (const k in m.emotion_colors){
    const s = document.createElement('span'), i = document.createElement('i');
    i.style.background = m.emotion_colors[k]; s.appendChild(i); s.appendChild(document.createTextNode(m.emotion_cn[k])); lg.appendChild(s);
  }
  const tb = document.querySelector('#tbl tbody'); tb.textContent = '';
  [...g.nodes].sort((a,b)=>b.weight-a.weight).slice(0,30).forEach(n=>{
    const tr = document.createElement('tr');
    [n.id, n.type, n.summary, n.weight.toFixed(2), n.dominant_emotion_cn, n.recall_count].forEach(v=>{
      const td = document.createElement('td'); td.textContent = String(v); tr.appendChild(td); });
    tb.appendChild(tr);
  });
}

/* ---------- 3D 布局：力导向（确定性初值，无随机），重要的记忆靠近中心 ---------- */
function layout3D(g){
  const n = g.nodes.length, idx = new Map();
  g.nodes.forEach((d,i)=>idx.set(d.id,i));
  const P = new Float32Array(n*3), F = new Float32Array(n*3);
  const order = g.nodes.map((_,i)=>i).sort((a,b)=>g.nodes[b].weight-g.nodes[a].weight);
  const ga = Math.PI*(3-Math.sqrt(5));
  order.forEach((i,k)=>{
    const y = 1-2*(k+0.5)/n, r = Math.sqrt(Math.max(0,1-y*y)), th = ga*k, R = 35+85*Math.sqrt((k+0.5)/n);
    P[i*3] = Math.cos(th)*r*R; P[i*3+1] = y*R; P[i*3+2] = Math.sin(th)*r*R;
  });
  const E = [];
  for (const e of g.edges){
    const a = idx.get(e.source), b = idx.get(e.target);
    if (a !== undefined && b !== undefined && a !== b) E.push([a,b,e.strength]);
  }
  const iters = n > 220 ? 70 : 130;
  for (let it=0; it<iters; it++){
    F.fill(0);
    for (let i=0;i<n;i++) for (let j=i+1;j<n;j++){
      let dx = P[i*3]-P[j*3], dy = P[i*3+1]-P[j*3+1], dz = P[i*3+2]-P[j*3+2];
      const d2 = dx*dx+dy*dy+dz*dz+0.5, f = Math.min(8, 600/d2)/Math.sqrt(d2);
      dx*=f; dy*=f; dz*=f;
      F[i*3]+=dx; F[i*3+1]+=dy; F[i*3+2]+=dz; F[j*3]-=dx; F[j*3+1]-=dy; F[j*3+2]-=dz;
    }
    for (const [a,b,s] of E){
      const dx = P[b*3]-P[a*3], dy = P[b*3+1]-P[a*3+1], dz = P[b*3+2]-P[a*3+2];
      const d = Math.sqrt(dx*dx+dy*dy+dz*dz)+0.01, Lr = 28+45*(1-s), f = (d-Lr)*0.03*(0.3+s)/d;
      F[a*3]+=dx*f; F[a*3+1]+=dy*f; F[a*3+2]+=dz*f; F[b*3]-=dx*f; F[b*3+1]-=dy*f; F[b*3+2]-=dz*f;
    }
    const step = 0.9*(1-it/iters)+0.1;
    for (let i=0;i<n*3;i++){
      F[i] -= P[i]*0.012;
      P[i] += Math.max(-6, Math.min(6, F[i]*step*4));
    }
  }
  let cx=0, cy=0, cz=0;
  for (let i=0;i<n;i++){ cx+=P[i*3]; cy+=P[i*3+1]; cz+=P[i*3+2]; }
  if (n){ cx/=n; cy/=n; cz/=n; }
  let mr = 1;
  for (let i=0;i<n;i++){
    P[i*3]-=cx; P[i*3+1]-=cy; P[i*3+2]-=cz;
    mr = Math.max(mr, Math.hypot(P[i*3],P[i*3+1],P[i*3+2]));
  }
  const sc = 100/mr;
  for (let i=0;i<n*3;i++) P[i]*=sc;
  return {P:P, E:E};
}

/* ---------- Three.js 加载：本地 → 多个 CDN，依次尝试 ---------- */
function loadScript(src){
  return new Promise((res, rej)=>{
    const s = document.createElement('script');
    const timer = setTimeout(()=>rej(new Error('timeout')), 9000);
    s.src = src; s.async = true;
    s.onload = ()=>{ clearTimeout(timer); res(); };
    s.onerror = ()=>{ clearTimeout(timer); rej(new Error('load failed')); };
    document.head.appendChild(s);
  });
}
async function ensureThree(){
  if (window.THREE) return true;
  const urls = [];
  if (!EMBEDDED && LOCAL_THREE) urls.push(LOCAL_THREE + '?key=' + encodeURIComponent(KEY));
  THREE_URLS.forEach(u=>urls.push(u));
  for (const u of urls){
    try { await loadScript(u); } catch(e) { /* 试下一个 */ }
    if (window.THREE) return true;
  }
  return !!window.THREE;
}

/* ---------- 3D 视图 ---------- */
function createView3D(g, onFail){
  const stage = $('stage'); stage.textContent = '';
  const n = g.nodes.length;
  if (!n){ stage.textContent = '她还没有形成记忆，先聊几句吧～'; return {destroy(){}}; }
  const canvas = document.createElement('canvas'); canvas.className = 'gl'; stage.appendChild(canvas);
  const renderer = new THREE.WebGLRenderer({canvas: canvas, antialias: true, alpha: true});   // 不支持 WebGL 会在这里抛错 → 外层降级
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.setClearColor(0x000000, 0);
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(55, 1, 1, 4000);
  const L = layout3D(g), P = L.P, E = L.E;

  // 节点：颜色=情绪，大小=权重，不透明度=亮度（最近被想起更亮），受保护的加一圈光晕
  const geo = new THREE.SphereGeometry(1, 14, 10);
  const meshes = g.nodes.map((d,i)=>{
    const base = 0.25 + 0.75*d.brightness;
    const mat = new THREE.MeshBasicMaterial({color: new THREE.Color(d.color), transparent: true, opacity: base});
    const m = new THREE.Mesh(geo, mat);
    const s = Math.max(1.3, d.size*0.55);
    m.scale.set(s,s,s); m.position.set(P[i*3],P[i*3+1],P[i*3+2]);
    m.userData = {i: i, base: base, s: s};
    if (d.protected){
      const halo = new THREE.Mesh(geo, new THREE.MeshBasicMaterial({color: new THREE.Color(d.color), transparent: true, opacity: 0.16, depthWrite: false}));
      halo.scale.set(1.9,1.9,1.9); m.add(halo);
    }
    scene.add(m);
    return m;
  });

  // 边：用顶点色表达关联强度（越强越显眼）
  let lines = null;
  if (E.length){
    const ep = new Float32Array(E.length*6), ec = new Float32Array(E.length*6);
    E.forEach(([a,b,s],k)=>{
      const t = 0.25+0.75*s;
      const c = dark ? [0.55*t, 0.62*t, 0.72*t] : [1-0.65*t, 1-0.58*t, 1-0.5*t];
      for (let q=0;q<3;q++){ ep[k*6+q]=P[a*3+q]; ep[k*6+3+q]=P[b*3+q]; ec[k*6+q]=c[q]; ec[k*6+3+q]=c[q]; }
    });
    const eg = new THREE.BufferGeometry();
    eg.setAttribute('position', new THREE.BufferAttribute(ep,3));
    eg.setAttribute('color', new THREE.BufferAttribute(ec,3));
    lines = new THREE.LineSegments(eg, new THREE.LineBasicMaterial({vertexColors: true, transparent: true, opacity: 0.8}));
    scene.add(lines);
  }

  // 选中：高亮它的关联边，淡化无关节点
  let hl = null;
  function select(i){
    if (hl){ scene.remove(hl); hl.geometry.dispose(); hl.material.dispose(); hl = null; }
    const nb = new Set();
    if (i >= 0){
      const pts = [];
      for (const [a,b] of E){
        if (a===i || b===i){ nb.add(a); nb.add(b); pts.push(P[a*3],P[a*3+1],P[a*3+2],P[b*3],P[b*3+1],P[b*3+2]); }
      }
      if (pts.length){
        const hg = new THREE.BufferGeometry();
        hg.setAttribute('position', new THREE.BufferAttribute(new Float32Array(pts),3));
        hl = new THREE.LineSegments(hg, new THREE.LineBasicMaterial({color: dark ? 0xffffff : 0x111111, transparent: true, opacity: 0.9}));
        scene.add(hl);
      }
    }
    meshes.forEach((m,k)=>{
      const ud = m.userData, on = i < 0 || k === i || nb.has(k);
      m.material.opacity = on ? Math.min(1, ud.base + (i >= 0 ? 0.25 : 0)) : ud.base*0.25;
      const s = ud.s*(k === i ? 1.6 : 1);
      m.scale.set(s,s,s);
    });
    $('detail').textContent = i >= 0 ? describe(g.nodes[i]) : '点击一个节点查看这条记忆。';
  }

  // 相机：球坐标轨道（自己实现，不依赖 OrbitControls）
  const R0 = 250;
  let theta = 0.6, phi = 1.15, radius = R0, auto = true;
  function updateCam(){
    camera.position.set(radius*Math.sin(phi)*Math.cos(theta), radius*Math.cos(phi), radius*Math.sin(phi)*Math.sin(theta));
    camera.lookAt(0,0,0);
  }
  const pts = new Map(); let moved = 0, pinch = 0;
  const ray = new THREE.Raycaster(), ndc = new THREE.Vector2();
  function pick(e){
    const r = canvas.getBoundingClientRect();
    ndc.set(((e.clientX-r.left)/r.width)*2-1, -((e.clientY-r.top)/r.height)*2+1);
    ray.setFromCamera(ndc, camera);
    const hit = ray.intersectObjects(meshes, false)[0];
    select(hit ? hit.object.userData.i : -1);
  }
  canvas.addEventListener('pointerdown', e=>{
    try { canvas.setPointerCapture(e.pointerId); } catch(_){}
    pts.set(e.pointerId, {x:e.clientX, y:e.clientY}); moved = 0;
    if (pts.size === 2){ const [a,b] = [...pts.values()]; pinch = Math.hypot(a.x-b.x, a.y-b.y); }
  });
  canvas.addEventListener('pointermove', e=>{
    const p = pts.get(e.pointerId); if (!p) return;
    const dx = e.clientX-p.x, dy = e.clientY-p.y; p.x = e.clientX; p.y = e.clientY;
    if (pts.size === 1){
      theta -= dx*0.006; phi = Math.max(0.12, Math.min(Math.PI-0.12, phi - dy*0.006)); moved += Math.abs(dx)+Math.abs(dy);
    } else if (pts.size === 2){
      const [a,b] = [...pts.values()], d = Math.hypot(a.x-b.x, a.y-b.y);
      if (pinch > 0 && d > 0) radius = Math.max(40, Math.min(900, radius*pinch/d));
      pinch = d; moved += 10;
    }
  });
  function up(e){
    const wasSingle = pts.size === 1 && pts.has(e.pointerId);
    pts.delete(e.pointerId);
    if (wasSingle && e.type === 'pointerup' && moved < 5) pick(e);
  }
  canvas.addEventListener('pointerup', up);
  canvas.addEventListener('pointercancel', up);
  canvas.addEventListener('wheel', e=>{ e.preventDefault(); radius = Math.max(40, Math.min(900, radius*Math.exp(e.deltaY*0.001))); }, {passive:false});
  $('btnRot').onclick = ()=>{ auto = !auto; $('btnRot').textContent = '自动旋转：'+(auto?'开':'关'); };
  $('btnReset').onclick = ()=>{ theta = 0.6; phi = 1.15; radius = R0; select(-1); };
  $('btnRot').textContent = '自动旋转：开';

  function fit(){
    const w = Math.max(10, stage.clientWidth), h = Math.max(10, stage.clientHeight);
    renderer.setSize(w, h, false); camera.aspect = w/h; camera.updateProjectionMatrix();
  }
  const ro = window.ResizeObserver ? new ResizeObserver(fit) : null;
  if (ro) ro.observe(stage);
  window.addEventListener('resize', fit);
  fit(); updateCam();

  let alive = true, raf = 0;
  function destroy(){
    alive = false; cancelAnimationFrame(raf);
    if (ro) ro.disconnect();
    window.removeEventListener('resize', fit);
    try { renderer.dispose(); geo.dispose(); } catch(_){}
    stage.textContent = '';
  }
  function frame(){
    if (!alive) return;
    try {
      if (auto && pts.size === 0) theta += 0.0016;
      updateCam(); renderer.render(scene, camera);
    } catch(e) { destroy(); onFail(e); return; }
    raf = requestAnimationFrame(frame);
  }
  frame();
  window.__gc_view3d_ok = true;
  return {destroy: destroy};
}

/* ---------- 2D 简化视图（降级用：无外部依赖） ---------- */
function createView2D(g){
  const stage = $('stage'); stage.textContent = '';
  if (!g.nodes.length){ stage.textContent = '她还没有形成记忆，先聊几句吧～'; return {destroy(){}}; }
  const cv = document.createElement('canvas'); cv.className = 'flat'; stage.appendChild(cv);
  const ctx = cv.getContext('2d'); let pos = {};
  function layout(){
    const W = cv.clientWidth, H = cv.clientHeight; cv.width = W*devicePixelRatio; cv.height = H*devicePixelRatio;
    ctx.setTransform(devicePixelRatio,0,0,devicePixelRatio,0,0);
    pos = {}; const cx = W/2, cy = H/2, R = Math.min(W,H)/2 - 16;
    const sorted = [...g.nodes].sort((a,b)=>b.weight-a.weight);
    sorted.forEach((n,i)=>{ const a = i*2.399963, r = R*Math.sqrt((i+0.5)/sorted.length); pos[n.id] = [cx+r*Math.cos(a), cy+r*Math.sin(a)]; });
  }
  function draw(){
    const W = cv.clientWidth, H = cv.clientHeight; ctx.clearRect(0,0,W,H); ctx.lineWidth = 1;
    for (const e of g.edges){ const a = pos[e.source], b = pos[e.target]; if (!a||!b) continue;
      ctx.strokeStyle = 'rgba(150,160,175,'+(0.1+0.4*e.strength).toFixed(2)+')'; ctx.beginPath(); ctx.moveTo(a[0],a[1]); ctx.lineTo(b[0],b[1]); ctx.stroke(); }
    for (const n of g.nodes){ const p = pos[n.id]; if (!p) continue;
      ctx.globalAlpha = n.brightness; ctx.fillStyle = n.color; ctx.beginPath(); ctx.arc(p[0],p[1],n.size*0.6,0,6.2832); ctx.fill();
      if (n.protected){ ctx.globalAlpha = 0.9; ctx.strokeStyle = n.color; ctx.lineWidth = 1.5; ctx.beginPath(); ctx.arc(p[0],p[1],n.size*0.6+3,0,6.2832); ctx.stroke(); }
    }
    ctx.globalAlpha = 1;
  }
  function onClick(ev){
    const r = cv.getBoundingClientRect(), x = ev.clientX-r.left, y = ev.clientY-r.top; let best = null, bd = 1e9;
    for (const n of g.nodes){ const p = pos[n.id]; if (!p) continue; const d = (p[0]-x)**2+(p[1]-y)**2; if (d < bd){ bd = d; best = n; } }
    if (best && bd < 400) $('detail').textContent = describe(best);
  }
  const onResize = ()=>{ layout(); draw(); };
  cv.addEventListener('click', onClick); window.addEventListener('resize', onResize);
  $('btnRot').style.display = 'none'; $('btnReset').style.display = 'none';
  $('hint').textContent = '点击节点查看记忆 ｜ 颜色=情绪　大小=权重　亮度=最近被想起　光圈=受保护的重要记忆';
  layout(); draw();
  return {destroy(){ window.removeEventListener('resize', onResize); stage.textContent = ''; }};
}

async function main(){
  try {
    let g = EMBEDDED;
    if (!g){
      const r = await fetch('/api/graph?key='+encodeURIComponent(KEY), {cache:'no-store'});
      if (!r.ok) throw new Error('HTTP '+r.status);
      g = await r.json();
    }
    renderMeta(g);
    $('stage').textContent = '正在加载 3D 库…';
    const fallback = why=>{ setNote(why); if (current) current.destroy(); current = createView2D(g); };
    if (!(await ensureThree())){
      fallback('3D 库没能加载（服务器或浏览器访问不了 CDN），已显示 2D 简化视图。可把 three.min.js 放进插件数据目录，见 README。');
      return;
    }
    try {
      current = createView3D(g, e=>fallback('3D 渲染出错（'+(e && e.message || e)+'），已显示 2D 简化视图。'));
    } catch(e) {
      fallback('3D 渲染不可用（'+(e && e.message || e)+'；可能是浏览器不支持 WebGL），已显示 2D 简化视图。');
    }
  } catch(e) {
    $('stat').textContent = '读取失败（'+(e && e.message || e)+'），请确认链接里的 key 正确。';
    $('stage').textContent = '';
  }
}
window.__gc = {layout3D: layout3D};
main();
})();
</script></body></html>
"""


def render_page(graph: dict[str, Any] | None = None) -> str:
    """graph 为 None → 服务模式（页面自行拉取 API）；否则内联数据，生成自包含 HTML。"""
    if graph is None:
        emb = "null"
    else:   # 内联进 <script>：转义 </、<!--、行分隔符，防止数据里的文本提前结束脚本
        emb = (json.dumps(graph, ensure_ascii=False).replace("</", "<\\/").replace("<!--", "<\\!--")
               .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))
    return (_TEMPLATE.replace("__EMBEDDED__", emb)
            .replace("__THREE_URLS__", json.dumps(THREE_CDN_URLS))
            .replace("__LOCAL_THREE__", LOCAL_THREE_PATH))
