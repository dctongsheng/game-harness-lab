#!/usr/bin/env python3
"""Isolated real-GEPA feedback A/B experiment. No live registry writes or publishing.

Usage (from repository root, .venv Python):
  python scripts/run_feedback_experiment.py prepare --game gomoku --id feedback-gomoku \
      --parent data/bootstrap/gomoku.json
  python scripts/run_feedback_experiment.py run-dev --id feedback-gomoku
  python scripts/run_feedback_experiment.py run-final --id feedback-gomoku

Use --retry-failed explicitly to resume a failed/interrupted operation. Completed
logical trials and coach responses are reused ONLY for recovery, never across
candidate proposals. Every retry has a separate immutable attempt directory.
prepare makes no model requests. run-dev/run-final call real configured providers.
The four development trials are train+selection-validation, NOT independent tests.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import inspect
import json
import math
from pathlib import Path
import random
import re
import sqlite3
import statistics
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
from game_harness.evolution import evolve_harness
from game_harness.games import get_game
from game_harness.providers import CoachProvider, DecisionProvider, Settings, validate_candidate
from game_harness.runtime import run_episode
from audit_saved_run import _audit_episode

FORMATS = ("legacy_excerpt_v1", "complete_events_v1")


class ExperimentError(RuntimeError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_new(path, value):
    """Exclusive files are never overwritten, including failed attempts."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))
        stream.flush()


def append(path, value):
    with Path(path).open("a", encoding="utf-8") as stream:
        stream.write(canonical({"at": now(), **value}) + "\n")
        stream.flush()


def read_lines(path):
    if not Path(path).exists():
        return []
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def code_hashes():
    paths = sorted(p for p in (ROOT / "src/game_harness").rglob("*")
                   if p.is_file() and p.suffix in {".py", ".cjs", ".ts", ".json"})
    paths += [Path(__file__).resolve(), ROOT / "scripts/audit_saved_run.py", ROOT / "pyproject.toml"]
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def snapshot_sources(destination, expected_hashes):
    """Freeze exactly the bytes hashed in the manifest, never a later edit."""
    sources = {}
    for relative, expected in expected_hashes.items():
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ExperimentError("Source snapshot paths must stay relative to the repository")
        content = (ROOT / relative_path).read_bytes()
        if hashlib.sha256(content).hexdigest() != expected:
            raise ExperimentError(f"Source changed while preparing snapshot: {relative}")
        sources[relative_path] = content
    # Validate every source before writing. The verified byte buffers prevent a
    # second filesystem read from silently copying an edit made after hashing.
    for relative_path, content in sources.items():
        target = Path(destination) / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(content)
            stream.flush()


def public_settings(settings):
    values = {"decision_url": settings.decision_url, "decision_model": settings.decision_model,
              "coach_base_url": settings.coach_base_url, "coach_model": settings.coach_model,
              "decision_timeout": settings.decision_timeout, "coach_timeout": settings.coach_timeout,
              "think_mode": "never"}
    if settings.decision_api_key_file:
        values["decision_auth"] = "bearer_key_file"
    for name in ("decision_url", "coach_base_url"):
        url = urlsplit(values[name])
        if url.username or url.password or url.query or url.fragment:
            raise ExperimentError("Provider URLs must not contain credentials, queries or fragments")
    return values


def experiment_path(identity, root=None):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}", identity):
        raise ExperimentError("Invalid experiment ID")
    return Path(root or ROOT / "data/experiments") / identity


def active_parent(game, data_root):
    """Read-only fallback; choosing an ambiguous active scope requires --parent."""
    db_path = Path(data_root) / "registry.sqlite"
    connection = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        found = [json.loads(row[0]) for row in connection.execute(
            "SELECT r.body FROM active a JOIN records r ON r.kind='version' AND r.id=a.version")]
    finally:
        connection.close()
    found = [item for item in found if item.get("game_id") == game]
    if len(found) != 1:
        raise ExperimentError("Specify --parent: expected exactly one active scope for this game")
    return found[0]


def scenario_plan(game, seed):
    rng = random.Random(seed)
    used = set()

    def take(parity=None):
        while True:
            value = rng.randrange(1, 2**31 - 1)
            if parity is not None:
                value = value // 2 * 2 + parity
            if value not in used:
                used.add(value)
                return value

    parity = [0, 1] if game == "gomoku" else [None, None]
    trigger = take()
    train = [take(p) for p in parity]
    validation = [take(p) for p in parity]
    final = []
    if game == "gomoku":
        for index in range(12):
            final.append({"id": f"final-{index:02d}", "seed": take(index % 2), "repeat": 0,
                          "stratum": index % 2, "order": ["parent", "candidate"] if (index // 2) % 2 == 0 else ["candidate", "parent"]})
    else:
        # Twelve fresh bags, each repeated twice. Bootstrap clusters by bag seed.
        bags = [take() for _ in range(12)]
        for repeat in range(2):
            for index, value in enumerate(bags):
                final.append({"id": f"final-{repeat:01d}-{index:02d}", "seed": value, "repeat": repeat,
                              "stratum": 0, "order": ["parent", "candidate"] if (repeat + index) % 2 == 0 else ["candidate", "parent"]})
    return {"trigger": trigger, "train": train, "selection_validation": validation,
            "development": train + validation, "final": final}


def prepare(*, identity, game, parent_path=None, seed=20261017, root=None, settings=None):
    if game not in {"gomoku", "tetris"}:
        raise ExperimentError("This preregistered experiment supports gomoku and tetris")
    settings = settings or Settings.load()
    source = load(parent_path) if parent_path else active_parent(game, ROOT / "data")
    candidate = validate_candidate(source.get("candidate", source))
    adapter = get_game(game)
    original_context = source.get("game_context")
    config = {"game_id": game, "board_size": 8, **adapter.defaults}
    if original_context:
        for key in ("max_steps", "gomoku_size"):
            if key in original_context.get("config", {}):
                config[key] = original_context["config"][key]
    adapter.validate_config(config)
    context = adapter.context(config)
    if original_context is not None and original_context != context:
        raise ExperimentError("Parent game context differs from installed rules/config/opponent")
    if source.get("decision_model", settings.decision_model) != settings.decision_model:
        raise ExperimentError("Parent Decision model differs from configured model")
    if source.get("think_mode", "never") != "never":
        raise ExperimentError("Parent does not use never thinking mode")
    parent = {**source, "candidate": candidate, "game_id": game, "game_context": context,
              "decision_model": settings.decision_model, "think_mode": "never"}
    parent.setdefault("id", "exp-parent-" + digest({"candidate": candidate, "context": context})[:16])
    scenarios = scenario_plan(game, seed)
    trials = [{"id": "legacy-1", "feedback_format": FORMATS[0], "replicate": 1},
              {"id": "complete-1", "feedback_format": FORMATS[1], "replicate": 1},
              {"id": "complete-2", "feedback_format": FORMATS[1], "replicate": 2},
              {"id": "legacy-2", "feedback_format": FORMATS[0], "replicate": 2}]
    final_pairs = len(scenarios["final"])
    manifest = {"schema": "feedback-ab-v1", "id": identity, "created_at": now(), "game_id": game,
                "parent": parent, "parent_hash": digest(parent), "config": config, "game_context": context,
                "providers": public_settings(settings), "code_hashes": code_hashes(), "seed": seed,
                "scenarios": scenarios, "proposal_order": trials,
                "development_gate": {"minimum_mean_gain": context["score_spec"]["minimum_mean_gain"],
                                     "no_pair_regression": True, "at_least_one_positive": True,
                                     "tie_break": "earliest_preregistered_proposal_order"},
                "final_gate": {"minimum_mean_gain": context["score_spec"]["minimum_mean_gain"],
                               "no_pair_regression": True, "bootstrap_95_lower_greater_than": 0},
                "statistics": {"bootstrap_seed": 74103, "bootstrap_resamples": 10000,
                               "bootstrap_unit": ("paired run ID, stratified by candidate seat; identical empty opening" if game == "gomoku" else "environment bag seed cluster; both repeats stay together"),
                               "paired_difference": "candidate raw score minus frozen parent raw score"},
                "budget": {"independent_single_round_proposals": 4, "max_coach_attempts_per_proposal": 2,
                           "max_gepa_attempts_per_proposal": 2, "max_episode_attempts_per_trial": 2,
                           "development_completed_episodes_max": 21, "final_pairs": final_pairs,
                           "final_episodes_max": final_pairs * 2,
                           "decision_request_attempts_max": (21 + final_pairs * 2) * config["max_steps"] * 2},
                "constraints": {"publish": False, "registry_writes": False, "recursive_optimization": False,
                                "candidate_cache": "only same proposal+seed during resume; never across independent proposals",
                                "parent_cache": "one frozen episode per dev seed shared across both arms",
                                "development_is_independent_test": False,
                                "candidate_supplement": "all valid proposals get all four dev trials even if GEPA rejects train improvement",
                                "failed_attempts": "counted; explicit retry only, bounded; no score for infrastructure failure"},
                "limitations": ["Only two proposals per feedback arm; descriptive A/B evidence, not a reliable treatment-effect test.",
                                "Development set is used for training and selection; only final is held out.",
                                "Episode IDs differ and appear in model observations; service drift and model nondeterminism remain possible.",
                                "Gomoku trial IDs change seats, not the standard empty opening; Tetris repeats share a bag seed.",
                                "Final checks only one development-selected candidate; no repeated winner search on final results."]}
    path = experiment_path(identity, root)
    path.mkdir(parents=True, exist_ok=False)
    snapshot_sources(path / "source_snapshot", manifest["code_hashes"])
    write_new(path / "manifest.json", manifest)
    write_new(path / "manifest.sha256.json", {"sha256": digest(manifest)})
    write_new(path / "parent.json", parent)
    usage = (f"# {identity}\n\nFrozen experiment; prepare made no model calls.\n\n"
             f"Run from the repository root:\n```sh\n.venv/bin/python scripts/run_feedback_experiment.py run-dev --id {identity}\n"
             f".venv/bin/python scripts/run_feedback_experiment.py run-final --id {identity}\n```\n\n"
             "`source_snapshot/` preserves every file listed in manifest `code_hashes`, verified byte-for-byte before writing. "
             "After a failed operation, add `--retry-failed` once; attempts remain bounded and retained. "
             "No failed result is a zero score. `run-final` refuses when the dev gate fails. "
             "`run-dev` reuses completed trials after restart and always evaluates all four development scenarios "
             "for every valid proposal, including GEPA-rejected proposals. It never publishes or modifies the live registry. "
             f"Final: {final_pairs} pairs / {2 * final_pairs} episodes. Native score is the primary metric.\n")
    (path / "README.md").write_text(usage)
    return manifest


def paired_stats(pairs, game, *, bootstrap_seed=74103, resamples=10000):
    if not pairs:
        raise ExperimentError("Cannot compute statistics without pairs")
    rows = [{**pair, "difference": float(pair["candidate_score"]) - float(pair["parent_score"])} for pair in pairs]
    grouped = defaultdict(lambda: defaultdict(list))
    for row in rows:
        grouped[row["seed"] % 2 if game == "gomoku" else 0][row["seed"]].append(row["difference"])
    clusters = {stratum: [statistics.mean(values) for values in seeds.values()] for stratum, seeds in grouped.items()}
    rng = random.Random(bootstrap_seed)
    boot = []
    for _ in range(resamples):
        draws = [rng.choice(values) for values in clusters.values() for _ in range(len(values))]
        boot.append(statistics.mean(draws))
    boot.sort()
    return {"pairs": rows, "pair_count": len(rows), "unique_seed_count": len({r["seed"] for r in rows}),
            "parent_mean": statistics.mean(r["parent_score"] for r in rows),
            "candidate_mean": statistics.mean(r["candidate_score"] for r in rows),
            "mean_gain": statistics.mean(r["difference"] for r in rows),
            "min_gain": min(r["difference"] for r in rows),
            "positive_pairs": sum(r["difference"] > 0 for r in rows),
            "negative_pairs": sum(r["difference"] < 0 for r in rows),
            "strata": {str(k): {"seed_clusters": len(v), "mean_gain": statistics.mean(v)} for k, v in clusters.items()},
            "bootstrap_95": [boot[int((len(boot) - 1) * .025)], boot[int((len(boot) - 1) * .975)]],
            "bootstrap_seed": bootstrap_seed, "bootstrap_resamples": resamples,
            "bootstrap_unit": ("paired run ID, stratified by candidate seat; identical empty opening" if game == "gomoku" else "environment bag seed cluster; both repeats stay together"),
            "inference_limit": "Small development samples and repeated identical Gomoku openings limit interpretation."}


def passes_gate(stats, gate, *, final=False):
    passed = stats["mean_gain"] >= gate["minimum_mean_gain"] and stats["min_gain"] >= 0 and stats["positive_pairs"] > 0
    return passed and (not final or stats["bootstrap_95"][0] > gate["bootstrap_95_lower_greater_than"])


class Experiment:
    def __init__(self, path, *, retry_failed=False, settings=None, decision=None, coach=None):
        self.path = Path(path)
        self.manifest = load(self.path / "manifest.json")
        if digest(self.manifest) != load(self.path / "manifest.sha256.json")["sha256"]:
            raise ExperimentError("Manifest changed after preregistration")
        if self.manifest["code_hashes"] != code_hashes():
            raise ExperimentError("Source changed since prepare; create a new experiment")
        self.settings = settings or Settings.load()
        if public_settings(self.settings) != self.manifest["providers"]:
            raise ExperimentError("Provider settings changed since prepare")
        self.retry_failed = retry_failed
        self.adapter = get_game(self.manifest["game_id"])
        self.parent = self.manifest["parent"]
        self._decision, self._coach = decision, coach
        self._owned_decision = decision is None
        self._owned_coach = coach is None
        self._lock = None
        self._decision_attempts = sum(row["kind"] == "decision_started" for row in read_lines(self.path / "calls.jsonl"))

    def __enter__(self):
        import fcntl
        self._lock = (self.path / ".run.lock").open("a")
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lock.close()
            raise ExperimentError("Another runner is active for this experiment") from None
        return self

    def __exit__(self, *_):
        for provider, owned in ((self._decision, self._owned_decision), (self._coach, self._owned_coach)):
            if provider is not None and owned:
                provider.close()
        if self._lock:
            self._lock.close()

    def attempt(self, directory, maximum):
        directory.mkdir(parents=True, exist_ok=True)
        previous = sorted(directory.glob("attempt-*"))
        if previous and not self.retry_failed:
            raise ExperimentError(f"Previous attempt exists at {directory.relative_to(self.path)}; use --retry-failed")
        if len(previous) >= maximum:
            raise ExperimentError(f"Attempt budget exhausted at {directory.relative_to(self.path)}")
        result = directory / f"attempt-{len(previous) + 1:02d}"
        result.mkdir()
        write_new(result / "started.json", {"at": now(), "attempt": len(previous) + 1})
        return result

    def ensure_health(self):
        """One saved identity preflight, separate from gameplay request costs."""
        directory = self.path / "health"
        completed = sorted(directory.glob("attempt-*/complete.json"))
        if completed:
            return load(completed[-1])
        attempt = self.attempt(directory, 2)
        append(self.path / "calls.jsonl", {"kind": "health_started", "attempt": attempt.name})
        if self._decision is None:
            self._decision = DecisionProvider(self.settings)
        try:
            result = self._decision.health()
            if not result.get("ok") or result.get("model") != self.settings.decision_model or result.get("request_think_mode") != "never":
                raise ExperimentError("Decision health must confirm the frozen model and never request mode")
            write_new(attempt / "complete.json", {"at": now(), **result})
            append(self.path / "calls.jsonl", {"kind": "health_completed", "attempt": attempt.name})
            return result
        except Exception as exc:
            write_new(attempt / "failed.json", {"error_type": type(exc).__name__})
            append(self.path / "calls.jsonl", {"kind": "health_failed", "attempt": attempt.name, "error_type": type(exc).__name__})
            raise

    def counted_decision(self):
        experiment = self
        if self._decision is None:
            self._decision = DecisionProvider(self.settings)

        class CountingDecision:
            def decide(self, state, instructions, criteria, request_id):
                if experiment._decision_attempts >= experiment.manifest["budget"]["decision_request_attempts_max"]:
                    raise ExperimentError("Decision request budget exhausted")
                append(experiment.path / "calls.jsonl", {"kind": "decision_started", "request_id": request_id})
                experiment._decision_attempts += 1
                try:
                    receipt = experiment._decision.decide(state, instructions, criteria, request_id)
                except Exception as exc:
                    append(experiment.path / "calls.jsonl", {"kind": "decision_failed", "request_id": request_id, "error_type": type(exc).__name__})
                    raise
                append(experiment.path / "calls.jsonl", {"kind": "decision_completed", "request_id": request_id,
                                                        "elapsed_ms": receipt.get("elapsed_ms"),
                                                        "model": receipt.get("model"),
                                                        "usage": receipt.get("raw_response", {}).get("usage", {})})
                return receipt
        return CountingDecision()

    def verify_episode(self, episode, harness):
        if episode.get("infra_error") or episode.get("termination_reason") in {"stopped", "infra_error"}:
            raise ExperimentError("Incomplete episode cannot be scored")
        result = _audit_episode(episode, {harness["id"]: harness})
        if result["thinking_verified"] != len(result["receipts"]):
            raise ExperimentError("Every action must report thinking disabled and zero reasoning tokens")
        return {"actions": len(result["receipts"]), "thinking_off_actions": result["thinking_verified"],
                "raw_response_and_rules_replay_verified": True}

    def episode(self, logical_id, harness, seed, purpose):
        directory = self.path / "episodes" / logical_id
        # Recovery cache is scoped by this proposal+seed, not by candidate text.
        for previous in sorted(directory.glob("attempt-*/complete.json")):
            evidence = load(previous)
            episode = load(previous.parent / "episode.json")
            if evidence["episode_hash"] != digest(episode) or evidence["harness_hash"] != digest(harness):
                raise ExperimentError("Saved episode/cache identity changed")
            if episode["seed"] != seed or episode["purpose"] != purpose:
                raise ExperimentError("Saved trial identity differs from frozen schedule")
            self.verify_episode(episode, harness)
            append(self.path / "events.jsonl", {"kind": "episode_reused", "logical_id": logical_id, "episode_id": episode["episode_id"]})
            return episode
        # A crash between episode persistence and audit receipt needs no replay.
        for saved in sorted(directory.glob("attempt-*/episode.json")):
            episode = load(saved)
            if episode.get("infra_error") or episode.get("termination_reason") in {"stopped", "infra_error"}:
                continue
            if episode["seed"] != seed or episode["purpose"] != purpose or episode["harness_id"] != harness["id"]:
                raise ExperimentError("Interrupted saved trial differs from requested identity")
            verified = self.verify_episode(episode, harness)
            write_new(saved.parent / "complete.json", {"episode_hash": digest(episode), "harness_hash": digest(harness), "audit": verified})
            append(self.path / "events.jsonl", {"kind": "episode_recovered", "logical_id": logical_id})
            return episode
        attempt = self.attempt(directory, self.manifest["budget"]["max_episode_attempts_per_trial"])
        episode_id = "exp-" + digest({"experiment": self.manifest["id"], "logical_id": logical_id,
                                     "attempt": attempt.name})[:24]
        try:
            episode = run_episode(self.counted_decision(), harness, episode_id=episode_id, seed=seed,
                                  adapter=self.adapter, config=self.manifest["config"],
                                  log_path=attempt / "trace.jsonl")
            episode.update(purpose=purpose, experiment_id=self.manifest["id"], logical_trial=logical_id)
            write_new(attempt / "episode.json", episode)
            verified = self.verify_episode(episode, harness)
            write_new(attempt / "complete.json", {"episode_hash": digest(episode), "harness_hash": digest(harness), "audit": verified})
            append(self.path / "events.jsonl", {"kind": "episode_completed", "logical_id": logical_id,
                                               "episode_id": episode_id, "score": episode["score"], "steps": episode["steps"]})
            return episode
        except Exception as exc:
            write_new(attempt / "failed.json", {"error_type": type(exc).__name__, "at": now()})
            raise

    def proposal_coach(self, trial_dir):
        experiment = self

        class SavedCoach:
            settings = experiment.settings

            def complete(self, messages, purpose="reflection"):
                # A failed GEPA evaluation resumes the exact original proposal.
                directory = trial_dir / "coach"
                for saved in sorted(directory.glob("attempt-*/response.json")):
                    value = load(saved)
                    if value["messages_hash"] != digest(messages):
                        raise ExperimentError("Reflection input changed during resume")
                    append(experiment.path / "events.jsonl", {"kind": "coach_response_reused", "proposal": trial_dir.name})
                    return value["content"]
                attempt = experiment.attempt(directory, experiment.manifest["budget"]["max_coach_attempts_per_proposal"])
                write_new(attempt / "input.json", messages)
                append(experiment.path / "calls.jsonl", {"kind": "coach_started", "proposal": trial_dir.name, "attempt": attempt.name})
                if experiment._coach is None:
                    experiment._coach = CoachProvider(experiment.settings)
                try:
                    content = experiment._coach.complete(messages, purpose=purpose)
                    calls = getattr(experiment._coach, "calls", [])
                    info = calls[-1] if calls else {}
                    write_new(attempt / "response.json", {"messages_hash": digest(messages), "content": content,
                                                          "provider_call": info})
                    append(experiment.path / "calls.jsonl", {"kind": "coach_completed", "proposal": trial_dir.name,
                                                            "attempt": attempt.name, "provider_call": info})
                    return content
                except Exception as exc:
                    write_new(attempt / "failed.json", {"error_type": type(exc).__name__})
                    append(experiment.path / "calls.jsonl", {"kind": "coach_failed", "proposal": trial_dir.name,
                                                            "attempt": attempt.name, "error_type": type(exc).__name__})
                    raise
        return SavedCoach()

    def harness(self, candidate, proposal_id):
        return {**self.parent, "id": "exp-candidate-" + digest({"proposal": proposal_id, "candidate": candidate})[:16],
                "candidate": deepcopy(candidate), "parent_id": self.parent["id"],
                "provenance": {"type": "isolated_feedback_experiment", "proposal": proposal_id}}

    def run_proposal(self, trial, trigger, baseline):
        directory = self.path / "proposals" / trial["id"]
        complete = directory / "complete.json"
        if complete.exists():
            return load(complete)
        # An optimization may be done while supplementary dev trials failed.
        outcomes = sorted(directory.glob("gepa/attempt-*/evolution_result.json"))
        outcome = next((load(path) for path in reversed(outcomes) if load(path).get("status") == "completed"), None)
        if outcome is None:
            attempt = self.attempt(directory / "gepa", self.manifest["budget"]["max_gepa_attempts_per_proposal"])

            def evaluate(candidate, seed, purpose):
                is_parent = candidate == self.parent["candidate"]
                append(self.path / "events.jsonl", {"kind": "gepa_evaluation_requested", "proposal": trial["id"],
                                                   "seed": seed, "purpose": purpose, "parent_cache": is_parent,
                                                   "candidate_hash": digest(candidate)})
                if is_parent:
                    return deepcopy(baseline[seed])
                return self.episode(f"{trial['id']}-dev-{seed}", self.harness(candidate, trial["id"]), seed, "development_candidate")

            try:
                outcome = evolve_harness(candidate=deepcopy(self.parent["candidate"]), trigger_episode=trigger,
                                         train_seeds=self.manifest["scenarios"]["train"],
                                         validation_seeds=self.manifest["scenarios"]["selection_validation"],
                                         evaluate_episode=evaluate, coach=self.proposal_coach(directory), run_dir=attempt,
                                         reflection_rounds=1, game_context=self.manifest["game_context"],
                                         feedback_format=trial["feedback_format"],
                                         on_event=lambda event: append(self.path / "events.jsonl", {"proposal": trial["id"], **event}))
            except Exception as exc:
                write_new(attempt / "runner_failed.json", {"error_type": type(exc).__name__, "at": now()})
                raise
        proposals = outcome.get("proposals", [])
        if len(proposals) != 1:
            raise ExperimentError("Expected exactly one GEPA reflection proposal")
        proposal = proposals[0]
        result = {**trial, "status": proposal["status"], "proposal": proposal,
                  "gepa": outcome.get("gepa"), "gepa_selected_changed": outcome.get("changed"),
                  "development_is_independent_test": False, "candidate": proposal.get("candidate")}
        if proposal.get("status") == "proposed" and proposal.get("candidate") is not None:
            candidate = validate_candidate(proposal["candidate"])
            harness = self.harness(candidate, trial["id"])
            result["candidate_harness"] = harness
            # Even an equal-text valid proposal is independently played here.
            pairs = []
            for seed in self.manifest["scenarios"]["development"]:
                episode = self.episode(f"{trial['id']}-dev-{seed}", harness, seed, "development_candidate")
                pairs.append({"seed": seed, "parent_score": baseline[seed]["score"], "candidate_score": episode["score"],
                              "parent_episode_id": baseline[seed]["episode_id"], "candidate_episode_id": episode["episode_id"]})
            result["development"] = paired_stats(pairs, self.manifest["game_id"],
                                                   bootstrap_seed=self.manifest["statistics"]["bootstrap_seed"],
                                                   resamples=self.manifest["statistics"]["bootstrap_resamples"])
            result["passes_development_gate"] = passes_gate(result["development"], self.manifest["development_gate"])
        else:
            result.update(passes_development_gate=False, development=None,
                          rejection_note="No valid candidate; parent cache reevaluation is not candidate improvement")
        write_new(complete, result)
        return result

    def costs(self):
        events = read_lines(self.path / "calls.jsonl")
        completed = list((self.path / "episodes").glob("*/attempt-*/complete.json"))
        episodes = [load(path.parent / "episode.json") for path in completed]
        counts = {kind: sum(event["kind"] == kind for event in events) for kind in
                  ("decision_started", "decision_completed", "decision_failed", "coach_started", "coach_completed", "coach_failed", "health_started", "health_completed", "health_failed")}
        usage = defaultdict(float)
        for event in events:
            source = event.get("provider_call", {}).get("usage", {}) if event["kind"] == "coach_completed" else event.get("usage", {})
            for key, value in source.items():
                if type(value) in (int, float):
                    usage[("coach_" if event["kind"] == "coach_completed" else "decision_") + key] += value
        return {**counts, "completed_episodes": len(episodes), "accepted_actions": sum(e["steps"] for e in episodes),
                "episode_attempts": len(list((self.path / "episodes").glob("*/attempt-*/started.json"))),
                "decision_started_without_response": counts["decision_started"] - counts["decision_completed"] - counts["decision_failed"],
                "coach_started_without_response": counts["coach_started"] - counts["coach_completed"] - counts["coach_failed"],
                "gepa_evaluation_callbacks": sum(e.get("kind") == "gepa_evaluation_requested" for e in read_lines(self.path / "events.jsonl")),
                "reported_usage": dict(usage), "currency_cost": "not available; no price assumed"}

    def run_dev(self):
        if (self.path / "development_result.json").exists():
            return load(self.path / "development_result.json")
        if "feedback_format" not in inspect.signature(evolve_harness).parameters:
            raise ExperimentError("Installed evolve_harness does not yet support feedback_format")
        self.ensure_health()
        seeds = self.manifest["scenarios"]
        trigger = self.episode("parent-trigger", self.parent, seeds["trigger"], "trigger")
        baseline = {seed: self.episode(f"parent-dev-{seed}", self.parent, seed, "development_parent") for seed in seeds["development"]}
        results = [self.run_proposal(trial, trigger, baseline) for trial in self.manifest["proposal_order"]]
        eligible = [r for r in results if r["passes_development_gate"]]
        selected = max(eligible, key=lambda item: item["development"]["mean_gain"]) if eligible else None
        arms = {}
        for format_name in FORMATS:
            group = [r for r in results if r["feedback_format"] == format_name]
            scores = [r["development"]["mean_gain"] for r in group if r["development"] is not None]
            arms[format_name] = {"proposals_attempted": len(group), "valid_proposals": len(scores),
                                 "proposal_mean_gains": scores, "mean_gain": statistics.mean(scores) if scores else None,
                                 "interpretation": "descriptive only; two independent proposals, same four development trials"}
        result = {"status": "completed", "proposals": results, "arms": arms,
                  "selected_proposal_id": selected["id"] if selected else None,
                  "final_allowed": selected is not None, "published": False,
                  "selection_rule": self.manifest["development_gate"], "costs": self.costs(),
                  "reason": "development_gate_passed" if selected else "no_candidate_passed_development_gate"}
        write_new(self.path / "development_result.json", result)
        return result

    def run_final(self):
        if (self.path / "final_result.json").exists():
            return load(self.path / "final_result.json")
        path = self.path / "development_result.json"
        if not path.exists():
            raise ExperimentError("Complete run-dev first")
        dev = load(path)
        if not dev["final_allowed"]:
            raise ExperimentError("No candidate passed the preregistered development gate; final will not run")
        selected = next(r for r in dev["proposals"] if r["id"] == dev["selected_proposal_id"])
        self.ensure_health()
        pairs = []
        for scenario in self.manifest["scenarios"]["final"]:
            values = {}
            for label in scenario["order"]:
                harness = self.parent if label == "parent" else selected["candidate_harness"]
                values[label] = self.episode(f"{scenario['id']}-{label}", harness, scenario["seed"], "final_" + label)
            pairs.append({**scenario, "parent_score": values["parent"]["score"], "candidate_score": values["candidate"]["score"],
                          "parent_episode_id": values["parent"]["episode_id"], "candidate_episode_id": values["candidate"]["episode_id"]})
        stats = paired_stats(pairs, self.manifest["game_id"],
                             bootstrap_seed=self.manifest["statistics"]["bootstrap_seed"],
                             resamples=self.manifest["statistics"]["bootstrap_resamples"])
        result = {"status": "completed", "selected_proposal_id": selected["id"], "statistics": stats,
                  "passed": passes_gate(stats, self.manifest["final_gate"], final=True),
                  "gate": self.manifest["final_gate"], "published": False, "costs": self.costs(),
                  "interpretation": "Independent holdout for one development-selected candidate; no automated publishing."}
        write_new(self.path / "final_result.json", result)
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("prepare")
    command.add_argument("--id", required=True)
    command.add_argument("--game", choices=("gomoku", "tetris"), required=True)
    command.add_argument("--parent", type=Path)
    command.add_argument("--seed", type=int, default=20261017)
    for name in ("run-dev", "run-final"):
        command = sub.add_parser(name)
        command.add_argument("--id", required=True)
        command.add_argument("--retry-failed", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare(identity=args.id, game=args.game, parent_path=args.parent, seed=args.seed)
            output = {"id": args.id, "prepared": True, "budget": result["budget"], "path": str(experiment_path(args.id))}
        else:
            with Experiment(experiment_path(args.id), retry_failed=args.retry_failed) as experiment:
                result = experiment.run_dev() if args.command == "run-dev" else experiment.run_final()
            output = {key: result[key] for key in ("status", "selected_proposal_id", "final_allowed", "passed", "costs", "reason") if key in result}
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        # Arbitrary provider exceptions may contain secrets; raw traces remain local.
        print(f"Experiment stopped ({type(exc).__name__}): " + (str(exc) if isinstance(exc, ExperimentError) else "inspect saved attempt evidence"), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
