#!/usr/bin/env python3
"""Frozen control probes from previously audited Decision trajectories.

These are six development tasks, NOT full-game score trials. Prefix actions are
historical Decision choices used only to reconstruct a public starting state.
Every subsequent goal and native input is selected by the real Decision model.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import fcntl
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts")]
from run_feedback_experiment import digest, load, write_new, public_settings, now, append, ExperimentError
from audit_saved_run import _audit_episode
from game_harness.games import get_game
from game_harness.providers import DecisionProvider, Settings, validate_candidate
from game_harness.runtime import _policy_instructions
from game_harness.tetris_context import TetrisDecisionHarness

ARMS = ("baseline", "repaired_policy", "repaired_feedback")
FIXTURES = (
    ("overshoot_from_left", "calibration-00-both", 3, 6, 0),
    ("overshoot_from_right", "calibration-00-both", 4, 6, 0),
    ("repeat_timer_to_wall", "evaluation-07-both", 42, 9, 1),
    ("repeat_timer_one_column", "evaluation-11-both", 49, 8, 1),
    ("wall_bounded_horizontal_i", "evaluation-01-both", 54, 6, 0),
    ("rotate_and_position_i", "evaluation-05-both", 40, 2, 1),
)
TASK_INSTRUCTIONS = """本次是控制诊断，不是完整得分对局。diagnostic_task给出外部测试目标列与朝向。
请你在自己的目标记忆调用中选择该目标，并自主选择每一个原生按键，使当前这一个方块到达目标列和朝向后硬降锁定。
不要换块、hold或追求其他落点。每步都读取真实状态；程序不会设置你的记忆、改选动作或自动硬降。
如果目标无法实现，仍由你自行决策；任务到预算或当前块变化结束。"""


def source_hashes():
    paths = [p for p in (ROOT / "src").rglob("*") if p.is_file()
             and "__pycache__" not in p.parts and p.suffix != ".pyc"]
    paths += [Path(__file__), ROOT / "scripts/run_feedback_experiment.py", ROOT / "scripts/audit_saved_run.py"]
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)}


def prepare(output, parent_path, repaired_path, source_path):
    parent, repaired, source = load(parent_path), load(repaired_path), Path(source_path)
    validate_candidate(parent["candidate"])
    validate_candidate(repaired["candidate"])
    for key in ("brief", "game_context", "decision_model", "think_mode"):
        if parent[key] != repaired[key]:
            raise ExperimentError("Repair changed shared parent context")
    original = load(source / "manifest.json")["arms"]["both"]
    fixtures = []
    for name, trial, index, column, rotation in FIXTURES:
        path = source / "episodes" / trial / "attempt-01/episode.json"
        episode = load(path)
        audit = _audit_episode(episode, {original["id"]: original})
        if audit["thinking_verified"] != len(episode["trace"]):
            raise ExperimentError("Historical setup is missing thinking-off receipts")
        fixtures.append({"id": name, "source_trial": trial, "source_episode_hash": digest(episode),
                         "source_episode_id": episode["episode_id"], "seed": episode["seed"],
                         "start_step": index, "prefix": [s["action"] for s in episode["trace"][:index]],
                         "before": episode["trace"][index]["before"],
                         "target": {"leftmost_column": column, "rotation": rotation}})
    harnesses = {}
    for arm in ARMS:
        harness = deepcopy(parent if arm == "baseline" else repaired)
        config = {"schema": "tetris-context-v1", "structured_facts": True,
                  "decision_goal_memory": True, "replan_every_actions": 4}
        if arm != "baseline":
            config.update(schema="tetris-context-v2", control_feedback=arm == "repaired_feedback")
        harness["tetris_context_config"] = config
        harnesses[arm] = harness
    manifest = {"schema": "tetris-control-diagnostics/v1", "created_at": now(),
                "arms": harnesses, "fixtures": fixtures, "max_actions": 12,
                "code_hashes": source_hashes(), "providers": public_settings(Settings.load()),
                "protocol": {"full_game_evidence": False, "prefixes_are_historical_setup_only": True,
                             "goal_and_actions_require_decision": True, "thinking": "never",
                             "score_based_early_stop": False, "max_attempts": 2,
                             "success": "Current source piece changes after Decision selects hard-drop:100 starting at externally specified column and rotation",
                             "order": "Cycle the three arms by fixture index", "publish": False}}
    Path(output).mkdir(parents=True, exist_ok=False)
    write_new(Path(output) / "manifest.json", manifest)
    write_new(Path(output) / "manifest.sha256.json", {"sha256": digest(manifest)})
    return manifest


def at_target(snapshot, target):
    active = snapshot["observation"]["active"]
    return (min(c["x"] for c in active["cells"]) == target["leftmost_column"]
            and active["rotation"] == target["rotation"])


def play(provider, harness, fixture, episode_id, max_actions=12, on_step=None):
    adapter = get_game("tetris")
    session = adapter.create(fixture["seed"], {"max_steps": 80})
    trace, reason = [], "action_budget"
    try:
        for action in fixture["prefix"]:
            session.step(action)
        if session.snapshot() != fixture["before"]:
            raise ExperimentError("Historical prefix no longer reproduces exact starting state")
        wrapper = TetrisDecisionHarness(provider, harness["tetris_context_config"])
        instructions = _policy_instructions(harness["candidate"], harness["game_context"]["rules"]) + "\n" + TASK_INSTRUCTIONS
        initial_piece = fixture["before"]["observation"]["pieceId"]
        success = False
        for i in range(max_actions):
            before, criteria = session.snapshot(), session.candidates()
            if before["done"]:
                reason = "native_done"
                break
            state = deepcopy(before)
            state.update(episode_id=episode_id, observation_hash=digest(before), candidates=criteria,
                         remaining_steps=80 - before["step"], diagnostic_task={
                             "target": fixture["target"], "remaining_diagnostic_actions": max_actions - i,
                             "objective": "reach the specified column and rotation, then hard-drop this same piece"})
            request_id = f"{episode_id}:step:{before['step']}:{digest(before)[:16]}"
            receipt = wrapper.decide(state, instructions, criteria, request_id)
            if session.snapshot() != before or session.candidates() != criteria:
                raise ExperimentError("Game advanced during model wait")
            action = receipt["choice_id"]
            after = session.step(action)
            row = {"before": before, "after": after, "action": action, "receipt": receipt}
            trace.append(row)
            if on_step:
                on_step(row)
            if after["observation"]["pieceId"] != initial_piece:
                # A hard drop preserves column/rotation until collision. Other
                # actions may rotate/move before a gravity lock, so their BEFORE
                # coordinates do not prove the locked piece met the objective.
                success = at_target(before, fixture["target"]) and action == "hard-drop:100"
                reason = "locked_at_target" if success else "piece_changed_before_target_lock"
                break
            if after["done"]:
                reason = "native_done"
                break
        return {"schema": "tetris-control-diagnostic-trial/v1", "episode_id": episode_id,
                "fixture_id": fixture["id"], "target": fixture["target"], "success": success,
                "termination_reason": reason, "actions": len(trace), "trace": trace,
                "goal_calls": sum(c["purpose"] == "goal" for t in trace for c in t["receipt"]["tetris_context"]["calls"])}
    finally:
        session.close()


class Recorder:
    def __init__(self, provider, directory):
        self.provider, self.directory, self.settings = provider, directory, provider.settings
    def decide(self, state, instructions, criteria, request_id):
        path = self.directory / "calls" / digest(request_id)[:32]
        write_new(path / "request.json", {"state": state, "instructions": instructions,
                                          "criteria": criteria, "request_id": request_id, "think_mode": "never"})
        receipt = self.provider.decide(state, instructions, criteria, request_id)
        write_new(path / "receipt.json", receipt)
        return receipt


def audit_trial(trial, harness, fixture, directory):
    calls = [c for row in trial["trace"] for c in row["receipt"]["tetris_context"]["calls"]]
    class Replay:
        from types import SimpleNamespace
        settings = SimpleNamespace(decision_model=harness["decision_model"])
        index = 0
        def decide(self, state, instructions, criteria, request_id):
            event = calls[self.index]
            self.index += 1
            path = directory / "calls" / digest(request_id)[:32]
            expected = {"state": state, "instructions": instructions, "criteria": criteria,
                        "request_id": request_id, "think_mode": "never"}
            if load(path / "request.json") != expected or load(path / "receipt.json") != event["receipt"]:
                raise ExperimentError("Diagnostic request/receipt archive mismatch")
            return deepcopy(event["receipt"])
    provider = Replay()
    replayed = play(provider, harness, fixture, trial["episode_id"])
    if replayed != trial or provider.index != len(calls):
        raise ExperimentError("Diagnostic native rules or context replay mismatch")
    return {"native_replay": True, "actual_inputs_verified": True, "thinking_off_verified_calls": len(calls)}


def run(output, retry_failed=False):
    path, settings = Path(output), Settings.load()
    manifest = load(path / "manifest.json")
    if digest(manifest) != load(path / "manifest.sha256.json")["sha256"]:
        raise ExperimentError("Diagnostic manifest changed")
    if manifest["code_hashes"] != source_hashes() or manifest["providers"] != public_settings(settings):
        raise ExperimentError("Diagnostic code/provider changed after freezing")
    results = []
    with (path / ".run.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        provider = DecisionProvider(settings)
        try:
            health = provider.health()
            if not (path / "health.json").exists():
                write_new(path / "health.json", health)
            for index, fixture in enumerate(manifest["fixtures"]):
                order = ARMS[index % 3:] + ARMS[:index % 3]
                for arm in order:
                    harness = manifest["arms"][arm]
                    trial_dir = path / "trials" / fixture["id"] / arm
                    complete = sorted(trial_dir.glob("attempt-*/complete.json"))
                    if complete:
                        attempt = complete[0].parent
                        trial = load(attempt / "trial.json")
                        audit = audit_trial(trial, harness, fixture, attempt)
                        if load(complete[0]) != {"trial_hash": digest(trial), "audit": audit}:
                            raise ExperimentError("Diagnostic completed result changed")
                    else:
                        attempts = sorted(trial_dir.glob("attempt-*"))
                        if attempts and (not retry_failed or len(attempts) >= 2):
                            raise ExperimentError("Retained failed diagnostic needs explicit bounded retry")
                        attempt = trial_dir / f"attempt-{len(attempts)+1:02d}"
                        write_new(attempt / "started.json", {"created_at": now()})
                        identity = "diag-" + digest({"manifest": digest(manifest), "fixture": fixture["id"],
                                                     "arm": arm, "attempt": len(attempts)+1})[:24]
                        try:
                            trial = play(Recorder(provider, attempt), harness, fixture, identity,
                                         on_step=lambda row: append(attempt / "trace.jsonl", row))
                            write_new(attempt / "trial.json", trial)
                            audit = audit_trial(trial, harness, fixture, attempt)
                            write_new(attempt / "complete.json", {"trial_hash": digest(trial), "audit": audit})
                        except Exception as exc:
                            write_new(attempt / "failed.json", {"error_type": type(exc).__name__})
                            raise
                    results.append({"fixture": fixture["id"], "arm": arm, "success": trial["success"],
                                    "actions": trial["actions"], "goal_calls": trial["goal_calls"],
                                    "reason": trial["termination_reason"], "audit": audit})
                    print(json.dumps(results[-1]), flush=True)
        finally:
            provider.client.close()
        result = {"schema": "tetris-control-diagnostics-result/v1", "manifest_hash": digest(manifest),
                  "full_game_evidence": False, "trials": results,
                  "summary": {arm: {"tasks": sum(t["arm"] == arm for t in results),
                                      "successes": sum(t["arm"] == arm and t["success"] for t in results),
                                      "actions": sum(t["actions"] for t in results if t["arm"] == arm),
                                      "goal_calls": sum(t["goal_calls"] for t in results if t["arm"] == arm)} for arm in ARMS}}
        if (path / "result.json").exists():
            if load(path / "result.json") != result:
                raise ExperimentError("Diagnostic aggregate changed")
        else:
            write_new(path / "result.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser("prepare")
    for option in ("output", "parent", "repaired", "source"):
        prepare_parser.add_argument("--" + option, required=True, type=Path)
    run_parser = commands.add_parser("run")
    run_parser.add_argument("--output", type=Path, required=True)
    run_parser.add_argument("--retry-failed", action="store_true")
    args = parser.parse_args()
    if args.command == "prepare":
        result = prepare(args.output, args.parent, args.repaired, args.source)
        print(json.dumps({"prepared": str(args.output), "manifest_hash": digest(result)}))
    else:
        print(json.dumps(run(args.output, args.retry_failed)["summary"]))
