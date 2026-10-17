#!/usr/bin/env python3
"""Read-only native replay and receipt audit for saved poke-env battles.

The simulator is replayed from private seeds and recorded Decision choices. No
model is called, no registry is modified, and cutoffs never enter score means.
Raw evidence is local-only; this report contains no teams, seeds, or requests.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import json
import math
from pathlib import Path
import sqlite3
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from game_harness.games import get_game
from game_harness.providers import digest


OUTCOME_SCORES = {"win": 100, "loss": 0, "draw": 50}


class AuditError(ValueError):
    """Evidence contradicts the frozen policy, receipt, or native replay."""


def require(condition, message):
    if not condition:
        raise AuditError(message)


def _finite_number(value, *, minimum=0):
    return type(value) in (int, float) and math.isfinite(value) and value >= minimum


def _public_projection_is_request_free(value):
    if isinstance(value, list):
        return all(_public_projection_is_request_free(item) for item in value)
    if isinstance(value, dict):
        forbidden = {"seed", "request", "request_payload", "request_evidence", "criteria",
                     "pending_action", "pending_choices"}
        return not forbidden.intersection(value) and all(
            _public_projection_is_request_free(item) for item in value.values())
    return True


def expected_request(episode, before, criteria, harness):
    """Reconstruct the game-neutral runtime's role-specific Decision request."""
    role = before["policy_role"]
    require(role in {"candidate", "opponent"}, "unknown policy role")
    policy = harness["candidate"] if role == "candidate" else episode["game_context"]["opponent_policy"]
    state = deepcopy(before)
    state.update(episode_id=episode["episode_id"], observation_hash=digest(before),
                 candidates=deepcopy(criteria), remaining_steps=episode["config"]["max_steps"] - before["step"])
    instructions = (episode["game_context"]["rules"] + "\n本局冻结的决策指导：\n"
                    + policy["decision_instructions"] + "\n策略参考：\n" + policy["strategy_notes"])
    return {"state": state, "instructions": instructions, "criteria": deepcopy(criteria),
            "request_id": f"{episode['episode_id']}:step:{before['step']}:{digest(before)[:16]}",
            "think_mode": "never", "allow_none": False}


def _receipt(receipt, step, episode, harness, require_requests):
    before, criteria, action = step["before"], step["criteria"], step["action"]
    allowed = {item["id"] for item in criteria}
    request = expected_request(episode, before, criteria, harness)
    require(receipt["request_id"] == request["request_id"], "request is not bound to this observation")
    require(type(receipt["observation_revision"]) is int and receipt["observation_revision"] == before["step"],
            "receipt revision mismatch")
    require(receipt["candidate_hash"] == digest(criteria), "receipt catalog hash mismatch")
    require(receipt["choice_id"] == action, "receipt action mismatch")
    require(receipt["think_mode"] == "never", "request did not disable thinking")
    raw = receipt["raw_response"]
    require(receipt["response_hash"] == digest(raw), "raw response hash mismatch")
    require(raw["model"] == receipt["model"] == episode["model"] == harness["decision_model"],
            "frozen Decision model identity mismatch")
    answer = raw["answers"]["primary_action"]
    require(answer["choice"] == action, "raw response action mismatch")
    probabilities = answer["probabilities"]
    require(set(probabilities) == allowed, "raw probabilities do not cover the complete legal catalog")
    require(all(_finite_number(value) and value <= 1
                for value in [answer["confidence"], *probabilities.values()]), "invalid probabilities")
    require(abs(sum(probabilities.values()) - 1) <= .02, "probabilities do not sum to one")
    thought = answer.get("details", {}).get("thought", {})
    require(thought.get("used") is False and type(thought.get("tokens")) is int and thought["tokens"] == 0,
            "raw response must explicitly report thinking off and zero reasoning tokens")
    require(receipt.get("think_used") is False and type(receipt.get("reasoning_tokens")) is int
            and receipt["reasoning_tokens"] == 0, "thinking receipt differs from raw response")
    require(_finite_number(receipt["elapsed_ms"]), "invalid decision latency")
    recorded = receipt.get("request_evidence")
    if recorded is None:
        require(not require_requests, "complete Decision request evidence is missing")
        return False
    require(recorded == request, "recorded request differs from frozen role policy or observation")
    payload = deepcopy(receipt.get("request_payload"))
    if payload is None:
        require(not require_requests, "actual outgoing Decision payload is missing")
        return False
    require(isinstance(payload, dict) and isinstance(payload.get("state"), str), "malformed outgoing payload")
    payload["state"] = json.loads(payload["state"])
    expected = {"model": harness["decision_model"], "state": request["state"],
                "questions": {"primary_action": {"type": "choice", "instructions": request["instructions"],
                              "criteria": {item["id"]: item["label"] for item in criteria}, "allow_none": False}},
                "think_mode": "never", "return_details": True, "allow_none": False}
    require(payload == expected, "outgoing payload differs from frozen role policy or observation")
    return True


def audit_episode(episode, harness, *, require_requests=True):
    """Replay one complete battle or incomplete prefix without model requests."""
    session = None
    try:
        require(episode["game_id"] == "pokemon", "not a Pokemon episode")
        adapter = get_game("pokemon")
        config = episode["config"]
        context = adapter.context(config)
        require(episode["rules_version"] == adapter.rules_version, "installed rules version mismatch")
        require(episode["game_context"] == context == harness["game_context"], "frozen game context mismatch")
        require(harness["id"] == episode["harness_id"] and harness["game_id"] == "pokemon",
                "frozen Harness identity mismatch")
        require(harness.get("think_mode", "never") == "never", "Harness thinking mode is not never")
        require(isinstance(adapter.opponent_policy, dict) and context["opponent_policy"] == adapter.opponent_policy,
                "frozen Decision opponent policy is missing or changed")
        policies = {"candidate": digest(harness["candidate"]), "opponent": digest(adapter.opponent_policy)}
        require(episode["policy_hashes"] == policies, "frozen policy hashes mismatch")
        incomplete = bool(episode.get("infra_error") or episode.get("failure")
                          or episode.get("termination_reason") in {"stopped", "infra_error"})
        session = adapter.create(episode["seed"], config)
        initial = session.snapshot()
        require(initial["step"] == 0 and not initial["done"] and not initial["native_terminal"],
                "battle did not begin at an active native start")
        require(initial["candidate_seat"] == episode["seed"] % 2, "candidate seat is not seed-balanced")
        trace = episode["trace"]
        actors, roles, latencies, requests = Counter(), Counter(), [], 0
        for index, step in enumerate(trace):
            before = session.snapshot()
            require(before == step["before"], "before-state differs from native replay")
            require(before["step"] == index and not before["done"], "choice outside active battle")
            role = before["policy_role"]
            require(step["policy_role"] == role and role in policies, "acting policy role mismatch")
            require(step["actor"] == before["actor"], "acting player mismatch")
            require(step["policy_hash"] == policies[role], "player did not use its frozen policy")
            require(before["candidate_seat"] == initial["candidate_seat"], "candidate seat changed")
            criteria = session.candidates()
            require(criteria and all(set(item) == {"id", "label"} for item in criteria), "malformed legal catalog")
            require(len({item["id"] for item in criteria}) == len(criteria), "duplicate legal choices")
            require(step["criteria"] == criteria and step["candidate_hash"] == digest(criteria),
                    "legal action catalog was pruned or changed")
            require(step["action"] in {item["id"] for item in criteria}, "selected action is illegal")
            requests += _receipt(step["receipt"], step, episode, harness, require_requests)
            after = session.step(step["action"])
            require(after == step["after"] == session.snapshot(), "after-state differs from native replay")
            require(step["applied_action"] == after.get("applied_action", step["action"]),
                    "applied action differs from Decision choice")
            require(after["step"] == index + 1, "one Decision choice did not advance one observation")
            # Native public state cannot expose a privately staged choice. The
            # independent replay also checks the full per-player request view.
            require(isinstance(before.get("public_view"), dict) and isinstance(after.get("public_view"), dict),
                    "spectator public projection is missing")
            for view in (before["public_view"], after["public_view"]):
                require(_public_projection_is_request_free(view),
                        "private request or pending choice in public projection")
            actors[step["actor"]] += 1
            roles[role] += 1
            latencies.append(step["receipt"]["elapsed_ms"])
        final = session.snapshot()
        require(episode["steps"] == len(trace) == final["step"], "episode choice count mismatch")
        require(final == episode["final_state"], "final state differs from native replay")
        require(episode["score"] == final["score"], "episode score differs from native replay")
        require(episode["latencies_ms"] == latencies, "latency list differs from receipts")
        native = final["native_terminal"]
        truncated = final["truncated"]
        require(type(native) is bool and type(truncated) is bool and not (native and truncated),
                "contradictory native-terminal and truncation flags")
        if native:
            result = final["result"]
            require(final["done"] and result in OUTCOME_SCORES, "native terminal outcome is invalid")
            require(final["score"] == OUTCOME_SCORES[result], "native outcome score mismatch")
            winner = final["winner"]
            expected_winner = (None if result == "draw" else initial["candidate_seat"] if result == "win"
                               else 1 - initial["candidate_seat"])
            require(winner == expected_winner and (winner is None or type(winner) is int), "native winner perspective mismatch")
            require(final["reason"] == ("native_tie" if result == "draw" else "native_win"),
                    "native terminal reason mismatch")
        elif truncated:
            require(final["done"] and final["result"] == "unresolved" and final["winner"] is None,
                    "cutoff was mislabeled as a native outcome")
            require(final["reason"] == "step_limit" and final["step"] == config["max_steps"],
                    "cutoff was not caused by the frozen action budget")
            require(final["score"] == 50, "cutoff compatibility placeholder changed")
        elif not incomplete:
            require(False, "completed episode has no native outcome or explicit cutoff")
        if not incomplete:
            require(final["done"] and episode["termination_reason"] == final["reason"], "termination reason mismatch")
        valid = not incomplete and native
        status = "excluded_incomplete" if incomplete else "audited_native_terminal" if native else "excluded_cutoff"
        return {"episode_id": episode["episode_id"], "harness_id": harness["id"],
                "purpose": episode.get("purpose"), "candidate_seat": initial["candidate_seat"],
                "steps": len(trace), "native_turn": final.get("native_turn"),
                "actors": dict(actors), "roles": dict(roles), "status": status,
                "valid_for_evaluation": valid, "native_terminal": native, "truncated": truncated,
                "result": final["result"], "score": final["score"] if valid else None,
                "observed_compatibility_score": final["score"] if not valid else None,
                "termination_reason": episode["termination_reason"],
                "recorded_request_count": requests, "decision_calls": len(trace),
                "thinking_off_verified_calls": len(trace), "decision_latency_total_ms": sum(latencies),
                "source_hash": digest(episode)}
    except AuditError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise AuditError(f"malformed episode evidence ({type(exc).__name__})") from None
    finally:
        if session is not None:
            session.close()


def audit(root, *, require_requests=True):
    """Audit Pokemon records in a Lab Store, with SQLite opened read-only."""
    root = Path(root)
    require((root / "registry.sqlite").is_file(), "No registry.sqlite")
    db = sqlite3.connect((root / "registry.sqlite").resolve().as_uri() + "?mode=ro", uri=True)
    try:
        versions = {row[0]: json.loads(row[1]) for row in
                    db.execute("SELECT id,body FROM records WHERE kind='version'")}
    finally:
        db.close()
    results = []
    for path in sorted((root / "episodes").glob("*.json")):
        episode = json.loads(path.read_text(encoding="utf-8"))
        if episode.get("game_id") != "pokemon":
            continue
        require(episode.get("episode_id") == path.stem, "episode filename does not match ID")
        require(episode.get("harness_id") in versions, "frozen episode Harness is missing")
        try:
            results.append(audit_episode(episode, versions[episode["harness_id"]], require_requests=require_requests))
        except AuditError as exc:
            raise AuditError(f"{path.stem}: {exc}") from None
    require(results, "No saved Pokemon episodes")
    groups = {}
    for item in results:
        group = groups.setdefault(item["harness_id"], {"native_completed": 0, "incomplete": 0,
                        "cutoffs": 0, "wins": 0, "losses": 0, "draws": 0, "scores": []})
        if item["valid_for_evaluation"]:
            group["native_completed"] += 1
            group["scores"].append(item["score"])
            group[{"win": "wins", "loss": "losses", "draw": "draws"}[item["result"]]] += 1
        elif item["status"] == "excluded_cutoff":
            group["cutoffs"] += 1
        else:
            group["incomplete"] += 1
    for group in groups.values():
        group["mean_score"] = statistics.mean(group["scores"]) if group["scores"] else None
        group["native_win_rate"] = group["wins"] / group["native_completed"] if group["native_completed"] else None
    calls = sum(item["decision_calls"] for item in results)
    return {"schema": "pokemon-native-audit-v1", "model_requests_made_by_audit": 0,
            "episode_count": len(results), "native_completed_episodes": sum(i["valid_for_evaluation"] for i in results),
            "excluded_cutoff_episodes": sum(i["status"] == "excluded_cutoff" for i in results),
            "excluded_incomplete_episodes": sum(i["status"] == "excluded_incomplete" for i in results),
            "audited_decision_calls": calls, "recorded_request_count": sum(i["recorded_request_count"] for i in results),
            "all_calls_explicitly_report_thinking_off": bool(calls), "all_recorded_states_replayed": True,
            "both_sides_use_frozen_decision_policies": True, "complete_native_legal_catalogs_verified": True,
            "cutoffs_excluded_from_score_and_win_rate": True,
            "statistical_claim": False, "by_harness": groups, "episodes": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="Lab evidence directory")
    parser.add_argument("--allow-unrecorded-requests", action="store_true")
    args = parser.parse_args()
    print(json.dumps(audit(args.root, require_requests=not args.allow_unrecorded_requests),
                     ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
