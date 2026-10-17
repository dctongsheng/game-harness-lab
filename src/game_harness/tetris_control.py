"""Past and present control facts, without simulating or selecting an action.

Selections are pending until a subsequent public observation confirms the
transition. All history is scoped to one episode, active piece and locked board.
"""
from __future__ import annotations

from copy import deepcopy


CONTROL_CONTRACT = """可选控制事实合同：
tetris_control_facts只描述当前公开状态、Decision已选目标与已经发生的状态变化，不包含动作建议、候选评分或未来落点。
current.leftmost_column是实际占用格最左列；horizontal_direction/horizontal_held_ticks是当前原生横移方向与重复计时，不是按键建议。
target_relation.column_error=目标最左列-当前最左列；rotation_equal和at_target仅表示相等关系，无目标时这些关系为null。达到目标不触发自动动作。
last_transition仅在下一公开观察到达后记录同一方块、种类、锁定棋盘和局内上一动作的真实前后坐标、朝向与物理tick差；换作用域后为null。
target_unchanged比较上一动作时的目标与本次目标；crossing_comparison_applicable仅在目标未变、朝向未变且上一动作为left/right时为true。
strictly_crossed_target_column仅在上述比较适用且前后列误差严格异号时为true，到达目标列不算越过。旋转或更换目标不能被解释为横移越界。
recent_window只含当前作用域最近最多4个已由后续观察确认的动作；horizontal_commands是其中left/right子序列，horizontal_command_reversals数相邻横移指令方向的变化，不是错误或优劣标签。
"""


def _require(condition, message):
    if not condition:
        raise ValueError(f"Invalid Tetris control evidence: {message}")


def _integer(value, low, high):
    return type(value) is int and low <= value <= high


def _public_position(state):
    _require(isinstance(state, dict) and state.get("game_id") == "tetris", "expected Tetris state")
    _require(_integer(state.get("step"), 0, 2000), "invalid step")
    observation = state.get("observation")
    _require(isinstance(observation, dict) and observation.get("gameId") == "tetris", "missing observation")
    active = observation.get("active")
    _require(isinstance(active, dict) and active.get("type") in set("IOTSZJL"), "missing active piece")
    _require(_integer(active.get("rotation"), 0, 3), "invalid rotation")
    cells = active.get("cells")
    _require(isinstance(cells, list) and len(cells) == 4, "missing active cells")
    _require(all(isinstance(cell, dict) and _integer(cell.get("x"), 0, 9)
                 and _integer(cell.get("y"), -2, 19) for cell in cells), "invalid active coordinates")
    _require(len({(cell["x"], cell["y"]) for cell in cells}) == 4, "duplicate active cells")
    _require(_integer(observation.get("tick"), 0, 7200), "invalid physical tick")
    _require(_integer(observation.get("horizontalDirection"), -1, 1), "invalid horizontal direction")
    _require(_integer(observation.get("horizontalHeldTicks"), 0, 7200), "invalid horizontal timer")
    return {"step": state["step"], "tick": observation["tick"],
            "leftmost_column": min(cell["x"] for cell in cells), "rotation": active["rotation"],
            "horizontal_direction": observation["horizontalDirection"],
            "horizontal_held_ticks": observation["horizontalHeldTicks"]}


def _target(value):
    if value is None:
        return None
    _require(isinstance(value, dict) and set(value) == {"leftmost_column", "rotation"}, "invalid target fields")
    _require(_integer(value["leftmost_column"], 0, 9) and _integer(value["rotation"], 0, 3), "invalid target")
    return deepcopy(value)


def _horizontal(action):
    return action.split(":", 1)[0] if action.split(":", 1)[0] in ("left", "right") else None


class TetrisControlFeedback:
    """Observe a transition, expose facts, then retain the model's selection.

    This class does not own a game, call a model, enumerate native candidates,
    or alter a selected input. ``scope`` is the existing public-state identity
    used by the goal-memory wrapper, never a seed or private game state.
    """
    def __init__(self):
        self._scope = self._current = self._pending = None
        self._history = []

    def observe(self, state, scope):
        current = _public_position(state)
        if scope != self._scope:
            self._scope = deepcopy(scope)
            self._history, self._pending = [], None
        elif self._current is not None:
            _require(self._pending is not None, "next observation without a selected action")
            pending = self._pending
            before = pending["before"]
            _require(current["step"] == before["step"] + 1, "non-consecutive observations")
            # The paused native adapter must have executed this duration. Scope
            # changes are discarded above, not mistaken for motion of this piece.
            _require(current["tick"] - before["tick"] == pending["duration_ms"] * 60 // 1000,
                     "observation has not confirmed the native action duration")
            self._history.append({
                "step": before["step"], "action": pending["action"],
                "physical_ticks": current["tick"] - before["tick"],
                "before_leftmost_column": before["leftmost_column"],
                "after_leftmost_column": current["leftmost_column"],
                "actual_column_delta": current["leftmost_column"] - before["leftmost_column"],
                "before_rotation": before["rotation"], "after_rotation": current["rotation"],
                "rotation_unchanged": before["rotation"] == current["rotation"],
                "target_at_action": deepcopy(pending["target"]),
            })
            self._history = self._history[-4:]
            self._pending = None
        self._current = current

    def facts(self, target):
        _require(self._current is not None, "observe before requesting facts")
        target = _target(target)
        current = self._current
        error = None if target is None else target["leftmost_column"] - current["leftmost_column"]
        equal = None if target is None else target["rotation"] == current["rotation"]
        last = deepcopy(self._history[-1]) if self._history else None
        if last is not None:
            last["target_unchanged"] = target == last["target_at_action"]
            applicable = (target is not None and last["target_unchanged"]
                          and last["rotation_unchanged"] and _horizontal(last["action"]) is not None)
            last["crossing_comparison_applicable"] = applicable
            last["strictly_crossed_target_column"] = bool(applicable and
                (target["leftmost_column"] - last["before_leftmost_column"]) *
                (target["leftmost_column"] - last["after_leftmost_column"]) < 0)
        horizontal = [{"step": item["step"], "action": item["action"]}
                      for item in self._history if _horizontal(item["action"]) is not None]
        return {"schema": "tetris-control-facts/v1",
                "current": {key: current[key] for key in (
                    "leftmost_column", "rotation", "horizontal_direction", "horizontal_held_ticks")},
                "target_relation": {"target": target, "column_error": error, "rotation_equal": equal,
                                    "at_target": None if target is None else error == 0 and equal},
                "last_transition": last,
                "recent_window": {
                    "max_actions": 4, "confirmed_actions": len(self._history),
                    "horizontal_commands": horizontal,
                    "horizontal_command_reversals": sum(
                        _horizontal(first["action"]) != _horizontal(second["action"])
                        for first, second in zip(horizontal, horizontal[1:])),
                }}

    def selected(self, action, target):
        _require(self._current is not None and self._pending is None, "selection without a fresh observation")
        _require(isinstance(action, str) and ":" in action, "invalid native action")
        button, milliseconds = action.split(":", 1)
        _require(button in {"noop", "left", "right", "rotate-cw", "rotate-ccw", "soft-drop", "hard-drop", "hold"}
                 and milliseconds in {"100", "300", "1000"}, "invalid native input")
        self._pending = {"before": deepcopy(self._current), "action": action,
                         "duration_ms": int(milliseconds), "target": _target(target)}
