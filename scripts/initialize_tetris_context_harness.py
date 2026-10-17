#!/usr/bin/env python3
"""One common GLM strategy and evidence-linked experience summary for the 2x2 study.

Only historical completed games enter this call. No experimental seeds or future
test outcomes are available. The same returned candidate is frozen in all arms.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from game_harness.evolution import _summary
from game_harness.feedback import fit_pack
from game_harness.games import get_game
from game_harness.initialization_contract import build_initialization_contract
from game_harness.providers import CoachProvider, DecisionProvider, Settings, digest, extract_json, validate_candidate
from game_harness.store import atomic_json, utcnow
from game_harness.tetris_context import CONTEXT_CONTRACT
from game_harness.tetris_features import build_tetris_features


def history_evidence(path, context):
    episodes = []
    for file in sorted(Path(path).glob("episodes/*/attempt-*/episode.json")):
        ep = json.loads(file.read_text())
        if ep.get("game_id") != "tetris" or ep.get("infra_error") or not ep["final_state"]["done"]:
            raise ValueError("Historical evidence must consist of complete Tetris episodes")
        episodes.append((file, ep))
    if not episodes:
        raise ValueError("No completed historical Tetris episodes")
    summaries = [{"episode_id": ep["episode_id"], "score": ep["score"], "steps": ep["steps"],
                  "termination_reason": ep["termination_reason"],
                  "action_counts": dict(Counter(row["action"] for row in ep["trace"])),
                  "source_sha256": digest(ep)} for _, ep in episodes]
    selected = []
    # Fixed representative source categories, chosen before any V5 game call.
    for prefix in ("parent-trigger", "complete-1-dev-", "legacy-2-dev-"):
        found = next((ep for file, ep in episodes if file.parents[1].name.startswith(prefix)), None)
        if found:
            selected.append(_summary(found, include_trace=True, game_context=context))
    return {"complete_episode_summaries": summaries,
            "complete_feedback_examples": fit_pack(selected, 20000, 6500),
            "source_scope": "Historical V4 only; no V5 development/evaluation outcomes or seeds.",
            "feedback_warning": "Feedback projections are evidence, not the actual player input schema."}


def validate_bootstrap(output, evidence):
    if not isinstance(output, dict) or set(output) != {"brief", "experience_summary", "candidate"}:
        raise ValueError("Unexpected bootstrap keys")
    candidate = validate_candidate(output["candidate"])
    brief = output["brief"]
    if (not isinstance(brief, dict) or set(brief) != {"background", "goal", "rules", "precautions", "strategy"}
            or any(not isinstance(value, str) or not value.strip() for value in brief.values())):
        raise ValueError("Invalid game brief")
    summaries = output["experience_summary"]
    known_ids = {ep["episode_id"] for ep in evidence["complete_episode_summaries"]}
    if not isinstance(summaries, list) or not 1 <= len(summaries) <= 6:
        raise ValueError("Invalid experience summary")
    for item in summaries:
        if (not isinstance(item, dict) or set(item) != {"claim", "evidence_episode_ids", "limitations"}
                or any(not isinstance(item[k], str) or not item[k].strip() for k in ("claim", "limitations"))
                or not isinstance(item["evidence_episode_ids"], list) or not item["evidence_episode_ids"]
                or not all(isinstance(i, str) and i in known_ids for i in item["evidence_episode_ids"])):
            raise ValueError("Experience claim lacks a valid source reference")
    return candidate, brief, summaries


def retain_referenced_experience(output, evidence):
    """Drop entire invalid evidence items; never guess or repair source IDs."""
    value, rejected = deepcopy(output), []
    if not isinstance(value, dict) or not isinstance(value.get("experience_summary"), list):
        raise ValueError("Cannot recover malformed bootstrap")
    accepted = []
    for index, item in enumerate(value["experience_summary"]):
        probe = {**value, "experience_summary": [item]}
        try:
            validate_bootstrap(probe, evidence)
        except ValueError:
            rejected.append({"index": index, "item": item, "reason": "schema_or_source_reference_invalid"})
        else:
            accepted.append(item)
    value["experience_summary"] = accepted
    validate_bootstrap(value, evidence)  # At least one usable experience item must remain.
    return value, rejected


def recover_valid_fields(destination, previous):
    """Finalize existing rejected output without buying another LLM response."""
    destination, previous = Path(destination), Path(previous)
    if destination.exists() or (previous / "parent.json").exists() or not (previous / "failed.json").exists():
        raise ValueError("Preserve evidence; only recover a rejected output into a fresh directory")
    manifest = json.loads((previous / "bootstrap-manifest.json").read_text())
    messages = json.loads((previous / "coach.input.json").read_text())
    response = json.loads((previous / "coach.output.json").read_text())
    if digest(messages) != manifest["input_hash"] or digest(response["text"]) != response["call"]["response_hash"]:
        raise ValueError("Rejected source evidence changed")
    prompt = json.loads(messages[1]["content"])
    output, rejected = retain_referenced_experience(extract_json(response["text"]), prompt["historical_evidence"])
    context, adapter = manifest["game_context"], get_game("tetris")
    if context != adapter.context(manifest["config"]) or manifest["context_contract_hash"] != digest(CONTEXT_CONTRACT):
        raise ValueError("Runtime contract changed since rejected generation")
    parent = {"id": "ctx-parent-" + digest(output["candidate"])[:16], **output,
              "game_id": "tetris", "config": manifest["config"], "game_context": context,
              "rules_version": adapter.rules_version, "decision_model": manifest["decision_model"],
              "think_mode": "never", "created_at": utcnow(), "provenance": {
                  "type": "shared_context_experiment_bootstrap", "coach": response["call"],
                  "input_hash": digest(messages), "context_contract_hash": digest(CONTEXT_CONTRACT),
                  "no_test_results_seen": True, "repair_from": manifest.get("repair_from"),
                  "recovered_from": str(previous.resolve()), "source_output_hash": digest(response),
                  "recovery": "Keep candidate and brief byte-equivalent as JSON values; discard whole invalid experience items; no new LLM call or game result",
                  "rejected_experience_items": rejected}}
    destination.mkdir(parents=True)
    atomic_json(destination / "recovery.json", {"source": str(previous.resolve()),
                "source_manifest_hash": digest(manifest), "source_output_hash": digest(response),
                "rejected_items": rejected, "additional_coach_calls": 0, "new_game_results_seen": False})
    atomic_json(destination / "parent.json", parent)
    return parent


def initialize(destination, history, *, settings=None, repair_from=None):
    destination = Path(destination)
    if destination.exists():
        raise ValueError("Preserve existing bootstrap evidence; choose a new destination")
    settings = settings or Settings.load()
    adapter = get_game("tetris")
    config = {"game_id": "tetris", "max_steps": 80}
    context = adapter.context(config)
    contract = build_initialization_contract(context, settings.decision_model)
    contract["player"]["memory"] = (
        "Raw baseline and facts-only calls are stateless. In memory-enabled arms, the host supplies "
        "Decision-selected per-piece goal plus the last four executed actions; never assume other history.")
    contract["game_guidance"]["design_focus"] = (
        "All arms keep the original rules and complete native inputs. Optional tetris_features describes "
        "the current locked board; optional decision_goal_memory provides the model's own target. "
        "Neither field provides placements, action scores, search, automatic moves, or a hidden future queue.")
    contract["design_requirements"] = [
        "Write one identical conditional strategy for all four arms, using optional fields only when present.",
        "No in-game LLM, no executable policy, no ranking or pruning the native action catalog.",
        "Every goal update and every native keypress is separately selected by Decision with thinking disabled.",
    ]
    sample = contract["sample"]["state"]
    facts_sample = build_tetris_features(sample)
    evidence = history_evidence(history, context)
    prompt = {"immutable_game_context": context, "actual_player_contract": contract,
              "optional_context_contract": CONTEXT_CONTRACT,
              "optional_structure_example": facts_sample,
              "optional_memory_shape": {"scope": "episode/piece/type/locked-board identity",
                  "target": {"leftmost_column": "integer 0..9", "rotation": "integer 0..3"},
                  "age_actions": "integer or null", "recent_actions": "at most four action/before_active records"},
              "historical_evidence": evidence}
    repair = None
    if repair_from is not None:
        previous = Path(repair_from)
        previous_manifest = json.loads((previous / "bootstrap-manifest.json").read_text())
        if (previous / "parent.json").exists() or not (previous / "failed.json").exists():
            raise ValueError("Only a rejected bootstrap can receive a single schema/grounding repair")
        if previous_manifest.get("repair_from") or previous_manifest["game_context"] != context:
            raise ValueError("A repair cannot be repaired again or cross game rules")
        previous_output = json.loads((previous / "coach.output.json").read_text())
        repair = {"source_output_sha256": digest(previous_output),
                  "source_input_hash": previous_manifest["input_hash"],
                  "source_path": str(previous.resolve()), "max_additional_coach_calls": 1,
                  "no_new_game_results": True}
        prompt["rejected_bootstrap"] = previous_output["text"]
        prompt["repair_requirements"] = [
            "Repair the rejected output before any new game. No policy selection from new game outcomes is allowed.",
            "Some episode IDs were truncated or misspelled. Copy complete IDs exactly from complete_episode_summaries; do not invent IDs.",
            "Check all concrete rule claims against immutable_game_context, including 1/2/3/4 line scores 100/300/500/800.",
            "Return the same three-key schema once. Preserve the design where possible; uncertainty must remain explicit."]
    messages = [{"role": "system", "content": (
        "你是俄罗斯方块Harness设计者，先提取已有失败经验，再设计适配实际Decision接口的简短策略。"
        "这次四组使用完全同一策略，只切换可选结构事实和目标记忆；你不会参与测试期间动作选择。"
        "旧轨迹只作证据。优先遵守actual_player_contract和optional_context_contract，反馈投影字段不是玩家字段。"
        "columns[].height是列顶高度，不是occupied_count；active.cells是实际占用格。不要假设存在落点预测或评分。"
        "有目标记忆时，规划调用自己选目标列/朝向并保留；按键调用自己逐步移动/旋转/锁块；没有记忆时不能假设保存了目标。"
        "必须用条件表达可选字段，不把它们当作每组都有。目标不可达可自行换或清空，代码不会替你选择。"
        "短按横移不保证一格，软降100ms推进6tick且通常可下落两格，旋转/硬降每次完整动作均有新边沿。"
        "返回一个JSON对象，恰含brief、experience_summary、candidate。brief恰含background、goal、rules、precautions、strategy；"
        "experience_summary为1到6条对象，恰含claim(简短文字)、evidence_episode_ids(来自输入的ID列表)、limitations(文字)。"
        "candidate恰含decision_instructions与strategy_notes两个非空字符串。动作策略尽量短，避免要求关闭思考的选手长篇推演。"
        "不要宣称尚未发生的分数提升、生成代码、具体测试答案或固定种子路线。")},
        {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)}]
    destination.mkdir(parents=True)
    atomic_json(destination / "bootstrap-manifest.json", {
        "created_at": utcnow(), "game_context": context, "config": config,
        "decision_model": settings.decision_model, "coach_model": settings.coach_model,
        "think_mode": "never", "context_contract_hash": digest(CONTEXT_CONTRACT),
        "input_hash": digest(messages), "shared_across_arms": True,
        "historical_episode_count": len(evidence["complete_episode_summaries"]),
        "max_coach_calls": 1, "repair_from": repair, "live_registry_changes": False})
    atomic_json(destination / "coach.input.json", messages)
    decision, coach = DecisionProvider(settings), CoachProvider(settings)
    try:
        atomic_json(destination / "health.json", decision.health())
        response = coach.complete(messages, purpose="tetris_context_bootstrap")
        atomic_json(destination / "coach.output.json", {"text": response, "call": coach.calls[-1]})
        output = extract_json(response)
        candidate, brief, summaries = validate_bootstrap(output, evidence)
        parent = {"id": "ctx-parent-" + digest(candidate)[:16], "candidate": candidate, "brief": brief,
                  "experience_summary": summaries, "game_id": "tetris", "config": config,
                  "game_context": context, "rules_version": adapter.rules_version,
                  "decision_model": settings.decision_model, "think_mode": "never",
                  "created_at": utcnow(), "provenance": {"type": "shared_context_experiment_bootstrap",
                      "coach": deepcopy(coach.calls[-1]), "input_hash": digest(messages),
                      "context_contract_hash": digest(CONTEXT_CONTRACT), "no_test_results_seen": True,
                      "repair_from": repair}}
        atomic_json(destination / "parent.json", parent)
        return parent
    except Exception as exc:
        atomic_json(destination / "failed.json", {"type": type(exc).__name__})
        raise
    finally:
        decision.close()
        coach.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--history", type=Path,
                        default=ROOT / "data/experiments/feedback-v4b-tetris-20261017")
    recovery = parser.add_mutually_exclusive_group()
    recovery.add_argument("--repair-from", type=Path,
                        help="One explicit repair of a rejected bootstrap, before playing new games")
    recovery.add_argument("--recover-valid-fields-from", type=Path,
                          help="Reuse a rejected response, discard invalid experience items, make zero new LLM calls")
    args = parser.parse_args()
    parent = (recover_valid_fields(args.output, args.recover_valid_fields_from)
              if args.recover_valid_fields_from else initialize(args.output, args.history, repair_from=args.repair_from))
    print(json.dumps({"parent_id": parent["id"], "shared_across_arms": True,
                      "parent_path": str((args.output / "parent.json").resolve())}, ensure_ascii=False))
