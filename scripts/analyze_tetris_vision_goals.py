#!/usr/bin/env python3
"""Read-only, post-hoc V7 geometry diagnostics and fixed-rule paired examples.

First audit the full frozen study while holding its existing shared lock. Then
inspect only the 48 formal evaluation episodes. Repeated actions are not
independent samples and these descriptions cannot establish a causal mechanism.
This script never selects actions, calls a model, modifies a strategy or writes
an experiment artifact. JSON goes to stdout for explicit redirection by callers.
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
from analyze_tetris_control_goals import inspect_episode
from report_tetris_vision_experiment import _report_locked as audit_study
from report_tetris_control_experiment import ReportError, digest, load, require

ARMS = ("text", "vision")


def board_profile(snapshot):
    """Observed locked cells only; active pieces, future positions and scores excluded."""
    rows = snapshot["observation"]["board"]["rows"]
    by_y = {row["y"]: row["cells"] for row in rows}
    require(len(rows) == 22 and set(by_y) == set(range(-2, 20))
            and all(isinstance(cells, str) and len(cells) == 10 for cells in by_y.values()),
            "Incomplete native board in explanatory case")
    heights, holes = [], 0
    for x in range(10):
        occupied = [y for y in range(-2, 20) if by_y[y][x] != "."]
        top = min(occupied) if occupied else None
        heights.append(20 - top if top is not None else 0)
        if top is not None:
            holes += sum(by_y[y][x] == "." for y in range(top + 1, 20))
    return {"locked_cells": sum(cell != "." for cells in by_y.values() for cell in cells),
            "holes": holes, "column_heights": heights, "maximum_height": max(heights)}


def inspect_extra(episode):
    """Describe visible stalls without assuming every zero displacement hit a wall."""
    counts = Counter(noop_actions=0, horizontal_actions=0, boundary_no_x_change_actions=0)
    for row in episode["trace"]:
        action = row["action"].split(":", 1)[0]
        counts["noop_actions"] += action == "noop"
        if action not in {"left", "right"}:
            continue
        counts["horizontal_actions"] += 1
        before = row["receipt"]["tetris_context"]["action_input_state"]["observation"]
        after = row["after"]["observation"]
        active, ending = before["active"], after.get("active")
        if not (ending and before["pieceId"] == after["pieceId"] and active["type"] == ending["type"]
                and active["rotation"] == ending["rotation"]):
            continue
        lo, hi = min(c["x"] for c in active["cells"]), max(c["x"] for c in active["cells"])
        still = min(c["x"] for c in ending["cells"]) == lo
        at_boundary = (action == "left" and lo == 0) or (action == "right" and hi == 9)
        counts["boundary_no_x_change_actions"] += still and at_boundary
    return dict(counts)


def inspect_choices(episode):
    """Keep complete native action and Decision goal IDs; no center-only grouping."""
    goals, actions = Counter(), Counter()
    for row in episode["trace"]:
        actions[row["action"]] += 1
        for call in row["receipt"]["tetris_context"].get("calls", []):
            if call["purpose"] == "goal":
                goals[call["receipt"]["choice_id"]] += 1
    return {"goal_choice_counts": dict(goals), "native_action_counts": dict(actions)}


def _ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def lock_timeline(episode):
    """Locked-piece counts use native cell/line conservation, not action names."""
    events = []
    for row in episode["trace"]:
        before, after = row["before"], row["after"]
        profile_before, profile_after = board_profile(before), board_profile(after)
        lines = after["metrics"]["lines"] - before["metrics"]["lines"]
        restored_cells = profile_after["locked_cells"] - profile_before["locked_cells"] + 10 * lines
        require(type(restored_cells) is int and restored_cells >= 0 and restored_cells % 4 == 0,
                "Native locked-cell conservation failed in case timeline")
        new_locks = restored_cells // 4
        if not new_locks and not lines:
            continue
        active = before["observation"]["active"]
        memory = row["receipt"]["tetris_context"]["action_input_state"].get("decision_goal_memory", {})
        target = memory.get("target")
        actual = {"leftmost_column": min(cell["x"] for cell in active["cells"]), "rotation": active["rotation"]}
        events.append({"step": before["step"], "piece_id": before["observation"]["pieceId"],
                       "piece_type": active["type"], "action": row["action"], "new_locked_pieces": new_locks,
                       "lines_cleared": lines, "total_lines_after": after["metrics"]["lines"],
                       "target_before_action": deepcopy(target), "pose_before_action": actual,
                       "target_reached_before_action": target == actual if target else None,
                       "board_before": profile_before, "board_after": profile_after,
                       "hole_change": profile_after["holes"] - profile_before["holes"],
                       "request_id": row["receipt"]["request_id"],
                       "response_hash": row["receipt"].get("response_hash")})
    return events


def _reference(path, experiment):
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path.relative_to(experiment))


def _case_episode(arm, episode, source, complete, path):
    initial = episode.get("initial_state") or (episode["trace"][0]["before"] if episode["trace"] else episode["final_state"])
    return {"arm": arm, "episode_id": episode["episode_id"], "scenario_id": episode["logical_trial"],
            "source_reference": _reference(source, path),
            "source_file_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "episode_hash": digest(episode), "harness_hash": complete["harness_hash"],
            "score": episode["score"], "lines": episode["final_state"]["metrics"]["lines"],
            "steps": episode["steps"], "termination_reason": episode["termination_reason"],
            "geometry": inspect_episode(episode), "extra_action_counts": inspect_extra(episode),
            "initial_board_profile": board_profile(initial), "final_board_profile": board_profile(episode["final_state"]),
            "lock_events": lock_timeline(episode)}


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
        return _analyze_locked(path)


def _analyze_locked(path):
    audited = audit_study(path)
    require(audited["status"] == "completed", "Analyze only a complete audited study")
    manifest = load(path / "manifest.json")
    require(manifest["schema"] == "tetris-vision-experiment/v1" and manifest["arm_order"] == list(ARMS), "Unexpected V7 study")
    scenarios = manifest["scenarios"]["evaluation"]
    require(len(scenarios) == 24 and len({s["seed"] for s in scenarios}) == 24, "Require all 24 frozen evaluation pairs")
    result = load(path / "evaluation_result.json")
    require(digest(result) == load(path / "evaluation_result.sha256.json")["sha256"], "Evaluation result changed after audit")
    require([row["id"] for row in result["per_seed"]] == [s["id"] for s in scenarios], "Frozen pair order differs")
    episodes, arms = {}, {}
    for arm in ARMS:
        rows, totals, actions, goals, native_actions = [], Counter(), Counter(), Counter(), Counter()
        for scenario in scenarios:
            completed = list((path / "episodes" / arm / scenario["id"]).glob("attempt-*/complete.json"))
            require(len(completed) == 1, "Require one successful attempt per formal arm/seed")
            evidence, source = load(completed[0]), completed[0].with_name("episode.json")
            episode = load(source)
            require(digest(episode) == evidence["episode_hash"], "Episode changed after full audit")
            require(episode["logical_trial"] == scenario["id"] and episode["seed"] == scenario["seed"]
                    and episode["purpose"] == "evaluation" and episode["experiment_arm"] == arm,
                    "Formal episode identity changed after audit")
            require(evidence["harness_hash"] == digest(manifest["arms"][arm]), "Harness identity changed after audit")
            geometry = inspect_episode(episode)
            geometry["counts"].update(inspect_extra(episode))
            choices = inspect_choices(episode)
            geometry.update(choices)
            goals.update(choices["goal_choice_counts"]); native_actions.update(choices["native_action_counts"])
            totals.update(geometry["counts"]); actions.update(geometry["actions_at_outside_bounds_target"])
            rows.append({"scenario_id": scenario["id"], "seed": scenario["seed"], "episode_hash": digest(episode), **geometry})
            episodes[(arm, scenario["id"])] = (episode, source, evidence)
        arms[arm] = {"episodes": len(rows), "counts": dict(totals),
                     "outside_bounds_per_checkable_action": _ratio(totals["outside_horizontal_bounds_actions"], totals["same_rotation_target_actions"]),
                     "noop_per_action": _ratio(totals["noop_actions"], totals["actions"]),
                     "no_x_change_per_horizontal_action": _ratio(totals["horizontal_no_x_change_actions"], totals["horizontal_actions"]),
                     "boundary_stall_per_horizontal_action": _ratio(totals["boundary_no_x_change_actions"], totals["horizontal_actions"]),
                     "actions_at_outside_bounds_target": dict(actions),
                     "goal_choice_counts": dict(goals), "native_action_counts": dict(native_actions), "per_episode": rows}
    eligible = {"positive_gain": [], "negative_gain": [], "zero_gain_vision_top_out": []}
    for row in result["per_seed"]:
        difference = row["arms"]["vision"]["points"] - row["arms"]["text"]["points"]
        category = "positive_gain" if difference > 0 else "negative_gain" if difference < 0 else (
            "zero_gain_vision_top_out" if row["arms"]["vision"]["termination_reason"] == "TOP_OUT" else None)
        if category:
            eligible[category].append(row)
    categories = {}
    for category, matches in eligible.items():
        categories[category] = []
        if matches:
            selected = matches[0]
            cases = []
            for arm in ARMS:
                episode, source, complete = episodes[(arm, selected["id"])]
                # Recheck the source once more immediately before case extraction.
                require(digest(load(source)) == complete["episode_hash"], "Case episode changed after audit")
                cases.append(_case_episode(arm, episode, source, complete, path))
            categories[category].append({"pair_id": selected["id"], "seed": selected["seed"],
                "eligible_pair_count": len(matches),
                "point_difference": selected["arms"]["vision"]["points"] - selected["arms"]["text"]["points"],
                "episodes": cases})
    return {"schema": "tetris-vision-posthoc-goal-diagnostics/v1", "experiment_id": manifest["id"],
            "post_hoc_exploratory": True, "phase": "evaluation", "paired_bags": 24, "formal_episodes": 48,
            "manifest_hash": audited["manifest_hash"], "evaluation_result_hash": digest(result), "audit": audited["audit"],
            "analysis_source_hashes": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in
                ("scripts/analyze_tetris_vision_goals.py", "scripts/analyze_tetris_control_goals.py")},
            "arms": arms, "case_selection_rule": "In frozen evaluation pair order: first positive vision-minus-text gain; first negative gain; first zero gain with vision TOP_OUT. Missing categories stay empty. No narrative-based reselection.",
            "categories": categories,
            "method": {"population": "Only the 48 formal episodes on 24 paired seeds. Calibration excluded. Repeated actions are not independent samples.",
                "geometry": "Reuse V6 inspect_episode: only target.rotation==active.rotation is checkable. Width=max(active.cells.x)-min(active.cells.x)+1; target left+width>10 or left<0 proves horizontal out-of-bounds.",
                "choice_distributions": "Post-hoc complete choice-ID counts: every Decision goal call, including keep/clear if present, and every executed native action including its duration. No center-only filtering or grouping. Repeated goals/actions are not independent samples and concentration alone does not prove causality.",
                "geometry_limits": "Other rotations are untested. Passing the boundary test does not prove reachability, safety, good placement or line clearing.",
                "stall": "No-x-change requires unchanged piece/type/rotation and minimum occupied x; falling is allowed. This alone does not prove wall collision.",
                "boundary_stall": "A no-x-change horizontal action toward an already contacted board edge (left min x=0, right max x=9). Other stalls may involve timing or locked pieces; not labeled wall collision.",
                "locked_piece_count": "(locked cells after - before + 10*newly cleared lines)/4. Hold is not a lock and TOP_OUT alone is not a lock.",
                "board_profile": "Locked cells only, including spawn rows y=-2,-1. Height=20-top occupied y; a hole is an empty cell below a locked cell in the same column. Active cells excluded.",
                "causality": "Read-only, post-hoc behavior descriptions. Do not establish the causal mechanism of an image effect and do not replace the registered paired score interval.",
                "scope": "No model calls, source/policy edits, filtering of actions, independent extra trials, score changes or promotion."}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("experiment", type=Path)
    args = parser.parse_args()
    print(json.dumps(analyze(args.experiment), ensure_ascii=False, indent=2, allow_nan=False))
