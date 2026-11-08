"""给棋盘配的本地网页服务：横屏当输入法用。"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Awaitable, Callable

from aiohttp import web

from game import ChessGame

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

HTML_PAGE = None


def _load_static(name: str) -> str:
    path = os.path.join(WEB_DIR, name)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


class ChessWebServer:
    def __init__(
        self,
        game: ChessGame,
        host: str,
        port: int,
        on_chat: Callable[[str], Awaitable[None]] | None = None,
        on_move: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> None:
        self.game = game
        self.host = host
        self.port = port
        self.on_chat = on_chat
        self.on_move = on_move
        self._runner: web.AppRunner | None = None
        self._msg_seq = 0

    # ---------- 消息流 ----------
    def push_message(self, role: str, text: str) -> None:
        self._msg_seq += 1
        self.game.chat.append(
            {"id": self._msg_seq, "role": role, "text": text, "ts": time.time()}
        )
        if len(self.game.chat) > 200:
            del self.game.chat[:100]

    # ---------- 路由 ----------
    async def _index(self, request: web.Request) -> web.Response:
        try:
            html = _load_static("index.html")
        except OSError:
            return web.Response(text="页面文件丢了，看看 web/index.html 还在不在", status=500)
        return web.Response(text=html, content_type="text/html", charset="utf-8")

    async def _static(self, request: web.Request) -> web.Response:
        name = request.match_info.get("name", "")
        if "/" in name or "\\" in name or name.startswith("."):
            raise web.HTTPNotFound()
        try:
            body = _load_static(name)
        except OSError:
            raise web.HTTPNotFound()
        ctype = "text/plain"
        if name.endswith(".css"):
            ctype = "text/css"
        elif name.endswith(".js"):
            ctype = "application/javascript"
        return web.Response(text=body, content_type=ctype, charset="utf-8")

    async def _state(self, request: web.Request) -> web.Response:
        return web.json_response(self.game.to_json())

    async def _move(self, request: web.Request) -> web.Response:
        data = await request.json()
        move = str(data.get("move", ""))
        promo = data.get("promotion") or None
        if len(move) == 4:
            ok, msg = self.game.move_uci(move, promo)
        else:
            ok, msg = self.game.move_san(move)
        if ok:
            self.push_message("system", f"你走了 {msg}")
            if self.on_move:
                asyncio.create_task(self.on_move(move, msg))
        return web.json_response({"ok": ok, "msg": msg, "state": self.game.to_json()})

    async def _bot_move(self, request: web.Request) -> web.Response:
        """我自己走的一步：只改局面并推到网页，不触发聊天注入。"""
        data = await request.json()
        move = str(data.get("move", ""))
        promo = data.get("promotion") or None
        if len(move) == 4:
            ok, msg = self.game.move_uci(move, promo)
        else:
            ok, msg = self.game.move_san(move)
        if ok and not data.get("silent"):
            self.push_message("bot", f"（我走了 {msg}）")
        return web.json_response({"ok": ok, "msg": msg, "state": self.game.to_json()})

    async def _new(self, request: web.Request) -> web.Response:
        data = await request.json() if request.can_read_body else {}
        side = data.get("side", "white")
        self.game.reset(player_side=side)
        self.push_message("system", "新开一局，你先手" if side == "white" else "新开一局，你先手（我执白）")
        return web.json_response({"ok": True, "state": self.game.to_json()})

    async def _undo(self, request: web.Request) -> web.Response:
        n = self.game.undo(2 if len(self.game.history) >= 2 else 1)
        return web.json_response({"ok": bool(n), "n": n, "state": self.game.to_json()})

    async def _resign(self, request: web.Request) -> web.Response:
        data = await request.json()
        side = data.get("side", self.game.player_side)
        msg = self.game.resign(side)
        self.push_message("system", msg)
        return web.json_response({"ok": True, "msg": msg, "state": self.game.to_json()})

    async def _chat(self, request: web.Request) -> web.Response:
        data = await request.json()
        text = str(data.get("text", "")).strip()
        if not text:
            return web.json_response({"ok": False, "msg": "空的"})
        self.push_message("user", text)
        if self.on_chat:
            asyncio.create_task(self.on_chat(text))
            return web.json_response({"ok": True})
        return web.json_response({"ok": False, "msg": "这条线还没接上"})

    async def _say(self, request: web.Request) -> web.Response:
        """我自己往网页聊天区说话：只推一条 bot 消息，不回注到 QQ。"""
        data = await request.json() if request.can_read_body else {}
        text = str(data.get("text", "")).strip()
        if not text:
            return web.json_response({"ok": False, "msg": "空的"})
        self.push_message("bot", text)
        return web.json_response({"ok": True})

    async def _events(self, request: web.Request) -> web.Response:
        try:
            since = int(request.query.get("since", "0"))
        except ValueError:
            since = 0
        if since > self._msg_seq:
            # 页面拿着旧序号来了（服务重启过），给它整份，别再空转
            since = 0
        items = [m for m in self.game.chat if m["id"] > since]
        return web.json_response({"messages": items, "seq": self._msg_seq})

    async def _health(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "port": self.port})

    def build(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/", self._index)
        app.router.add_get("/api/state", self._state)
        app.router.add_post("/api/move", self._move)
        app.router.add_post("/api/bot_move", self._bot_move)
        app.router.add_post("/api/new", self._new)
        app.router.add_post("/api/undo", self._undo)
        app.router.add_post("/api/resign", self._resign)
        app.router.add_post("/api/chat", self._chat)
        app.router.add_get("/api/events", self._events)
        app.router.add_post("/api/say", self._say)
        app.router.add_get("/api/health", self._health)
        app.router.add_get("/s/{name}", self._static)
        app.router.add_get("/{name}", self._static)
        return app

    async def start(self) -> None:
        runner = web.AppRunner(self.build(), access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, self.host, self.port)
        await site.start()
        self._runner = runner

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
