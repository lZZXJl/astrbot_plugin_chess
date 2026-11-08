"""国际象棋插件：网页棋盘当输入法，我在这边接着走子聊天。"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
import time
import uuid

# 插件自己的目录进 path，按文件加载时也能 import 同目录模块
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.message.components import Plain
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType

# 插件重载时顺手把同目录的模块从缓存里清掉，不然改完文件还是跑旧的
for _m in ("game", "web_server"):
    sys.modules.pop(_m, None)

from game import ChessGame
from web_server import ChessWebServer


def _lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.4)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


@register("chess_board", "lzzxjl", "网页棋盘，横屏当输入法用", "0.1.0")
class ChessPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self.game = ChessGame()
        self.server: ChessWebServer | None = None
        self._owner_id = str(self.config.get("owner_id", "") or "")

    # ---------- 生命周期 ----------
    async def initialize(self):
        if not self.config.get("web_enabled", True):
            logger.info("[chess] 网页服务没开，只走聊天")
            return
        host = str(self.config.get("web_host", "0.0.0.0"))
        port = int(self.config.get("web_port", 8765) or 8765)
        self.server = ChessWebServer(
            self.game,
            host=host,
            port=port,
            on_chat=self._on_web_chat,
            on_move=self._on_web_move,
        )
        try:
            await self.server.start()
            logger.info(f"[chess] 网页棋盘启动：http://{_lan_ip()}:{port}")
        except Exception as e:
            logger.error(f"[chess] 网页棋盘起不来：{e}")
            self.server = None

    async def terminate(self):
        if self.server:
            await self.server.stop()
            self.server = None

    # ---------- 网页消息注入 ----------
    def _find_adapter(self):
        try:
            for p in self.context.platform_manager.platform_insts:
                meta = p.meta() if hasattr(p, "meta") else None
                if meta and getattr(meta, "name", "") == "aiocqhttp":
                    return p
        except Exception as e:
            logger.error(f"[chess] 找不到平台适配器：{e}")
        return None

    async def _inject(self, text: str) -> bool:
        if not self._owner_id:
            if self.server:
                self.server.push_message("system", "还没配置 owner_id，这条消息注不进聊天")
            logger.warning("[chess] owner_id 未配置，网页消息无法注入会话")
            return False
        adapter = self._find_adapter()
        if not adapter:
            if self.server:
                self.server.push_message("system", "这条线没通到 QQ，先当作记下来")
            return False
        try:
            msg = AstrBotMessage()
            msg.type = MessageType.FRIEND_MESSAGE
            msg.self_id = str(getattr(adapter, "self_id", "") or "")
            msg.session_id = self._owner_id
            msg.message_id = f"web-{uuid.uuid4().hex[:12]}"
            msg.group = None
            msg.sender = MessageMember(user_id=self._owner_id, nickname="网页")
            msg.message = [Plain(text)]
            msg.message_str = text
            msg.raw_message = {}
            msg.timestamp = int(time.time())
            event = adapter.create_event(msg)
            adapter.commit_event(event)
            return True
        except Exception as e:
            logger.error(f"[chess] 注入失败：{e}")
            return False

    async def _on_web_move(self, move: str, san: str):
        if self.game.result:
            text = f"（网页棋盘）我刚走了 {san}，这局已经结束了：{self.game.result}。要是还想下，开新的。"
        else:
            text = (
                f"（网页棋盘）夏季刚在棋盘上走了 {san}。"
                "轮到你了，先用 chess_state 看一眼局面再走，可以用 chess_move。"
            )
        await self._inject(text)

    async def _on_web_chat(self, text: str):
        await self._inject(f"（网页聊天）{text}")

    # ---------- 聊天指令 ----------
    @filter.command("棋")
    async def show_board(self, event: AstrMessageEvent):
        """看当前棋局。"""
        url = f"http://{_lan_ip()}:{int(self.config.get('web_port', 8765) or 8765)}"
        yield event.plain_result(f"{self.game.render_text()}\n网页棋盘：{url}")

    @filter.command("新局")
    async def new_game(self, event: AstrMessageEvent):
        """开一局新的，默认我执黑。"""
        self.game.reset(player_side="white")
        if self.server:
            self.server.push_message("system", "新开一局")
        yield event.plain_result("开好了，你执白先走。")

    # ---------- LLM 工具 ----------
    @filter.llm_tool(name="chess_state")
    async def tool_state(self, event: AstrMessageEvent):
        """查看当前国际象棋局面、走子记录和轮到谁走。

        Returns:
            string: 局面摘要
        """
        g = self.game
        return (
            f"{g.render_text()}\n"
            f"网页那边夏季执{g.player_side}，现在轮到{g.turn}。"
        )

    @filter.llm_tool(name="chess_move")
    async def tool_move(self, event: AstrMessageEvent, move: str):
        """在当前棋局走一步。

        Args:
            move(string): 走法，uci 坐标（如 e2e4）或者标准记谱（如 Nf3）
        Returns:
            string: 结果
        """
        ok, msg = self.game.try_move_any(move)
        if not ok:
            return f"这步没走成：{msg}"
        if self.server:
            self.server.push_message("bot", f"（我走了 {msg}）")
        tail = f"现在轮到{self.game.turn}。"
        if self.game.result:
            tail = f"这局结束了：{self.game.result}。"
        return f"走了 {msg}。{tail}"

    @filter.llm_tool(name="chess_new")
    async def tool_new(self, event: AstrMessageEvent, side: str = "white"):
        """开一局新的国际象棋。

        Args:
            side(string): 夏季执哪一方，white 或 black
        Returns:
            string: 结果
        """
        self.game.reset(player_side=side if side in ("white", "black") else "white")
        if self.server:
            self.server.push_message("system", "新开一局")
        return "新局开好了。"

    @filter.llm_tool(name="chess_undo")
    async def tool_undo(self, event: AstrMessageEvent, plies: int = 2):
        """悔棋，默认退回两步（你和夏季各一步）。

        Args:
            plies(number): 退几步
        Returns:
            string: 结果
        """
        n = self.game.undo(int(plies))
        return f"退了 {n} 步。{self.game.render_text()}" if n else "没得退了。"
