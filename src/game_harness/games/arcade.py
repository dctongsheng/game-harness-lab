"""Common paused adapter for the vendored MIT Decision Arena arcade engines."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

from .node_bridge import NodeBridge


_ROOT = Path(__file__).parent
SOURCE = json.loads((_ROOT / "vendor/arena-arcade/SOURCE.json").read_text())
SOURCE_CONTEXT = {key: SOURCE[key] for key in (
    "upstream_repository", "upstream_commit", "license", "source_snapshot_sha256")}
CONTROL_RULES = """本框架采用可暂停的决策步进。模型等待期间推进0物理tick；一个决策仅选择一个原生按键候选。
候选中的100/300/1000毫秒分别执行6/18/60个60Hz物理tick；noop:100松键后也推进6tick。
若引擎在这段期间自然结束，则立即结束，不再推进剩余tick。每次输入替换整组按住的键，到期自动松键。
跳跃、旋转、硬降、暂存只在新按下的边沿触发一次，持续按住不自动重复；每个完整动作结束后已自然松键，
下一次同键候选会产生新的按下边沿。横移按原生加速度/重复规则运行，没有寻路、自动跳跃、落点选择或危险动作过滤。
step是Decision决策次数，tick是物理时钟。到max_steps记step_limit截断，保留原生points，不授予生存奖励。
本项目评分只用原生points；不使用参考竞技场compare的存活或其他平局排序。"""


class ArcadeSession:
    def __init__(self, game_id: str, seed: int, max_steps: int):
        if type(seed) is not int or not -(2**53 - 1) <= seed <= 2**53 - 1:
            raise ValueError("seed must be a safe integer")
        if type(max_steps) is not int or not 1 <= max_steps <= 2000:
            raise ValueError("max_steps must be an integer between 1 and 2000")
        self._bridge = NodeBridge("arcade_bridge.cjs")
        try:
            self._state = self._bridge.request({"op": "init", "game_id": game_id,
                                                "seed": seed, "max_steps": max_steps})
        except Exception:
            self.close()
            raise

    def snapshot(self) -> dict:
        return deepcopy(self._state["snapshot"])

    def candidates(self) -> list[dict[str, str]]:
        return deepcopy(self._state["candidates"])

    def step(self, action_id: str) -> dict:
        if self._state["snapshot"]["done"]:
            raise RuntimeError("episode already ended")
        if not isinstance(action_id, str) or action_id not in {c["id"] for c in self._state["candidates"]}:
            raise ValueError("action must be in the complete native input catalog")
        self._state = self._bridge.request({"op": "step", "action": action_id})
        return self.snapshot()

    def close(self):
        if hasattr(self, "_bridge"):
            self._bridge.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class ArcadeAdapter:
    opponent_policy = None

    def validate_config(self, config):
        steps = config.get("max_steps", self.defaults["max_steps"])
        if type(steps) is not int or not 1 <= steps <= 2000:
            raise ValueError("max_steps必须为1到2000的整数决策次数")

    def scope_config(self, config):
        self.validate_config(config)
        return {"max_steps": config.get("max_steps", self.defaults["max_steps"]),
                "clock": "paused_native_input_duration", "tick_rate": 60}

    def context(self, config):
        return {"game_id": self.id, "name": self.name, "rules_version": self.rules_version,
                "rules": self.rules, "score_spec": deepcopy(self.score_spec),
                "config": self.scope_config(config), "opponent_policy": None,
                "scenario_policy": self.scenario_policy,
                "action_policy": CONTROL_RULES,
                "evaluation": deepcopy(self.evaluation),
                "source": {**SOURCE_CONTEXT,
                           "bridge_sha256": hashlib.sha256((_ROOT / "arcade_bridge.cjs").read_bytes()).hexdigest()}}

    def create(self, seed, config):
        self.validate_config(config)
        return ArcadeSession(self.id, seed, config.get("max_steps", self.defaults["max_steps"]))
