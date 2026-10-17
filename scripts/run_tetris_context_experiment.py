#!/usr/bin/env python3
"""Frozen 2x2 Tetris context experiment; every goal and input uses Decision.

prepare makes no model calls. Then run-calibration runs 2 fresh seeds x 4 arms,
and run-evaluation runs 12 different fresh seeds x 4 arms regardless of scores.
No coaching, GEPA, registry writes, promotion, or publication occurs here.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import random
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from run_feedback_experiment import (
    Experiment as _BaseExperiment, ExperimentError, append, canonical, digest,
    experiment_path, load, now, public_settings, read_lines, snapshot_sources, write_new,
)
from game_harness.games import get_game
from game_harness.providers import DecisionProvider, Settings, validate_candidate
from game_harness.runtime import run_episode
from audit_saved_run import _audit_episode


ARMS = ("baseline", "facts", "memory", "both")
GAME_CONFIG = {"max_steps": 80}
DEFAULT_SEED = 2026101705


def context_config(arm):
    if arm not in ARMS:
        raise ExperimentError("Unknown Tetris context arm")
    return {"schema": "tetris-context-v1", "structured_facts": arm in {"facts", "both"},
            "decision_goal_memory": arm in {"memory", "both"}, "replan_every_actions": 4}


def code_hashes():
    # Include all source assets, not only Python: an adapter, contract, bridge,
    # rules source, or packaged asset change cannot silently resume a study.
    paths = [path for path in (ROOT / "src").rglob("*")
             if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"]
    paths += [ROOT / "scripts/run_tetris_context_experiment.py",
              ROOT / "scripts/initialize_tetris_context_harness.py",
              ROOT / "scripts/run_feedback_experiment.py", ROOT / "scripts/audit_saved_run.py",
              ROOT / "pyproject.toml"]
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(set(paths))}


def _scenario_seeds(value):
    """Read seed declarations, never numeric metrics or arm/order indexes."""
    if isinstance(value, list):
        for item in value:
            if type(item) is int:
                yield item
            else:
                yield from _scenario_seeds(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            if key in {"seed", "trigger"} and type(item) is int:
                yield item
            elif isinstance(item, (dict, list)):
                yield from _scenario_seeds(item)


def prior_tetris_seeds(data_root):
    """Include completed, failed, and preregistered seeds, not just winners.

    Source snapshots are excluded: they preserve code, not additional trials.
    Each trace's first line covers a crash before its episode.json was saved.
    """
    data_root = Path(data_root)
    used, evidence = set(), []
    paths = set((data_root / "episodes").glob("*.json"))
    experiments = data_root / "experiments"
    paths.update(path for path in experiments.rglob("manifest.json") if "source_snapshot" not in path.parts)
    paths.update(path for path in experiments.rglob("episode.json") if "source_snapshot" not in path.parts)
    for path in sorted(paths):
        value = load(path)
        if value.get("game_id", value.get("parent", {}).get("game_id")) != "tetris":
            continue
        seeds = set(_scenario_seeds(value.get("scenarios", {}))) if path.name == "manifest.json" else set()
        if path.name != "manifest.json" and type(value.get("seed")) is int:
            seeds.add(value["seed"])
        used.update(seeds)
        evidence.append({"path": str(path.relative_to(data_root)), "seed_count": len(seeds),
                         "seeds_hash": digest(sorted(seeds))})
    for path in sorted(experiments.rglob("trace.jsonl")):
        if "source_snapshot" in path.parts:
            continue
        with path.open(encoding="utf-8") as stream:
            first = stream.readline().strip()
        if not first:
            continue
        row = json.loads(first)
        if row.get("type") == "episode_start" and row.get("game_id") == "tetris" and type(row.get("seed")) is int:
            used.add(row["seed"])
            evidence.append({"path": str(path.relative_to(data_root)), "seed_count": 1,
                             "seeds_hash": digest([row["seed"]])})
    return used, evidence


def scenario_plan(seed, excluded=()):
    rng, used = random.Random(seed), set(excluded)
    phases = {}
    for phase, count in (("calibration", 2), ("evaluation", 12)):
        rows = []
        for index in range(count):
            value = rng.randrange(1, 2**31 - 1)
            while value in used:
                value = rng.randrange(1, 2**31 - 1)
            used.add(value)
            offset = index % len(ARMS)
            rows.append({"id": f"{phase}-{index:02d}", "seed": value,
                         "order": list(ARMS[offset:] + ARMS[:offset])})
        phases[phase] = rows
    return phases


def prepare(*, identity, parent_path, seed=DEFAULT_SEED, root=None, data_root=None, settings=None):
    settings = settings or Settings.load()
    if type(seed) is not int:
        raise ExperimentError("Study schedule seed must be an integer")
    source = load(parent_path)
    candidate = validate_candidate(source.get("candidate", source))
    if not isinstance(source.get("brief"), dict) or not source["brief"]:
        raise ExperimentError("A shared initialized game brief is required")
    if source.get("game_id", "tetris") != "tetris":
        raise ExperimentError("Shared parent must be a Tetris Harness")
    adapter = get_game("tetris")
    context = adapter.context(GAME_CONFIG)
    if source.get("game_context") != context:
        raise ExperimentError("Parent must use current Tetris rules and the fixed 80-action budget")
    if source.get("decision_model", settings.decision_model) != settings.decision_model or source.get("think_mode", "never") != "never":
        raise ExperimentError("Parent Decision model or thinking configuration differs")
    if "tetris_context_config" in source:
        raise ExperimentError("Shared parent cannot already be assigned to a treatment arm")
    parent = {**source, "game_id": "tetris", "candidate": candidate,
              "game_context": context, "decision_model": settings.decision_model, "think_mode": "never"}
    parent.setdefault("id", "exp-parent-" + digest({"candidate": candidate, "context": context})[:16])
    excluded, evidence = prior_tetris_seeds(data_root or ROOT / "data")
    scenarios = scenario_plan(seed, excluded)
    harnesses = {}
    for arm in ARMS:
        harnesses[arm] = {**deepcopy(parent), "id": "exp-" + arm + "-" + digest({"parent": parent, "arm": arm})[:16],
                          "parent_id": parent["id"], "tetris_context_config": context_config(arm)}
    manifest = {
        "schema": "tetris-context-experiment/v1", "id": identity, "created_at": now(),
        "game_id": "tetris", "config": deepcopy(GAME_CONFIG), "game_context": context,
        "parent": parent, "parent_hash": digest(parent), "candidate_hash": digest(candidate),
        "brief_hash": digest(parent["brief"]), "arms": harnesses, "arm_order": list(ARMS),
        "providers": public_settings(settings), "code_hashes": code_hashes(), "seed": seed,
        "scenarios": scenarios, "excluded_environment_seeds": sorted(excluded), "prior_seed_evidence": evidence,
        "statistics": {"bootstrap_seed": 74105, "bootstrap_resamples": 10000,
                       "bootstrap_unit": "one paired environment bag seed; all four arms stay together",
                       "primary_comparisons": ["facts-baseline", "memory-baseline", "both-baseline"],
                       "primary_metric": "native line-clear points", "secondary_metric": "native cleared lines",
                       "confidence_levels": [0.95, 1 - .05 / 3], "bonferroni_comparisons": 3,
                       "interaction": "both - facts - memory + baseline; exploratory, not a promotion test"},
        "screening_gate": {"minimum_mean_gain": 100, "no_pair_regression": True,
                           "familywise_bootstrap_lower_greater_than": 0},
        "budget": {"calibration_episodes": 8, "evaluation_episodes": 48, "completed_episodes": 56,
                   "max_game_actions_per_episode": 80, "max_calls_per_action": 2,
                   "decision_request_attempts_without_episode_retries_max": 8960,
                   "max_episode_attempts_per_trial": 2, "decision_request_attempts_max": 17920,
                   "health_preflights_without_retries": 1, "health_preflight_attempts_max": 2,
                   "coach_calls": 0, "retry_definition": "One original attempt plus at most one explicitly requested retry per logical episode; completed episodes are never redrawn. Health is counted separately."},
        "constraints": {"publish": False, "registry_writes": False, "automatic_export": False,
                        "gepa": False, "in_game_coach": False, "arm_policy_text_changes": False,
                        "scoring_early_stop": False, "score_dependent_extra_trials": False,
                        "calibration_is_independent_test": False, "evaluation_requires_calibration_complete_only": True,
                        "failed_attempts": "Stop on any infrastructure/audit error; retain evidence; never score a failed episode as zero."},
        "limitations": ["Twelve unique bag seeds give limited statistical power; bootstrap intervals are descriptive, not proof of universal game ability.",
                        "Calibration cannot change policy, context, gates, sample count or evaluation seeds within this frozen study.",
                        "Decision goal memory adds Decision calls and a fixed goal interface; its comparison does not isolate memory from extra inference budget.",
                        "Episode identifiers differ between arms and are opaque; provider nondeterminism and service drift remain possible.",
                        "Fresh seed IDs avoid all discovered prior scheduled or played seeds; distinct IDs are not a guarantee of distinct full seven-bag sequences.",
                        "Passing a screening gate never publishes or promotes an experimental Harness."],
    }
    path = experiment_path(identity, root)
    path.mkdir(parents=True, exist_ok=False)
    snapshot_sources(path / "source_snapshot", manifest["code_hashes"])
    write_new(path / "manifest.json", manifest)
    write_new(path / "manifest.sha256.json", {"sha256": digest(manifest)})
    write_new(path / "parent.json", parent)
    (path / "README.md").write_text(
        f"# {identity}\n\nFrozen four-arm experiment, 80 native Decision actions per episode.\n\n"
        f"```sh\n.venv/bin/python scripts/run_tetris_context_experiment.py run-calibration --id {identity}\n"
        f".venv/bin/python scripts/run_tetris_context_experiment.py run-evaluation --id {identity}\n```\n\n"
        "Calibration: 2 seeds x 4 arms. Evaluation: 12 disjoint seeds x 4 arms, regardless of calibration score. "
        "All arms use identical frozen policy text and brief; only the two context flags change. "
        "Source bytes, providers, schedule and protocol are frozen. `--retry-failed` permits at most one additional "
        "attempt per failed logical episode. Successful episodes are reused and re-audited. Raw call inputs/receipts "
        "are retained under calls/, game traces under episodes/. No live registry, export or publication writes occur.\n",
        encoding="utf-8")
    return manifest


def _quantile(values, fraction):
    position = (len(values) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def contrast_stats(rows, *, resamples=10000, bootstrap_seed=74105):
    if not rows or len({row["seed"] for row in rows}) != len(rows):
        raise ExperimentError("Contrasts require one paired row per unique bag seed")
    if type(resamples) is not int or resamples < 1:
        raise ExperimentError("Bootstrap resamples must be positive")
    values = [row["difference"] for row in rows]
    if any(type(value) not in (int, float) or not math.isfinite(value) for value in values):
        raise ExperimentError("Contrast differences must be finite numbers")
    rng = random.Random(bootstrap_seed)
    samples = sorted(statistics.mean(rng.choices(values, k=len(values))) for _ in range(resamples))
    tail = .05 / 3 / 2
    return {"pairs": deepcopy(rows), "pair_count": len(rows), "unique_seed_count": len(rows),
            "mean_gain": statistics.mean(values), "min_gain": min(values),
            "positive_pairs": sum(value > 0 for value in values), "negative_pairs": sum(value < 0 for value in values),
            "bootstrap_95": [_quantile(samples, .025), _quantile(samples, .975)],
            "bootstrap_98_333": [_quantile(samples, tail), _quantile(samples, 1 - tail)],
            "familywise_interval_confidence": 1 - .05 / 3, "bonferroni_primary_comparisons": 3,
            "bootstrap_resamples": resamples, "bootstrap_seed": bootstrap_seed,
            "bootstrap_unit": "paired environment bag seed", "small_sample_descriptive": True}


def summarize(per_seed, statistics_config, gate):
    arms = {}
    for arm in ARMS:
        values = [row["arms"][arm] for row in per_seed]
        arms[arm] = {"episodes": len(values), "points": [row["points"] for row in values],
                     "mean_points": statistics.mean(row["points"] for row in values),
                     "lines": [row["lines"] for row in values], "total_lines": sum(row["lines"] for row in values),
                     "mean_lines": statistics.mean(row["lines"] for row in values),
                     "any_line_episodes": sum(row["lines"] > 0 for row in values),
                     "any_line_rate": statistics.mean(row["lines"] > 0 for row in values),
                     "top_out_episodes": sum(row["termination_reason"] == "TOP_OUT" for row in values),
                     "termination_reasons": dict(Counter(row["termination_reason"] for row in values)),
                     "steps": [row["steps"] for row in values], "total_steps": sum(row["steps"] for row in values),
                     "goal_calls": sum(row["goal_calls"] for row in values)}
    comparisons = {}
    for arm in ARMS[1:]:
        paired = [{"seed": row["seed"], "baseline_points": row["arms"]["baseline"]["points"],
                   "candidate_points": row["arms"][arm]["points"],
                   "difference": row["arms"][arm]["points"] - row["arms"]["baseline"]["points"]}
                  for row in per_seed]
        stats = contrast_stats(paired, resamples=statistics_config["bootstrap_resamples"],
                               bootstrap_seed=statistics_config["bootstrap_seed"])
        stats["screening_gate_pass"] = (stats["mean_gain"] >= gate["minimum_mean_gain"]
                                         and stats["min_gain"] >= 0
                                         and stats["bootstrap_98_333"][0] > gate["familywise_bootstrap_lower_greater_than"])
        comparisons[f"{arm}-baseline"] = stats
    interaction = contrast_stats([
        {"seed": row["seed"], "difference": row["arms"]["both"]["points"] - row["arms"]["facts"]["points"]
         - row["arms"]["memory"]["points"] + row["arms"]["baseline"]["points"]} for row in per_seed
    ], resamples=statistics_config["bootstrap_resamples"], bootstrap_seed=statistics_config["bootstrap_seed"])
    interaction["interpretation"] = "Exploratory interaction; not one of the three primary comparisons and not a promotion gate."
    return {"arms": arms, "comparisons": comparisons, "interaction": interaction}


class TetrisContextExperiment(_BaseExperiment):
    """Reuse only the old runner's file locking and bounded health/attempt helpers."""
    def __init__(self, path, *, retry_failed=False, settings=None, decision=None):
        self.path = Path(path)
        self.manifest = load(self.path / "manifest.json")
        if digest(self.manifest) != load(self.path / "manifest.sha256.json")["sha256"]:
            raise ExperimentError("Manifest changed after preregistration")
        if self.manifest["schema"] != "tetris-context-experiment/v1" or self.manifest["config"] != GAME_CONFIG:
            raise ExperimentError("Unexpected experiment schema or fixed 80-action configuration")
        if self.manifest["code_hashes"] != code_hashes():
            raise ExperimentError("Source changed since prepare; create a new experiment")
        self.settings = settings or Settings.load()
        if public_settings(self.settings) != self.manifest["providers"]:
            raise ExperimentError("Provider settings changed since prepare")
        self.retry_failed, self.adapter = retry_failed, get_game("tetris")
        self.parent = self.manifest["parent"]
        self._decision, self._coach = decision, None
        self._owned_decision, self._owned_coach, self._lock = decision is None, False, None
        self._decision_attempts = sum(row["kind"] == "decision_started" for row in read_lines(self.path / "calls.jsonl"))
        for arm in ARMS:
            harness = self.manifest["arms"][arm]
            if (harness["candidate"] != self.parent["candidate"] or harness["brief"] != self.parent["brief"]
                    or harness["tetris_context_config"] != context_config(arm)):
                raise ExperimentError("Arm changed the shared parent or its fixed context flags")

    def counted_decision(self):
        experiment = self
        if self._decision is None:
            self._decision = DecisionProvider(self.settings)

        class CountingDecision:
            settings = experiment.settings

            def decide(self, state, instructions, criteria, request_id):
                if experiment._decision_attempts >= experiment.manifest["budget"]["decision_request_attempts_max"]:
                    raise ExperimentError("Decision request attempt budget exhausted")
                purpose = "goal" if request_id.endswith(":goal") else "game_action"
                call_dir = experiment.path / "calls" / digest(request_id)[:32]
                write_new(call_dir / "request.json", {"request_id": request_id, "purpose": purpose,
                          "state": state, "instructions": instructions, "criteria": criteria,
                          "think_mode": "never", "expected_model": experiment.settings.decision_model})
                append(experiment.path / "calls.jsonl", {"kind": "decision_started", "purpose": purpose, "request_id": request_id})
                experiment._decision_attempts += 1
                try:
                    receipt = experiment._decision.decide(state, instructions, criteria, request_id)
                    write_new(call_dir / "receipt.json", receipt)
                except Exception as exc:
                    write_new(call_dir / "failed.json", {"error_type": type(exc).__name__})
                    append(experiment.path / "calls.jsonl", {"kind": "decision_failed", "purpose": purpose,
                                                             "request_id": request_id, "error_type": type(exc).__name__})
                    raise
                append(experiment.path / "calls.jsonl", {"kind": "decision_completed", "purpose": purpose,
                          "request_id": request_id, "elapsed_ms": receipt.get("elapsed_ms"),
                          "model": receipt.get("model"), "usage": receipt.get("raw_response", {}).get("usage", {})})
                return receipt
        return CountingDecision()

    def verify_episode(self, episode, harness):
        if episode.get("infra_error") or episode.get("termination_reason") in {"stopped", "infra_error"}:
            raise ExperimentError("Incomplete episode cannot be scored")
        result = _audit_episode(episode, {harness["id"]: harness})
        if result["thinking_verified"] != len(result["receipts"]):
            raise ExperimentError("Every game action must explicitly report thinking off and zero reasoning tokens")
        context = result.get("context_audit", {})
        if (context.get("game_actions") != len(result["receipts"])
                or type(context.get("goal_calls")) is not int
                or not 0 <= context["goal_calls"] <= len(result["receipts"])
                or context.get("goal_calls") != context.get("thinking_off_goal_calls")
                or context.get("actual_inputs_verified") is not True
                or context.get("memory_replay_verified") is not True):
            raise ExperimentError("Every context input and Decision-owned goal must pass replay and thinking verification")
        if not harness["tetris_context_config"]["decision_goal_memory"] and context["goal_calls"] != 0:
            raise ExperimentError("Goal calls are forbidden in memory-disabled arms")
        return {"actions": len(result["receipts"]), "thinking_off_actions": result["thinking_verified"],
                "raw_response_and_rules_replay_verified": True, "context_audit": context}

    def episode(self, logical_id, harness, seed, purpose):
        directory = self.path / "episodes" / logical_id
        for previous in sorted(directory.glob("attempt-*/complete.json")):
            evidence, episode = load(previous), load(previous.parent / "episode.json")
            if evidence["episode_hash"] != digest(episode) or evidence["harness_hash"] != digest(harness):
                raise ExperimentError("Saved episode/cache identity changed")
            if episode["seed"] != seed or episode["purpose"] != purpose or episode["harness_id"] != harness["id"]:
                raise ExperimentError("Saved trial differs from frozen schedule")
            if self.verify_episode(episode, harness) != evidence["audit"]:
                raise ExperimentError("Saved episode audit changed")
            append(self.path / "events.jsonl", {"kind": "episode_reused", "logical_id": logical_id})
            return episode
        for saved in sorted(directory.glob("attempt-*/episode.json")):
            episode = load(saved)
            if episode.get("infra_error") or episode.get("termination_reason") in {"stopped", "infra_error"}:
                continue
            if episode["seed"] != seed or episode["purpose"] != purpose or episode["harness_id"] != harness["id"]:
                raise ExperimentError("Interrupted saved trial differs from requested identity")
            audit = self.verify_episode(episode, harness)
            write_new(saved.parent / "complete.json", {"episode_hash": digest(episode), "harness_hash": digest(harness), "audit": audit})
            append(self.path / "events.jsonl", {"kind": "episode_recovered", "logical_id": logical_id})
            return episode
        attempt = self.attempt(directory, self.manifest["budget"]["max_episode_attempts_per_trial"])
        episode_id = "exp-" + digest({"experiment": self.manifest["id"], "logical_id": logical_id,
                                     "attempt": attempt.name})[:24]
        try:
            episode = run_episode(self.counted_decision(), harness, episode_id=episode_id, seed=seed,
                                  adapter=self.adapter, config=self.manifest["config"], log_path=attempt / "trace.jsonl")
            episode.update(purpose=purpose, experiment_id=self.manifest["id"], logical_trial=logical_id)
            write_new(attempt / "episode.json", episode)
            audit = self.verify_episode(episode, harness)
            write_new(attempt / "complete.json", {"episode_hash": digest(episode), "harness_hash": digest(harness), "audit": audit})
            append(self.path / "events.jsonl", {"kind": "episode_completed", "logical_id": logical_id,
                          "episode_id": episode_id, "score": episode["score"], "steps": episode["steps"]})
            return episode
        except Exception as exc:
            write_new(attempt / "failed.json", {"error_type": type(exc).__name__, "at": now()})
            raise

    def costs(self):
        events = read_lines(self.path / "calls.jsonl")
        completed = list((self.path / "episodes").glob("*/attempt-*/complete.json"))
        episodes = [load(path.parent / "episode.json") for path in completed]
        counts = {kind: sum(event["kind"] == kind for event in events) for kind in
                  ("decision_started", "decision_completed", "decision_failed", "health_started", "health_completed", "health_failed")}
        by_purpose = {purpose: {kind: sum(event["kind"] == kind and event.get("purpose") == purpose for event in events)
                               for kind in ("decision_started", "decision_completed", "decision_failed")}
                      for purpose in ("goal", "game_action")}
        usage = defaultdict(float)
        for event in events:
            for key, value in event.get("usage", {}).items():
                if type(value) in (int, float):
                    usage[key] += value
        return {**counts, "by_purpose": by_purpose, "completed_episodes": len(episodes),
                "accepted_game_actions": sum(episode["steps"] for episode in episodes),
                "episode_attempts": len(list((self.path / "episodes").glob("*/attempt-*/started.json"))),
                "decision_started_without_response": counts["decision_started"] - counts["decision_completed"] - counts["decision_failed"],
                "reported_usage": dict(usage), "coach_calls": 0, "currency_cost": "not available; no price assumed"}

    @staticmethod
    def _episode_row(episode):
        return {"episode_id": episode["episode_id"], "points": episode["score"],
                "lines": episode["final_state"]["metrics"]["lines"], "steps": episode["steps"],
                "termination_reason": episode["termination_reason"],
                "goal_calls": sum(call["purpose"] == "goal" for step in episode["trace"]
                                  for call in step["receipt"]["tetris_context"]["calls"])}

    def _run_phase(self, phase):
        result_path = self.path / f"{phase}_result.json"
        saved = None
        hash_path = result_path.with_suffix(".sha256.json")
        recover_result_hash = False
        if result_path.exists():
            saved = load(result_path)
            recover_result_hash = not hash_path.exists()
            if not recover_result_hash and digest(saved) != load(hash_path)["sha256"]:
                raise ExperimentError("Saved phase result changed")
        self.ensure_health()
        per_seed = []
        for scenario in self.manifest["scenarios"][phase]:
            row = {**deepcopy(scenario), "arms": {}}
            for arm in scenario["order"]:
                harness = self.manifest["arms"][arm]
                episode = self.episode(f"{scenario['id']}-{arm}", harness, scenario["seed"], phase)
                row["arms"][arm] = self._episode_row(episode)
            per_seed.append(row)
        summary = summarize(per_seed, self.manifest["statistics"], self.manifest["screening_gate"])
        if phase == "calibration":
            for stats in summary["comparisons"].values():
                stats["screening_gate_pass"] = False
                stats["screening_gate_applicable"] = False
        result = {"status": "completed", "phase": phase, "per_seed": per_seed, **summary,
                  "candidate_hash": self.manifest["candidate_hash"], "brief_hash": self.manifest["brief_hash"],
                  "published": False, "registry_writes": False, "policy_changed": False,
                  "costs": self.costs(), "evaluation_allowed": phase == "calibration",
                  "interpretation": "Calibration checks integration only; evaluation is mandatory regardless of these scores."
                  if phase == "calibration" else "Fixed-policy factorial screening on twelve fresh bag seeds; no automatic promotion."}
        if saved is not None:
            for key in ("per_seed", "arms", "comparisons", "interaction", "candidate_hash", "brief_hash", "published"):
                if saved[key] != result[key]:
                    raise ExperimentError("Saved phase summary differs from audited episodes")
            if recover_result_hash:
                # A crash may persist the result before its hash. Only a full
                # deterministic reconstruction from audited cached games can
                # complete that write, without buying any replacement games.
                if saved != result:
                    raise ExperimentError("Unhashed phase result differs from full reconstruction")
                write_new(hash_path, {"sha256": digest(saved)})
            return saved
        write_new(result_path, result)
        write_new(hash_path, {"sha256": digest(result)})
        return result

    def run_calibration(self):
        return self._run_phase("calibration")

    def run_evaluation(self):
        path = self.path / "calibration_result.json"
        if not path.exists() or load(path).get("status") != "completed":
            raise ExperimentError("Complete run-calibration first; no score threshold applies")
        self._run_phase("calibration")  # Re-audit cached integration evidence, no new games.
        return self._run_phase("evaluation")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("prepare")
    command.add_argument("--id", required=True)
    command.add_argument("--parent", type=Path, required=True)
    command.add_argument("--seed", type=int, default=DEFAULT_SEED)
    for name in ("run-calibration", "run-evaluation"):
        command = sub.add_parser(name)
        command.add_argument("--id", required=True)
        command.add_argument("--retry-failed", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare(identity=args.id, parent_path=args.parent, seed=args.seed)
            output = {"prepared": True, "id": args.id, "budget": result["budget"], "path": str(experiment_path(args.id))}
        else:
            with TetrisContextExperiment(experiment_path(args.id), retry_failed=args.retry_failed) as experiment:
                result = experiment.run_calibration() if args.command == "run-calibration" else experiment.run_evaluation()
            output = {key: result[key] for key in ("status", "phase", "arms", "published", "costs")}
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(f"Experiment stopped ({type(exc).__name__}): " + (str(exc) if isinstance(exc, ExperimentError)
              else "inspect saved attempt evidence"), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
