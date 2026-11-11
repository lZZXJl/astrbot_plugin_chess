"""棋局核心：用 python-chess 管规则，网页和插件工具共用同一份状态。"""

from __future__ import annotations

import threading
import time

import chess

PIECE_NAME_CN = {
    "P": "兵",
    "N": "马",
    "B": "象",
    "R": "车",
    "Q": "后",
    "K": "王",
}

FILES = "abcdefgh"


class ChessGame:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.reset()

    # ---------- 基础 ----------
    def reset(self, player_side: str = "white") -> None:
        with self._lock:
            self.board = chess.Board()
            self.player_side = "white" if player_side != "black" else "black"
            self.history: list[dict] = []  # {ply, side, san, uci, ts}
            self.last_move: tuple[str, str] | None = None
            self.result: str | None = None
            self.resigned_by: str | None = None
            self.started_at = time.time()
            self.chat: list[dict] = []

    @property
    def turn(self) -> str:
        return "white" if self.board.turn else "black"

    @property
    def bot_side(self) -> str:
        """执棋的那一方永远和夏季对着来。"""
        return "black" if self.player_side == "white" else "white"

    @property
    def is_player_turn(self) -> bool:
        return self.turn == self.player_side

    def _after_change(self) -> None:
        if self.board.is_game_over(claim_draw=True):
            self.result = self._outcome_text()

    def _outcome_text(self) -> str:
        if self.board.is_checkmate():
            winner = "黑方" if self.board.turn else "白方"
            return f"{winner}将死"
        if self.board.is_stalemate():
            return "和棋（逼和）"
        if self.board.is_insufficient_material():
            return "和棋（子力不足）"
        if self.board.is_seventyfive_moves() or self.board.is_fivefold_repetition():
            return "和棋（规则判和）"
        return "对局结束"

    # ---------- 走子 ----------
    def _record(self, move: chess.Move) -> str:
        san = self.board.san(move)
        side = "white" if self.board.turn else "black"
        self.board.push(move)
        self.last_move = (chess.square_name(move.from_square), chess.square_name(move.to_square))
        self.history.append(
            {"ply": len(self.history) + 1, "side": side, "san": san, "uci": move.uci(), "ts": time.time()}
        )
        self._after_change()
        return san

    def _side_guard(self, side: str | None) -> str | None:
        """按“这步该谁走”卡一道，防止替对方动子。

        只认白/黑两种取值，写错了就当没传，不因为参数瑕疵把棋下死。
        """
        if side not in ("white", "black"):
            return None
        if side != self.turn:
            who = "白方" if self.turn == "white" else "黑方"
            return f"现在轮到{who}走，这步不是那一方该动的"
        return None

    def move_uci(
        self, uci: str, promotion: str | None = None, side: str | None = None
    ) -> tuple[bool, str]:
        with self._lock:
            if self.result:
                return False, "这局已经结束了"
            guard = self._side_guard(side)
            if guard:
                return False, guard
            text = (uci or "").strip().lower().replace(" ", "")
            if len(text) == 4 and promotion:
                text = text + promotion.lower()[:1]
            try:
                move = chess.Move.from_uci(text)
            except ValueError:
                return False, f"看不懂「{uci}」这个坐标，要像 e2e4 这样"
            if move not in self.board.legal_moves:
                return False, f"{text} 这步走不了，要么不合规则，要么现在有别的子挡着"
            san = self._record(move)
            return True, san

    def move_san(self, san: str, side: str | None = None) -> tuple[bool, str]:
        with self._lock:
            if self.result:
                return False, "这局已经结束了"
            guard = self._side_guard(side)
            if guard:
                return False, guard
            try:
                move = self.board.parse_san((san or "").strip())
            except ValueError:
                return False, f"「{san}」不是合法的记谱，换个写法试试"
            if move not in self.board.legal_moves:
                return False, "这步不合规则"
            return True, self._record(move)

    def try_move_any(self, text: str, side: str | None = None) -> tuple[bool, str]:
        t = (text or "").strip()
        if not t:
            return False, "没收到走法"
        ok, msg = self.move_uci(t, side=side)
        if ok:
            return ok, msg
        return self.move_san(t, side=side)

    def undo(self, plies: int = 1) -> int:
        with self._lock:
            n = 0
            for _ in range(max(1, plies)):
                if not self.board.move_stack:
                    break
                self.board.pop()
                if self.history:
                    self.history.pop()
                n += 1
            self.result = None
            self.last_move = None
            if self.history:
                last = self.history[-1]
                self.last_move = (last["uci"][:2], last["uci"][2:4])
            return n

    def resign(self, side: str) -> str:
        with self._lock:
            self.resigned_by = "white" if side == "white" else "black"
            self.result = f"{'白方' if side == 'white' else '黑方'}认输"
            return self.result

    # ---------- 输出 ----------
    def board_matrix(self) -> list[list[str | None]]:
        """从白方视角，第 8 行在前。"""
        rows: list[list[str | None]] = []
        for rank in range(7, -1, -1):
            row: list[str | None] = []
            for file in range(8):
                piece = self.board.piece_at(chess.square(file, rank))
                row.append(piece.symbol() if piece else None)
            rows.append(row)
        return rows

    def legal_map(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for m in self.board.legal_moves:
            out.setdefault(chess.square_name(m.from_square), []).append(chess.square_name(m.to_square))
        return out

    def to_json(self) -> dict:
        with self._lock:
            return {
                "fen": self.board.fen(),
                "board": self.board_matrix(),
                "turn": self.turn,
                "player_side": self.player_side,
                "history": self.history,
                "last_move": list(self.last_move) if self.last_move else None,
                "check": self.board.is_check(),
                "result": self.result,
                "legal": self.legal_map(),
            }

    def render_text(self, max_moves: int = 12) -> str:
        """给聊天用的文字棋盘。"""
        with self._lock:
            lines = ["　　ａｂｃｄｅｆｇｈ"]
            for idx, rank in enumerate(range(7, -1, -1)):
                cells = []
                for file in range(8):
                    piece = self.board.piece_at(chess.square(file, rank))
                    if not piece:
                        cells.append("·")
                    else:
                        # python-chess 的 symbol() 白子大写、黑子小写，直接原样用
                        cells.append(piece.symbol())
                lines.append(f"{rank + 1:<3}{' '.join(cells)}")
            lines.append("　　（大写为白方，小写为黑方）")
            seq = self.history
            if len(seq) > max_moves:
                seq = seq[-max_moves:]
                move_text = "… " + " ".join(m["san"] for m in seq)
            else:
                move_text = " ".join(m["san"] for m in seq) or "还没走"
            status = self.result or f"轮到{'白方' if self.board.turn else '黑方'}"
            if self.board.is_check() and not self.result:
                status += "，被将军"
            return (
                "\n".join(lines)
                + f"\n{move_text}\n{status}\nFEN: {self.board.fen()}"
            )
