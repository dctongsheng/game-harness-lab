#!/usr/bin/env python3
"""Read-only native replay of saved Overcooked episodes and Decision evidence.

No model is called and the registry is opened read-only. Seed labels identify
repeated trials on a deterministic layout, not different unseen environments.
Incomplete games are replayed as prefixes but excluded from score aggregates.
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

from game_harness.games import get_game
from game_harness.providers import digest


ACTIONS = {"north", "south", "east", "west", "stay", "interact"}
TURN_FIELDS = {"step", "actor", "acting_player"}


class AuditError(ValueError):
    """Saved evidence contradicts the frozen policy or native replay."""


def require(condition, message):
    if not condition:
        raise AuditError(message)


def _finite_number(value, *, minimum=0):
    return type(value) in (int, float) and math.isfinite(value) and value >= minimum


def expected_request(episode, before, criteria, harness):
    """Rebuild the unmodified game-neutral runner's actual Decision arguments."""
    state = deepcopy(before)
    state.update(episode_id=episode["episode_id"], observation_hash=digest(before),
                 candidates=deepcopy(criteria), remaining_steps=episode["config"]["max_steps"] - before["step"])
    policy = harness["candidate"]
    instructions = (episode["game_context"]["rules"] + "\n本局冻结的决策指导：\n"
                    + policy["decision_instructions"] + "\n策略参考：\n" + policy["strategy_notes"])
    return {"state": state, "instructions": instructions, "criteria": deepcopy(criteria),
            "request_id": f"{episode['episode_id']}:step:{before['step']}:{digest(before)[:16]}",
            "think_mode": "never", "allow_none": False}


def _receipt(receipt, step, episode, harness, require_requests):
    before, criteria, action = step["before"], step["criteria"], step["action"]
    request = expected_request(episode, before, criteria, harness)
    require(receipt["request_id"] == request["request_id"], "request is not bound to this episode observation")
    require(type(receipt["observation_revision"]) is int and receipt["observation_revision"] == before["step"],
            "receipt revision mismatch")
    require(receipt["candidate_hash"] == digest(criteria), "receipt catalog hash mismatch")
    require(receipt["choice_id"] == action, "receipt action mismatch")
    require(receipt["think_mode"] == "never", "request did not disable thinking")
    raw = receipt["raw_response"]
    require(receipt["response_hash"] == digest(raw), "raw response hash mismatch")
    require(raw["model"] == receipt["model"] == episode["model"] == harness["decision_model"],
            "fixed Decision model identity mismatch")
    answer = raw["answers"]["primary_action"]
    require(answer["choice"] == action, "raw response action mismatch")
    probabilities = answer["probabilities"]
    require(set(probabilities) == ACTIONS, "raw probabilities do not cover all six actions")
    require(all(_finite_number(value) and value <= 1
                for value in [answer["confidence"], *probabilities.values()]), "invalid decision probabilities")
    require(abs(sum(probabilities.values()) - 1) <= .02, "decision probabilities do not sum to one")
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
    require(recorded == request, "recorded Decision request differs from frozen policy or public observation")
    payload = deepcopy(receipt.get("request_payload"))
    if payload is None:
        require(not require_requests, "actual outgoing Decision payload is missing")
        return False
    require(isinstance(payload, dict) and isinstance(payload.get("state"), str),
            "actual outgoing Decision payload is malformed")
    # Compare parsed content because equivalent JSON state whitespace is allowed.
    payload["state"] = json.loads(payload["state"])
    expected = {"model": harness["decision_model"], "state": request["state"],
                "questions": {"primary_action": {"type": "choice", "instructions": request["instructions"],
                              "criteria": {item["id"]: item["label"] for item in criteria}, "allow_none": False}},
                "think_mode": "never", "return_details": True, "allow_none": False}
    require(payload == expected, "actual outgoing Decision payload differs from frozen policy or public observation")
    return True


def audit_episode(episode, harness, *, require_requests=True):
    """Audit one result against its frozen harness, including incomplete prefixes."""
    session = None
    try:
        require(episode["game_id"] == "overcooked", "not an Overcooked episode")
        adapter = get_game("overcooked")
        config = episode["config"]
        context = adapter.context(config)
        require(episode["rules_version"] == adapter.rules_version, "installed rules version mismatch")
        require(episode["game_context"] == context == harness["game_context"], "fixed game context mismatch")
        require(harness["id"] == episode["harness_id"] and harness["game_id"] == "overcooked",
                "frozen Harness identity mismatch")
        require(harness.get("think_mode", "never") == "never", "Harness thinking mode is not never")
        policy_hash = digest(harness["candidate"])
        require(episode["policy_hashes"] == {"candidate": policy_hash}, "frozen policy hash mismatch")
        require(adapter.opponent_policy is None, "cooperative game must not use a scripted opponent")
        incomplete = bool(episode.get("infra_error") or episode.get("failure")
                          or episode.get("termination_reason") in {"stopped", "infra_error"})
        session = adapter.create(episode["seed"], config)
        initial = session.snapshot()
        require(initial["native_tick"] == 0 and initial["step"] == 0 and initial["score"] == 0,
                "native episode did not begin at the standard zero-score start")
        trace = episode["trace"]
        actors, latencies, requests, committed_actions, committed_score_deltas = Counter(), [], 0, [], []
        deliveries_by_player = [0, 0]
        scoring_deliveries_by_player = [0, 0]
        for index, step in enumerate(trace):
            before = session.snapshot()
            require(before == step["before"], "before-state differs from native replay")
            require(before["step"] == index and not before["done"], "choice outside the active episode")
            require(before["native_tick"] == index // 2, "native world advanced before a joint action")
            actor = f"chef_{index % 2}"
            require(before["acting_player"] == index % 2 and before["actor"] == step["actor"] == actor,
                    "chef order mismatch")
            require(before["policy_role"] == step["policy_role"] == "candidate", "chef is not Decision candidate")
            require(step["policy_hash"] == policy_hash, "chef did not use frozen candidate policy")
            criteria = session.candidates()
            require(len(criteria) == 6 and {item["id"] for item in criteria} == ACTIONS,
                    "adapter did not expose all six actions")
            require(step["criteria"] == criteria and step["candidate_hash"] == digest(criteria),
                    "recorded action catalog was pruned or changed")
            require(step["action"] in ACTIONS, "invalid native action")
            requests += _receipt(step["receipt"], step, episode, harness, require_requests)
            after = session.step(step["action"])
            require(after == step["after"] == session.snapshot(), "after-state differs from native replay")
            require(step["applied_action"] == after.get("applied_action", step["action"]),
                    "applied action differs from selected action")
            require(after["step"] == index + 1, "adapter did not advance one Decision choice")
            if index % 2 == 0:
                before_world = {key: value for key, value in before.items() if key not in TURN_FIELDS}
                after_world = {key: value for key, value in after.items() if key not in TURN_FIELDS}
                require(after_world == before_world,
                        "first chef choice advanced the world or leaked pending action to second chef")
            else:
                require(after["native_tick"] == before["native_tick"] + 1,
                        "paired choices did not advance exactly one native tick")
                pair = [trace[index - 1]["action"], step["action"]]
                require(after["last_joint_actions"] == pair, "native joint action differs from both Decision choices")
                committed_actions.append(pair)
                delta = after["score"] - before["score"]
                require(_finite_number(delta), "native sparse score decreased or became invalid")
                transition = after["last_transition"]
                require(transition["native_tick"] == after["native_tick"]
                        and transition["sparse_reward"] == delta, "native transition reward mismatch")
                rewards = transition["sparse_reward_by_player"]
                require(isinstance(rewards, list) and len(rewards) == 2
                        and all(_finite_number(value) for value in rewards) and sum(rewards) == delta,
                        "sparse rewards from both chefs do not sum to game score delta")
                delivered = transition["delivery_by_player"]
                require(isinstance(delivered, list) and len(delivered) == 2
                        and all(type(value) is bool for value in delivered), "delivery events are malformed")
                for player in (0, 1):
                    deliveries_by_player[player] += int(delivered[player])
                    scoring_deliveries_by_player[player] += int(delivered[player] and rewards[player] > 0)
                committed_score_deltas.append(delta)
            actors[actor] += 1
            latencies.append(step["receipt"]["elapsed_ms"])
        final = session.snapshot()
        require(episode["steps"] == len(trace) == final["step"], "episode choice count mismatch")
        require(final == episode["final_state"], "final native state mismatch")
        require(episode["score"] == final["score"] == sum(committed_score_deltas), "native sparse score mismatch")
        require(final["native_tick"] == len(committed_actions), "joint tick total mismatch")
        require(final["delivery_counts"] == deliveries_by_player
                and final["deliveries"] == sum(deliveries_by_player), "native delivery event count mismatch")
        if not incomplete:
            require(final["done"] and len(trace) % 2 == 0, "completed episode ended before a joint tick")
            require(episode["termination_reason"] == final["reason"], "native termination reason mismatch")
        require(episode["latencies_ms"] == latencies, "latency list differs from action receipts")
        return {"episode_id": episode["episode_id"], "harness_id": harness["id"],
                "purpose": episode.get("purpose"), "seed_trial_label": episode["seed"],
                "layout": config["overcooked_layout"], "steps": len(trace),
                "native_ticks": final["native_tick"], "actors": dict(actors),
                "deliveries": final["deliveries"], "deliveries_by_player": deliveries_by_player,
                "scoring_deliveries": sum(scoring_deliveries_by_player),
                "scoring_deliveries_by_player": scoring_deliveries_by_player,
                "status": "excluded_incomplete" if incomplete else "audited_completed",
                "valid_for_evaluation": not incomplete,
                "score": None if incomplete else final["score"],
                "observed_prefix_score": final["score"] if incomplete else None,
                "termination_reason": episode["termination_reason"],
                "recorded_request_count": requests, "decision_calls": len(trace),
                "thinking_off_verified_calls": len(trace),
                "decision_latency_total_ms": sum(latencies),
                "native_sparse_reward_deltas": committed_score_deltas,
                "source_hash": digest(episode)}
    except AuditError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise AuditError(f"malformed episode evidence ({type(exc).__name__})") from None
    finally:
        if session is not None:
            session.close()


def audit(root, *, require_requests=True):
    """Audit only Overcooked records in a Lab Store, without mutating it."""
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
        if episode.get("game_id") != "overcooked":
            continue
        require(episode.get("episode_id") == path.stem, "episode filename does not match ID")
        require(episode.get("harness_id") in versions, "frozen episode Harness is missing")
        try:
            results.append(audit_episode(episode, versions[episode["harness_id"]], require_requests=require_requests))
        except AuditError as exc:
            raise AuditError(f"{path.stem}: {exc}") from None
    require(results, "No saved Overcooked episodes")
    completed = [item for item in results if item["valid_for_evaluation"]]
    groups = {}
    for item in results:
        group = groups.setdefault(item["harness_id"], {"completed": 0, "incomplete": 0, "scores": []})
        if item["valid_for_evaluation"]:
            group["completed"] += 1
            group["scores"].append(item["score"])
        else:
            group["incomplete"] += 1
    for group in groups.values():
        group["mean_score"] = statistics.mean(group["scores"]) if group["scores"] else None
    calls = sum(item["decision_calls"] for item in results)
    return {"schema": "overcooked-native-audit-v1", "model_requests_made_by_audit": 0,
            "episode_count": len(results), "completed_episodes": len(completed),
            "excluded_incomplete_episodes": len(results) - len(completed),
            "audited_decision_calls": calls, "native_joint_ticks": sum(item["native_ticks"] for item in results),
            "recorded_request_count": sum(item["recorded_request_count"] for item in results),
            "all_calls_explicitly_report_thinking_off": bool(calls),
            "all_recorded_states_replayed": True, "both_chef_actions_match_full_catalog_receipts": True,
            "first_chef_pending_choices_hidden": True,
            "delivery_semantics": "deliveries counts native soup_delivery events, including zero-point wrong recipes; scoring_deliveries counts positive-reward delivery events",
            "seed_interpretation": "repeated trial labels; native standard starts are deterministic per layout",
            "statistical_claim": False, "by_harness": groups, "episodes": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="Lab data directory containing registry.sqlite and episodes/")
    parser.add_argument("--allow-unrecorded-requests", action="store_true",
                        help="replay older receipts but report missing full request evidence")
    args = parser.parse_args()
    print(json.dumps(audit(args.root, require_requests=not args.allow_unrecorded_requests),
                     ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
