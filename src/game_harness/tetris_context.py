"""Optional, auditable Tetris observations and Decision-owned goal memory.

This module never steps a game or selects a native input. Goals are unranked
declarations selected by the same Decision provider, not executable macros.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from types import SimpleNamespace

from .runtime import ReceiptError, _digest, _policy_instructions, _validate_receipt
from .tetris_features import build_tetris_features
from .tetris_control import CONTROL_CONTRACT, TetrisControlFeedback
from .components import CONTEXT_SECTIONS, MEMORY_BOUNDS, resolve_context_config


CONTEXT_CONTRACT = """可选玩家接口合同（四组共用）：
若输入有 tetris_features，它仅是当前可见棋盘的确定性结构事实；不是动作、落点评分或建议。没有该字段时读取原始 observation。
若输入有 decision_goal_memory，它包含当前方块有效的 target、age_actions 和最近最多4个已选择并在后续步骤确认执行的原生动作及其前坐标。
target.leftmost_column 表示目标朝向下实际占用格最左列（0..9），不是 active.x 局部框原点；target.rotation 是原生0..3朝向。
目标由Decision自己选择；候选完整列举40种列与朝向组合，加keep/clear，不按棋盘过滤、搜索或排序，也不保证目标可达或安全。
同一方块、类型与已锁定棋盘未变时目标会保留；新块（包括hold生成）、棋盘改变或新局使旧目标失效。移动、旋转、下落本身不会清目标。
每个新有效期和同一有效期每4次游戏动作，Decision另外更新一次目标；clear后等到下一固定更新点，不立即重问。
每个实际游戏按键依然必须由Decision从完整原生目录另选。达到目标不会自动硬降，无法达到也不会由程序改目标或代选。
left/right:100 不保证一格；原生横移计时器可能跨短动作保留，必须观察实际占用格与计时器。所有调用think_mode=never。
"""

GOAL_INSTRUCTIONS = """本次只更新目标记忆，不执行游戏按键。
从完整目录选一项：keep保留已有目标（无目标时保持无目标）；clear主动清空；target选择当前块的目标最左占用列与朝向。
结合当前可见棋盘、冻结策略、现有目标与近期动作，自行决定是否保留或更换目标。目录不会告诉你哪个目标最好或可达。
目标只是你下一次选择游戏动作时的参考，不是移动指令；不要把active.x误当成最左占用列。
"""


class TetrisContextError(ReceiptError):
    pass


def validate_config(config):
    expected = {"schema", "structured_facts", "decision_goal_memory", "replan_every_actions"}
    schema = config.get("schema") if isinstance(config, dict) else None
    if schema in ("tetris-context-v2", "tetris-context-v3", "tetris-context-v4"):
        expected = expected | {"control_feedback"}
    if schema in ("tetris-context-v3", "tetris-context-v4"):
        expected = expected | {"goal_instructions"}
    if schema == "tetris-context-v4":
        expected |= {"recent_actions", "memory_instructions", "observation_instructions", "section_order"}
    if not isinstance(config, dict) or set(config) != expected:
        raise TetrisContextError("Tetris context requires its exact frozen configuration")
    if schema not in ("tetris-context-v1", "tetris-context-v2", "tetris-context-v3", "tetris-context-v4"):
        raise TetrisContextError("Unknown Tetris context schema")
    if any(type(config[key]) is not bool for key in ("structured_facts", "decision_goal_memory")):
        raise TetrisContextError("Tetris context feature flags must be booleans")
    periods = MEMORY_BOUNDS if schema == "tetris-context-v4" else (4,)
    if type(config["replan_every_actions"]) is not int or config["replan_every_actions"] not in periods:
        raise TetrisContextError("Tetris v4 replan interval must be 2, 4, or 8" if schema == "tetris-context-v4"
                                 else "Tetris context replans every four game actions")
    if schema in ("tetris-context-v2", "tetris-context-v3", "tetris-context-v4"):
        if config["structured_facts"] is not True or config["decision_goal_memory"] is not True:
            raise TetrisContextError("Tetris context v2 requires structural facts and Decision goal memory")
        if type(config["control_feedback"]) is not bool:
            raise TetrisContextError("Tetris control feedback flag must be boolean")
    if schema in ("tetris-context-v3", "tetris-context-v4"):
        if config["control_feedback"] is not True:
            raise TetrisContextError("Tetris v3 requires control feedback")
        prompt = config["goal_instructions"]
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 12000:
            raise TetrisContextError("Tetris v3 requires a frozen nonempty goal prompt of at most 12000 characters")
    if schema == "tetris-context-v4":
        if type(config["recent_actions"]) is not int or config["recent_actions"] not in MEMORY_BOUNDS:
            raise TetrisContextError("Tetris v4 recent_actions must be 2, 4, or 8")
        for name in ("memory_instructions", "observation_instructions"):
            if not isinstance(config[name], str) or not config[name].strip() or len(config[name]) > 12000:
                raise TetrisContextError("Tetris v4 component guidance must be nonempty text of at most 12000 characters")
        order = config["section_order"]
        if (not isinstance(order, list) or len(order) != len(CONTEXT_SECTIONS)
                or any(not isinstance(key, str) for key in order) or set(order) != set(CONTEXT_SECTIONS)):
            raise TetrisContextError("Tetris v4 section_order must permute all five observation sections")
    return deepcopy(config)


def _context_contract(config):
    if config["schema"] not in ("tetris-context-v3", "tetris-context-v4"):
        return CONTEXT_CONTRACT
    result = CONTEXT_CONTRACT.replace("可选玩家接口合同（四组共用）：", "玩家接口合同（v3条件落点规划）：").replace(
        "不按棋盘过滤、搜索或排序，也不保证目标可达或安全。",
        "不按棋盘过滤或优劣排序；额外条件落点投影与后果描述不改变候选ID和顺序，也不保证目标可达或安全。")
    if config["schema"] == "tetris-context-v4":
        result = result.replace("v3条件落点规划", "v4可进化组件").replace(
            "最近最多4个", f"最近最多{config['recent_actions']}个").replace(
            "每4次游戏动作", f"每{config['replan_every_actions']}次游戏动作")
    return result


def _component_guidance(config):
    if config["schema"] != "tetris-context-v4":
        return ""
    return ("\n本局冻结的记忆使用指导：\n" + config["memory_instructions"]
            + "\n本局冻结的观察阅读指导：\n" + config["observation_instructions"])


def context_instructions(instructions, config):
    """Exact shared instructions, retaining v1 bytes when feedback is off."""
    frozen = validate_config(config)
    result = instructions + _component_guidance(frozen) + "\n" + _context_contract(frozen)
    if frozen.get("control_feedback", False):
        result += "\n" + CONTROL_CONTRACT
    if frozen["schema"] in ("tetris-context-v3", "tetris-context-v4"):
        from .tetris_landing import LANDING_CONTRACT
        result += "\n" + LANDING_CONTRACT
    return result


def goal_instructions(instructions, config):
    """Separate v3 planning from action policy; retain exact v1/v2 bytes."""
    frozen = validate_config(config)
    if frozen["schema"] not in ("tetris-context-v3", "tetris-context-v4"):
        return context_instructions(instructions, frozen) + "\n" + GOAL_INSTRUCTIONS
    from .tetris_landing import LANDING_CONTRACT
    return (frozen["goal_instructions"] + _component_guidance(frozen) + "\n" + _context_contract(frozen) + "\n"
            + CONTROL_CONTRACT + "\n" + LANDING_CONTRACT + "\n" + GOAL_INSTRUCTIONS)


def goal_catalog():
    """Same forty coordinate pairs plus keep/clear on every planning call."""
    return [{"id": "keep", "label": "保持当前目标；当前无目标则继续无目标"},
            {"id": "clear", "label": "清空目标，本次不设新目标"}] + [
        {"id": f"target:{column}:{rotation}",
         "label": f"目标最左占用列={column}，目标朝向={rotation}；不保证可达或安全"}
        for column in range(10) for rotation in range(4)
    ]


def planning_catalog(state, config):
    """Describe each conditional outcome equally; never prune or rank goals."""
    catalog = goal_catalog()
    if validate_config(config)["schema"] not in ("tetris-context-v3", "tetris-context-v4"):
        return catalog
    projections = state["tetris_landing_projections"]["candidates"]
    _require([p["id"] for p in projections] == [c["id"] for c in catalog[2:]],
             "Conditional projections must preserve every goal in coordinate order")
    for choice, result in zip(catalog[2:], projections):
        if not result["pose_valid"]:
            consequence = "假设起始姿态无效：" + result["invalid_reason"] + "；后果未知"
        else:
            consequence = (f"假设先达到目标姿态再直落：消{result['lines_cleared']}行，"
                f"原生积分+{result['points_delta']}，空洞{result['holes_before']}→{result['holes_after']}"
                f"（净变化{result['holes_delta']:+d}），最大列高{result['max_height']}，"
                f"总列高{result['total_height']}，相邻列高度差绝对值之和{result['bumpiness']}，"
                f"锁定后隐藏行占用={'是' if result['top_out'] else '否'}")
        choice["label"] += "；" + consequence + "；原生按键路径未验证"
    return catalog


def _require(condition, message):
    if not condition:
        raise TetrisContextError(message)


def _scope(state):
    observation = state.get("observation", {})
    active = observation.get("active")
    _require(state.get("game_id") == "tetris", "Tetris context cannot wrap another game")
    _require(isinstance(state.get("episode_id"), str) and state["episode_id"], "Missing episode identity")
    _require(type(state.get("step")) is int and state["step"] >= 0, "Invalid observation revision")
    _require(type(observation.get("pieceId")) is int and observation["pieceId"] > 0, "Invalid piece identity")
    _require(isinstance(active, dict) and active.get("type") in {"I", "O", "T", "S", "Z", "J", "L"}, "Missing active piece")
    rows = observation.get("board", {}).get("rows")
    _require(isinstance(rows, list) and len(rows) == 22, "Missing locked board rows")
    return {"episode_id": state["episode_id"], "piece_id": observation["pieceId"],
            "piece_type": active["type"], "locked_board_hash": _digest(rows)}


def _check_receipt(receipt, state, criteria, request_id, expected_model):
    _validate_receipt(receipt, request_id, state["step"], _digest(criteria),
                      {item["id"] for item in criteria})
    raw = receipt["raw_response"]
    _require(isinstance(raw, dict) and receipt.get("response_hash") == _digest(raw), "Raw response hash mismatch")
    answer = raw.get("answers", {}).get("primary_action", {})
    _require(answer.get("choice") == receipt["choice_id"], "Raw response choice mismatch")
    _require(raw.get("model") == receipt["model"], "Raw response model mismatch")
    if expected_model is not None:
        _require(receipt["model"] == expected_model, "Decision model changed between goal and action")
    probabilities = answer.get("probabilities")
    _require(isinstance(probabilities, dict) and set(probabilities) == {c["id"] for c in criteria},
             "Response probabilities do not cover the complete catalog")
    values = [answer.get("confidence"), *probabilities.values()]
    _require(all(type(value) in (float, int) and math.isfinite(value) and 0 <= value <= 1 for value in values),
             "Invalid response probabilities")
    _require(abs(sum(probabilities.values()) - 1) <= .02, "Response probabilities do not sum to one")
    thought = answer.get("details", {}).get("thought", {})
    _require(thought.get("used") is False and type(thought.get("tokens")) is int and thought["tokens"] == 0,
             "Tetris experiment requires explicit thinking-off telemetry")
    _require(receipt.get("think_used") is False and type(receipt.get("reasoning_tokens")) is int
             and receipt["reasoning_tokens"] == 0, "Receipt thinking telemetry mismatch")


def _organize_state(state, config):
    """Keep every fact; only move full sections in the declared reading order.

    Stable metadata/nested ordering permits replay after JSON stores sort keys.
    The explicit section order remains model-visible and has a separate hash.
    """
    if config["schema"] != "tetris-context-v4":
        return state
    _require(all(section in state for section in CONTEXT_SECTIONS), "Missing required observation section")
    stable = json.loads(json.dumps(state, ensure_ascii=False, sort_keys=True, allow_nan=False))
    result = {key: value for key, value in stable.items() if key not in CONTEXT_SECTIONS}
    result.update({key: stable[key] for key in config["section_order"]})
    return result


def _serialized_hash(state):
    # Same JSON state-string serialization as DecisionProvider; no sort_keys.
    return hashlib.sha256(json.dumps(state, ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()


class TetrisDecisionHarness:
    """A per-episode context wrapper; calls a real provider for every selection.

    ``pending_context`` retains planning evidence even when the following action
    fails. The runtime should attach it to failed-episode diagnostics.
    """
    def __init__(self, provider, config):
        self.provider = provider
        self.config = validate_config(config)
        self.settings = getattr(provider, "settings", None)
        self.pending_context = None
        self._episode = self._scope = self._target = self._created_step = None
        self._scope_actions = 0
        self._history = []
        self._last_step = None
        self._model = getattr(self.settings, "decision_model", None)
        self._control = TetrisControlFeedback() if self.config.get("control_feedback", False) else None

    def _memory(self, step):
        return {"scope": deepcopy(self._scope), "target": deepcopy(self._target),
                "age_actions": None if self._created_step is None else step - self._created_step,
                "recent_actions": deepcopy(self._history)}

    def _call(self, purpose, state, instructions, criteria, request_id):
        event = {"purpose": purpose, "input_state": deepcopy(state), "input_hash": _digest(state),
                 "instructions_hash": _digest(instructions), "catalog": deepcopy(criteria),
                 "candidate_hash": _digest(criteria), "request_id": request_id, "status": "pending"}
        if self.config["schema"] == "tetris-context-v4":
            event.update({"input_serialized_hash": _serialized_hash(state), "input_key_order": list(state)})
        self.pending_context["calls"].append(event)
        state_copy, catalog_copy = deepcopy(state), deepcopy(criteria)
        try:
            receipt = self.provider.decide(state, instructions, criteria, request_id)
            event["receipt"] = deepcopy(receipt)
            _require(state == state_copy and criteria == catalog_copy, "Provider mutated its request")
            if self.config["schema"] == "tetris-context-v4":
                _require(_serialized_hash(state) == event["input_serialized_hash"], "Provider mutated request ordering")
            _check_receipt(receipt, state, criteria, request_id, self._model)
            self._model = receipt["model"]
            event["status"] = "validated"
            return deepcopy(receipt)
        except Exception as exc:
            event["status"] = "failed"
            event["error"] = {"type": type(exc).__name__, "message": str(exc)}
            raise

    def decide(self, state, instructions, criteria, request_id):
        self.pending_context = None
        state = deepcopy(state)
        criteria = deepcopy(criteria)
        context_fields = ("tetris_features", "decision_goal_memory")
        if self.config["schema"] in ("tetris-context-v2", "tetris-context-v3", "tetris-context-v4"):
            context_fields += ("tetris_control_facts",)
        if self.config["schema"] in ("tetris-context-v3", "tetris-context-v4"):
            context_fields += ("tetris_landing_projections",)
        _require(not any(key in state for key in context_fields),
                 "Raw runtime state already contains context fields")
        scope = _scope(state)
        new_episode = state["episode_id"] != self._episode
        if not new_episode:
            _require(state["step"] == self._last_step + 1, "Context requests must follow executed game steps")
        before = self._memory(state["step"])
        if new_episode:
            self._episode, self._last_step = state["episode_id"], None
            self._model = getattr(self.settings, "decision_model", None)
        changed = new_episode or scope != self._scope
        if changed:
            self._scope, self._target, self._created_step = scope, None, None
            self._scope_actions, self._history = 0, []
        self.pending_context = {"schema": "tetris-context-ledger-v1", "config": deepcopy(self.config),
                                "raw_input_hash": _digest(state), "scope_changed": changed,
                                "memory_before": before, "calls": []}
        if self._control is not None:
            self._control.observe(state, scope)
        if self.config["structured_facts"]:
            state["tetris_features"] = build_tetris_features(state)
        if self.config["decision_goal_memory"]:
            state["decision_goal_memory"] = self._memory(state["step"])
        if self._control is not None:
            state["tetris_control_facts"] = self._control.facts(self._target)
        if self.config["schema"] in ("tetris-context-v3", "tetris-context-v4"):
            from .tetris_landing import build_landing_projections
            state["tetris_landing_projections"] = build_landing_projections(state)
        action_instructions = context_instructions(instructions, self.config)
        due = self.config["decision_goal_memory"] and self._scope_actions % self.config["replan_every_actions"] == 0
        if due:
            planning_state = deepcopy(state)
            planning_state["selection_purpose"] = "update_goal_memory_only"
            choices = planning_catalog(state, self.config)
            planning_state["candidates"] = choices
            planning_state = _organize_state(planning_state, self.config)
            planning_id = f"{request_id}:goal"
            receipt = self._call("goal", planning_state, goal_instructions(instructions, self.config),
                                 choices, planning_id)
            choice = receipt["choice_id"]
            if choice == "clear":
                self._target, self._created_step = None, None
            elif choice != "keep":
                _, column, rotation = choice.split(":")
                self._target = {"leftmost_column": int(column), "rotation": int(rotation)}
                self._created_step = state["step"]
            state["decision_goal_memory"] = self._memory(state["step"])
            if self._control is not None:
                state["tetris_control_facts"] = self._control.facts(self._target)
        state = _organize_state(state, self.config)
        self.pending_context["memory_after_planning"] = self._memory(state["step"])
        self.pending_context.update({"action_input_state": deepcopy(state), "action_input_hash": _digest(state),
                                     "instructions_hash": _digest(action_instructions), "request_id": request_id,
                                     "action_catalog": deepcopy(criteria)})
        if self.config["schema"] == "tetris-context-v4":
            self.pending_context.update({"action_input_serialized_hash": _serialized_hash(state),
                                         "action_input_key_order": list(state)})
        receipt = self._call("game_action", state, action_instructions, criteria, request_id)
        if self._control is not None:
            self._control.selected(receipt["choice_id"], self._target)
        if self.config["decision_goal_memory"]:
            active = state["observation"]["active"]
            self._history.append({"step": state["step"], "action": receipt["choice_id"],
                                  "before_active": {key: deepcopy(active[key]) for key in
                                                    ("type", "x", "y", "rotation", "cells")}})
            self._history = self._history[-self.config.get("recent_actions", 4):]
        self._scope_actions += 1
        self._last_step = state["step"]
        self.pending_context["memory_after_action"] = self._memory(state["step"] + 1)
        receipt["tetris_context"] = deepcopy(self.pending_context)
        return receipt


def audit_context_episode(episode, harness):
    """Reconstruct all goals and actual inputs using saved receipts, no network."""
    config = validate_config(episode.get("tetris_context_config"))
    _require(config == resolve_context_config(harness, "tetris"), "Episode/harness context configuration mismatch")
    expected_calls = []
    for step in episode["trace"]:
        ledger = step["receipt"].get("tetris_context")
        _require(isinstance(ledger, dict), "Missing Tetris context ledger")
        expected_calls.extend(deepcopy(ledger.get("calls", [])))

    class ReplayProvider:
        def __init__(self):
            self.index = 0
            self.settings = SimpleNamespace(decision_model=harness["decision_model"])

        def decide(self, state, instructions, criteria, request_id):
            _require(self.index < len(expected_calls), "Missing recorded Decision call")
            expected = expected_calls[self.index]
            self.index += 1
            _require(expected["input_state"] == state and expected["input_hash"] == _digest(state),
                     "Recorded Decision input differs from reconstruction")
            if config["schema"] == "tetris-context-v4":
                _require(expected.get("input_serialized_hash") == _serialized_hash(state)
                         and expected.get("input_key_order") == list(state),
                         "Recorded Decision input serialization/order differs from reconstruction")
            _require(expected["instructions_hash"] == _digest(instructions), "Recorded Decision instructions changed")
            _require(expected["catalog"] == criteria and expected["candidate_hash"] == _digest(criteria),
                     "Recorded Decision catalog changed")
            _require(expected["request_id"] == request_id and expected["status"] == "validated",
                     "Recorded Decision call identity/status changed")
            return deepcopy(expected["receipt"])

    replay = ReplayProvider()
    wrapper = TetrisDecisionHarness(replay, config)
    rules = episode["game_context"]["rules"]
    instructions = _policy_instructions(harness["candidate"], rules)
    goals = actions = 0
    for step in episode["trace"]:
        before = step["before"]
        state = deepcopy(before)
        state.update({"episode_id": episode["episode_id"], "observation_hash": _digest(before),
                      "candidates": deepcopy(step["criteria"]),
                      "remaining_steps": episode["config"]["max_steps"] - before["step"]})
        request_id = f"{episode['episode_id']}:step:{before['step']}:{_digest(before)[:16]}"
        reconstructed = wrapper.decide(state, instructions, deepcopy(step["criteria"]), request_id)
        _require(reconstructed == step["receipt"], "Tetris context receipt/ledger differs from reconstruction")
        goals += sum(call["purpose"] == "goal" for call in reconstructed["tetris_context"]["calls"])
        actions += 1
        _require(reconstructed["model"] == episode["model"] == harness["decision_model"], "Context model identity changed")
    _require(replay.index == len(expected_calls), "Unexpected extra Decision calls")
    return {"game_actions": actions, "goal_calls": goals, "thinking_off_goal_calls": goals,
            "actual_inputs_verified": True, "memory_replay_verified": True}
