#!/usr/bin/env python3
"""Run one bounded native Overcooked bootstrap/GEPA cycle with HTTP evidence.

Raw evidence stays in ignored data/. Only an approved candidate is exported.
No resume/retry is implicit: a new run requires a new evidence directory.
"""
from __future__ import annotations

import argparse
import hashlib
from copy import deepcopy
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import httpx

from game_harness.games import get_game
from game_harness.providers import CoachProvider, DecisionProvider, ProviderError, Settings, digest
from game_harness.service import DEFAULTS, Lab
from game_harness.store import atomic_json, utcnow


def append(path, value):
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n")


class RecordingDecision(DecisionProvider):
    def __init__(self, settings, directory, budget):
        self.directory, self.budget, self.attempts = directory, budget, 0
        self.payloads = []

        def capture(request):
            if request.method == "POST":
                # Never retain headers, authorization or endpoint credentials.
                self.payloads.append(json.loads(request.content))

        super().__init__(settings, client=httpx.Client(
            timeout=settings.decision_timeout, trust_env=False,
            event_hooks={"request": [capture]}))

    def decide(self, state, instructions, criteria, request_id):
        if self.attempts >= self.budget:
            raise ProviderError("Frozen Decision request budget exhausted")
        self.attempts += 1
        self.payloads.clear()
        append(self.directory / "calls.jsonl", {"at": utcnow(), "kind": "decision_started", "request_id": request_id})
        receipt = super().decide(state, instructions, criteria, request_id)
        if len(self.payloads) != 1:
            raise ProviderError("Expected one captured HTTP request per Decision choice")
        if (receipt.get("think_used") is not False
                or type(receipt.get("reasoning_tokens")) is not int or receipt["reasoning_tokens"] != 0):
            raise ProviderError("Experiment requires explicit server evidence of zero thinking")
        receipt["request_payload"] = deepcopy(self.payloads[0])
        receipt["request_evidence"] = {
            "state": deepcopy(state), "instructions": instructions,
            "criteria": deepcopy(criteria), "request_id": request_id,
            "think_mode": "never", "allow_none": False,
        }
        append(self.directory / "calls.jsonl", {"at": utcnow(), "kind": "decision_completed",
            "request_id": request_id, "elapsed_ms": receipt["elapsed_ms"],
            "model": receipt["model"], "usage": receipt["raw_response"].get("usage", {})})
        return receipt


class RecordingCoach(CoachProvider):
    def __init__(self, settings, directory, on_call):
        super().__init__(settings, on_call=on_call)
        self.directory, self.attempts = directory, 0

    def complete(self, messages, purpose="reflection"):
        if self.attempts >= 4:
            raise ProviderError("Frozen LLM request budget exhausted")
        self.attempts += 1
        content = super().complete(messages, purpose)
        atomic_json(self.directory / f"coach-{self.attempts:02d}.json", {
            "purpose": purpose, "messages": messages, "response": content, "call": self.calls[-1]})
        return content


class EvidenceLab(Lab):
    def play(self, provider, harness, config, seed, purpose, job_id=None):
        result = super().play(provider, harness, config, seed, purpose, job_id)
        if result.get("infra_error") or result.get("termination_reason") == "stopped":
            raise ProviderError("Incomplete episode; not a zero-score policy result")
        # Independent upstream replay is performed before this score reaches GEPA.
        from audit_overcooked_run import audit_episode
        evidence = audit_episode(result, harness)
        atomic_json(self.store.root / "audits" / (result["episode_id"] + ".json"), evidence)
        print(json.dumps({"episode": result["episode_id"], "purpose": purpose,
                          "score": result["score"], "steps": result["steps"]}), flush=True)
        return result


def run(directory, layout, max_steps):
    if directory.exists():
        raise ValueError("Evidence directory already exists; use a fresh --data-dir")
    settings = Settings.load()
    adapter = get_game("overcooked")
    config = {**DEFAULTS, **adapter.defaults, "game_id": "overcooked",
              "overcooked_layout": layout, "max_steps": max_steps,
              "train_games": 1, "validation_games": 1, "promotion_games": 4,
              "reflection_rounds": 1, "episodes": 1, "seed": 42}
    adapter.validate_config(config)
    if not 8 <= max_steps <= 1000:
        raise ValueError("max_steps must be 8..1000 and even")
    context = adapter.context(config)
    # Verify the native install before creating an experimental evidence record.
    session = adapter.create(config["seed"], config)
    session.close()
    directory.mkdir(parents=True)
    sources = [*sorted((ROOT / "src/game_harness").glob("*.py")),
               ROOT / "src/game_harness/games/__init__.py",
               ROOT / "src/game_harness/games/overcooked.py",
               ROOT / "src/game_harness/games/overcooked_worker.py",
               Path(__file__), ROOT / "scripts/audit_overcooked_run.py",
               ROOT / "requirements-overcooked.txt"]
    manifest = {"schema": "overcooked-real-cycle/v1", "created_at": utcnow(),
        "source_hashes": {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                          for path in sources},
        "game_context": context, "config": config,
        "decision_model": settings.decision_model, "coach_model": settings.coach_model,
        "think_mode": "never", "identity_verification": "service_reported_name",
        "schedule": {"train_seeds": [42], "validation_seeds": [4242],
                     "promotion_seeds": [1042, 2042, 3042, 4042]},
        "budget": {"max_episodes": 14, "decision_request_attempts_max": 14 * max_steps,
                   "coach_attempts_max": 4, "gepa_reflections": 1},
        "interpretation": "Same deterministic standard layout; seed IDs are repeated trials, not independent maps or controlled model RNG. No claim of generalization or significance.",
        "protocol": "GLM initial Harness; real trigger; GEPA train/selection; only if selected changes, 4 alternating-order held-out empirical pairs. No manual policy editing, shaped score, retries, or forced promotion."}
    atomic_json(directory / "protocol.json", manifest)
    lab = EvidenceLab(settings, directory, export_dir=ROOT / "harnesses")
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
    parser.add_argument("--layout", choices=("cramped_room", "asymmetric_advantages", "coordination_ring"), default="cramped_room")
    parser.add_argument("--max-steps", type=int, default=240, help="Decision calls; two calls per native tick")
    parser.add_argument("--port", type=int, default=18766)
    args = parser.parse_args()
    if args.command == "run":
        run(args.data_dir.resolve(), args.layout, args.max_steps)
    else:
        import uvicorn
        from game_harness.app import create_app
        uvicorn.run(create_app(data_dir=args.data_dir.resolve()), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
