#!/usr/bin/env python3
"""Read-only, public aggregate diagnostics for the frozen Tetris control package study.

Usage: .venv/bin/python scripts/report_tetris_control_experiment.py EXPERIMENT_DIR
Writes JSON to stdout only. Never calls a model or changes evidence.
Registered statistics are recomputed only to verify equality. Behavioral
diagnostics below are exploratory descriptions.
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
from game_harness.tetris_context import context_instructions, GOAL_INSTRUCTIONS

CONTROL_KEYS = ("locked_pieces", "same_piece_horizontal_reversals", "horizontal_actions",
                "horizontal_reversal_opportunities", "strict_overshoots", "strict_overshoot_opportunities",
                "goal_already_reached_before_actions")


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
    """Observed transitions only; target switches/rotations cannot create false overshoots."""
    locked = reversals = reached = horizontal = reversal_opportunities = 0
    overshoots = overshoot_opportunities = departures = 0
    goal_choices, targets, reached_actions, actions = Counter(), Counter(), Counter(), Counter()
    previous_signature = previous_direction = None
    latencies = {"game_action": [], "goal": []}
    for step in episode["trace"]:
        before, after = step["before"], step["after"]
        delta = _locked_cells(after) - _locked_cells(before)
        delta += 10 * (after["metrics"]["lines"] - before["metrics"]["lines"])
        require(delta >= 0 and delta % 4 == 0, "Locked-piece count is inconsistent with native board/line transitions")
        locked += delta // 4
        action = step["action"]
        actions[action] += 1
        observation, after_observation = before["observation"], after["observation"]
        active, after_active = observation["active"], after_observation.get("active")
        piece = (observation["pieceId"], active["type"])
        ledger = step["receipt"]["tetris_context"]
        memory = ledger["action_input_state"].get("decision_goal_memory")
        target = memory.get("target") if memory else None
        target_key = (target["leftmost_column"], target["rotation"]) if target else None
        board_hash = digest(observation["board"]["rows"])
        signature = (piece, board_hash, active["rotation"], target_key)
        if signature != previous_signature:
            previous_direction = None
        direction = action.split(":", 1)[0]
        if direction in {"left", "right"}:
            horizontal += 1
            if previous_direction is not None:
                reversal_opportunities += 1
                reversals += previous_direction != direction
            previous_direction = direction
            comparable = (target is not None and isinstance(after_active, dict)
                          and after_observation["pieceId"] == observation["pieceId"]
                          and after_active["type"] == active["type"]
                          and after_active["rotation"] == active["rotation"]
                          and digest(after_observation["board"]["rows"]) == board_hash)
            if comparable:
                error_before = target["leftmost_column"] - min(cell["x"] for cell in active["cells"])
                error_after = target["leftmost_column"] - min(cell["x"] for cell in after_active["cells"])
                if error_before != 0:
                    overshoot_opportunities += 1
                    overshoots += error_before * error_after < 0
                else:
                    departures += error_after != 0
        previous_signature = signature
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
            "horizontal_actions": horizontal, "horizontal_reversal_opportunities": reversal_opportunities,
            "strict_overshoots": overshoots, "strict_overshoot_opportunities": overshoot_opportunities,
            "departures_from_goal_column": departures,
            "native_action_counts": dict(actions), "goal_choice_counts": dict(goal_choices),
            "selected_target_counts": dict(targets), "goal_already_reached_before_actions": reached,
            "actions_when_goal_already_reached": dict(reached_actions), "latencies": latencies}


def verify_call_archive(path, episode, harness):
    """Match every full recorded request and response, not merely their hashes."""
    matched = []
    base_instructions = context_instructions(_policy_instructions(harness["candidate"], episode["game_context"]["rules"]), harness["tetris_context_config"])
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
    values = {"episodes": len(rows), "episode_ids": [row["episode_id"] for row in rows],
            "points": [row["points"] for row in rows], "mean_points": statistics.mean(row["points"] for row in rows) if rows else None,
            "lines": [row["lines"] for row in rows], "total_lines": sum(row["lines"] for row in rows),
            "any_line_episodes": sum(row["lines"] > 0 for row in rows),
            "any_line_rate": statistics.mean(row["lines"] > 0 for row in rows) if rows else None,
            "steps": [row["steps"] for row in rows], "total_steps": sum(row["steps"] for row in rows),
            "goal_calls": sum(row["goal_calls"] for row in rows),
            "top_out_episodes": sum(row["termination_reason"] == "TOP_OUT" for row in rows),
            "termination_reasons": dict(Counter(row["termination_reason"] for row in rows)),
            **{key: sum(row["diagnostics"][key] for row in rows) for key in (*CONTROL_KEYS, "departures_from_goal_column")},
            **{key: dict(value) for key, value in counts.items()},
            "latency_ms": {purpose: _latency(items) for purpose, items in latencies.items()}}
    ratio = lambda n, d: n / d if d else None
    values.update(horizontal_reversal_rate=ratio(values["same_piece_horizontal_reversals"], values["horizontal_reversal_opportunities"]),
                  strict_overshoot_rate=ratio(values["strict_overshoots"], values["strict_overshoot_opportunities"]),
                  steps_per_locked_piece=ratio(values["total_steps"], values["locked_pieces"]))
    return values


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
    require(manifest["schema"] == "tetris-control-experiment/v1", "Unsupported study schema")
    from run_tetris_control_experiment import code_hashes, summarize
    require(manifest["code_hashes"] == code_hashes(), "Current source differs from frozen study; use its source snapshot")
    for relative, expected in manifest["code_hashes"].items():
        source = path / "source_snapshot" / relative
        require(source.is_file() and hashlib.sha256(source.read_bytes()).hexdigest() == expected,
                "Frozen source snapshot checksum mismatch")
    require(digest(manifest["parent"]) == manifest["parent_hash"]
            and digest(manifest["repaired_source"]) == manifest["repaired_hash"], "Frozen strategy source hash mismatch")
    arms, phases = manifest["arm_order"], manifest["scenarios"]
    require(set(arms) == {"baseline", "repaired"}, "Unexpected study arms")
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
            context_audit = audit["context_audit"]
            require(audit["thinking_verified"] == len(episode["trace"])
                    and context_audit["actual_inputs_verified"] is True
                    and context_audit["memory_replay_verified"] is True
                    and context_audit["game_actions"] == len(episode["trace"])
                    and context_audit["goal_calls"] == context_audit["thinking_off_goal_calls"],
                    "Episode thinking/context audit failed")
            expected_audit = {"actions": len(audit["receipts"]), "thinking_off_actions": audit["thinking_verified"],
                              "raw_response_and_rules_replay_verified": True, "context_audit": context_audit}
            require(evidence["audit"] == expected_audit, "Saved audit differs from full native/context replay")
            matched = verify_call_archive(path, episode, harness)
            require(not set(matched) & archive_verified, "Request reused across completed episodes")
            archive_verified.update(matched)
            diagnostics = episode_diagnostics(episode)
            completed[logical_directory.name] = {**metadata, "episode_id": episode["episode_id"],
                    "points": episode["score"], "lines": episode["final_state"]["metrics"]["lines"],
                    "steps": episode["steps"], "termination_reason": episode["termination_reason"],
                    "goal_calls": sum(diagnostics["goal_choice_counts"].values()), "diagnostics": diagnostics,
                    "control_diagnostics": {key: diagnostics[key] for key in CONTROL_KEYS}}
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
                    for key in ("episode_id", "points", "lines", "steps", "termination_reason", "goal_calls", "control_diagnostics")}
                    for arm in scenario["order"]}})
            require(result["per_seed"] == expected_rows, "Frozen phase result differs from audited episode scores")
            expected_summary = summarize(expected_rows, manifest["statistics"], manifest["screening_gate"])
            if phase == "calibration":
                for comparison in expected_summary["comparisons"].values():
                    comparison.update(screening_gate_pass=False, screening_gate_applicable=False)
            for key, value in expected_summary.items():
                require(result[key] == value, "Frozen statistics differ from registered calculation")
            require(result["candidate_hashes"] == manifest["candidate_hashes"] and result["brief_hash"] == manifest["brief_hash"]
                    and result["published"] is False and result["registry_writes"] is False
                    and result["policy_changed_during_study"] is False, "Frozen phase provenance changed")
            frozen_result = {key: deepcopy(result[key]) for key in ("arms", "comparisons", "any_line_comparison")}
            status = "completed"
        elif result_path.exists():
            status = "summary_unhashed"
        phase_reports[phase] = {"status": status, "scheduled_episodes": len(scheduled), "completed_episodes": len(rows),
                               "missing_trials": missing, "arms": {arm: _arm_summary([row for row in rows if row["arm"] == arm]) for arm in arms},
                               "frozen_registered_result": frozen_result}
    call_status, call_events, request_status, event_status = Counter(), Counter(), {}, defaultdict(Counter)
    all_latency = {phase: {purpose: [] for purpose in ("game_action", "goal")} for phase in phases}
    archive_count = 0
    usage_by_purpose = {purpose: defaultdict(float) for purpose in ("game_action", "goal")}
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
        request_status[request_id] = state
        if receipt_path.exists():
            receipt = load(receipt_path)
            require(receipt["request_id"] == request_id, "Archived receipt identity mismatch")
            all_latency[attempt_episodes[episode_id]["phase"]][purpose].append(receipt["elapsed_ms"])
            for key, value in receipt.get("raw_response", {}).get("usage", {}).items():
                if type(value) in (int, float):
                    require(math.isfinite(value), "Invalid archived usage value")
                    usage_by_purpose[purpose][key] += value
    if (path / "calls.jsonl").exists():
        for line in (path / "calls.jsonl").read_text().splitlines():
            if line.strip():
                event = json.loads(line)
                call_events[event["kind"]] += 1
                if event["kind"].startswith("decision_"):
                    require(event.get("request_id") in request_status, "Decision event has no archived request")
                    event_status[event["request_id"]][event["kind"]] += 1
    for request_id, status in request_status.items():
        events = event_status[request_id]
        require(events["decision_started"] == 1, "Each archived request needs exactly one start event")
        require(events["decision_completed"] == int(status == "receipt_saved")
                and events["decision_failed"] == int(status == "failed"), "Call events disagree with saved outcomes")
    final_status = "completed" if all(value["status"] == "completed" for value in phase_reports.values()) else "incomplete"
    return {"schema": "tetris-control-public-diagnostics/v1", "experiment_id": manifest["id"],
            "status": final_status, "manifest_hash": digest(manifest),
            "candidate_hashes": manifest["candidate_hashes"], "brief_hash": manifest["brief_hash"],
            "decision_model": manifest["providers"]["decision_model"], "think_mode": "never",
            "fixed_policies": {arm: _public_text(manifest["arms"][arm]["candidate"]) for arm in arms},
            "protocol": {"max_steps": manifest["config"]["max_steps"], "arm_order": arms,
                         "constraints": {key: manifest["constraints"][key] for key in
                                         ("publish", "registry_writes", "in_game_coach", "arm_policy_text_changes")}},
            "phases": phase_reports, "incomplete_attempts": incomplete,
            "audit": {"completed_episodes_replayed": len(completed), "ledger_calls_matched_to_archived_requests_and_receipts": len(archive_verified),
                      "all_completed_ledger_calls_match_archive": True, "matching_compares_full_content": True},
            "call_archive": {"requests_saved": archive_count, "status_counts": dict(call_status),
                             "event_counts": dict(call_events), "calls_not_in_completed_episodes": archive_count - len(archive_verified),
                             "reported_usage_by_purpose": {purpose: dict(usage) for purpose, usage in usage_by_purpose.items()},
                             "currency_cost": "Not available; reported usage is provider telemetry, not uncached compute or a price.",
                             "all_saved_receipt_latency_ms_by_phase": {phase: {purpose: _latency(values)
                                 for purpose, values in purposes.items()} for phase, purposes in all_latency.items()}},
            "definitions": {"locked_pieces": "Sum of (locked occupied cells after - before + 10 * newly cleared lines) / 4; hold is not a lock.",
                            "horizontal_reversals": "Opposite successive horizontal choices while piece, locked board, rotation and target stay unchanged. Denominator is eligible choices with a preceding horizontal choice; not automatically an error.",
                            "strict_overshoot": "A horizontal input crosses the current target column (nonzero error changes sign), with identical piece/type/rotation/locked board before and after. Denominator is such comparable inputs starting off target. No predicted trajectory.",
                            "rates": "Rates divide summed event counts by summed eligible opportunities, not means of per-episode percentages.",
                            "goal_reached": "Before the actual game action, active minimum occupied x and rotation equal the current Decision-owned goal.",
                            "latency": "Arm latency uses completed audited episodes. Call-archive latency also includes saved responses from incomplete attempts.",
                            "interpretation": "Exploratory behavior diagnostics; package comparison cannot isolate feedback from policy repair. Registered statistics are verified against audited paired episodes and never changed."}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("experiment", type=Path)
    args = parser.parse_args()
    print(json.dumps(report(args.experiment), ensure_ascii=False, indent=2, allow_nan=False))
