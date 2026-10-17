#!/usr/bin/env python3
"""Post-hoc goal geometry diagnostics; does not change the frozen V6 statistics.

Only check targets with the CURRENT orientation. Current occupied-cell width
proves a target is outside the board when column + width > 10. This is a
necessary boundary test, not a reachability solver or a proposed game policy.
The full frozen study is audited under its existing read lock before analysis.
"""
from __future__ import annotations

import argparse
from collections import Counter
import fcntl
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "src")]
from report_tetris_control_experiment import _report_locked, digest, load, require


def inspect_episode(episode):
    counts = Counter(actions=0, same_rotation_target_actions=0,
                     outside_horizontal_bounds_actions=0, outside_bounds_noop_actions=0,
                     horizontal_no_x_change_actions=0)
    invalid_actions = Counter()
    examples = []
    for row in episode["trace"]:
        before = row["receipt"]["tetris_context"]["action_input_state"]
        observation = before["observation"]
        active = observation["active"]
        target = before["decision_goal_memory"]["target"]
        x = min(cell["x"] for cell in active["cells"])
        width = max(cell["x"] for cell in active["cells"]) - x + 1
        counts["actions"] += 1
        if target and target["rotation"] == active["rotation"]:
            counts["same_rotation_target_actions"] += 1
            if target["leftmost_column"] < 0 or target["leftmost_column"] + width > 10:
                counts["outside_horizontal_bounds_actions"] += 1
                counts["outside_bounds_noop_actions"] += row["action"] == "noop:100"
                invalid_actions[row["action"]] += 1
                if len(examples) < 3:
                    examples.append({"step": before["step"], "piece_type": active["type"],
                                     "current_leftmost_column": x, "width": width,
                                     "target": target, "action": row["action"],
                                     "request_id": row["receipt"]["request_id"]})
        after = row["after"]["observation"]
        ending = after.get("active")
        if (row["action"].split(":")[0] in {"left", "right"} and ending
                and after["pieceId"] == observation["pieceId"]
                and ending["type"] == active["type"] and ending["rotation"] == active["rotation"]
                and min(cell["x"] for cell in ending["cells"]) == x):
            counts["horizontal_no_x_change_actions"] += 1
    return {"counts": dict(counts), "actions_at_outside_bounds_target": dict(invalid_actions),
            "first_three_outside_bounds_examples": examples}


def analyze(path):
    path = Path(path)
    with (path / ".run.lock").open("rb") as lock:
        fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        audited = _report_locked(path)
        require(audited["status"] == "completed", "Analyze only a complete audited study")
        manifest = load(path / "manifest.json")
        arms = {}
        for arm in manifest["arm_order"]:
            rows, totals, actions = [], Counter(), Counter()
            for scenario in manifest["scenarios"]["evaluation"]:
                logical_id = f"{scenario['id']}-{arm}"
                paths = list((path / "episodes" / logical_id).glob("attempt-*/complete.json"))
                require(len(paths) == 1, "Exactly one successful logical trial required")
                episode = load(paths[0].with_name("episode.json"))
                require(digest(episode) == load(paths[0])["episode_hash"], "Episode changed after audit")
                row = inspect_episode(episode)
                totals.update(row["counts"])
                actions.update(row["actions_at_outside_bounds_target"])
                rows.append({"trial": logical_id, "seed": scenario["seed"],
                             "episode_hash": digest(episode), **row})
            arms[arm] = {"episodes": len(rows), "counts": dict(totals),
                         "actions_at_outside_bounds_target": dict(actions), "per_episode": rows}
        return {"schema": "tetris-control-goal-diagnostics/v1", "post_hoc_exploratory": True,
                "manifest_hash": audited["manifest_hash"], "audit": audited["audit"], "arms": arms,
                "method": {"population": "Formal 24 paired seeds only; repeated actions are not independent trials.",
                           "bounds_check": "When target.rotation == active.rotation, width=max(active.cells.x)-min(active.cells.x)+1. Target min column + width > 10 or < 0 proves the specified pose cannot fit horizontally.",
                           "limits": "Other orientations are unchecked. Passing this check does not establish reachability, safety or line clearing. It is read-only analysis, not action filtering or goal selection.",
                           "no_x_change": "A left/right action with same piece/type/rotation before and after and unchanged minimum occupied x; vertical movement is allowed.",
                           "statistics": "No changes to registered primary metrics, sample count, promotion gate or frozen policy."}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("experiment", type=Path)
    args = parser.parse_args()
    print(json.dumps(analyze(args.experiment), ensure_ascii=False, indent=2))
