#!/usr/bin/env python3
"""Repair the frozen V5 strategy using old evidence before any V6 game.

Only GLM writes the two strategy strings. This offline preparation never creates
a Decision provider or plays a game. A second call is allowed only if the first
response fails the declared schema/source checks, never after seeing new scores.
A separate explicitly selected semantic-repair stage permits exactly one call,
links its rejected preparation and static review, and still sees no new games.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from game_harness.games import get_game
from game_harness.providers import CoachProvider, ProviderError, Settings, digest, extract_json, validate_candidate
from game_harness.store import atomic_json, utcnow
from game_harness.tetris_context import CONTEXT_CONTRACT, GOAL_INSTRUCTIONS
from game_harness.tetris_control import CONTROL_CONTRACT
from audit_saved_run import _audit_episode


DEFAULT_PARENT = ROOT / "data/experiments/tetris-context-v5-bootstrap-final/parent.json"
DEFAULT_HISTORY = ROOT / "data/experiments/tetris-context-v5b-20261017"
SOURCES = {
    "S_oscillation": ("calibration-00-both", tuple(range(3, 9))),
    "L_oscillation_then_clear": ("evaluation-01-both", (3, 4, 5, 6, 50, 51, 52, 53, 54, 55)),
    "I_short_repeat_to_9": ("evaluation-07-both", (40, 41, 42)),
    "I_short_repeat_past_8": ("evaluation-11-both", (47, 48, 49)),
}
IMMUTABLE = ("brief", "experience_summary", "game_id", "config", "game_context",
             "rules_version", "decision_model", "think_mode")
ENGINE_FACTS = {
    "source": "Same vendored paused Tetris engine; deterministic control probes, not model performance results",
    "duration_ticks": {"100ms": 6, "300ms": 18, "1000ms": 60},
    "horizontal_repeat": "A new direction attempts one immediate move, sets horizontalHeldTicks=0; subsequent ticks increment it, with repeats at 9,12,15,...",
    "unobstructed_same_piece_example": [
        {"action": "left:100", "starting_direction": 0, "starting_timer": 0, "movement_attempts": 1, "ending_timer": 5},
        {"action": "left:100", "starting_direction": -1, "starting_timer": 5, "movement_attempts": 1, "ending_timer": 11},
        {"action": "left:100", "starting_direction": -1, "starting_timer": 11, "movement_attempts": 2, "ending_timer": 17}],
    "fresh_direction_long_inputs": {"300ms_movement_attempts": 4, "1000ms_movement_attempts": 18},
    "bounds": "Attempts are not guaranteed displacement: walls/locked cells/rotation/locking constrain the result. Read actual cells after each action.",
    "release": "End-of-action release clears held keys but does not itself advance a nonhorizontal physical tick or reset horizontalDirection/horizontalHeldTicks.",
    "reset": "A nonhorizontal physical tick resets the horizontal timer. Choosing noop:100 incurs six physics ticks and one action; gravity/lock still advance. A purposeful rotation also advances physics. Never assume release alone resets it.",
    "target": "Goal updates occur separately through Decision's full 42 choices at a new scope and every 4 game actions. The native action call cannot output keep/clear/target. No immediate replan after clear.",
    "geometry": "Target column means minimum occupied-cell x, not the piece's rotation-box active.x. Covered holes cannot be assumed vertically fillable. Four missing cells in one row and a horizontal I may clear ONE row; four lines require four completed rows.",
}


def load(path):
    return json.loads(Path(path).read_text())


def history_evidence(history, parent):
    history = Path(history)
    manifest = load(history / "manifest.json")
    harness = manifest["arms"]["both"]
    if harness["candidate"] != parent["candidate"] or harness["game_context"] != parent["game_context"]:
        raise ValueError("Historical episodes must use the frozen parent strategy and rules")
    evidence = {}
    for alias, (logical_id, indices) in SOURCES.items():
        path = history / "episodes" / logical_id / "attempt-01/episode.json"
        episode = load(path)
        audit = _audit_episode(episode, {harness["id"]: harness})
        if audit["thinking_verified"] != episode["steps"]:
            raise ValueError("Historical game thinking telemetry is not fully verified")
        excerpts = []
        for index in indices:
            row = episode["trace"][index]
            before, after = row["before"]["observation"], row["after"]["observation"]
            memory = row["receipt"]["tetris_context"]["memory_after_planning"]
            excerpts.append({"step": index, "action": row["action"], "target": memory["target"],
                "piece_id_before": before["pieceId"], "piece_id_after": after["pieceId"],
                "piece_type_before": before["active"]["type"],
                "before_active": before["active"], "after_active": after["active"],
                "before_horizontal_direction": before["horizontalDirection"],
                "before_horizontal_held_ticks": before["horizontalHeldTicks"],
                "after_horizontal_direction": after["horizontalDirection"],
                "after_horizontal_held_ticks": after["horizontalHeldTicks"],
                "locked_board_unchanged": before["board"] == after["board"],
                "occupied_bottom_rows_before": [r for r in before["board"]["rows"] if r["y"] >= 17],
                "points_delta": row["after"]["score"] - row["before"]["score"],
                "lines_delta": after["score"]["lines"] - before["score"]["lines"],
                "receipt_sha256": digest(row["receipt"])})
        evidence[alias] = {"logical_id": logical_id, "episode_id": episode["episode_id"],
            "score": episode["score"], "steps": episode["steps"],
            "source_file_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "source_json_sha256": digest(episode), "full_rules_and_receipt_replay": True,
            "excerpts": excerpts}
    return evidence


def validate_output(output, evidence):
    if not isinstance(output, dict) or set(output) != {"candidate", "changes", "limitations"}:
        raise ValueError("Expected exactly candidate, changes, limitations")
    candidate = validate_candidate(output["candidate"])
    if candidate != output["candidate"]:
        raise ValueError("Candidate strings must not require whitespace normalization")
    if not isinstance(output["changes"], list) or not 1 <= len(output["changes"]) <= 8:
        raise ValueError("Expected one to eight documented changes")
    known = set(evidence) | {"engine_control_facts", "interface_contract"}
    for change in output["changes"]:
        if (not isinstance(change, dict) or set(change) != {"change", "evidence", "limitation"}
                or any(not isinstance(change[k], str) or not change[k].strip() for k in ("change", "limitation"))
                or not isinstance(change["evidence"], list) or not change["evidence"]
                or not all(isinstance(ref, str) and ref in known for ref in change["evidence"])):
            raise ValueError("Change requires exact known evidence aliases and a limitation")
    if not isinstance(output["limitations"], str) or not output["limitations"].strip():
        raise ValueError("Explicit limitations required")
    return deepcopy(output)


def messages_for(parent, evidence):
    system = (
        "你是离线俄罗斯方块Harness修订者，依据真实旧对局和确定性引擎事实修正策略。"
        "只输出JSON，恰含candidate、changes、limitations。candidate恰含decision_instructions和strategy_notes两个非空字符串，无首尾空白。"
        "changes为1到8项，恰含change(文字)、evidence(给定短别名数组)、limitation(文字)。limitations是文字。"
        "写精炼、适合关闭思考的Decision模型的策略；不生成代码，不自行执行，不删减/评分动作目录、不声称已经提高分数。"
        "所有goal和游戏按键仍由Decision分别选；宿主仅提供事实与最近历史。你不会参与局内选择。"
        "修订必须适用于有/无可选tetris_control_facts两种条件；缺字段时用observation及decision_goal_memory，不能虚构存在的字段。"
        "修正旧策略的差2-3格用300ms、差至少4格用1000ms映射，以及缺4格I可消4行/直接填封闭洞等错误。"
        "100ms也不保证一格，要考虑跨动作计时器；300ms和1000ms是移动尝试次数而非精确位移。"
        "若使用noop重置计时器必须说明其重力/锁定/动作预算代价，不能把noop变成每步固定程序。"
        "反转或越过是事实不一定错误，只有与当前目标、朝向、障碍合并才能判断。禁止无限往返和用硬降假装目标已达成。"
        "keep/clear/target只能在目标调用选，不能在游戏按键调用输出。没有即时目标更换接口。"
        "优先清楚区分目标规划与单步执行；不要求思维链。保留用户约束think_mode=never。"
        "不要复制旧brief里的错误为新决策指令；brief仅留作共同元数据，实际玩家只读candidate与冻结接口合同。")
    prompt = {"parent_candidate": parent["candidate"], "immutable_game_context": parent["game_context"],
              "actual_common_contract": CONTEXT_CONTRACT, "actual_goal_call_addendum": GOAL_INSTRUCTIONS,
              "optional_control_contract": CONTROL_CONTRACT, "engine_control_facts": ENGINE_FACTS,
              "historical_evidence": evidence,
              "evidence_aliases": list(evidence) + ["engine_control_facts", "interface_contract"],
              "experimental_boundary": "Only historical V5 records. No V6 diagnostics, calibration or evaluation results exist in this input; no test-score-based selection. Changing text cannot guarantee game strength."}
    return [{"role": "system", "content": system},
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)}]


def build_repaired(parent, output, provenance):
    result = deepcopy(parent)
    result.update({"id": "control-repair-" + digest(output["candidate"])[:16],
                   "candidate": deepcopy(output["candidate"]), "created_at": utcnow(),
                   "llm_revision": {"changes": deepcopy(output["changes"]), "limitations": output["limitations"]},
                   "provenance": deepcopy(provenance)})
    if any(result[key] != parent[key] for key in IMMUTABLE):
        raise ValueError("Repair changed immutable parent fields")
    return result


def semantic_input(prior_repair, semantic_review, parent, evidence):
    prior, review = load(prior_repair), load(semantic_review)
    if (prior["provenance"]["parent_hash"] != digest(parent)
            or prior["provenance"]["historical_evidence_hash"] != digest(evidence)
            or prior["provenance"].get("new_game_results_seen") is not False
            or any(prior[key] != parent[key] for key in IMMUTABLE)):
        raise ValueError("Prior repair must use the same frozen historical-only parent")
    if (review.get("schema") != "tetris-control-semantic-review-v1"
            or review.get("candidate_hash") != digest(prior["candidate"])
            or review.get("status") != "rejected_before_any_v6_game"
            or review.get("new_game_results_seen") is not False
            or not isinstance(review.get("findings"), list) or not review["findings"]):
        raise ValueError("Static review must bind the rejected candidate before new game results")
    if prior["provenance"].get("semantic_repair_from"):
        raise ValueError("Only one semantic-repair stage is allowed")
    return {"prior_path": str(Path(prior_repair).resolve()), "prior_hash": digest(prior),
            "review_path": str(Path(semantic_review).resolve()), "review_hash": digest(review),
            "prior_coach_calls": deepcopy(prior["provenance"]["coach_calls"]),
            "rejected_candidate": prior["candidate"], "static_review": review,
            "max_additional_coach_calls": 1}


def repair(destination, parent_path=DEFAULT_PARENT, history=DEFAULT_HISTORY, *, settings=None, coach=None,
           prior_repair=None, semantic_review=None):
    destination = Path(destination)
    if destination.exists():
        raise ValueError("Use a fresh output directory; prior evidence must remain immutable")
    settings = settings or Settings.load()
    parent = load(parent_path)
    if parent["think_mode"] != "never" or parent["decision_model"] != settings.decision_model:
        raise ValueError("Parent and configured Decision identity/thinking mode differ")
    if parent["game_context"] != get_game("tetris").context(parent["config"]):
        raise ValueError("Frozen parent game rules differ from the installed adapter")
    evidence = history_evidence(history, parent)
    messages = messages_for(parent, evidence)
    if (prior_repair is None) != (semantic_review is None):
        raise ValueError("Prior repair and semantic review must be supplied together")
    semantic = semantic_input(prior_repair, semantic_review, parent, evidence) if prior_repair else None
    max_calls = 1 if semantic else 2
    if semantic:
        messages.append({"role": "user", "content": json.dumps({
            "semantic_repair": semantic,
            "instruction": (
                "仅有一次额外离线修复机会，必须纠正static_review的全部明确错误和冲突；没有新游戏结果。"
                "不要把越界/反转当成无条件立即锁块的理由，目标仍由Decision固定时点规划，按键仍由Decision每步选。"
                "精确事实：成熟同向加速期18tick有6次横移尝试，不是2；6tick也可能有2次尝试；"
                "但墙和锁块会限制实际位移。不要说1000ms必越过。"
                "说明什么时候可以由Decision自己用noop重置计时器再短按、相应重力代价；不自动执行或固定程序。"
                "返回同样三字段JSON：candidate、changes、limitations。changes每项必须同时含change、evidence、limitation。"
                "只保留有根据、互不矛盾、短而可执行的策略；不能宣称修复已提高游戏得分。")}, ensure_ascii=False)})
    manifest = {"schema": "tetris-control-repair-v1", "created_at": utcnow(),
                "parent_path": str(Path(parent_path).resolve()), "parent_hash": digest(parent),
                "history_path": str(Path(history).resolve()), "historical_evidence_hash": digest(evidence),
                "control_contract_hash": digest(CONTROL_CONTRACT), "input_hash": digest(messages),
                "max_coach_calls": max_calls,
                "repair_only_for": "one static semantic correction" if semantic else "invalid output schema or evidence references",
                "semantic_repair_from": semantic,
                "coach_model": settings.coach_model, "decision_model": settings.decision_model,
                "new_game_results_seen": False, "new_decision_calls": 0, "live_registry_changes": False}
    destination.mkdir(parents=True)
    atomic_json(destination / "manifest.json", manifest)
    atomic_json(destination / "historical-evidence.json", evidence)
    owns_coach = coach is None
    coach = coach or CoachProvider(settings)
    records, output = [], None
    try:
        for index in range(max_calls):
            atomic_json(destination / f"coach-{index + 1}.input.json", messages)
            purpose = ("tetris_control_semantic_repair" if semantic else
                       "tetris_control_policy_repair" if index == 0 else "tetris_control_schema_repair")
            response = coach.complete(messages, purpose=purpose)
            call = deepcopy(coach.calls[-1])
            atomic_json(destination / f"coach-{index + 1}.output.json", {"text": response, "call": call})
            record = {"input_hash": digest(messages), "call": call}
            records.append(record)
            try:
                output = validate_output(extract_json(response), evidence)
            except (ValueError, ProviderError) as exc:
                record["rejected"] = str(exc)
                atomic_json(destination / f"coach-{index + 1}.rejected.json", {"reason": str(exc)})
                if index + 1 == max_calls:
                    raise
                messages = messages + [{"role": "assistant", "content": response},
                    {"role": "user", "content": "仅修复输出结构或证据引用，不看新游戏结果。验证失败：" + str(exc)}]
            else:
                break
        provenance = {"type": "offline_llm_control_policy_repair", "parent_id": parent["id"],
                      "parent_hash": digest(parent), "manifest_hash": digest(manifest),
                      "historical_evidence_hash": digest(evidence), "control_contract_hash": digest(CONTROL_CONTRACT),
                      "coach_calls": (semantic["prior_coach_calls"] if semantic else []) + records,
                      "stage_coach_calls": len(records), "semantic_repair_from": semantic,
                      "new_game_results_seen": False, "new_decision_calls": 0,
                      "strategy_source": "Accepted GLM response JSON values, copied without manual text edits",
                      "validation_limit": "Schema, known source aliases and unchanged rules; semantic strategy effectiveness remains unproven"}
        result = build_repaired(parent, output, provenance)
        atomic_json(destination / "repaired.json", result)
        return result
    except Exception as exc:
        atomic_json(destination / "failed.json", {"type": type(exc).__name__, "completed_calls": len(records)})
        raise
    finally:
        if owns_coach:
            coach.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--parent", default=DEFAULT_PARENT, type=Path)
    parser.add_argument("--history", default=DEFAULT_HISTORY, type=Path)
    parser.add_argument("--prior-repair", type=Path,
                        help="Rejected prior repaired.json; requires its static semantic review")
    parser.add_argument("--semantic-review", type=Path,
                        help="A candidate-hash-bound review made before any new game results")
    args = parser.parse_args()
    result = repair(args.output, args.parent, args.history,
                    prior_repair=args.prior_repair, semantic_review=args.semantic_review)
    print(json.dumps({"id": result["id"], "repaired_path": str((args.output / "repaired.json").resolve()),
                      "coach_calls": len(result["provenance"]["coach_calls"]), "new_decision_calls": 0}, ensure_ascii=False))
