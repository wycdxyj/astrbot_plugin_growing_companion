"""aiohttp 只读脑图：默认 0.0.0.0:5208，独立 mode=ro 连接、鉴权、无写路由。"""
from __future__ import annotations

import asyncio
import hmac
import logging
from pathlib import Path
from typing import Any

from aiohttp import web

from ..clock import Clock
from ..db import ReadOnlyDB
from .export import export_graph
from .page import CDN_HOSTS, LOCAL_THREE_PATH, render_page

log = logging.getLogger('growing_companion.brainmap')


class BrainMapServer:
    def __init__(self, db_path: str, *, host: str = '0.0.0.0', port: int = 5208,
                 access_key: str = '', max_nodes: int = 300, decay_speed: float = 1.0,
                 fallback_tries: int = 0, clock: Clock | None = None):
        self.db_path, self.host, self.port = db_path, host, int(port)
        if not 0 <= self.port <= 65535:
            raise ValueError('脑图端口必须在 0..65535（0 仅供测试随机端口）')
        self.access_key = access_key
        self.max_nodes = max(1, min(1000, int(max_nodes)))
        self.decay_speed = decay_speed
        self.fallback_tries = max(0, min(20, int(fallback_tries)))
        self.clock = clock or Clock()
        self.running = False
        self.bound_port: int | None = None
        self.last_error = ''
        self._runner: web.AppRunner | None = None
        self._guard = asyncio.Lock()
        self._read_slots = asyncio.Semaphore(2)

    async def start(self) -> bool:
        async with self._guard:
            if self.running:
                return True
            for offset in range(self.fallback_tries + 1):
                port = self.port + offset if self.port else 0
                if port > 65535:
                    break
                app = web.Application(client_max_size=1024)
                app.router.add_route('*', '/{path:.*}', self._handle)
                runner = web.AppRunner(app, access_log=None, shutdown_timeout=3)
                try:
                    await runner.setup()
                    await web.TCPSite(runner, self.host, port).start()
                except OSError as exc:
                    await runner.cleanup()
                    self.last_error = f'脑图端口 {self.host}:{port} 无法监听（{exc}）；请修改端口并检查端口映射。'
                    log.error(self.last_error)
                    continue
                self._runner = runner
                self.bound_port = int(runner.addresses[0][1])
                self.running, self.last_error = True, ''
                return True
            return False

    async def stop(self) -> None:
        async with self._guard:
            if self._runner:
                await self._runner.cleanup()
            self._runner, self.running, self.bound_port = None, False, None

    def _authorized(self, key: str) -> bool:
        return bool(self.access_key) and hmac.compare_digest(key.encode(), self.access_key.encode())

    def _build_graph(self) -> dict[str, Any]:
        ro = ReadOnlyDB(self.db_path)
        try:
            return export_graph(ro, self.clock.now(), max_nodes=self.max_nodes, decay_speed=self.decay_speed)
        finally:
            ro.close()

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        if request.method not in ('GET', 'HEAD'):
            response = web.json_response({'error': 'read-only service'}, status=405, headers={'Allow': 'GET, HEAD'})
        elif request.path == '/api/health':
            response = web.json_response({'ok': True, 'readonly': True})
        elif not self._authorized(request.headers.get('X-Access-Key', '') or request.query.get('key', '')):
            response = web.json_response({'error': 'unauthorized'}, status=401)
        else:
            try:
                response = await self._read(request.path)
            except Exception:
                log.exception('脑图只读请求失败')
                response = web.json_response({'error': 'internal error'}, status=500)
        response.headers.update({'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
                                 'X-Content-Type-Options': 'nosniff', 'X-Frame-Options': 'DENY'})
        return response

    async def _read(self, path: str) -> web.StreamResponse:
        if path in ('/', '/index.html'):
            response = web.Response(text=render_page(None), content_type='text/html')
            response.headers['Content-Security-Policy'] = (
                f"default-src 'none'; script-src 'unsafe-inline' 'self' {CDN_HOSTS}; "
                "style-src 'unsafe-inline'; connect-src 'self'; img-src data:; frame-ancestors 'none'")
            return response
        if path == '/api/graph':
            if not Path(self.db_path).is_file():
                return web.json_response({'error': 'no data yet'}, status=503)
            async with self._read_slots:
                graph = await asyncio.to_thread(self._build_graph)
            return web.json_response(graph)
        if path == LOCAL_THREE_PATH:
            vendor = Path(self.db_path).parent / 'three.min.js'
            if not vendor.is_file():
                vendor = Path(__file__).parent / 'vendor' / 'three.min.js'
            if vendor.is_file() and vendor.stat().st_size <= 8 * 1024 * 1024:
                return web.Response(body=await asyncio.to_thread(vendor.read_bytes), content_type='application/javascript')
        return web.json_response({'error': 'not found'}, status=404)
