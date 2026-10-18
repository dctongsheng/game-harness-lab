#!/usr/bin/env python3
"""Frozen visual ablation: same V6 Harness/context; only the image differs.

2 paired engineering calibration seeds then 24 new paired evaluation seeds.
No registry writes, policy selection, coaching, GEPA, or automatic promotion.
All target/action choices remain Decision with thinking disabled.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import random
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
from run_feedback_experiment import (ExperimentError, append, digest, experiment_path, load,
    now, public_settings, read_lines, snapshot_sources, write_new)
from run_tetris_context_experiment import TetrisContextExperiment, prior_tetris_seeds
from run_tetris_control_experiment import context_config as control_context_config, contrast_stats
from report_tetris_control_experiment import episode_diagnostics, CONTROL_KEYS
from game_harness.games import get_game
from game_harness.providers import DecisionProvider, Settings, validate_candidate
from game_harness.runtime import run_episode

ARMS = ("text", "vision")
GAME_CONFIG = {"max_steps": 80}
DEFAULT_SEED = 2026101807


def context_config():
    return control_context_config("repaired")


def code_hashes():
    paths = [p for p in (ROOT / "src").rglob("*")
             if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"]
    paths += [ROOT / name for name in (
        "scripts/run_tetris_vision_experiment.py", "scripts/report_tetris_vision_experiment.py",
        "scripts/run_tetris_control_experiment.py", "scripts/report_tetris_control_experiment.py",
        "scripts/run_tetris_context_experiment.py", "scripts/run_feedback_experiment.py",
        "scripts/audit_saved_run.py", "pyproject.toml")]
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(set(paths))}


def runtime_dependencies():
    return {name: version(name) for name in ("Pillow", "httpx")}


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


def shared_episode_id(identity, scenario_id, attempt=None):
    """Arm is deliberately absent from model-visible episode/request metadata."""
    return "exp-" + digest({"experiment": identity, "scenario": scenario_id})[:24]


def prepare(*, identity, parent_path, seed=DEFAULT_SEED, root=None, data_root=None, settings=None):
    settings = settings or Settings.load()
    if type(seed) is not int:
        raise ExperimentError("Study schedule seed must be an integer")
    source = load(parent_path)
    context = get_game("tetris").context(GAME_CONFIG)
    if source.get("game_id", "tetris") != "tetris" or source.get("game_context") != context:
        raise ExperimentError("Parent must match current Tetris rules and fixed 80-action budget")
    if not isinstance(source.get("brief"), dict) or not source["brief"]:
        raise ExperimentError("Frozen initialized brief is required")
    if (source.get("decision_model", settings.decision_model) != settings.decision_model
            or source.get("think_mode", "never") != "never"):
        raise ExperimentError("Parent model or thinking mode differs")
    if source.get("tetris_context_config") != context_config():
        raise ExperimentError("Parent must be the frozen V6 control-feedback condition")
    candidate = validate_candidate(source.get("candidate", {}))
    if runtime_dependencies()["Pillow"] != "12.0.0":
        raise ExperimentError("Visual rendering requires frozen Pillow 12.0.0")
    parent = {**deepcopy(source), "candidate": candidate, "game_id": "tetris", "game_context": context,
              "decision_model": settings.decision_model, "think_mode": "never"}
    parent.setdefault("id", "vision-parent-" + digest(candidate)[:16])
    excluded, evidence = prior_tetris_seeds(data_root or ROOT / "data")
    hashes = code_hashes()
    manifest = {
        "schema": "tetris-vision-experiment/v1", "id": identity, "created_at": now(), "game_id": "tetris",
        "config": deepcopy(GAME_CONFIG), "game_context": context,
        "parent_source": {"path": str(Path(parent_path).resolve()),
                          "file_sha256": hashlib.sha256(Path(parent_path).read_bytes()).hexdigest()},
        "parent": parent, "parent_hash": digest(parent), "candidate_hash": digest(candidate),
        "brief_hash": digest(parent["brief"]), "arms": {arm: deepcopy(parent) for arm in ARMS},
        "arm_order": list(ARMS), "providers": public_settings(settings), "code_hashes": hashes, "runtime_dependencies": runtime_dependencies(),
        "seed": seed, "scenarios": scenario_plan(seed, excluded),
        "excluded_environment_seeds": sorted(excluded), "prior_seed_evidence": evidence,
        "treatment": {"text": "Existing structured board + Decision goal memory + V6 control facts",
                      "vision": "Exact same text/context/catalog/strategy plus current board image on every goal and action request",
                      "shared_model_visible_episode_metadata": True,
                      "image_source": "Current native observation only; no policy recommendation or future-state simulation",
                      "image_archive": "Deduplicated original PNG bytes by SHA256; every request and receipt bound to image hash"},
        "statistics": {"bootstrap_seed": 74107, "bootstrap_resamples": 10000,
                       "bootstrap_unit": "paired bag seed; both arms sampled together", "confidence_level": .95,
                       "primary_comparisons": ["vision-text"], "primary_metric": "native line-clear points",
                       "secondary_metrics": ["native cleared lines", "any-line paired risk difference", "TOP_OUT", "latency"]},
        "screening_gate": {"bootstrap_lower_greater_than": 0, "positive_mean_gain": True,
                           "automatic_promotion": False},
        "budget": {"calibration_episodes": 4, "evaluation_episodes": 48, "completed_episodes": 52,
                   "max_game_actions_per_episode": 80, "max_calls_per_action": 2,
                   "decision_request_attempts_without_episode_retries_max": 8320,
                   "max_episode_attempts_per_trial": 2, "decision_request_attempts_max": 16640,
                   "health_preflight_attempts_max": 2, "coach_calls": 0,
                   "retry_definition": "Only infrastructure failures may receive one explicitly requested retry; completed games never redraw."},
        "constraints": {"publish": False, "registry_writes": False, "automatic_export": False,
                        "gepa": False, "in_game_coach": False, "arm_policy_text_changes": False,
                        "scoring_early_stop": False, "score_dependent_extra_trials": False,
                        "calibration_is_independent_test": False,
                        "evaluation_requires_calibration_complete_only": True,
                        "failed_attempts": "Stop; preserve error evidence; never score a failed episode as zero."},
        "limitations": ["Same seed and same model-visible metadata isolate the added image; trajectories may diverge after choices.",
                        "Provider nondeterminism, checkpoint drift and extra visual token cost remain possible.",
                        "Calibration cannot select strategy or change seeds, sample count, scoring, input contract or stopping rules.",
                        "Twenty-four paired bags are a small screening sample; confidence interval containing zero cannot establish a clear improvement.",
                        "No promotion even when the descriptive screening interval is positive.",
                        "Fresh seed IDs do not guarantee all generated piece sequences are unique."],
    }
    path = experiment_path(identity, root)
    path.mkdir(parents=True, exist_ok=False)
    snapshot_sources(path / "source_snapshot", hashes)
    write_new(path / "manifest.json", manifest)
    write_new(path / "manifest.sha256.json", {"sha256": digest(manifest)})
    write_new(path / "parent.json", parent)
    (path / "README.md").write_text(f"# {identity}\n\nFrozen image-only ablation, 2 calibration pairs + 24 evaluation pairs. "
        "Each game has 80 native actions. Same policy, brief, catalogs, structured context and model-visible episode metadata. "
        "All goals/actions use Decision never. Calls and episodes are separated by arm; original image bytes are hash-deduplicated. "
        "No registry, coaching, GEPA or automatic publication.\n", encoding="utf-8")
    return manifest


def summarize(per_seed, statistics_config, gate=None):
    if not per_seed:
        raise ExperimentError("Summary requires completed paired games")
    arms = {}
    for arm in ARMS:
        values = [row["arms"][arm] for row in per_seed]
        arms[arm] = {"episodes": len(values), "points": [v["points"] for v in values],
                     "mean_points": statistics.mean(v["points"] for v in values),
                     "lines": [v["lines"] for v in values], "total_lines": sum(v["lines"] for v in values),
                     "mean_lines": statistics.mean(v["lines"] for v in values),
                     "any_line_episodes": sum(v["lines"] > 0 for v in values),
                     "any_line_rate": statistics.mean(v["lines"] > 0 for v in values),
                     "top_out_episodes": sum(v["termination_reason"] == "TOP_OUT" for v in values),
                     "termination_reasons": dict(Counter(v["termination_reason"] for v in values)),
                     "total_steps": sum(v["steps"] for v in values), "goal_calls": sum(v["goal_calls"] for v in values),
                     **{key: sum(v.get("control_diagnostics", {}).get(key, 0) for v in values) for key in CONTROL_KEYS}}
    options = {"resamples": statistics_config["bootstrap_resamples"], "bootstrap_seed": statistics_config["bootstrap_seed"]}
    stats = contrast_stats([{"seed": row["seed"], "text_points": row["arms"]["text"]["points"],
                            "vision_points": row["arms"]["vision"]["points"],
                            "difference": row["arms"]["vision"]["points"] - row["arms"]["text"]["points"]}
                           for row in per_seed], **options)
    stats["screening_gate_pass"] = stats["mean_gain"] > 0 and stats["bootstrap_95"][0] > 0
    risk = contrast_stats([{"seed": row["seed"], "difference": int(row["arms"]["vision"]["lines"] > 0)
                            - int(row["arms"]["text"]["lines"] > 0)} for row in per_seed], **options)
    risk["interpretation"] = "Secondary exploratory proportion difference, not percentage points; no independent confirmation claim."
    return {"arms": arms, "comparisons": {"vision-text": stats}, "any_line_comparison": risk}


class TetrisVisionExperiment(TetrisContextExperiment):
    def __init__(self, path, *, retry_failed=False, settings=None, decision=None, provider_factory=None):
        self.path, self.settings = Path(path), settings or Settings.load()
        self.manifest = load(self.path / "manifest.json")
        if digest(self.manifest) != load(self.path / "manifest.sha256.json")["sha256"]:
            raise ExperimentError("Manifest changed after preregistration")
        if self.manifest["schema"] != "tetris-vision-experiment/v1" or self.manifest["config"] != GAME_CONFIG:
            raise ExperimentError("Unexpected study schema or fixed action budget")
        if self.manifest["code_hashes"] != code_hashes():
            raise ExperimentError("Source changed since prepare; create a new experiment")
        if runtime_dependencies() != self.manifest["runtime_dependencies"]:
            raise ExperimentError("Runtime dependencies changed since prepare")
        if public_settings(self.settings) != self.manifest["providers"]:
            raise ExperimentError("Provider settings changed since prepare")
        self.parent = self.manifest["parent"]
        if (self.manifest["arm_order"] != list(ARMS) or digest(self.parent) != self.manifest["parent_hash"]
                or any(h != self.parent for h in self.manifest["arms"].values())):
            raise ExperimentError("Both arms must retain the exact shared Harness")
        self.retry_failed, self.adapter = retry_failed, get_game("tetris")
        self._decision, self._coach = decision, None
        self._owned_decision, self._owned_coach, self._lock = decision is None, False, None
        self._decision_attempts = sum(row["kind"] == "decision_started" for row in read_lines(self.path / "calls.jsonl"))
        self.provider_factory, self._providers = provider_factory, {}

    def __exit__(self, *args):
        try:
            for provider in self._providers.values():
                provider.close()
        finally:
            super().__exit__(*args)

    def _provider(self, arm, scenario_id, attempt):
        key = (arm, scenario_id, attempt)
        archive_dir = self.path / "calls" / arm / scenario_id / attempt
        if key not in self._providers:
            if self.provider_factory:
                provider = self.provider_factory(arm, archive_dir, self.path / "images")
            else:
                from game_harness.vision_provider import VisionDecisionProvider
                provider = VisionDecisionProvider(self.settings, enabled=arm == "vision",
                    archive_dir=archive_dir, image_store=self.path / "images")
            self._providers[key] = provider
        return self._providers[key]

    def counted_decision(self, arm, scenario_id, attempt):
        experiment, provider = self, self._provider(arm, scenario_id, attempt)
        class CountingDecision:
            settings = experiment.settings
            def decide(self, state, instructions, criteria, request_id):
                if experiment._decision_attempts >= experiment.manifest["budget"]["decision_request_attempts_max"]:
                    raise ExperimentError("Decision request attempt budget exhausted")
                purpose = "goal" if request_id.endswith(":goal") else "game_action"
                directory = experiment.path / "calls" / arm / scenario_id / attempt / digest(request_id)[:32]
                write_new(directory / "request.json", {"request_id": request_id, "purpose": purpose,
                    "state": state, "instructions": instructions, "criteria": criteria,
                    "think_mode": "never", "expected_model": experiment.settings.decision_model})
                append(experiment.path / "calls.jsonl", {"kind": "decision_started", "arm": arm, "scenario_id": scenario_id, "attempt": attempt,
                        "purpose": purpose, "request_id": request_id})
                experiment._decision_attempts += 1
                try:
                    receipt = provider.decide(state, instructions, criteria, request_id)
                    if (directory / "receipt.json").exists():
                        if load(directory / "receipt.json") != receipt:
                            raise ExperimentError("Provider archive differs from returned receipt")
                    else:
                        write_new(directory / "receipt.json", receipt)
                except Exception as exc:
                    write_new(directory / "failed.json", {"error_type": type(exc).__name__})
                    append(experiment.path / "calls.jsonl", {"kind": "decision_failed", "arm": arm, "scenario_id": scenario_id, "attempt": attempt,
                        "purpose": purpose, "request_id": request_id, "error_type": type(exc).__name__})
                    raise
                append(experiment.path / "calls.jsonl", {"kind": "decision_completed", "arm": arm, "scenario_id": scenario_id, "attempt": attempt,
                    "purpose": purpose, "request_id": request_id, "elapsed_ms": receipt.get("elapsed_ms"),
                    "model": receipt.get("model"), "usage": receipt.get("raw_response", {}).get("usage", {})})
                return receipt
        return CountingDecision()

    def verify_episode(self, episode, harness):
        audit = super().verify_episode(episode, harness)
        from report_tetris_vision_experiment import verify_call_archive
        matched, images = verify_call_archive(self.path, episode, harness, episode["experiment_arm"])
        return {**audit, "full_requests_verified": len(matched), "image_requests_verified": len(images)}

    def episode(self, scenario_id, arm, seed, purpose):
        harness = self.manifest["arms"][arm]
        directory = self.path / "episodes" / arm / scenario_id
        for saved in sorted(directory.glob("attempt-*/episode.json")):
            episode = load(saved)
            if episode.get("infra_error") or episode.get("termination_reason") in {"stopped", "infra_error"}:
                continue
            expected_id = shared_episode_id(self.manifest["id"], scenario_id, saved.parent.name)
            if (episode["seed"] != seed or episode["purpose"] != purpose or episode["harness_id"] != harness["id"]
                    or episode["episode_id"] != expected_id or episode.get("experiment_arm") != arm
                    or episode.get("experiment_attempt") != saved.parent.name):
                raise ExperimentError("Saved episode differs from frozen trial identity")
            audit = self.verify_episode(episode, harness)
            evidence = {"episode_hash": digest(episode), "harness_hash": digest(harness), "audit": audit}
            complete = saved.parent / "complete.json"
            if complete.exists():
                if load(complete) != evidence:
                    raise ExperimentError("Saved episode/cache evidence changed")
            else:
                write_new(complete, evidence)
            append(self.path / "events.jsonl", {"kind": "episode_reused", "scenario_id": scenario_id, "arm": arm})
            return episode
        attempt = self.attempt(directory, self.manifest["budget"]["max_episode_attempts_per_trial"])
        episode_id = shared_episode_id(self.manifest["id"], scenario_id, attempt.name)
        try:
            episode = run_episode(self.counted_decision(arm, scenario_id, attempt.name), harness, episode_id=episode_id, seed=seed,
                adapter=self.adapter, config=self.manifest["config"], log_path=attempt / "trace.jsonl")
            episode.update(purpose=purpose, experiment_id=self.manifest["id"], logical_trial=scenario_id, experiment_arm=arm, experiment_attempt=attempt.name)
            write_new(attempt / "episode.json", episode)
            audit = self.verify_episode(episode, harness)
            write_new(attempt / "complete.json", {"episode_hash": digest(episode), "harness_hash": digest(harness), "audit": audit})
            append(self.path / "events.jsonl", {"kind": "episode_completed", "scenario_id": scenario_id, "arm": arm,
                "episode_id": episode_id, "score": episode["score"], "steps": episode["steps"]})
            return episode
        except Exception as exc:
            write_new(attempt / "failed.json", {"error_type": type(exc).__name__, "at": now()})
            raise

    @staticmethod
    def _episode_row(episode):
        row = TetrisContextExperiment._episode_row(episode)
        diagnostics = episode_diagnostics(episode)
        row["control_diagnostics"] = {key: diagnostics[key] for key in CONTROL_KEYS}
        return row

    def costs(self):
        events = read_lines(self.path / "calls.jsonl")
        episodes = [load(p.parent / "episode.json") for p in (self.path / "episodes").glob("*/*/attempt-*/complete.json")]
        kinds = ("decision_started", "decision_completed", "decision_failed", "health_started", "health_completed", "health_failed")
        counts = {kind: sum(e["kind"] == kind for e in events) for kind in kinds}
        by_arm = {arm: {kind: sum(e["kind"] == kind and e.get("arm") == arm for e in events) for kind in kinds[:3]} for arm in ARMS}
        usage = defaultdict(float)
        for event in events:
            for key, value in event.get("usage", {}).items():
                if type(value) in (int, float):
                    usage[key] += value
        return {**counts, "by_arm": by_arm, "completed_episodes": len(episodes),
                "accepted_game_actions": sum(e["steps"] for e in episodes),
                "episode_attempts": len(list((self.path / "episodes").glob("*/*/attempt-*/started.json"))),
                "decision_started_without_response": counts["decision_started"]-counts["decision_completed"]-counts["decision_failed"],
                "reported_usage": dict(usage), "coach_calls": 0, "currency_cost": "not available; no price assumed"}

    def _run_phase(self, phase):
        result_path = self.path / f"{phase}_result.json"
        hash_path = result_path.with_suffix(".sha256.json")
        saved = load(result_path) if result_path.exists() else None
        if saved is not None and hash_path.exists() and digest(saved) != load(hash_path)["sha256"]:
            raise ExperimentError("Saved phase result changed")
        self.ensure_health()
        per_seed = []
        for scenario in self.manifest["scenarios"][phase]:
            row = {**deepcopy(scenario), "arms": {}}
            for arm in scenario["order"]:
                episode = self.episode(scenario["id"], arm, scenario["seed"], phase)
                row["arms"][arm] = self._episode_row(episode)
            per_seed.append(row)
        summary = summarize(per_seed, self.manifest["statistics"], self.manifest["screening_gate"])
        if phase == "calibration":
            for stats in summary["comparisons"].values():
                stats.update(screening_gate_pass=False, screening_gate_applicable=False)
        result = {"status": "completed", "phase": phase, "per_seed": per_seed, **summary,
            "candidate_hash": self.manifest["candidate_hash"], "brief_hash": self.manifest["brief_hash"],
            "published": False, "registry_writes": False, "policy_changed_during_study": False,
            "costs": self.costs(), "evaluation_allowed": phase == "calibration",
            "interpretation": "Engineering calibration only; no score threshold or policy selection." if phase == "calibration"
                else "Image-only paired ablation; positive mean alone does not establish a clear improvement."}
        if saved is not None:
            for key in ("per_seed", "arms", "comparisons", "any_line_comparison", "candidate_hash", "brief_hash", "published"):
                if saved[key] != result[key]:
                    raise ExperimentError("Saved phase summary differs from audited episodes")
            if not hash_path.exists():
                if saved != result:
                    raise ExperimentError("Unhashed phase result differs from full reconstruction")
                write_new(hash_path, {"sha256": digest(saved)})
            return saved
        write_new(result_path, result)
        write_new(hash_path, {"sha256": digest(result)})
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("prepare")
    command.add_argument("--id", required=True); command.add_argument("--parent", type=Path, required=True)
    command.add_argument("--seed", type=int, default=DEFAULT_SEED)
    for name in ("run-calibration", "run-evaluation"):
        command = sub.add_parser(name); command.add_argument("--id", required=True)
        command.add_argument("--retry-failed", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare(identity=args.id, parent_path=args.parent, seed=args.seed)
            output = {"prepared": True, "id": args.id, "budget": result["budget"], "path": str(experiment_path(args.id))}
        else:
            with TetrisVisionExperiment(experiment_path(args.id), retry_failed=args.retry_failed) as experiment:
                result = experiment.run_calibration() if args.command == "run-calibration" else experiment.run_evaluation()
            output = {key: result[key] for key in ("status", "phase", "arms", "published", "costs")}
        print(json.dumps(output, ensure_ascii=False, indent=2)); return 0
    except Exception as exc:
        print(f"Experiment stopped ({type(exc).__name__}): " + (str(exc) if isinstance(exc, ExperimentError)
            else "inspect saved attempt evidence"), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
