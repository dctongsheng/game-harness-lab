"""Bounded, text-only Harness components shared by search and execution.

Components are data, never executable code. A full profile can reorganize facts
and change a bounded memory schedule, but cannot remove observations, rank an
action catalog, choose actions locally, change rules, or enable vision.
"""
from __future__ import annotations

from copy import deepcopy
import json


TEXT_COMPONENTS = ("decision_instructions", "strategy_notes")
TETRIS_COMPONENTS = TEXT_COMPONENTS + ("goal_instructions", "memory_policy", "observation_policy")
COMPONENT_PROFILES = {"text-v1": TEXT_COMPONENTS, "tetris-components-v1": TETRIS_COMPONENTS}
CONTEXT_SECTIONS = ("observation", "tetris_features", "decision_goal_memory",
                    "tetris_control_facts", "tetris_landing_projections")
MEMORY_BOUNDS = (2, 4, 8)


class ComponentValidationError(ValueError):
    """A candidate attempts to leave its declared component search space."""


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ComponentValidationError("Component JSON cannot contain duplicate keys")
        result[key] = value
    return result


def _policy(value, expected, label):
    try:
        result = json.loads(value, object_pairs_hook=_object,
                            parse_constant=lambda _: (_ for _ in ()).throw(
                                ComponentValidationError("Component JSON must be finite")))
    except (TypeError, json.JSONDecodeError) as exc:
        raise ComponentValidationError(f"{label} must be a strict JSON object") from exc
    if not isinstance(result, dict) or set(result) != set(expected):
        raise ComponentValidationError(f"{label} requires exactly {', '.join(expected)}")
    if not isinstance(result["instructions"], str) or not result["instructions"].strip():
        raise ComponentValidationError(f"{label}.instructions must be nonempty text")
    return result


def _memory(value):
    result = _policy(value, ("instructions", "recent_actions", "replan_every_actions"), "memory_policy")
    for name in ("recent_actions", "replan_every_actions"):
        if type(result[name]) is not int or result[name] not in MEMORY_BOUNDS:
            raise ComponentValidationError(f"memory_policy.{name} must be 2, 4, or 8")
    return result


def _observation(value):
    result = _policy(value, ("instructions", "section_order"), "observation_policy")
    order = result["section_order"]
    if (not isinstance(order, list) or len(order) != len(CONTEXT_SECTIONS)
            or any(not isinstance(item, str) for item in order)
            or set(order) != set(CONTEXT_SECTIONS)):
        raise ComponentValidationError("observation_policy.section_order must permute all five observation sections")
    return result


def component_names(candidate):
    """Return a stable search/update order for one exact profile."""
    if isinstance(candidate, dict):
        for names in COMPONENT_PROFILES.values():
            if set(candidate) == set(names):
                return names
    raise ComponentValidationError("Harness requires exactly the two text components or all five Tetris components")


def validate_candidate(candidate):
    names = component_names(candidate)
    if any(not isinstance(candidate[key], str) or not candidate[key].strip()
           or len(candidate[key]) > 12000 for key in names):
        raise ComponentValidationError("Harness component strings must be nonempty and at most 12000 characters")
    # Retain the legacy validator's strip behavior without rewriting JSON policy
    # bodies or the model's instruction text inside those bodies.
    result = {key: candidate[key].strip() for key in sorted(names)}
    if names == TETRIS_COMPONENTS:
        _memory(result["memory_policy"])
        _observation(result["observation_policy"])
    return result


def upgrade_tetris_candidate(legacy_candidate, goal_instructions):
    result = validate_candidate(legacy_candidate)
    if component_names(result) != TEXT_COMPONENTS:
        raise ComponentValidationError("Only a legacy two-field candidate can be upgraded")
    result.update({
        "goal_instructions": goal_instructions,
        "memory_policy": json.dumps({"instructions": "结合自己选择的目标、目标年龄与近期动作，检查实际执行结果；目标失效后重新决定。",
                                      "recent_actions": 4, "replan_every_actions": 4}, ensure_ascii=False),
        "observation_policy": json.dumps({"instructions": "结合原始观察、结构事实、自己的目标、控制反馈和完整条件落点后果做判断。",
                                           "section_order": list(CONTEXT_SECTIONS)}, ensure_ascii=False),
    })
    return validate_candidate(result)


def context_config(candidate):
    candidate = validate_candidate(candidate)
    if component_names(candidate) != TETRIS_COMPONENTS:
        raise ComponentValidationError("A component context requires the full five-field Tetris candidate")
    memory, observation = _memory(candidate["memory_policy"]), _observation(candidate["observation_policy"])
    return {"schema": "tetris-context-v4", "structured_facts": True,
            "decision_goal_memory": True, "control_feedback": True,
            "goal_instructions": candidate["goal_instructions"],
            "memory_instructions": memory["instructions"], "recent_actions": memory["recent_actions"],
            "replan_every_actions": memory["replan_every_actions"],
            "observation_instructions": observation["instructions"],
            "section_order": deepcopy(observation["section_order"])}


def resolve_context_config(harness, game_id=None):
    """An explicit legacy config cannot silently override evolvable components."""
    candidate = harness.get("candidate")
    names = component_names(candidate)
    explicit = harness.get("tetris_context_config")
    if names == TEXT_COMPONENTS:
        # Legacy direct runtime callers may omit strategy prose. Initialization,
        # search and exports still apply their existing stricter validation.
        if isinstance(explicit, dict) and explicit.get("schema") == "tetris-context-v4":
            raise ComponentValidationError("Tetris context v4 requires all five candidate components")
        return deepcopy(explicit)
    if game_id is not None and game_id != "tetris":
        raise ComponentValidationError("The full component profile is currently supported only for Tetris")
    derived = context_config(candidate)
    if explicit is not None and explicit != derived:
        raise ComponentValidationError("Explicit Tetris context configuration conflicts with candidate components")
    return derived


def component_contract(candidate=None):
    """Machine-readable reflection guidance; the runtime enforces these bounds."""
    names = TEXT_COMPONENTS if candidate is None else component_names(validate_candidate(candidate))
    result = {"schema": "harness-components-contract-v1", "component_names": list(names),
              "all_components_are_strings": True, "max_characters_per_component": 12000,
              "immutable": ["game rules and scoring", "complete native action catalog and order",
                            "Decision selects every goal and game action with think_mode=never",
                            "scope invalidation on new episode, piece or locked board",
                            "no local policy, automatic path execution, fallback or executable code",
                            "vision policy remains externally fixed", "independent promotion evaluation"]}
    if names == TETRIS_COMPONENTS:
        result.update({"supported_games": ["tetris"], "context_schema": "tetris-context-v4",
                       "goal_instructions": "Nonempty text used on Decision's separate goal-selection calls; all 42 choices remain available.",
                       "memory_policy": {"encoding": "strict JSON string, exact keys below",
                           "instructions": "Nonempty memory-use guidance for goal and action calls",
                           "recent_actions": list(MEMORY_BOUNDS), "replan_every_actions": list(MEMORY_BOUNDS)},
                       "observation_policy": {"encoding": "strict JSON string, exact keys below",
                           "instructions": "Nonempty observation-use guidance for goal and action calls",
                           "section_order": list(CONTEXT_SECTIONS),
                           "section_order_rule": "Any full permutation; no section may be removed, repeated, or added"},
                       "conditional_projections": "All forty column/rotation outcomes are geometric conditionals; native path reachability is not established."})
    return result


def runtime_contract(candidate, game_context, decision_model=None):
    """Describe actual component inputs without pretending legacy play has them."""
    from .initialization_contract import build_runtime_contract
    candidate = validate_candidate(candidate)
    result = build_runtime_contract(game_context, decision_model)
    result["components"] = component_contract(candidate)
    if component_names(candidate) == TEXT_COMPONENTS:
        return result
    if game_context.get("game_id") != "tetris":
        raise ComponentValidationError("The full component profile is currently supported only for Tetris")
    config = context_config(candidate)
    result["schema_version"] = "decision-harness-runtime/v2-components"
    result["player"]["memory"] = (
        "调用本身无会话记忆，但state.decision_goal_memory显式提供Decision自己选择的目标、目标年龄及近期实际动作。"
        f"当前保留最近{config['recent_actions']}个动作，同一有效期每{config['replan_every_actions']}次游戏动作另外调用Decision更新目标。"
        "新局、新方块（含hold）或锁定棋盘变化使目标与近期记忆失效；目标不是自动执行的动作。")
    result["player"]["search_helpers"] = [
        "所有40种列/朝向的确定性条件落点后果；不筛选、不评分排名、不保证原生按键路径可达，也不替Decision选择"]
    result["runtime_fields"].update({
        "tetris_features": "当前可见锁定棋盘与活动块的确定性结构事实，包括列高、空洞；不含动作评分或策略建议。",
        "tetris_control_facts": "原生按键时长与横移计时状态、最近控制效果、当前块与Decision目标的坐标关系。",
        "tetris_landing_projections": "完整40个列/朝向组合的条件几何直落后果，含消行、原生积分变化、空洞和堆高；假设先达到该姿态，实际按键路径未验证。",
        "decision_goal_memory": "scope、target、age_actions、recent_actions；target由Decision选择，recent_actions来自实际已选择动作且下一步观察确认。",
        "selection_purpose": "仅目标调用出现update_goal_memory_only；该调用从keep/clear/40个target中选一项，随后另一次Decision调用选择原生按键。",
    })
    result["game_guidance"]["design_focus"] = (
        "先由Decision根据完整条件落点后果选目标，再由Decision逐次选择原生按键和时长。"
        "可使用实际提供的结构事实、控制反馈和目标记忆；观察顺序、记忆窗口及重规划频率可在组件合同范围内调整。"
        "不能将目标当作已验证路径，不允许局部规则代选、自动硬降、自动寻路或删减动作。只有原生消行积分计分。")
    result["component_context"] = config
    result["design_requirements"] = [
        "全部五个组件都是文本；memory_policy和observation_policy必须是规定schema的严格JSON字符串。",
        "LLM只在局前/局后优化；目标和原生游戏动作均由Decision选择，think_mode=never。",
        "事实、规则、动作目录、目标目录与计分保持完整固定；没有动态代码、自动路径或失败降级。",
        "记忆仅为显式有限历史与Decision目标；不能声称有无限历史或模型权重训练。",
        "本组件profile不启用或关闭视觉，视觉由实验的独立固定设置决定。",
    ]
    return result
