#!/usr/bin/env python3
"""Frozen V6 package comparison: unchanged V5 both versus LLM repair + control facts.

2 fresh calibration bags x 2 arms, then 24 disjoint evaluation bags x 2 arms.
All target and native action choices use Decision with thinking disabled. No
model calls occur during prepare; no coaching, registry writes or promotion.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import hashlib
import math
from pathlib import Path
import random
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
from run_feedback_experiment import (ExperimentError, canonical, digest, experiment_path,
    load, now, public_settings, read_lines, snapshot_sources, write_new)
from run_tetris_context_experiment import (TetrisContextExperiment, prior_tetris_seeds,
    context_config as v5_context_config)
from game_harness.games import get_game
from game_harness.providers import Settings, validate_candidate

ARMS = ("baseline", "repaired")
GAME_CONFIG = {"max_steps": 80}
DEFAULT_SEED = 2026101706
CONTROL_KEYS = ("locked_pieces", "same_piece_horizontal_reversals", "horizontal_actions",
                "horizontal_reversal_opportunities", "strict_overshoots", "strict_overshoot_opportunities",
                "goal_already_reached_before_actions")


def context_config(arm):
    if arm == "baseline":
        return v5_context_config("both")
    if arm == "repaired":
        return {**v5_context_config("both"), "schema": "tetris-context-v2", "control_feedback": True}
    raise ExperimentError("Unknown Tetris control arm")


def code_hashes():
    paths = [path for path in (ROOT / "src").rglob("*")
             if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"]
    paths += [ROOT / name for name in (
        "scripts/run_tetris_control_experiment.py", "scripts/report_tetris_control_experiment.py",
        "scripts/repair_tetris_control_harness.py", "scripts/run_tetris_control_diagnostics.py",
        "scripts/run_tetris_context_experiment.py", "scripts/report_tetris_context_experiment.py",
        "scripts/run_feedback_experiment.py", "scripts/audit_saved_run.py", "pyproject.toml")]
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(set(paths))}


def scenario_plan(seed, excluded=()):
    rng, used, phases = random.Random(seed), set(excluded), {}
    for phase, count in (("calibration", 2), ("evaluation", 24)):
        rows = []
        for index in range(count):
            value = rng.randrange(1, 2**31 - 1)
            while value in used:
                value = rng.randrange(1, 2**31 - 1)
            used.add(value)
            rows.append({"id": f"{phase}-{index:02d}", "seed": value,
                         "order": list(ARMS if index % 2 == 0 else ARMS[::-1])})
        phases[phase] = rows
    return phases


def _parent_and_repair(parent_path, repaired_path, settings):
    parent, repaired = load(parent_path), load(repaired_path)
    context = get_game("tetris").context(GAME_CONFIG)
    for name, source in (("Parent", parent), ("Repair", repaired)):
        if source.get("game_id", "tetris") != "tetris":
            raise ExperimentError(f"{name} must be a Tetris Harness")
        if source.get("game_context") != context:
            raise ExperimentError(f"{name} must use current Tetris rules and the fixed 80-action budget")
        if (source.get("decision_model", settings.decision_model) != settings.decision_model
                or source.get("think_mode", "never") != "never"):
            raise ExperimentError(f"{name} model or thinking mode differs")
        source["candidate"] = validate_candidate(source.get("candidate", {}))
    if not isinstance(parent.get("brief"), dict) or not parent["brief"]:
        raise ExperimentError("Shared initialized brief is required")
    if repaired.get("brief") != parent["brief"]:
        raise ExperimentError("Repair must preserve the exact parent brief")
    if parent.get("tetris_context_config", context_config("baseline")) != context_config("baseline"):
        raise ExperimentError("Parent must be the unchanged V5 both condition")
    if repaired.get("tetris_context_config", context_config("repaired")) != context_config("repaired"):
        raise ExperimentError("Repair context differs from fixed v2 control condition")
    if parent["candidate"] == repaired["candidate"]:
        raise ExperimentError("Repair must contain an actually revised candidate")
    for source in (parent, repaired):
        source.update(game_id="tetris", game_context=context,
                      decision_model=settings.decision_model, think_mode="never")
        source.setdefault("id", "control-parent-" + digest(source["candidate"])[:16])
    return parent, repaired, context


def prepare(*, identity, parent_path, repaired_path, seed=DEFAULT_SEED, root=None, data_root=None, settings=None):
    settings = settings or Settings.load()
    if type(seed) is not int:
        raise ExperimentError("Study schedule seed must be an integer")
    parent, repaired, context = _parent_and_repair(parent_path, repaired_path, settings)
    excluded, evidence = prior_tetris_seeds(data_root or ROOT / "data")
    harnesses = {}
    for arm, source in (("baseline", parent), ("repaired", repaired)):
        harnesses[arm] = {**deepcopy(source), "id": "exp-v6-" + arm + "-" + digest({"source": source, "arm": arm})[:16],
                          "parent_id": parent["id"], "tetris_context_config": context_config(arm)}
    manifest = {
        "schema": "tetris-control-experiment/v1", "id": identity, "created_at": now(),
        "game_id": "tetris", "config": deepcopy(GAME_CONFIG), "game_context": context,
        "parent": parent, "repaired_source": repaired,
        "parent_hash": digest(parent), "repaired_hash": digest(repaired),
        "candidate_hashes": {arm: digest(h["candidate"]) for arm, h in harnesses.items()},
        "brief_hash": digest(parent["brief"]), "arms": harnesses, "arm_order": list(ARMS),
        "providers": public_settings(settings), "code_hashes": code_hashes(), "seed": seed,
        "scenarios": scenario_plan(seed, excluded), "excluded_environment_seeds": sorted(excluded),
        "prior_seed_evidence": evidence,
        "statistics": {"bootstrap_seed": 74106, "bootstrap_resamples": 10000,
                       "bootstrap_unit": "one paired bag seed; both arms stay together",
                       "primary_comparisons": ["repaired-baseline"],
                       "primary_metric": "native line-clear points", "confidence_level": .95,
                       "secondary_metrics": ["native cleared lines", "any-line paired risk difference",
                                             "TOP_OUT", "control diagnostics", "model calls and latency"]},
        "screening_gate": {"minimum_mean_gain": 100, "no_pair_regression": True,
                           "bootstrap_lower_greater_than": 0},
        "budget": {"calibration_episodes": 4, "evaluation_episodes": 48, "completed_episodes": 52,
                   "max_game_actions_per_episode": 80, "max_calls_per_action": 2,
                   "decision_request_attempts_without_episode_retries_max": 8320,
                   "max_episode_attempts_per_trial": 2, "decision_request_attempts_max": 16640,
                   "health_preflights_without_retries": 1, "health_preflight_attempts_max": 2,
                   "coach_calls": 0,
                   "retry_definition": "One original plus at most one explicitly requested retry per failed logical episode. Completed episodes are never redrawn. Health is separate."},
        "constraints": {"publish": False, "registry_writes": False, "automatic_export": False,
                        "gepa": False, "in_game_coach": False, "arm_policy_text_changes": False,
                        "scoring_early_stop": False, "score_dependent_extra_trials": False,
                        "calibration_is_independent_test": False,
                        "evaluation_requires_calibration_complete_only": True,
                        "failed_attempts": "Stop on infrastructure/audit error; retain evidence; never score failure as zero."},
        "limitations": ["A package comparison of revised strategy plus control facts; it does not isolate either causal component.",
                        "The baseline retains the V5 v1 both contract and policy; no common v2 instruction is appended to baseline.",
                        "All development and LLM repairs finish before this study; no scores can alter policy, sample count or seeds.",
                        "Twenty-four unique bags are a small screening sample; bootstrap intervals are descriptive and each bag has only one run per arm.",
                        "Opaque episode IDs differ; provider nondeterminism and checkpoint drift remain possible.",
                        "Fresh seed IDs are not a guarantee of distinct full bag sequences.",
                        "Passing the screening gate never exports, publishes or promotes a research Harness.",
                        "LLM repair and control microtasks are separate development costs, not complete-game trial evidence."],
    }
    path = experiment_path(identity, root)
    path.mkdir(parents=True, exist_ok=False)
    snapshot_sources(path / "source_snapshot", manifest["code_hashes"])
    write_new(path / "manifest.json", manifest)
    write_new(path / "manifest.sha256.json", {"sha256": digest(manifest)})
    write_new(path / "parent.json", parent)
    write_new(path / "repaired.json", repaired)
    (path / "README.md").write_text(
        f"# {identity}\n\nFrozen package comparison: unchanged V5 both versus LLM repair + control feedback.\n\n"
        "Calibration: 2 bags x 2 arms. Evaluation: 24 disjoint bags x 2 arms, irrespective of scores. "
        "Each game has 80 native actions; all targets and keys use Decision never. Shared brief/rules. "
        "No publication, registry writes, coaching or mid-study policy changes.\n\n"
        f"```sh\n.venv/bin/python scripts/run_tetris_control_experiment.py run-calibration --id {identity}\n"
        f".venv/bin/python scripts/run_tetris_control_experiment.py run-evaluation --id {identity}\n```\n",
        encoding="utf-8")
    return manifest


def _quantile(values, fraction):
    position = (len(values) - 1) * fraction
    lower, upper = math.floor(position), math.ceil(position)
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def contrast_stats(rows, *, resamples=10000, bootstrap_seed=74106):
    if not rows or len({row["seed"] for row in rows}) != len(rows):
        raise ExperimentError("Contrasts require one pair per unique bag seed")
    if type(resamples) is not int or resamples < 1:
        raise ExperimentError("Bootstrap resamples must be positive")
    values = [row["difference"] for row in rows]
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in values):
        raise ExperimentError("Contrast differences must be finite")
    rng = random.Random(bootstrap_seed)
    samples = sorted(statistics.mean(rng.choices(values, k=len(values))) for _ in range(resamples))
    return {"pairs": deepcopy(rows), "pair_count": len(rows), "unique_seed_count": len(rows),
            "mean_gain": statistics.mean(values), "min_gain": min(values),
            "positive_pairs": sum(v > 0 for v in values), "negative_pairs": sum(v < 0 for v in values),
            "bootstrap_95": [_quantile(samples, .025), _quantile(samples, .975)],
            "bootstrap_resamples": resamples, "bootstrap_seed": bootstrap_seed,
            "bootstrap_unit": "paired environment bag seed", "small_sample_descriptive": True}


def _ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def summarize(per_seed, statistics_config, gate):
    arms = {}
    for arm in ARMS:
        values = [row["arms"][arm] for row in per_seed]
        diagnostics = {key: sum(v.get("control_diagnostics", {}).get(key, 0) for v in values) for key in CONTROL_KEYS}
        arms[arm] = {"episodes": len(values), "points": [v["points"] for v in values],
                     "mean_points": statistics.mean(v["points"] for v in values),
                     "lines": [v["lines"] for v in values], "total_lines": sum(v["lines"] for v in values),
                     "mean_lines": statistics.mean(v["lines"] for v in values),
                     "any_line_episodes": sum(v["lines"] > 0 for v in values),
                     "any_line_rate": statistics.mean(v["lines"] > 0 for v in values),
                     "top_out_episodes": sum(v["termination_reason"] == "TOP_OUT" for v in values),
                     "termination_reasons": dict(Counter(v["termination_reason"] for v in values)),
                     "steps": [v["steps"] for v in values], "total_steps": sum(v["steps"] for v in values),
                     "goal_calls": sum(v["goal_calls"] for v in values), **diagnostics}
        arms[arm].update(horizontal_reversal_rate=_ratio(diagnostics["same_piece_horizontal_reversals"], diagnostics["horizontal_reversal_opportunities"]),
                         strict_overshoot_rate=_ratio(diagnostics["strict_overshoots"], diagnostics["strict_overshoot_opportunities"]),
                         steps_per_locked_piece=_ratio(arms[arm]["total_steps"], diagnostics["locked_pieces"]))
    paired = [{"seed": row["seed"], "baseline_points": row["arms"]["baseline"]["points"],
               "candidate_points": row["arms"]["repaired"]["points"],
               "difference": row["arms"]["repaired"]["points"] - row["arms"]["baseline"]["points"]} for row in per_seed]
    options = {"resamples": statistics_config["bootstrap_resamples"], "bootstrap_seed": statistics_config["bootstrap_seed"]}
    stats = contrast_stats(paired, **options)
    stats["screening_gate_pass"] = (stats["mean_gain"] >= gate["minimum_mean_gain"] and stats["min_gain"] >= 0
                                     and stats["bootstrap_95"][0] > gate["bootstrap_lower_greater_than"])
    binary = [{"seed": row["seed"], "baseline_any_line": row["arms"]["baseline"]["lines"] > 0,
               "repaired_any_line": row["arms"]["repaired"]["lines"] > 0,
               "difference": int(row["arms"]["repaired"]["lines"] > 0) - int(row["arms"]["baseline"]["lines"] > 0)} for row in per_seed]
    risk = contrast_stats(binary, **options)
    n01, n10 = risk["positive_pairs"], risk["negative_pairs"]
    discordant = n01 + n10
    risk.update(n01_baseline_no_repaired_yes=n01, n10_baseline_yes_repaired_no=n10,
                unit="proportion difference, not percentage points",
                exact_mcnemar_two_sided_p=min(1., 2 * sum(math.comb(discordant, k) for k in range(min(n01, n10) + 1)) / 2**discordant) if discordant else 1.,
                interpretation="Secondary exploratory stability metric; no multiplicity-adjusted confirmation claim.")
    return {"arms": arms, "comparisons": {"repaired-baseline": stats}, "any_line_comparison": risk}


class TetrisControlExperiment(TetrisContextExperiment):
    """Reuse only frozen evidence/call/attempt machinery, not V5 arm semantics."""
    def __init__(self, path, *, retry_failed=False, settings=None, decision=None):
        self.path = Path(path)
        self.manifest = load(self.path / "manifest.json")
        if digest(self.manifest) != load(self.path / "manifest.sha256.json")["sha256"]:
            raise ExperimentError("Manifest changed after preregistration")
        if self.manifest["schema"] != "tetris-control-experiment/v1" or self.manifest["config"] != GAME_CONFIG:
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
        if self.manifest["arm_order"] != list(ARMS):
            raise ExperimentError("Frozen arm order changed")
        for arm, source in (("baseline", self.parent), ("repaired", self.manifest["repaired_source"])):
            harness = self.manifest["arms"][arm]
            if (harness["candidate"] != source["candidate"] or harness["brief"] != self.parent["brief"]
                    or digest(harness["candidate"]) != self.manifest["candidate_hashes"][arm]
                    or harness["game_context"] != self.manifest["game_context"]
                    or harness["tetris_context_config"] != context_config(arm)):
                raise ExperimentError("Arm differs from its frozen strategy or context")

    @staticmethod
    def _episode_row(episode):
        from report_tetris_control_experiment import episode_diagnostics
        row = TetrisContextExperiment._episode_row(episode)
        diagnostics = episode_diagnostics(episode)
        row["control_diagnostics"] = {key: diagnostics[key] for key in CONTROL_KEYS}
        return row

    def _run_phase(self, phase):
        result_path = self.path / f"{phase}_result.json"
        hash_path = result_path.with_suffix(".sha256.json")
        saved = load(result_path) if result_path.exists() else None
        recover = saved is not None and not hash_path.exists()
        if saved is not None and not recover and digest(saved) != load(hash_path)["sha256"]:
            raise ExperimentError("Saved phase result changed")
        self.ensure_health()
        per_seed = []
        for scenario in self.manifest["scenarios"][phase]:
            row = {**deepcopy(scenario), "arms": {}}
            for arm in scenario["order"]:
                episode = self.episode(f"{scenario['id']}-{arm}", self.manifest["arms"][arm], scenario["seed"], phase)
                row["arms"][arm] = self._episode_row(episode)
            per_seed.append(row)
        summary = summarize(per_seed, self.manifest["statistics"], self.manifest["screening_gate"])
        if phase == "calibration":
            for stats in summary["comparisons"].values():
                stats.update(screening_gate_pass=False, screening_gate_applicable=False)
        result = {"status": "completed", "phase": phase, "per_seed": per_seed, **summary,
                  "candidate_hashes": self.manifest["candidate_hashes"], "brief_hash": self.manifest["brief_hash"],
                  "published": False, "registry_writes": False, "policy_changed_during_study": False,
                  "costs": self.costs(), "evaluation_allowed": phase == "calibration",
                  "interpretation": "Engineering calibration only; evaluation proceeds regardless of scores."
                  if phase == "calibration" else "Frozen package comparison on 24 fresh paired bags; no automatic promotion."}
        if saved is not None:
            for key in ("per_seed", "arms", "comparisons", "any_line_comparison", "candidate_hashes", "brief_hash", "published"):
                if saved[key] != result[key]:
                    raise ExperimentError("Saved phase summary differs from audited episodes")
            if recover:
                if saved != result:
                    raise ExperimentError("Unhashed phase result differs from full reconstruction")
                write_new(hash_path, {"sha256": digest(saved)})
            return saved
        write_new(result_path, result)
        write_new(hash_path, {"sha256": digest(result)})
        return result


def main(argv=None):
    import json
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("prepare")
    command.add_argument("--id", required=True)
    command.add_argument("--parent", type=Path, required=True)
    command.add_argument("--repaired", type=Path, required=True)
    command.add_argument("--seed", type=int, default=DEFAULT_SEED)
    for name in ("run-calibration", "run-evaluation"):
        command = sub.add_parser(name)
        command.add_argument("--id", required=True)
        command.add_argument("--retry-failed", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare(identity=args.id, parent_path=args.parent, repaired_path=args.repaired, seed=args.seed)
            output = {"prepared": True, "id": args.id, "budget": result["budget"], "path": str(experiment_path(args.id))}
        else:
            with TetrisControlExperiment(experiment_path(args.id), retry_failed=args.retry_failed) as experiment:
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
