#!/usr/bin/env python3
"""Ask the real coach for an isolated, game-specific Decision Harness.

This writes a research parent and its full coach evidence, never a live champion.
The Decision preflight verifies identity/thinking; initialization does not play.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path

from game_harness.games import get_game
from game_harness.providers import CoachProvider, DecisionProvider, Settings, digest
from game_harness.store import atomic_json, utcnow


def initialize(game_id: str, destination: Path) -> dict:
    if destination.exists():
        raise ValueError("Destination already exists; preserve prior initialization evidence")
    settings = Settings.load()
    adapter = get_game(game_id)
    config = {**adapter.defaults, "game_id": game_id}
    context = adapter.context(config)
    player = DecisionProvider(settings)
    try:
        health = player.health()
    finally:
        player.close()
    destination.mkdir(parents=True)
    atomic_json(destination / "request-manifest.json", {
        "game_id": game_id, "game_context": context, "config": config,
        "decision_model": settings.decision_model, "think_mode": "never",
        "coach_model": settings.coach_model, "created_at": utcnow(),
        "purpose": "isolated shared parent for feedback experiment", "health": health,
    })

    class RecordingCoach(CoachProvider):
        def complete(self, messages, purpose="reflection"):
            number = len(list(destination.glob("call-*.input.json"))) + 1
            atomic_json(destination / f"call-{number}.input.json", deepcopy(messages))
            response = super().complete(messages, purpose)
            atomic_json(destination / f"call-{number}.output.json", {
                "text": response, "call": deepcopy(self.calls[-1]),
            })
            return response

    coach = RecordingCoach(settings)
    try:
        brief, candidate = coach.initialize(game_context=context)
        scope = digest({"game_context": context, "model": settings.decision_model,
                        "think_mode": "never", "experiment_parent": str(destination.resolve())})[:20]
        harness = {
            "id": "h-" + digest({"candidate": candidate, "scope": scope})[:16],
            "candidate": candidate, "scope": scope, "parent_id": None,
            "brief": brief, "game_id": game_id, "game_context": context,
            "rules_version": adapter.rules_version, "config": config,
            "decision_model": settings.decision_model, "think_mode": "never",
            "created_at": utcnow(), "identity_verification": "service_reported_name",
            "provenance": {"type": "experimental_bootstrap", "coach": settings.coach_model,
                           "initialization": deepcopy(getattr(coach, "initialization_provenance", None)),
                           "calls": deepcopy(coach.calls)},
        }
        atomic_json(destination / "parent.json", harness)
        return harness
    finally:
        coach.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game", required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    result = initialize(arguments.game, arguments.output)
    print(json.dumps({"game_id": result["game_id"], "harness_id": result["id"],
                      "parent_path": str((arguments.output / "parent.json").resolve()),
                      "live_champion_changed": False}, ensure_ascii=False))
