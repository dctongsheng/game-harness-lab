"""A small, paused Snake engine with explicit, versioned rules.

This is an original engine, not an adapter for the previously researched package.
Its vacating-tail rule intentionally differs from that package's frozen rules.
"""

from __future__ import annotations

import hashlib
import json
import random


RULES_VERSION = "game-harness-lab/snake-v1"
ACTION_IDS = ("up", "right", "down", "left")
ACTION_CANDIDATES = [
    {"id": "up", "label": "向上移动一格"},
    {"id": "right", "label": "向右移动一格"},
    {"id": "down", "label": "向下移动一格"},
    {"id": "left", "label": "向左移动一格"},
]
_VECTORS = {"up": (0, -1), "right": (1, 0), "down": (0, 1), "left": (-1, 0)}
_OPPOSITE = {"up": "down", "right": "left", "down": "up", "left": "right"}

GAME_RULES = """规则版本：game-harness-lab/snake-v1。
棋盘坐标为[x,y]，左上角[0,0]，x向右增大，y向下增大。
snake按蛇头到蛇尾排列。每次只能选up/right/down/left，推进一格。
请求与当前方向相反时，游戏规则保持原方向；不会立即掉头。
撞墙或撞到未腾出的身体则本局结束。未吃食物时尾格在本步腾出，允许进入。
吃食物加1分并增长一格；新食物由环境在空格产生，未来位置不可见。
棋盘填满结束；达到固定步数预算也结束。等待模型时游戏完全暂停。
全部四个方向都可选择，包含可能导致死亡的方向，程序不代选安全方向。
目标是在固定步数预算内获得最多食物分数。你是唯一动作决策者。
"""


def candidate_hash(candidates=None) -> str:
    """Canonical action-catalog hash shared with provider receipts."""
    payload = ACTION_CANDIDATES if candidates is None else candidates
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class SnakeGame:
    """Advance only on step(); elapsed wall time never advances the snake."""

    def __init__(self, size: int = 8, seed: int = 0, max_steps: int = 120):
        if type(size) is not int or size < 4:
            raise ValueError("size must be an integer >= 4")
        if type(max_steps) is not int or max_steps < 1:
            raise ValueError("max_steps must be a positive integer")
        if type(seed) is not int:
            raise ValueError("seed must be an integer")
        self.size = size
        self.seed = seed
        self.max_steps = max_steps
        self._rng = random.Random(seed)
        mid = size // 2
        self.snake = [(mid, mid), (mid - 1, mid), (mid - 2, mid)]
        self.direction = "right"
        self.score = 0
        self.step_count = 0
        self.done = False
        self.reason = None
        self.food = self._new_food()

    def _new_food(self):
        occupied = set(self.snake)
        empty = [(x, y) for y in range(self.size) for x in range(self.size) if (x, y) not in occupied]
        return self._rng.choice(empty) if empty else None

    def snapshot(self) -> dict:
        """Return detached JSON data; no RNG state or future food is exposed."""
        return {
            "size": self.size,
            "snake": [list(cell) for cell in self.snake],
            "food": list(self.food) if self.food is not None else None,
            "direction": self.direction,
            "score": self.score,
            "step": self.step_count,
            "done": self.done,
            "reason": self.reason,
            "rules_version": RULES_VERSION,
            "max_steps": self.max_steps,
        }

    def step(self, action_id: str) -> dict:
        if self.done:
            raise RuntimeError("episode already ended")
        if not isinstance(action_id, str) or action_id not in ACTION_IDS:
            raise ValueError("action must be up/right/down/left")
        applied = self.direction if action_id == _OPPOSITE[self.direction] else action_id
        self.direction = applied
        self.step_count += 1
        dx, dy = _VECTORS[applied]
        head = (self.snake[0][0] + dx, self.snake[0][1] + dy)
        eating = head == self.food
        body = self.snake if eating else self.snake[:-1]
        if not (0 <= head[0] < self.size and 0 <= head[1] < self.size):
            self.done, self.reason = True, "wall_collision"
        elif head in body:
            self.done, self.reason = True, "self_collision"
        else:
            self.snake = [head] + (self.snake if eating else self.snake[:-1])
            if eating:
                self.score += 1
                self.food = self._new_food()
                if self.food is None:
                    self.done, self.reason = True, "board_filled"
        if not self.done and self.step_count >= self.max_steps:
            self.done, self.reason = True, "step_limit"
        return self.snapshot()
