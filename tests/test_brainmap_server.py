"""脑图 Web 服务：端口冲突、改端口、只读与密钥校验。"""
import asyncio
import json
import socket

from growing_companion.core.brainmap.server import BrainMapServer

from .conftest import Env, run


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


async def _http(port, method, path, headers=None):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    h = "".join(f"{k}: {v}\r\n" for k, v in (headers or {}).items())
    w.write(f"{method} {path} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n{h}\r\n".encode())
    await w.drain()
    raw = await r.read()
    w.close()
    head, _, body = raw.partition(b"\r\n\r\n")
    return int(head.split(b" ", 2)[1]), head.decode("latin-1"), body


def test_port_conflict_is_reported_not_crashed(tmp_path):
    env = Env(tmp_path)
    blocker = socket.socket()
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    blocker.bind(("0.0.0.0", 0))
    blocker.listen(1)
    busy = blocker.getsockname()[1]

    async def go():
        srv = BrainMapServer(env.db_path, port=busy, access_key="k", fallback_tries=0)
        ok = await srv.start()
        assert ok is False and srv.running is False
        assert "端口" in srv.last_error and str(busy) in srv.last_error           # 给出可操作的提示
        # 改端口（等价于在 AstrBot 后台改配置后重载）：不改代码即可恢复
        srv2 = BrainMapServer(env.db_path, port=_free_port(), access_key="k")
        assert await srv2.start() is True and srv2.running
        status, _, body = await _http(srv2.bound_port, "GET", "/api/health")
        assert status == 200 and json.loads(body)["readonly"] is True
        await srv2.stop()
        # 可选：自动顺延
        srv3 = BrainMapServer(env.db_path, port=busy, access_key="k", fallback_tries=5)
        assert await srv3.start() is True and srv3.bound_port != busy
        await srv3.stop()
    try:
        run(go())
    finally:
        blocker.close()
        env.engine.close()


def test_default_port_and_bind_address(tmp_path):
    env = Env(tmp_path)
    srv = BrainMapServer(env.db_path, access_key="k")
    assert srv.port == 5208 and srv.host == "0.0.0.0"           # 默认 5208（我爱你爸爸），监听 0.0.0.0

    async def go():
        s = BrainMapServer(env.db_path, port=0, access_key="k")
        assert await s.start()
        addr = s._runner.addresses[0][0]
        await s.stop()
        return addr
    try:
        assert run(go()) == "0.0.0.0"
    finally:
        env.engine.close()


def test_readonly_and_key_check(tmp_path):
    env = Env(tmp_path, {"persona_init": {"initial_stage": "child"}})

    async def go():
        await env.say("爸爸来啦，你真乖！")
        await env.say("不许再这样了")
        srv = BrainMapServer(env.db_path, port=0, access_key="secret-key", clock=env.clock)
        assert await srv.start()
        p = srv.bound_port
        assert (await _http(p, "GET", "/api/graph"))[0] == 401                       # 无密钥
        assert (await _http(p, "GET", "/api/graph?key=wrong"))[0] == 401
        st, head, body = await _http(p, "GET", "/api/graph?key=secret-key")
        data = json.loads(body)
        assert st == 200 and data["nodes"] and "no-store" in head
        assert (await _http(p, "GET", "/api/graph", {"X-Access-Key": "secret-key"}))[0] == 200
        for m in ("POST", "PUT", "DELETE", "PATCH"):                                  # 任何写方法都拒绝
            assert (await _http(p, "POST" if m == "POST" else m, "/api/graph?key=secret-key"))[0] == 405
        st, _, page = await _http(p, "GET", "/?key=secret-key")
        assert st == 200 and b"createView3D" in page and b"createView2D" in page
        assert (await _http(p, "GET", "/nope?key=secret-key"))[0] == 404
        await srv.stop()
        # 没配密钥 → 拒绝一切数据访问（安全默认）
        s2 = BrainMapServer(env.db_path, port=0, access_key="", clock=env.clock)
        assert await s2.start()
        assert (await _http(s2.bound_port, "GET", "/api/graph?key="))[0] == 401
        await s2.stop()
    try:
        run(go())
    finally:
        env.engine.close()
