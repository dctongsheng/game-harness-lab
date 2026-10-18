#!/usr/bin/env python3
"""Read-only native/context/landing-facts replay and frozen V8 paired statistics."""
from __future__ import annotations

import argparse
import base64
from collections import Counter, defaultdict
from copy import deepcopy
import fcntl
import hashlib
import json
import struct

import httpx
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src")); sys.path.insert(0, str(ROOT / "scripts"))
from report_tetris_control_experiment import (ReportError, require, digest, load, _latency,
    _arm_summary, episode_diagnostics, CONTROL_KEYS)
from audit_saved_run import _audit_episode
from game_harness.runtime import _policy_instructions
from game_harness.tetris_context import context_instructions, goal_instructions, planning_catalog


def verify_transport_archive(path, directory, request, receipt, arm):
    # Both V8 arms use the exact V7 image transport contract and original bytes.
    from report_tetris_vision_experiment import verify_transport_archive as verify_visual_transport
    require(arm in {"baseline", "planning"}, "Unknown planning study arm")
    return verify_visual_transport(path, directory, request, receipt, "vision")


def verify_call_archive(path, episode, harness, arm):
    matched, images = [], []
    base = _policy_instructions(harness["candidate"], episode["game_context"]["rules"])
    instructions = context_instructions(base, harness["tetris_context_config"])
    planning_instructions = goal_instructions(base, harness["tetris_context_config"])
    for step in episode["trace"]:
        game_calls = []
        for call in step["receipt"]["tetris_context"]["calls"]:
            directory = Path(path) / "calls" / arm / episode["logical_trial"] / episode["experiment_attempt"] / digest(call["request_id"])[:32]
            require((directory / "request.json").is_file() and (directory / "receipt.json").is_file(), "Missing archived request/receipt")
            request, receipt = load(directory / "request.json"), load(directory / "receipt.json")
            expected = {"request_id": call["request_id"], "purpose": call["purpose"], "state": call["input_state"],
                        "instructions": planning_instructions if call["purpose"] == "goal" else instructions,
                        "criteria": (planning_catalog(call["input_state"], harness["tetris_context_config"])
                                     if call["purpose"] == "goal" else step["criteria"]),
                        "think_mode": "never", "expected_model": harness["decision_model"]}
            require(request == expected and digest(request["instructions"]) == call["instructions_hash"], "Archived outbound request differs from replayed ledger")
            require(receipt == call["receipt"] and call["status"] == "validated" and digest(request["state"]) == call["input_hash"], "Archived receipt differs from validated ledger")
            image_hash = verify_transport_archive(Path(path), directory, request, receipt, arm)
            if image_hash:
                images.append(image_hash)
            if call["purpose"] == "game_action":
                game_calls.append(receipt)
                stripped = deepcopy(step["receipt"]); stripped.pop("tetris_context")
                require(stripped == receipt, "Game action receipt differs beyond context ledger")
            matched.append(call["request_id"])
        require(len(game_calls) == 1, "Each action must have one game decision call")
    require(len(matched) == len(set(matched)), "Duplicate request in one completed episode")
    return matched, images


def report(path):
    path = Path(path)
    try:
        lock = (path / ".run.lock").open("rb")
    except FileNotFoundError:
        raise ReportError("Existing .run.lock is missing; report an initialized idle experiment") from None
    with lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ReportError("Experiment is running; finish or pause before report") from None
        try:
            return _report_locked(path)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _report_locked(path):
    from run_tetris_landing_experiment import (ARMS, TetrisLandingExperiment, code_hashes, runtime_dependencies, shared_episode_id, summarize, validate_manifest_arms, ExperimentError)
    manifest = load(path / "manifest.json")
    require(digest(manifest) == load(path / "manifest.sha256.json")["sha256"], "Manifest checksum mismatch")
    require(manifest["schema"] == "tetris-landing-experiment/v1" and manifest["arm_order"] == list(ARMS), "Unexpected landing study")
    require(manifest["runtime_dependencies"] == runtime_dependencies(), "Frozen image/runtime dependencies changed")
    require(manifest["code_hashes"] == code_hashes(), "Current source differs from frozen study; use source snapshot")
    for relative, expected in manifest["code_hashes"].items():
        source = path / "source_snapshot" / relative
        require(source.is_file() and hashlib.sha256(source.read_bytes()).hexdigest() == expected, "Frozen source snapshot checksum mismatch")
    try:
        validate_manifest_arms(manifest, path)
    except ExperimentError as exc:
        raise ReportError(str(exc)) from exc
    expected_trials = {(arm, s["id"]): {"arm": arm, "phase": phase, "seed": s["seed"]}
        for phase, scenarios in manifest["scenarios"].items() for s in scenarios for arm in s["order"]}
    completed, incomplete, verified, images, attempts = {}, [], set(), [], {}
    initial_inputs = {}
    for arm_path in sorted((path / "episodes").glob("*")):
        require(arm_path.name in ARMS, "Unknown episode arm")
        for trial in sorted(arm_path.glob("*")):
            key = (arm_path.name, trial.name)
            require(key in expected_trials, "Episode outside frozen schedule")
            metadata, harness = expected_trials[key], manifest["arms"][arm_path.name]
            trial_attempts = sorted(trial.glob("attempt-*"))
            require(len(trial_attempts) <= manifest["budget"]["max_episode_attempts_per_trial"], "Trial exceeded fixed retry budget")
            for attempt in trial_attempts:
                identity = shared_episode_id(manifest["id"], trial.name, attempt.name)
                attempts[(arm_path.name, trial.name, attempt.name)] = {**metadata, "episode_id": identity}
                if not (attempt / "complete.json").exists():
                    incomplete.append({**metadata, "scenario_id": trial.name, "attempt": attempt.name,
                        "status": "failed" if (attempt / "failed.json").exists() else "unfinished"})
                    continue
                require(key not in completed, "Multiple successful attempts for one trial")
                evidence, episode = load(attempt / "complete.json"), load(attempt / "episode.json")
                require(evidence["episode_hash"] == digest(episode) and evidence["harness_hash"] == digest(harness), "Completed evidence checksum mismatch")
                require(episode["episode_id"] == identity and episode["seed"] == metadata["seed"]
                    and episode["purpose"] == metadata["phase"] and episode["logical_trial"] == trial.name
                    and episode["harness_id"] == harness["id"] and episode["experiment_arm"] == arm_path.name
                    and episode["experiment_attempt"] == attempt.name, "Frozen trial identity mismatch")
                require(not episode.get("infra_error") and episode["termination_reason"] not in {"stopped", "infra_error"}, "Incomplete game scored")
                audit = _audit_episode(episode, {harness["id"]: harness})
                context = audit["context_audit"]
                require(audit["thinking_verified"] == len(episode["trace"]) and context["actual_inputs_verified"] is True
                    and context["memory_replay_verified"] is True and context["game_actions"] == len(episode["trace"])
                    and context["goal_calls"] == context["thinking_off_goal_calls"], "Native/context/thinking replay failed")
                matched, episode_images = verify_call_archive(path, episode, harness, arm_path.name)
                expected_audit = {"actions": len(audit["receipts"]), "thinking_off_actions": audit["thinking_verified"],
                    "raw_response_and_rules_replay_verified": True, "context_audit": context,
                    "full_requests_verified": len(matched), "image_requests_verified": len(episode_images)}
                require(evidence["audit"] == expected_audit, "Saved audit differs from native/image replay")
                keys = {(arm_path.name, trial.name, attempt.name, request_id) for request_id in matched}
                require(not keys & verified, "Request reused inside arm")
                verified.update(keys); images.extend(episode_images)
                completed[key] = {**metadata, **TetrisLandingExperiment._episode_row(episode), "diagnostics": episode_diagnostics(episode)}
                first_call = episode["trace"][0]["receipt"]["tetris_context"]["calls"][0]
                require(first_call["purpose"] == "goal", "Initial decision must plan the new piece")
                initial_inputs[key] = {"state": first_call["input_state"], "image": first_call["receipt"]["vision"]["sha256"]}
    initial_pairs = []
    for phase, scenarios in manifest["scenarios"].items():
        for scenario in scenarios:
            keys = [(arm, scenario["id"]) for arm in ARMS]
            if not all(key in initial_inputs for key in keys):
                continue
            baseline, planning = [deepcopy(initial_inputs[key]) for key in keys]
            require("tetris_landing_projections" not in baseline["state"] and "tetris_landing_projections" in planning["state"],
                    "Initial planning facts treatment is missing or leaked into baseline")
            planning["state"].pop("tetris_landing_projections")
            for value in (baseline, planning):
                value["state"]["candidates"] = [choice["id"] for choice in value["state"]["candidates"]]
            require(baseline == planning, "Initial paired state/image differs beyond projections and goal descriptions")
            initial_pairs.append({"phase": phase, "scenario_id": scenario["id"], "same_public_state_image_catalog_ids_and_metadata": True})
    phase_reports = {}
    for phase, scenarios in manifest["scenarios"].items():
        scheduled = [(arm, s["id"]) for s in scenarios for arm in s["order"]]
        missing = [key for key in scheduled if key not in completed]
        rows = [completed[key] for key in scheduled if key in completed]
        status = "not_started" if not rows else "partial" if missing else "games_complete_summary_pending"
        frozen = None
        result_path = path / f"{phase}_result.json"; hash_path = result_path.with_suffix(".sha256.json")
        if result_path.exists() and hash_path.exists():
            result = load(result_path)
            require(digest(result) == load(hash_path)["sha256"] and not missing and result["status"] == "completed", "Frozen result checksum/completion mismatch")
            expected_rows = [{**s, "arms": {arm: {key: completed[(arm, s["id"])][key] for key in
                ("episode_id", "points", "lines", "steps", "termination_reason", "goal_calls", "control_diagnostics")}
                for arm in s["order"]}} for s in scenarios]
            require(result["per_seed"] == expected_rows, "Frozen phase scores differ from native replay")
            expected_summary = summarize(expected_rows, manifest["statistics"], manifest["screening_gate"])
            if phase == "calibration":
                for comparison in expected_summary["comparisons"].values():
                    comparison.update(screening_gate_pass=False, screening_gate_applicable=False)
            require(all(result[key] == value for key, value in expected_summary.items()), "Registered statistics changed")
            require(result["candidate_hash"] == manifest["candidate_hash"] and result["brief_hash"] == manifest["brief_hash"], "Frozen summary policy hashes changed")
            require(result["published"] is False and result["registry_writes"] is False and result["policy_changed_during_study"] is False, "Frozen provenance changed")
            frozen = expected_summary; status = "completed"
        elif result_path.exists():
            status = "summary_unhashed"
        phase_reports[phase] = {"status": status, "scheduled_episodes": len(scheduled), "completed_episodes": len(rows),
            "missing_trials": missing, "arms": {arm: _arm_summary([row for row in rows if row["arm"] == arm]) for arm in ARMS},
            "frozen_registered_result": frozen}
    call_status, archive_count, statuses, event_counts, per_call_events = Counter(), 0, {}, Counter(), defaultdict(Counter)
    latency = {arm: {purpose: [] for purpose in ("goal", "game_action")} for arm in ARMS}
    usage = {arm: defaultdict(float) for arm in ARMS}
    for request_file in sorted((path / "calls").glob("*/*/*/*/request.json")):
        arm, scenario_id, attempt_name, _, _ = request_file.relative_to(path / "calls").parts
        request = load(request_file)
        request_id = request["request_id"]; key = (arm, scenario_id, attempt_name, request_id)
        require(arm in ARMS and request_file.parent.name == digest(request_id)[:32], "Archived request path mismatch")
        require((arm, scenario_id, attempt_name) in attempts and request["state"]["episode_id"] ==
                attempts[(arm, scenario_id, attempt_name)]["episode_id"], "Archived request has no scheduled attempt")
        receipt_file, failed_file = request_file.parent / "receipt.json", request_file.parent / "failed.json"
        require(not (receipt_file.exists() and failed_file.exists()), "Call both succeeded and failed")
        status = "receipt_saved" if receipt_file.exists() else "failed" if failed_file.exists() else "pending"
        statuses[key] = status; call_status[status] += 1; archive_count += 1
        if not receipt_file.exists() and (request_file.parent / "payload.json").exists():
            verify_transport_archive(path, request_file.parent, request, None, arm)
        if receipt_file.exists():
            receipt = load(receipt_file)
            require(receipt["request_id"] == request_id, "Archived receipt identity mismatch")
            verify_transport_archive(path, request_file.parent, request, receipt, arm)
            latency[arm][request["purpose"]].append(receipt["elapsed_ms"])
            for name, value in receipt.get("raw_response", {}).get("usage", {}).items():
                if type(value) in (int, float):
                    usage[arm][name] += value
    events_path = path / "calls.jsonl"
    for line in events_path.read_text().splitlines() if events_path.exists() else []:
        if not line.strip():
            continue
        event = json.loads(line); event_counts[event["kind"]] += 1
        if event["kind"].startswith("decision_"):
            key = (event.get("arm"), event.get("scenario_id"), event.get("attempt"), event.get("request_id"))
            require(key in statuses, "Decision event has no archived request")
            per_call_events[key][event["kind"]] += 1
    require(archive_count <= manifest["budget"]["decision_request_attempts_max"], "Decision request budget exceeded")
    for key, status in statuses.items():
        counts = per_call_events[key]
        require(counts["decision_started"] == 1 and counts["decision_completed"] == int(status == "receipt_saved")
            and counts["decision_failed"] == int(status == "failed"), "Call events disagree with archived outcomes")
    return {"schema": "tetris-landing-public-diagnostics/v1", "experiment_id": manifest["id"],
        "status": "completed" if all(p["status"] == "completed" for p in phase_reports.values()) else "incomplete",
        "manifest_hash": digest(manifest), "candidate_hash": manifest["candidate_hash"], "brief_hash": manifest["brief_hash"], "planner_hash": manifest["planner_hash"],
        "decision_model": manifest["providers"]["decision_model"], "think_mode": "never",
        "protocol": {"max_steps": 80, "arm_order": list(ARMS), "same_action_policy_and_episode_metadata": True,
                     "both_arms_vision_enabled": True, "planning_package_difference": True, "publish": False, "registry_writes": False},
        "phases": phase_reports, "incomplete_attempts": incomplete, "initial_input_comparisons": initial_pairs,
        "audit": {"completed_episodes_replayed": len(completed), "ledger_calls_matched_to_archived_requests_and_receipts": len(verified),
            "image_requests_verified": len(images), "unique_images_used_by_completed_episodes": len(set(images)),
            "all_completed_ledger_calls_match_archive": True, "actual_payloads_and_original_image_bytes_reconstructed": True,
            "all_goal_memory_and_landing_facts_reconstructed": True},
        "call_archive": {"requests_saved": archive_count, "status_counts": dict(call_status), "event_counts": dict(event_counts),
            "calls_not_in_completed_episodes": archive_count - len(verified),
            "reported_usage_by_arm": {arm: dict(v) for arm, v in usage.items()},
            "latency_ms_by_arm": {arm: {purpose: _latency(v) for purpose, v in values.items()} for arm, values in latency.items()}},
        "interpretation": "Only evaluation pairs support the primary contrast. A confidence interval containing zero cannot establish a clear improvement. Auxiliary behavior metrics are exploratory."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("experiment", type=Path)
    args = parser.parse_args()
    print(json.dumps(report(args.experiment), ensure_ascii=False, indent=2, allow_nan=False))
