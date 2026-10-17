#!/usr/bin/env python3
"""Run one bounded native Pokemon bootstrap/GEPA cycle with HTTP evidence.

Uses the shared HTTP evidence recorders from run_overcooked.py.
Raw evidence stays in ignored data/. Only an approved candidate is exported.
No resume/retry is implicit: a new run requires a new evidence directory.
"""
from __future__ import annotations

import argparse
import hashlib
from copy import deepcopy
import json
import importlib.util
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from game_harness.games import get_game
from game_harness.providers import ProviderError, Settings, digest
from game_harness.service import DEFAULTS, Lab
from game_harness.store import atomic_json, utcnow


from run_overcooked import RecordingDecision, RecordingCoach
from game_harness.scoring import IncompleteGameResult, require_evaluable_result


def runtime_fingerprint():
    """Hash executable upstream files, not only their claimed source revision."""
    source = ROOT / '.runtime/pokemon-showdown'
    dirty = subprocess.check_output(['git', '-C', str(source), 'status', '--porcelain', '--untracked-files=no'], text=True)
    if dirty.strip():
        raise ValueError('Showdown source has local edits; experiment requires the pinned clean source')
    package = Path(importlib.util.find_spec('poke_env').origin).parent
    def tree(root, suffixes):
        files = {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in sorted(root.rglob('*')) if path.is_file() and path.suffix in suffixes}
        if not files:
            raise ValueError('Native dependency files missing')
        return {'files': len(files), 'sha256': digest(files)}
    return {'showdown_dist': tree(source / 'dist', {'.js', '.json'}),
            'poke_env_package': tree(package, {'.py', '.json'}),
            'node': subprocess.check_output(['node', '--version'], text=True).strip(),
            'python': sys.version.split()[0]}


def verify_frozen_sources(manifest):
    for path, expected in manifest['source_hashes'].items():
        if hashlib.sha256((ROOT / path).read_bytes()).hexdigest() != expected:
            raise ValueError('Experiment source changed: ' + path)


class EvidenceLab(Lab):
    def play(self, provider, harness, config, seed, purpose, job_id=None):
        self.verify_frozen_runtime()
        result = super().play(provider, harness, config, seed, purpose, job_id)
        self.verify_frozen_runtime()
        if result.get("infra_error") or result.get("termination_reason") == "stopped":
            raise ProviderError("Incomplete episode; not a zero-score policy result")
        # Independent upstream replay is performed before this score reaches GEPA.
        from audit_pokemon_run import audit_episode
        evidence = audit_episode(result, harness)
        atomic_json(self.store.root / "audits" / (result["episode_id"] + ".json"), evidence)
        print(json.dumps({"episode": result["episode_id"], "purpose": purpose,
                          "score": evidence["score"], "outcome": evidence["result"], "steps": result["steps"]}), flush=True)
        if evidence.get('valid_for_evaluation') is not True:
            raise IncompleteGameResult('Native audit excluded this episode from evaluation')
        require_evaluable_result(result, result["game_context"])
        return result

    def verify_frozen_runtime(self):
        verify_frozen_sources(self.frozen_protocol)
        if runtime_fingerprint() != self.frozen_protocol['runtime_fingerprint']:
            raise ValueError('Native runtime changed during frozen experiment')


def run(directory, max_steps=160):
    if directory.exists():
        raise ValueError("Evidence directory already exists; use a fresh --data-dir")
    settings = Settings.load()
    adapter = get_game("pokemon")
    config = {**DEFAULTS, **adapter.defaults, "game_id": "pokemon",
              "max_steps": max_steps,
              "train_games": 1, "validation_games": 1, "promotion_games": 4,
              "reflection_rounds": 1, "episodes": 1, "seed": 42}
    adapter.validate_config(config)
    if not 1 <= max_steps <= 2000:
        raise ValueError("max_steps must be 1..2000")
    context = adapter.context(config)
    # Verify the native install before creating an experimental evidence record.
    session = adapter.create(config["seed"], config)
    session.close()
    fingerprint = runtime_fingerprint()
    directory.mkdir(parents=True)
    sources = [*sorted((ROOT / "src/game_harness").glob("*.py")),
               ROOT / "src/game_harness/games/__init__.py",
               ROOT / "src/game_harness/games/pokemon.py",
               ROOT / "src/game_harness/games/pokemon_bridge.cjs",
               ROOT / "src/game_harness/games/node_bridge.py",
               ROOT / "scripts/run_overcooked.py", ROOT / "scripts/setup_pokemon.sh",
               Path(__file__), ROOT / "scripts/audit_pokemon_run.py",
               ROOT / "requirements-pokemon.lock"]
    manifest = {"schema": "pokemon-real-cycle/v1", "created_at": utcnow(),
        "runtime_fingerprint": fingerprint,
        "source_hashes": {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                          for path in sources},
        "game_context": context, "config": config,
        "decision_model": settings.decision_model, "coach_model": settings.coach_model,
        "think_mode": "never", "identity_verification": "service_reported_name",
        "schedule": {"train_seeds": [42], "validation_seeds": [4243],
                     "promotion_seeds": [1042, 2043, 3042, 4043]},
        "budget": {"max_episodes": 14, "decision_request_attempts_max": 14 * max_steps,
                   "coach_attempts_max": 4, "gepa_reflections": 1},
        "interpretation": "Paired private seeds freeze team composition, lead order and initial native RNG; candidate seats alternate. Different policies can consume RNG differently. Remote model sampling is not controlled. Fixed six-species pool, no ladder or unseen-team generalization or statistical significance claim.",
        "protocol": "GLM initial Harness; real trigger; GEPA train/selection; only if selected changes, 4 alternating-order held-out-seed empirical pairs, balanced by candidate seat. Budget cutoffs are censored and cannot enter training, selection or promotion. No manual policy editing, shaped score, retries, or forced promotion."}
    atomic_json(directory / "protocol.json", manifest)
    lab = EvidenceLab(settings, directory, export_dir=ROOT / "harnesses")
    lab.frozen_protocol = manifest
    lab.config = config
    lab.scope = digest({"game_context": context, "model": settings.decision_model,
                        "endpoint": settings.decision_url, "think_mode": "never"})[:20]
    lab.store.put("metadata", {"id": "last_run", "scope": lab.scope, "config": config})
    provider = RecordingDecision(settings, directory, manifest["budget"]["decision_request_attempts_max"])
    coach = RecordingCoach(settings, directory, lab.coach_called)
    job = None
    try:
        atomic_json(directory / "health.json", provider.health())
        brief, candidate = coach.initialize(game_context=context)
        atomic_json(directory / "initialization-contract.json", coach.initialization_contract)
        harness = lab.version(candidate, lab.scope,
            {"type": "bootstrap", "coach": settings.coach_model,
             "initialization": coach.initialization_provenance}, brief=brief, game_context=context)
        lab.store.activate(lab.scope, harness["id"], None)
        print(json.dumps({"initialized": harness["id"], "protocol_hash": digest(manifest)}), flush=True)
        episode = lab.play(provider, harness, config, 42, "play")
        job = lab.new_job(episode, config, lab.scope)
        job.update(deepcopy(manifest["schedule"]))
        lab.store.put("job", job)
        lab.optimize(job, provider, coach)
        saved_job = lab.store.get("job", job["id"])
        if saved_job["status"] not in {"promoted", "retained"}:
            raise RuntimeError("Evolution did not complete: " + saved_job.get("error", saved_job["status"]))
        episodes = list(reversed(lab.store.list("episode", 10000)))
        summary = {"status": "completed", "at": utcnow(), "protocol_hash": digest(manifest),
            "initial_harness": harness["id"], "active_harness": lab.store.active(lab.scope)["id"],
            "job": lab.store.get("job", job["id"]), "episodes": episodes,
            "decision_attempts": provider.attempts, "coach_calls": coach.calls,
            "interpretation": manifest["interpretation"]}
        atomic_json(directory / "summary.json", summary)
        print(json.dumps({"status": "completed", "job": summary["job"]["status"],
                          "scores": [ep["score"] for ep in episodes]}), flush=True)
    except Exception as exc:
        atomic_json(directory / "failed.json", {"at": utcnow(), "error_type": type(exc).__name__,
                    "error": str(exc)[:500], "decision_attempts": provider.attempts,
                    "coach_attempts": coach.attempts, "job_id": job["id"] if job else None})
        raise
    finally:
        provider.close()
        coach.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "serve"))
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--max-steps", type=int, default=160, help="Decision choices; budget cutoff is excluded from evaluation")
    parser.add_argument("--port", type=int, default=18767)
    args = parser.parse_args()
    if args.command == "run":
        run(args.data_dir.resolve(), args.max_steps)
    else:
        import uvicorn
        from game_harness.app import create_app
        uvicorn.run(create_app(data_dir=args.data_dir.resolve()), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
