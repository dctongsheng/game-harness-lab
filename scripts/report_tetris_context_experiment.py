#!/usr/bin/env python3
"""Read-only, public aggregate diagnostics for the frozen Tetris context study.

Usage: .venv/bin/python scripts/report_tetris_context_experiment.py EXPERIMENT_DIR
Writes JSON to stdout only. Never calls a model, changes evidence, or recomputes
the registered screening gate. Diagnostics below are exploratory descriptions.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
import fcntl
import hashlib
import json
import math
from pathlib import Path
import re
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
from audit_saved_run import _audit_episode
from game_harness.runtime import _policy_instructions
from game_harness.tetris_context import CONTEXT_CONTRACT, GOAL_INSTRUCTIONS


class ReportError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ReportError(message)


def digest(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _public_text(value):
    # Fixed policy text may be published; infrastructure addresses and accidental
    # credential-shaped strings are excluded even if embedded in that text.
    if isinstance(value, str):
        value = re.sub(r"https?://[^\s<>\"']+", "[address omitted]", value)
        return re.sub(r"\b(?:sk-|Bearer\s+)[A-Za-z0-9_.-]{8,}", "[credential omitted]", value)
    if isinstance(value, list):
        return [_public_text(item) for item in value]
    if isinstance(value, dict):
        return {key: _public_text(item) for key, item in value.items()}
    return value


def _latency(values):
    require(all(type(v) in (int, float) and math.isfinite(v) and v >= 0 for v in values), "Invalid saved latency")
    ordered = sorted(values)
    def quantile(fraction):
        index = (len(ordered) - 1) * fraction
        lo, hi = math.floor(index), math.ceil(index)
        return ordered[lo] + (ordered[hi] - ordered[lo]) * (index - lo)
    return {"calls": len(values), "total_ms": sum(values),
            "mean_ms": statistics.mean(values) if values else None,
            "p50_ms": quantile(.5) if values else None,
            "p95_ms": quantile(.95) if values else None}


def _locked_cells(snapshot):
    return sum(cell != "." for row in snapshot["observation"]["board"]["rows"] for cell in row["cells"])


def episode_diagnostics(episode):
    """Only observed transitions; no inferred optimal placement or action."""
    locked = reversals = reached = 0
    goal_choices, targets, reached_actions, actions = Counter(), Counter(), Counter(), Counter()
    previous_direction = {}
    latencies = {"game_action": [], "goal": []}
    for step in episode["trace"]:
        before, after = step["before"], step["after"]
        delta = _locked_cells(after) - _locked_cells(before)
        delta += 10 * (after["metrics"]["lines"] - before["metrics"]["lines"])
        require(delta >= 0 and delta % 4 == 0, "Locked-piece count is inconsistent with native board/line transitions")
        locked += delta // 4
        action = step["action"]
        actions[action] += 1
        observation = before["observation"]
        active = observation["active"]
        piece = (observation["pieceId"], active["type"])
        direction = action.split(":", 1)[0]
        if direction in {"left", "right"}:
            reversals += piece in previous_direction and previous_direction[piece] != direction
            previous_direction[piece] = direction
        ledger = step["receipt"]["tetris_context"]
        memory = ledger["action_input_state"].get("decision_goal_memory")
        target = memory.get("target") if memory else None
        if target and min(cell["x"] for cell in active["cells"]) == target["leftmost_column"] and active["rotation"] == target["rotation"]:
            reached += 1
            reached_actions[action] += 1
        for call in ledger["calls"]:
            latencies[call["purpose"]].append(call["receipt"]["elapsed_ms"])
            if call["purpose"] == "goal":
                choice = call["receipt"]["choice_id"]
                goal_choices["target" if choice.startswith("target:") else choice] += 1
                if choice.startswith("target:"):
                    targets[choice] += 1
    return {"locked_pieces": locked, "same_piece_horizontal_reversals": reversals,
            "native_action_counts": dict(actions), "goal_choice_counts": dict(goal_choices),
            "selected_target_counts": dict(targets), "goal_already_reached_before_actions": reached,
            "actions_when_goal_already_reached": dict(reached_actions), "latencies": latencies}


def verify_call_archive(path, episode, harness):
    """Match every full recorded request and response, not merely their hashes."""
    matched = []
    base_instructions = _policy_instructions(harness["candidate"], episode["game_context"]["rules"]) + "\n" + CONTEXT_CONTRACT
    for step in episode["trace"]:
        ledger = step["receipt"]["tetris_context"]
        game_calls = []
        for call in ledger["calls"]:
            request_id = call["request_id"]
            directory = path / "calls" / digest(request_id)[:32]
            require((directory / "request.json").is_file() and (directory / "receipt.json").is_file(),
                    "Completed episode is missing an archived request or receipt")
            request, receipt = load(directory / "request.json"), load(directory / "receipt.json")
            instructions = base_instructions + ("\n" + GOAL_INSTRUCTIONS if call["purpose"] == "goal" else "")
            expected_request = {"request_id": request_id, "purpose": call["purpose"],
                                "state": call["input_state"], "instructions": instructions,
                                "criteria": call["catalog"], "think_mode": "never", "expected_model": harness["decision_model"]}
            require(request == expected_request, "Archived outbound request differs from episode ledger")
            require(isinstance(request["instructions"], str) and digest(request["instructions"]) == call["instructions_hash"],
                    "Archived outbound instructions differ from reconstructed ledger")
            require(receipt == call["receipt"], "Archived raw receipt differs from episode ledger")
            require(call["status"] == "validated" and digest(request["state"]) == call["input_hash"],
                    "Archived request was not validated")
            if call["purpose"] == "game_action":
                game_calls.append(receipt)
                without_context = deepcopy(step["receipt"])
                without_context.pop("tetris_context")
                require(receipt == without_context, "Game receipt differs beyond its attached context ledger")
            matched.append(request_id)
        require(len(game_calls) == 1, "Each completed action must have exactly one archived game call")
    require(len(matched) == len(set(matched)), "Duplicate request identity in a completed episode")
    return matched


def _arm_summary(rows):
    counts = {key: Counter() for key in ("native_action_counts", "goal_choice_counts", "selected_target_counts",
                                        "actions_when_goal_already_reached")}
    latencies = {"game_action": [], "goal": []}
    for row in rows:
        for key in counts:
            counts[key].update(row["diagnostics"][key])
        for purpose in latencies:
            latencies[purpose].extend(row["diagnostics"]["latencies"][purpose])
    return {"episodes": len(rows), "episode_ids": [row["episode_id"] for row in rows],
            "points": [row["points"] for row in rows], "mean_points": statistics.mean(row["points"] for row in rows) if rows else None,
            "lines": [row["lines"] for row in rows], "total_lines": sum(row["lines"] for row in rows),
            "any_line_episodes": sum(row["lines"] > 0 for row in rows),
            "steps": [row["steps"] for row in rows], "total_steps": sum(row["steps"] for row in rows),
            "termination_reasons": dict(Counter(row["termination_reason"] for row in rows)),
            **{key: sum(row["diagnostics"][key] for row in rows) for key in
               ("locked_pieces", "same_piece_horizontal_reversals", "goal_already_reached_before_actions")},
            **{key: dict(value) for key, value in counts.items()},
            "latency_ms": {purpose: _latency(values) for purpose, values in latencies.items()}}


def report(path):
    """Hold the runner's existing lock for one consistent read-only snapshot."""
    path = Path(path)
    try:
        lock = (path / ".run.lock").open("rb")
    except FileNotFoundError:
        raise ReportError("Existing .run.lock is missing; report only an initialized, idle experiment") from None
    with lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ReportError("Experiment is running; finish or pause it before generating a consistent report") from None
        try:
            return _report_locked(path)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _report_locked(path):
    path = Path(path)
    manifest = load(path / "manifest.json")
    require(digest(manifest) == load(path / "manifest.sha256.json")["sha256"], "Manifest checksum mismatch")
    require(manifest["schema"] == "tetris-context-experiment/v1", "Unsupported study schema")
    arms, phases = manifest["arm_order"], manifest["scenarios"]
    require(set(arms) == {"baseline", "facts", "memory", "both"}, "Unexpected study arms")
    expected_trials, attempt_episodes = {}, {}
    for phase, scenarios in phases.items():
        for scenario in scenarios:
            for arm in scenario["order"]:
                logical_id = f"{scenario['id']}-{arm}"
                expected_trials[logical_id] = {"phase": phase, "arm": arm, "seed": scenario["seed"]}
    completed, archive_verified, incomplete = {}, set(), []
    for logical_directory in sorted((path / "episodes").glob("*")):
        require(logical_directory.name in expected_trials, "Episode directory is outside the frozen schedule")
        metadata = expected_trials[logical_directory.name]
        harness = manifest["arms"][metadata["arm"]]
        for attempt in sorted(logical_directory.glob("attempt-*")):
            expected_id = "exp-" + digest({"experiment": manifest["id"], "logical_id": logical_directory.name,
                                           "attempt": attempt.name})[:24]
            attempt_episodes[expected_id] = metadata
            complete_path, episode_path = attempt / "complete.json", attempt / "episode.json"
            if not complete_path.exists():
                incomplete.append({"logical_trial": logical_directory.name, "attempt": attempt.name,
                                   "phase": metadata["phase"], "arm": metadata["arm"],
                                   "status": "failed" if (attempt / "failed.json").exists() else "unfinished",
                                   "episode_saved": episode_path.exists()})
                continue
            require(logical_directory.name not in completed, "Multiple successful attempts for a logical trial")
            evidence, episode = load(complete_path), load(episode_path)
            require(evidence["episode_hash"] == digest(episode) and evidence["harness_hash"] == digest(harness),
                    "Completed episode evidence checksum mismatch")
            require(episode["episode_id"] == expected_id and episode["seed"] == metadata["seed"]
                    and episode["purpose"] == metadata["phase"] and episode["logical_trial"] == logical_directory.name
                    and episode["harness_id"] == harness["id"], "Episode differs from frozen trial identity")
            require(not episode.get("infra_error") and episode.get("termination_reason") not in {"stopped", "infra_error"},
                    "Incomplete episode cannot be counted as complete")
            audit = _audit_episode(episode, {harness["id"]: harness})
            require(audit["thinking_verified"] == len(episode["trace"])
                    and audit["context_audit"]["actual_inputs_verified"], "Episode thinking/context audit failed")
            matched = verify_call_archive(path, episode, harness)
            require(not set(matched) & archive_verified, "Request reused across completed episodes")
            archive_verified.update(matched)
            diagnostics = episode_diagnostics(episode)
            completed[logical_directory.name] = {**metadata, "episode_id": episode["episode_id"],
                    "points": episode["score"], "lines": episode["final_state"]["metrics"]["lines"],
                    "steps": episode["steps"], "termination_reason": episode["termination_reason"],
                    "goal_calls": sum(diagnostics["goal_choice_counts"].values()), "diagnostics": diagnostics}
    phase_reports = {}
    for phase, scenarios in phases.items():
        scheduled = [f"{scenario['id']}-{arm}" for scenario in scenarios for arm in scenario["order"]]
        rows = [completed[key] for key in scheduled if key in completed]
        missing = [key for key in scheduled if key not in completed]
        result_path, hash_path = path / f"{phase}_result.json", path / f"{phase}_result.sha256.json"
        status = "not_started" if not rows else "partial" if missing else "games_complete_summary_pending"
        frozen_result = None
        if result_path.exists() and hash_path.exists():
            result = load(result_path)
            require(digest(result) == load(hash_path)["sha256"], "Frozen phase result checksum mismatch")
            require(not missing and result["status"] == "completed" and result["phase"] == phase,
                    "Phase result claims completion without all frozen episodes")
            expected_rows = []
            for scenario in scenarios:
                expected_rows.append({**scenario, "arms": {arm: {key: completed[f"{scenario['id']}-{arm}"][key]
                    for key in ("episode_id", "points", "lines", "steps", "termination_reason", "goal_calls")}
                    for arm in scenario["order"]}})
            require(result["per_seed"] == expected_rows, "Frozen phase result differs from audited episode scores")
            frozen_result = {key: deepcopy(result[key]) for key in ("arms", "comparisons", "interaction")}
            status = "completed"
        elif result_path.exists():
            status = "summary_unhashed"
        phase_reports[phase] = {"status": status, "scheduled_episodes": len(scheduled), "completed_episodes": len(rows),
                               "missing_trials": missing, "arms": {arm: _arm_summary([row for row in rows if row["arm"] == arm]) for arm in arms},
                               "frozen_registered_result": frozen_result}
    call_status, call_events = Counter(), Counter()
    all_latency = {phase: {purpose: [] for purpose in ("game_action", "goal")} for phase in phases}
    archive_count = 0
    for request_path in sorted((path / "calls").glob("*/request.json")):
        request = load(request_path)
        request_id, purpose = request["request_id"], request["purpose"]
        require(request_path.parent.name == digest(request_id)[:32], "Archived call path does not match request identity")
        require(purpose in {"game_action", "goal"}, "Unknown archived call purpose")
        episode_id = request["state"]["episode_id"]
        require(episode_id in attempt_episodes, "Archived call has no scheduled attempt")
        archive_count += 1
        receipt_path, failed_path = request_path.parent / "receipt.json", request_path.parent / "failed.json"
        require(not (receipt_path.exists() and failed_path.exists()), "Archived call is both successful and failed")
        state = "receipt_saved" if receipt_path.exists() else "failed" if failed_path.exists() else "pending"
        call_status[state] += 1
        if receipt_path.exists():
            receipt = load(receipt_path)
            require(receipt["request_id"] == request_id, "Archived receipt identity mismatch")
            all_latency[attempt_episodes[episode_id]["phase"]][purpose].append(receipt["elapsed_ms"])
    if (path / "calls.jsonl").exists():
        for line in (path / "calls.jsonl").read_text().splitlines():
            if line.strip():
                event = json.loads(line)
                call_events[event["kind"]] += 1
    final_status = "completed" if all(value["status"] == "completed" for value in phase_reports.values()) else "incomplete"
    return {"schema": "tetris-context-public-diagnostics/v1", "experiment_id": manifest["id"],
            "status": final_status, "manifest_hash": digest(manifest),
            "candidate_hash": manifest["candidate_hash"], "brief_hash": manifest["brief_hash"],
            "decision_model": manifest["providers"]["decision_model"], "think_mode": "never",
            "fixed_policy": _public_text(manifest["parent"]["candidate"]),
            "protocol": {"max_steps": manifest["config"]["max_steps"], "arm_order": arms,
                         "constraints": {key: manifest["constraints"][key] for key in
                                         ("publish", "registry_writes", "in_game_coach", "arm_policy_text_changes")}},
            "phases": phase_reports, "incomplete_attempts": incomplete,
            "audit": {"completed_episodes_replayed": len(completed), "ledger_calls_matched_to_archived_requests_and_receipts": len(archive_verified),
                      "all_completed_ledger_calls_match_archive": True, "matching_compares_full_content": True},
            "call_archive": {"requests_saved": archive_count, "status_counts": dict(call_status),
                             "event_counts": dict(call_events), "calls_not_in_completed_episodes": archive_count - len(archive_verified),
                             "all_saved_receipt_latency_ms_by_phase": {phase: {purpose: _latency(values)
                                 for purpose, values in purposes.items()} for phase, purposes in all_latency.items()}},
            "definitions": {"locked_pieces": "Sum of (locked occupied cells after - before + 10 * newly cleared lines) / 4; hold is not a lock.",
                            "horizontal_reversals": "Opposite successive horizontal choices within the same piece, even with intervening non-horizontal choices; not automatically an error.",
                            "goal_reached": "Before the actual game action, active minimum occupied x and rotation equal the current Decision-owned goal.",
                            "latency": "Arm latency uses completed audited episodes. Call-archive latency also includes saved responses from incomplete attempts.",
                            "interpretation": "Exploratory behavior diagnostics. Registered confidence intervals and gates are copied from verified completed phase results, never changed here."}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("experiment", type=Path)
    args = parser.parse_args()
    print(json.dumps(report(args.experiment), ensure_ascii=False, indent=2, allow_nan=False))
