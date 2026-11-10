"""脑图页面：脚本语法、数据转义、CSP/本地 Three.js 路由，以及（有浏览器时）降级路径与布局算法。

说明：沙箱/CI 里通常没有真实 Three.js，所以“3D 渲染成功”那条用一个宽松的桩库只验证我们自己的初始化/循环代码不抛错；
真实的 WebGL 画面需要在浏览器里肉眼确认。
"""
import asyncio
import json
import os
import re
import shutil
import subprocess

import pytest

from growing_companion.core.brainmap.page import CDN_HOSTS, THREE_CDN_URLS, render_page
from growing_companion.core.brainmap.server import BrainMapServer

from .conftest import Env, run
from .test_brainmap_server import _http


def _graph(n=6, hostile=False):
    nodes = [{"id": i + 1, "type": "episodic", "summary": f"记忆{i}", "weight": 0.9 - i * 0.1, "size": 6,
              "emotion": {}, "dominant_emotion": "joy", "dominant_emotion_cn": "开心", "valence": 0.5, "arousal": 0.4,
              "emotion_intensity": 0.5, "color": "#f7c948", "brightness": 0.8, "recall_count": i,
              "last_recalled_at": None, "created_at": 1.0, "clarity": 0.9, "compressed": False,
              "compressed_into": None, "protected": i % 2 == 0, "dormant": False} for i in range(n)]
    if hostile:
        nodes[0]["summary"] = "</script><script>window.__pwn=1</script><!-- x"
    edges = [{"source": i + 1, "target": i + 2, "kind": "semantic", "strength": 0.5} for i in range(n - 1)]
    return {"nodes": nodes, "edges": edges,
            "meta": {"generated_at": 1.0, "node_count": n, "edge_count": len(edges), "total_memories": n,
                     "truncated": False, "readonly": True,
                     "emotion_colors": {"joy": "#f7c948"}, "emotion_cn": {"joy": "开心"}}}


def _script(html):
    return re.search(r"<script>(.*)</script>", html, re.S).group(1)


def test_page_modes_and_cdn_list():
    served, embedded = render_page(None), render_page(_graph())
    assert "const EMBEDDED = null;" in served and "/vendor/three.min.js" in served
    assert "createView3D" in served and "createView2D" in served          # 3D + 2D 降级都在
    for u in THREE_CDN_URLS:
        assert u in served and u in embedded                              # 多个 CDN 依次尝试
    assert '"nodes"' in embedded and "记忆0" in embedded                    # 自包含：数据内联


def test_embedded_data_cannot_break_out_of_script():
    html = render_page(_graph(hostile=True))
    assert "</script><script>window.__pwn" not in html and "<!-- x" not in html
    assert html.count("</script>") == 1                                    # 只有页面自己的那一个结尾标签


@pytest.mark.skipif(not shutil.which("node"), reason="需要 node 做语法检查")
def test_page_script_is_valid_javascript(tmp_path):
    for html in (render_page(None), render_page(_graph())):
        f = tmp_path / "p.js"
        f.write_text(_script(html), encoding="utf-8")
        r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr


def test_server_csp_and_local_three_route(tmp_path):
    env = Env(tmp_path)

    async def go():
        srv = BrainMapServer(env.db_path, port=0, access_key="k", clock=env.clock)
        assert await srv.start()
        p = srv.bound_port
        st, head, body = await _http(p, "GET", "/?key=k")
        assert st == 200 and b"createView3D" in body
        csp = re.search(r"Content-Security-Policy: (.*)", head).group(1)
        assert "'self'" in csp and all(h in csp for h in CDN_HOSTS.split())    # 允许本地 + 三个 CDN
        assert "frame-src" not in csp and "default-src 'none'" in csp
        vendor = os.path.join(os.path.dirname(env.db_path), "three.min.js")
        bundled_status, _, bundled_body = await _http(p, "GET", "/vendor/three.min.js?key=k")
        assert bundled_status == 200 and b'Three.js Authors' in bundled_body
        open(vendor, "w").write("window.THREE={fake:1};")
        st, head, body = await _http(p, "GET", "/vendor/three.min.js?key=k")
        assert st == 200 and body == b"window.THREE={fake:1};" and "javascript" in head
        assert (await _http(p, "GET", "/vendor/three.min.js"))[0] == 401        # 没密钥 → 拒绝
        assert (await _http(p, "POST", "/vendor/three.min.js?key=k"))[0] == 405  # 只读
        await srv.stop()
    try:
        run(go())
    finally:
        env.engine.close()


# ---------------- 需要无头浏览器的测试（没有就跳过） ----------------

@pytest.fixture(scope="module")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    pw = sync_api.sync_playwright().start()
    try:
        b = pw.chromium.launch()
    except Exception as e:                     # 没装浏览器
        pw.stop()
        pytest.skip(f"无头浏览器不可用: {e}")
    yield b
    b.close()
    pw.stop()


STUB_THREE = """
const handler = { get: (t, k) => (k === 'then' ? undefined : (k === Symbol.toPrimitive ? () => 0 : stub())),
                  apply: () => stub(), construct: () => stub() };
function stub(){ return new Proxy(function(){}, handler); }
window.THREE = new Proxy({}, { get: () => stub() });
"""


def _open(browser, html, cdn):
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    if cdn == "blocked":
        page.route(re.compile(r"^https?://(?!localhost).*"), lambda r: r.abort())
    else:
        page.route(re.compile(r"^https://.*three.*\.js$"),
                   lambda r: r.fulfill(status=200, content_type="application/javascript", body=STUB_THREE))
    page.set_content(html)
    return page, errors


def test_falls_back_to_2d_when_cdn_unreachable(browser):
    page, errors = _open(browser, render_page(_graph(hostile=True)), "blocked")
    page.wait_for_function("document.getElementById('note').textContent.length > 0", timeout=20000)
    assert "2D" in page.inner_text("#note") and "CDN" in page.inner_text("#note")
    assert page.query_selector("canvas.flat") and not page.query_selector("canvas.gl")
    assert page.evaluate("window.__pwn") is None                            # 恶意文本没有被当成脚本执行
    assert page.inner_text("#stat").startswith("记忆节点 6")
    assert errors == []
    page.close()


def test_3d_path_runs_without_errors_using_stub_three(browser):
    page, errors = _open(browser, render_page(_graph(40)), "stub")
    page.wait_for_function("window.__gc_view3d_ok === true", timeout=20000)
    page.wait_for_timeout(300)                                              # 让渲染循环跑几帧
    assert page.query_selector("canvas.gl") and page.inner_text("#note") == ""
    page.click("#btnRot")
    assert "关" in page.inner_text("#btnRot")
    page.click("#btnReset")
    assert errors == []
    page.close()


def test_layout3d_is_finite_bounded_and_deterministic(browser):
    page, _ = _open(browser, render_page(_graph(3)), "blocked")
    res = page.evaluate("""() => {
      const mk = (n) => ({nodes: Array.from({length:n}, (_,i)=>({id:i+1, weight: 1-i/Math.max(1,n)})),
                          edges: Array.from({length:n*2}, (_,k)=>({source:(k*7)%n+1, target:(k*13+3)%n+1, strength:((k%10)+1)/10}))});
      const out = {};
      for (const n of [0, 1, 2, 50, 300]) {
        const a = window.__gc.layout3D(mk(n)), b = window.__gc.layout3D(mk(n));
        const arr = Array.from(a.P);
        out[n] = {len: arr.length, finite: arr.every(Number.isFinite),
                  maxr: n ? Math.max(...Array.from({length:n}, (_,i)=>Math.hypot(arr[i*3],arr[i*3+1],arr[i*3+2]))) : 0,
                  same: Array.from(b.P).every((v,i)=>v===arr[i])};
      }
      return out;
    }""")
    for n in (0, 1, 2, 50, 300):
        r = res[str(n)]
        assert r["len"] == n * 3 and r["finite"] and r["same"]              # 无 NaN、确定性
        if n >= 2:
            assert 90 <= r["maxr"] <= 101                                  # 归一化到半径约 100
    page.close()


def test_real_bundled_three_renders_webgl(browser):
    """使用真实 Three.js，全部脚本来自随包文件，不使用代理桩或网络。"""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    vendor = root / 'core/brainmap/vendor/three.min.js'
    page = browser.new_page(viewport={'width': 1180, 'height': 900}, color_scheme='dark')
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.route(re.compile(r'^https://.*three.*\.js$'), lambda route: route.fulfill(path=str(vendor), content_type='application/javascript'))
    graph = _graph(24)
    from growing_companion.core.brainmap.export import EMOTION_COLORS
    from growing_companion.core.models import EMOTION_CN
    graph['meta']['emotion_colors'] = EMOTION_COLORS
    graph['meta']['emotion_cn'] = EMOTION_CN
    emotions = list(EMOTION_COLORS)
    colors = ['#f7c948', '#5ec2a5', '#8e7cc3', '#4a90d9', '#f28c38', '#e5484d']
    for i, node in enumerate(graph['nodes']):
        node['weight'] = 0.2 + (i % 8) * 0.09
        node['color'] = colors[i % 6]
        node['dominant_emotion'] = emotions[i % 6]
        node['dominant_emotion_cn'] = EMOTION_CN[emotions[i % 6]]
        node['size'] = 2 + node['weight'] * 10
        node['summary'] = ['初次相识', '爸爸的鼓励', '学会道歉', '群里的新词', '我的小疑问', '再试一次'][i % 6]
    page.set_content(render_page(graph))
    page.wait_for_function('window.__gc_view3d_ok === true', timeout=20000)
    assert page.evaluate('window.THREE.REVISION') == '128'
    assert page.locator('canvas.gl').count() == 1 and page.inner_text('#note') == ''
    page.click('#btnRot')
    page.click('#btnReset')
    assert not errors
    preview = root / 'docs/brainmap-preview.png'
    preview.parent.mkdir(exist_ok=True)
    page.screenshot(path=str(preview), full_page=True)
    page.close()
