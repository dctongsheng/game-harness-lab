#!/usr/bin/env python3
"""Read-only V8 post-hoc goal/outcome diagnostics after the complete study audit.

The rules in this file were fixed before inspecting V8 scores. They describe
the 12 formal seed pairs, not calibration. All original native points and line
counts remain the outcomes; hypothetical geometry never replaces game scores.
No model calls, new games, source changes or experiment writes are performed.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import fcntl
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "src")]
from report_tetris_landing_experiment import report as audit_study
from report_tetris_control_experiment import ReportError, digest, load, require
from analyze_tetris_vision_goals import board_profile, lock_timeline
from game_harness.tetris_landing import build_landing_projections

ARMS = ("baseline", "planning")
CASE_CATEGORIES = ("positive_gain", "negative_gain", "zero_gain_planning_top_out")


def _label(value):
    return "null" if value is None else "true" if value is True else "false" if value is False else str(value)


def _target_id(target):
    if target is None:
        return None
    require(isinstance(target, dict) and set(target) == {"leftmost_column", "rotation"}, "Invalid saved target")
    column, rotation = target["leftmost_column"], target["rotation"]
    require(type(column) is int and 0 <= column <= 9 and type(rotation) is int and 0 <= rotation <= 3,
            "Saved target outside complete goal catalog")
    return f"target:{column}:{rotation}"


def _projections(state):
    facts = build_landing_projections(state)
    if "tetris_landing_projections" in state:
        require(state["tetris_landing_projections"] == facts, "Saved planning facts differ from post-hoc reconstruction")
    return {row["id"]: row for row in facts["candidates"]}


def _outcome(row):
    if row is None:
        return None
    return {key: deepcopy(row[key]) for key in (
        "id", "pose_valid", "invalid_reason", "native_reachability", "lines_cleared", "points_delta",
        "top_out", "landing_origin", "landing_cells", "holes_before", "holes_after", "holes_delta",
        "max_height", "total_height", "bumpiness")}


def _can_clear(row):
    return row is not None and row["pose_valid"] and row["lines_cleared"] > 0


def inspect_episode(episode):
    """Analyze saved real native transitions; no inference or action selection."""
    counts = Counter(actions=0, goal_calls=0, explicit_target_choices=0,
        actions_with_target=0, actions_without_target=0, target_pose_valid_actions=0,
        target_pose_invalid_actions=0, target_reached_before_actions=0,
        goal_calls_with_conditional_clear_option=0, goal_calls_selecting_conditional_clear_option=0,
        explicit_target_choices_with_conditional_clear_option=0,
        explicit_target_choices_selecting_conditional_clear_option=0,
        actions_with_conditional_clear_option=0, actions_targeting_conditional_clear_option=0,
        hard_drop_actions=0, hard_drop_actions_with_target=0, hard_drop_actions_at_target=0,
        hard_drop_actions_clearing_lines=0, hard_drop_actual_lines=0, hard_drop_native_points=0,
        native_lines_cleared=0, native_points_gained=0)
    goals, native_actions = Counter(), Counter()
    new_target = {name: Counter() for name in ("pose_valid", "invalid_reason", "top_out", "lines_cleared")}
    target_per_action = {name: Counter() for name in ("pose_valid", "invalid_reason", "top_out", "lines_cleared")}
    step_diagnostics, hard_drops = [], []
    for row in episode["trace"]:
        ledger = row["receipt"]["tetris_context"]
        state = ledger["action_input_state"]
        projections = _projections(state)
        any_clear = any(_can_clear(value) for value in projections.values())
        target = state["decision_goal_memory"]["target"]
        target_id = _target_id(target)
        chosen = projections.get(target_id)
        active = state["observation"]["active"]
        actual = {"leftmost_column": min(cell["x"] for cell in active["cells"]), "rotation": active["rotation"]}
        reached = target == actual if target is not None else None
        counts["actions"] += 1
        native_actions[row["action"]] += 1
        counts["actions_with_target"] += target is not None
        counts["actions_without_target"] += target is None
        counts["target_reached_before_actions"] += reached is True
        if chosen is not None:
            counts["target_pose_valid_actions"] += chosen["pose_valid"]
            counts["target_pose_invalid_actions"] += not chosen["pose_valid"]
            for field in target_per_action:
                target_per_action[field][_label(chosen[field])] += 1
        counts["actions_with_conditional_clear_option"] += any_clear
        counts["actions_targeting_conditional_clear_option"] += any_clear and _can_clear(chosen)
        goal_events = []
        for call in ledger["calls"]:
            if call["purpose"] != "goal":
                continue
            choice = call["receipt"]["choice_id"]
            goals[choice] += 1
            counts["goal_calls"] += 1
            goal_state = call["input_state"]
            goal_projections = _projections(goal_state)
            goal_any_clear = any(_can_clear(value) for value in goal_projections.values())
            old_target_id = _target_id(goal_state["decision_goal_memory"]["target"])
            selected_id = old_target_id if choice == "keep" else None if choice == "clear" else choice
            require(selected_id is None or selected_id in goal_projections, "Goal response outside full projection catalog")
            selected = goal_projections.get(selected_id)
            counts["goal_calls_with_conditional_clear_option"] += goal_any_clear
            counts["goal_calls_selecting_conditional_clear_option"] += goal_any_clear and _can_clear(selected)
            if choice not in ("keep", "clear"):
                counts["explicit_target_choices"] += 1
                counts["explicit_target_choices_with_conditional_clear_option"] += goal_any_clear
                counts["explicit_target_choices_selecting_conditional_clear_option"] += goal_any_clear and _can_clear(selected)
                for field in new_target:
                    new_target[field][_label(selected[field])] += 1
            goal_events.append({"choice_id": choice, "selected_or_kept_target_id": selected_id,
                "any_conditional_clear_option": goal_any_clear, "selected_conditional_outcome": _outcome(selected),
                "request_id": call["request_id"]})
        native_lines = row["after"]["metrics"]["lines"] - row["before"]["metrics"]["lines"]
        native_points = row["after"]["score"] - row["before"]["score"]
        require(type(native_lines) is int and native_lines >= 0 and native_points >= 0,
                "Native points or line delta invalid")
        counts["native_lines_cleared"] += native_lines
        counts["native_points_gained"] += native_points
        detail = {"step": row["before"]["step"], "action": row["action"],
            "target": deepcopy(target), "target_conditional_outcome": _outcome(chosen),
            "any_conditional_clear_option": any_clear, "target_reached_before_action": reached,
            "actual_pose_before_action": actual, "actual_active_before_action": deepcopy(active),
            "native_lines_cleared": native_lines, "native_points_delta": native_points,
            "goal_events": goal_events}
        step_diagnostics.append(detail)
        if row["action"] == "hard-drop:100":
            counts["hard_drop_actions"] += 1
            counts["hard_drop_actions_with_target"] += target is not None
            counts["hard_drop_actions_at_target"] += reached is True
            counts["hard_drop_actions_clearing_lines"] += native_lines > 0
            counts["hard_drop_actual_lines"] += native_lines
            counts["hard_drop_native_points"] += native_points
            hard_drops.append(deepcopy(detail))
    final = episode["final_state"]
    require(episode["score"] == final["score"], "Episode score disagrees with native final score")
    return {"counts": dict(counts), "goal_choice_counts": dict(goals), "native_action_counts": dict(native_actions),
        "explicit_target_outcome_counts": {key: dict(value) for key, value in new_target.items()},
        "per_action_target_outcome_counts": {key: dict(value) for key, value in target_per_action.items()},
        "score": episode["score"], "final_native_lines": final["metrics"]["lines"],
        "termination_reason": episode["termination_reason"], "hard_drops": hard_drops,
        "step_diagnostics": step_diagnostics}


def _case_episode(arm, episode, source, complete, path, diagnostics):
    details = {row["step"]: row for row in diagnostics["step_diagnostics"]}
    native_rows = {row["before"]["step"]: row for row in episode["trace"]}
    locks = lock_timeline(episode)
    for row in locks:
        detail = details[row["step"]]
        native = native_rows[row["step"]]
        row.update({"actual_active_before_action": detail["actual_active_before_action"],
            "observed_locked_board_before": deepcopy(native["before"]["observation"]["board"]),
            "observed_locked_board_after": deepcopy(native["after"]["observation"]["board"]),
            "goal_events_at_step": detail["goal_events"],
            "target_conditional_outcome": detail["target_conditional_outcome"],
            "any_conditional_clear_option": detail["any_conditional_clear_option"],
            "native_points_delta": detail["native_points_delta"]})
    final = episode["final_state"]
    return {"arm": arm, "episode_id": episode["episode_id"], "scenario_id": episode["logical_trial"],
        "source_reference": str(source.relative_to(path)), "source_file_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "episode_hash": digest(episode), "harness_hash": complete["harness_hash"],
        "score": episode["score"], "native_lines": final["metrics"]["lines"], "steps": episode["steps"],
        "termination_reason": episode["termination_reason"],
        "all_lock_events": locks,
        "final_state": {"step": final["step"], "score": final["score"], "metrics": deepcopy(final["metrics"]),
            "reason": final.get("reason"), "active": deepcopy(final["observation"].get("active")),
            "board": deepcopy(final["observation"]["board"]), "board_profile": board_profile(final)}}


def analyze(path):
    path = Path(path)
    try:
        lock = (path / ".run.lock").open("rb")
    except FileNotFoundError:
        raise ReportError("Existing .run.lock is missing; analyze only a complete idle experiment") from None
    with lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ReportError("Experiment is running; wait for completion before post-hoc analysis") from None
        # Keep this outer read lock throughout the public full audit AND analysis.
        # The public audit owns its own shared descriptor and can safely release it.
        return _analyze_locked(path)


def _analyze_locked(path):
    audited = audit_study(path)
    require(audited["status"] == "completed", "Analyze only a complete audited study")
    manifest = load(path / "manifest.json")
    require(manifest["schema"] == "tetris-landing-experiment/v1" and manifest["arm_order"] == list(ARMS), "Unexpected V8 study")
    require(digest(manifest) == audited["manifest_hash"], "Manifest changed after full audit")
    scenarios = manifest["scenarios"]["evaluation"]
    require(len(scenarios) == 12 and len({s["seed"] for s in scenarios}) == 12, "Require all 12 frozen evaluation pairs")
    result = load(path / "evaluation_result.json")
    require(digest(result) == load(path / "evaluation_result.sha256.json")["sha256"], "Evaluation result changed after audit")
    require([row["id"] for row in result["per_seed"]] == [s["id"] for s in scenarios], "Frozen pair order differs")
    episodes, arms = {}, {}
    for arm in ARMS:
        totals, goals, actions = Counter(), Counter(), Counter()
        distributions = {name: {field: Counter() for field in ("pose_valid", "invalid_reason", "top_out", "lines_cleared")}
                         for name in ("explicit_target_outcome_counts", "per_action_target_outcome_counts")}
        rows = []
        for scenario in scenarios:
            completed = list((path / "episodes" / arm / scenario["id"]).glob("attempt-*/complete.json"))
            require(len(completed) == 1, "Require one successful attempt per formal arm/seed")
            complete, source = load(completed[0]), completed[0].with_name("episode.json")
            episode = load(source)
            require(digest(episode) == complete["episode_hash"], "Episode changed after full audit")
            require(episode["logical_trial"] == scenario["id"] and episode["seed"] == scenario["seed"]
                    and episode["purpose"] == "evaluation" and episode["experiment_arm"] == arm,
                    "Formal episode identity changed after audit")
            require(complete["harness_hash"] == digest(manifest["arms"][arm]), "Harness identity changed after audit")
            diagnostics = inspect_episode(episode)
            totals.update(diagnostics["counts"]); goals.update(diagnostics["goal_choice_counts"])
            actions.update(diagnostics["native_action_counts"])
            for name, fields in distributions.items():
                for field, counter in fields.items():
                    counter.update(diagnostics[name][field])
            rows.append({"scenario_id": scenario["id"], "seed": scenario["seed"], "episode_hash": digest(episode),
                **{key: value for key, value in diagnostics.items() if key != "hard_drops"}})
            episodes[(arm, scenario["id"])] = (episode, source, complete, diagnostics)
        arms[arm] = {"episodes": len(rows), "counts": dict(totals), "goal_choice_counts": dict(goals),
            "native_action_counts": dict(actions), "projection_facts_were_sent_to_model": arm == "planning",
            **{name: {field: dict(counts) for field, counts in fields.items()} for name, fields in distributions.items()},
            "per_episode": rows}
    eligible = {category: [] for category in CASE_CATEGORIES}
    for pair in result["per_seed"]:
        actual = {}
        for arm in ARMS:
            episode = episodes[(arm, pair["id"])][0]
            require(pair["arms"][arm]["points"] == episode["score"]
                    and pair["arms"][arm]["termination_reason"] == episode["termination_reason"],
                    "Paired scores or terminal reason differ from analyzed native episode")
            actual[arm] = episode["score"]
        gain = actual["planning"] - actual["baseline"]
        category = "positive_gain" if gain > 0 else "negative_gain" if gain < 0 else (
            "zero_gain_planning_top_out" if pair["arms"]["planning"]["termination_reason"] == "TOP_OUT" else None)
        if category:
            eligible[category].append(pair)
    categories = {category: [] for category in CASE_CATEGORIES}
    for category, pairs in eligible.items():
        if pairs:
            pair = pairs[0]
            cases = []
            for arm in ARMS:
                episode, source, complete, diagnostics = episodes[(arm, pair["id"])]
                require(digest(load(source)) == complete["episode_hash"], "Case episode changed after full audit")
                cases.append(_case_episode(arm, episode, source, complete, path, diagnostics))
            categories[category].append({"pair_id": pair["id"], "seed": pair["seed"], "eligible_pair_count": len(pairs),
                "point_difference": pair["arms"]["planning"]["points"] - pair["arms"]["baseline"]["points"],
                "episodes": cases})
    return {"schema": "tetris-landing-posthoc-goal-diagnostics/v1", "experiment_id": manifest["id"],
        "post_hoc_exploratory": True, "phase": "evaluation", "paired_bags": 12, "formal_episodes": 24,
        "manifest_hash": audited["manifest_hash"], "evaluation_result_hash": digest(result),
        "audit": audited["audit"], "full_audit": audited,
        "analysis_source_hashes": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in (
            "scripts/analyze_tetris_landing_goals.py", "scripts/analyze_tetris_vision_goals.py")},
        "arms": arms, "categories": categories,
        "case_selection_rule": "In frozen evaluation pair order: first positive planning-minus-baseline points; first negative points; first zero points difference with planning TOP_OUT. Missing categories stay empty. No narrative-based reselection.",
        "method": {
            "population": "Only 24 formal episodes on 12 paired seeds; calibration excluded. Repeated goals/actions are not independent samples.",
            "geometry": "Every saved goal/action recomputes all 40 conditional projections with the frozen build_landing_projections. Baseline computation is post-hoc only, never supplied to its model.",
            "reachability": "Conditional clear option means pose_valid and lines_cleared > 0 under instantaneous hypothetical pose alignment; it does NOT mean reachable by real timed inputs.",
            "goal_distributions": "All original goal IDs including keep/clear are counted. Explicit new targets count target:* responses, even if the ID equals an older target. Null predicted values remain null, not zero. Per-action targets include retained memory.",
            "opportunity_denominators": "Conditional-clear goal counts use all goal calls; selected includes a keep retaining such a target. Explicit-target counts exclude keep/clear. Per-action counts use the actual target after any planning call at that step.",
            "target_reached": "Before the selected native action: actual active cell minimum x equals target.leftmost_column AND native rotation equals target.rotation. No y condition or path claim.",
            "hard_drop": "Only native hard-drop:100 actions; observed line/point deltas come from after minus before. Being at a target or having a hypothetical clear result is not credited as actual success.",
            "locked_piece_count": "(locked cells after - before + 10*actual newly cleared lines)/4, including locks from soft drop or gravity; hold alone is not a lock.",
            "actual_cells": "Case events show actual active cells BEFORE the chosen action; hypothetical landing cells are labeled conditional and are never described as observed locked cells.",
            "score": "Frozen native final points remain the outcome. Geometry counts neither alter scores nor replace the registered paired score confidence interval.",
            "causality": "Read-only exploratory behavioral descriptions; no causal mechanism or game-strength claim from repeated action counts.",
            "scope": "No model calls, new trials, source edits, policy changes, experiment writes, native action filtering or promotion."}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("experiment", type=Path)
    args = parser.parse_args()
    print(json.dumps(analyze(args.experiment), ensure_ascii=False, indent=2, allow_nan=False))
