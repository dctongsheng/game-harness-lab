"""Paused Chinese chess: a BSD rules engine, with every move chosen by Decision.

The engine only generates legal moves and applies the chosen move. The fixed
material score is a cutoff proxy; it is never used to select or prune actions.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import selectors
import shutil
import subprocess
import threading
import time


RULES_VERSION = "game-harness-lab/xiangqi-casual-v1-f9019ac"
MATERIAL_WEIGHTS = {"r": 9, "n": 4, "c": 4, "b": 2, "a": 2, "p": 1, "k": 0}
SCORE_SPEC = {
    "id": "xiangqi-outcome-material-v1",
    "range": [0, 100],
    "min": 0,
    "max": 100,
    "direction": "maximize",
    "minimum_mean_gain": 2.0,
    "higher_is_better": True,
    "perspective": "candidate_color",
    "win": 100,
    "loss": 0,
    "draw": 50,
    "cutoff_formula": "50 + max(-40, min(40, candidate_material - opponent_material))",
    "material_weights": MATERIAL_WEIGHTS,
    "cutoff_is_proxy_not_win": True,
    "description": "胜100、负0、规则和棋50；半回合上限仅按固定子力差给10..90代理分，不判胜负。",
}
RULES = """中国象棋规则版本：game-harness-lab/xiangqi-casual-v1-f9019ac。
标准开局，红先黑后。坐标a-i从红方左至右，0为红底线，9为黑底线。
红方兵向较大横线前进，黑方卒向较小横线前进。cells从黑底线到红底线逐行排列。
马腿、象眼、象不过河、将帅士仕九宫、炮隔一子吃子、过河兵卒、将帅不能照面和必须应将均适用。
所有候选都是规则引擎给出的完整合法ICCS走法（例如h2h9），没有按战术得分筛选。
将死或困毙对手即获胜；同一局面和行棋方三次出现、连续120半回合无吃子、
或双方仅剩将帅士象则和棋。暂不实施赛事长将长捉责任裁定。
每次选择一个走法，仅推进一个半回合；等待模型时棋局暂停。
候选策略执红或黑由种子奇偶决定。双方全部走法均由同一Decision模型选择；对手使用冻结参考提示。
目标是赢棋。实际胜100分、负0分、和50分；若达到半回合预算则记截断而非胜负，
代理分=50+裁剪到[-40,40]的己方减对方子力差；车9、马4、炮4、士象2、兵卒1、将帅0。
种子不会随机棋盘；目前均从标准局面开始，仅用于切换候选红黑方和标记重复试验。
"""
OPPONENT_POLICY = {
    "decision_instructions": "你执state.turn对应颜色，参考策略固定。根据当前棋盘，从全部合法ICCS候选中选择一步。优先避免被将死，必须应将；把握直接将死、吃掉无保护高价值棋子的机会，注意对手威胁和自身子力安全，协调车马炮并发展棋子。不要为提高候选方的得分而故意让棋。",
    "strategy_notes": "这是冻结参考对手xiangqi-reference-v1。只使用当前公开局面和完整合法走法，逐步决策；对局目标是赢棋。代理材料权重固定为车9、马炮4、士象2、兵卒1，但不能用材料领先替代将帅安全。",
}


class XiangqiEngineError(RuntimeError):
    """Rules process failed; the runtime must stop rather than choose a move."""


class XiangqiSession:
    def __init__(self, seed: int = 0, max_steps: int = 80, *, fen: str | None = None,
                 node_binary: str | None = None, timeout: float = 10):
        if type(seed) is not int:
            raise ValueError("seed must be an integer")
        if type(max_steps) is not int or max_steps < 1:
            raise ValueError("max_steps must be a positive integer")
        self.seed = seed
        self.max_steps = max_steps
        self.candidate_color = "r" if seed % 2 == 0 else "b"
        self.step_count = 0
        self.timeout = timeout
        self._closed = False
        self._serial = 0
        self._buffer = b""
        self._lock = threading.Lock()
        node = node_binary or shutil.which("node")
        if not node:
            raise XiangqiEngineError("中国象棋需要 Node.js 18 或更新版本，请先安装 Node.js。")
        bridge = Path(__file__).with_name("xiangqi_bridge.cjs")
        try:
            self._process = subprocess.Popen(
                [node, str(bridge)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, bufsize=0, cwd=bridge.parent,
                env={"PATH": os.environ.get("PATH", ""), "LANG": "C.UTF-8"},
            )
            self._selector = selectors.DefaultSelector()
            self._selector.register(self._process.stdout, selectors.EVENT_READ)
            self._engine = self._request({"op": "init", **({"fen": fen} if fen else {})})
        except Exception:
            self.close()
            raise

    def _request(self, payload: dict) -> dict:
        if self._closed:
            raise XiangqiEngineError("象棋规则进程已关闭")
        with self._lock:
            self._serial += 1
            payload = {**payload, "id": self._serial}
            try:
                self._process.stdin.write((json.dumps(payload) + "\n").encode())
                self._process.stdin.flush()
                deadline = time.monotonic() + self.timeout
                while b"\n" not in self._buffer:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not self._selector.select(remaining):
                        raise XiangqiEngineError("象棋规则进程响应超时")
                    chunk = os.read(self._process.stdout.fileno(), 65536)
                    if not chunk:
                        raise XiangqiEngineError("象棋规则进程意外退出")
                    self._buffer += chunk
                    if len(self._buffer) > 2_000_000:
                        raise XiangqiEngineError("象棋规则响应超过大小限制")
                line, self._buffer = self._buffer.split(b"\n", 1)
                response = json.loads(line)
                if response.get("id") != self._serial:
                    raise XiangqiEngineError("象棋规则响应序号不匹配")
                if not response.get("ok"):
                    raise ValueError(f"象棋规则拒绝操作：{response.get('error', 'unknown')}")
                return response["value"]
            except (BrokenPipeError, OSError, json.JSONDecodeError) as exc:
                self.close()
                raise XiangqiEngineError("象棋规则通信失败") from exc
            except XiangqiEngineError:
                self.close()
                raise

    def snapshot(self) -> dict:
        engine = self._engine
        outcome = engine["outcome"]
        truncated = not outcome and self.step_count >= self.max_steps
        done = bool(outcome or truncated)
        material = {"r": 0, "b": 0}
        for cell in engine["cells"]:
            if cell:
                material[cell["color"]] += MATERIAL_WEIGHTS[cell["type"]]
        other = "b" if self.candidate_color == "r" else "r"
        advantage = material[self.candidate_color] - material[other]
        score, result = 50 + max(-40, min(40, advantage)), None
        if outcome:
            if outcome["winner"] is None:
                score, result = 50, "draw"
            elif outcome["winner"] == self.candidate_color:
                score, result = 100, "win"
            else:
                score, result = 0, "loss"
        return copy.deepcopy({
            "game_id": "xiangqi", "rules_version": RULES_VERSION,
            "step": self.step_count, "max_steps": self.max_steps, "score": score,
            "done": done, "reason": outcome["reason"] if outcome else "step_limit" if truncated else None,
            "result": result, "winner": outcome["winner"] if outcome else None,
            "truncated": bool(truncated), "score_kind": "outcome" if outcome else "material_proxy",
            "fen": engine["fen"], "turn": engine["turn"], "candidate_color": self.candidate_color,
            "policy_role": "candidate" if engine["turn"] == self.candidate_color else "opponent",
            "cells": engine["cells"], "rows": 10, "cols": 9,
            "board_ascii": engine["board_ascii"], "in_check": engine["in_check"],
            "material": material, "candidate_material_advantage": advantage,
            "trial_design": "standard_position_repeated_trials_color_by_seed_parity",
        })

    def candidates(self) -> list[dict[str, str]]:
        if self.snapshot()["done"]:
            return []
        return copy.deepcopy(self._engine["candidates"])

    def step(self, action_id: str) -> dict:
        if self.snapshot()["done"]:
            raise RuntimeError("episode already ended")
        if not isinstance(action_id, str) or action_id not in {c["id"] for c in self._engine["candidates"]}:
            raise ValueError("action must be a current rules-legal ICCS move")
        self._engine = self._request({"op": "step", "action": action_id})
        self.step_count += 1
        return self.snapshot()

    def close(self) -> None:
        if getattr(self, "_closed", True):
            return
        self._closed = True
        selector = getattr(self, "_selector", None)
        if selector:
            selector.close()
        process = getattr(self, "_process", None)
        if process:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
            for stream in (process.stdin, process.stdout):
                if stream:
                    stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def __del__(self):
        self.close()


class XiangqiAdapter:
    id = "xiangqi"
    name = "中国象棋"
    rules_version = RULES_VERSION
    rules = RULES
    score_spec = SCORE_SPEC
    defaults = {"max_steps": 80}
    opponent_policy = OPPONENT_POLICY

    def validate_config(self, config: dict) -> None:
        value = config.get("max_steps", self.defaults["max_steps"])
        if type(value) is not int or not 1 <= value <= 2000:
            raise ValueError("中国象棋 max_steps 必须是1到2000的半回合数")

    def scope_config(self, config: dict) -> dict:
        self.validate_config(config)
        return {"max_steps": config.get("max_steps", self.defaults["max_steps"]),
                "start_position": "standard", "candidate_color": "seed_parity",
                "reference_policy_version": "xiangqi-reference-v1"}

    def context(self, config: dict) -> dict:
        return {"game_id": self.id, "name": self.name, "rules": self.rules,
                "rules_version": self.rules_version, "score_spec": self.score_spec,
                "config": self.scope_config(config),
                "opponent_policy": self.opponent_policy,
                "scenario_policy": "standard_position_repeated_trials_color_by_seed_parity",
                "evaluation": {"seed_modulus": 2,
                               "seed_semantics": "trial id; parity sets candidate color; same standard initial position"},
                "trial_design": "标准局面重复试验，偶数种子候选执红、奇数执黑；不同种子不代表不同棋盘。",
                "opponent": "同一Decision模型；使用冻结参考提示xiangqi-reference-v1；无搜索引擎或随机代走。",
                "limitations": "体验规则未实现赛事长将长捉责任裁定；截断子力代理分不能当作胜率。"}

    def create(self, seed: int, config: dict) -> XiangqiSession:
        self.validate_config(config)
        return XiangqiSession(seed=seed, max_steps=config.get("max_steps", self.defaults["max_steps"]))
