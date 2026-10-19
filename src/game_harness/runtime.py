"""Game-neutral, receipt-checked execution; every actor uses the decision model."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import time


class ReceiptError(ValueError):
    """A decision cannot be bound to this observation and action catalog."""


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _validate_receipt(receipt, request_id, revision, expected_hash, allowed_ids=None):
    if allowed_ids is None:
        # Compatibility for direct users of the old helper. The runner always
        # passes the adapter's complete per-observation catalog.
        from .games import get_game
        session = get_game("snake").create(0, {"board_size": 8, "max_steps": 120})
        try:
            allowed_ids = {item["id"] for item in session.candidates()}
        finally:
            session.close()
    if not isinstance(receipt, dict):
        raise ReceiptError("provider receipt must be an object")
    if receipt.get("request_id") != request_id:
        raise ReceiptError("receipt request_id mismatch")
    if type(receipt.get("observation_revision")) is not int or receipt["observation_revision"] != revision:
        raise ReceiptError("receipt observation_revision mismatch")
    if receipt.get("candidate_hash") != expected_hash:
        raise ReceiptError("receipt candidate_hash mismatch")
    if receipt.get("choice_id") not in allowed_ids:
        raise ReceiptError("receipt choice_id outside the current action catalog")
    if not isinstance(receipt.get("model"), str) or not receipt["model"].strip():
        raise ReceiptError("receipt model is missing")
    elapsed = receipt.get("elapsed_ms")
    if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)) or not math.isfinite(elapsed) or elapsed < 0:
        raise ReceiptError("receipt elapsed_ms must be finite and non-negative")
    if "raw_response" not in receipt:
        raise ReceiptError("receipt raw_response is missing")
    if receipt.get("think_mode") != "never":
        raise ReceiptError("receipt think_mode must be never")
    reasoning_tokens = receipt.get("reasoning_tokens")
    think_used = receipt.get("think_used")
    if reasoning_tokens is not None and (type(reasoning_tokens) is not int or reasoning_tokens != 0):
        raise ReceiptError("receipt reports reasoning tokens")
    if think_used is not None and think_used is not False:
        raise ReceiptError("receipt reports thinking was used")
    _canonical(receipt)


def _snapshot(session) -> dict:
    snapshot = deepcopy(session.snapshot())
    if not isinstance(snapshot, dict):
        raise ReceiptError("adapter snapshot must be a JSON object")
    if type(snapshot.get("step")) is not int or snapshot["step"] < 0:
        raise ReceiptError("adapter snapshot step must be a nonnegative integer")
    if type(snapshot.get("done")) is not bool:
        raise ReceiptError("adapter snapshot done must be a boolean")
    score = snapshot.get("score")
    if type(score) not in (int, float) or not math.isfinite(score):
        raise ReceiptError("adapter snapshot score must be finite")
    if snapshot.get("policy_role", "candidate") not in ("candidate", "opponent"):
        raise ReceiptError("adapter returned an unknown policy_role")
    _canonical(snapshot)
    return snapshot


def _catalog(session) -> list[dict[str, str]]:
    criteria = deepcopy(session.candidates())
    if not isinstance(criteria, list) or not criteria:
        raise ReceiptError("nonterminal adapter state must expose an action catalog")
    if any(not isinstance(item, dict) or set(item) != {"id", "label"}
           or not isinstance(item["id"], str) or not item["id"]
           or not isinstance(item["label"], str) or not item["label"] for item in criteria):
        raise ReceiptError("adapter candidates must contain nonempty id and label strings")
    if len({item["id"] for item in criteria}) != len(criteria):
        raise ReceiptError("adapter candidate IDs must be unique")
    _canonical(criteria)
    return criteria


def _policy_instructions(policy, rules):
    if not isinstance(policy, dict):
        raise ValueError("harness candidate must be an object")
    decision_instructions = policy.get("decision_instructions", "")
    notes = policy.get("strategy_notes", "")
    if not isinstance(decision_instructions, str) or not isinstance(notes, str):
        raise ValueError("harness candidate instructions and strategy_notes must be strings")
    return rules + "\n本局冻结的决策指导：\n" + decision_instructions + "\n策略参考：\n" + notes


def run_episode(provider, harness, *, episode_id, seed, size=8, max_steps=120,
                adapter=None, config=None, on_step=None, should_stop=None,
                wait_if_paused=None, log_path=None, on_spectator=None) -> dict:
    """Run a frozen candidate against adapter-owned rules and optional opponent.

    Adapters expose observations and the complete allowed action catalog, but
    never select an action. Both candidate and opponent turns require a matching
    Decision receipt. The opponent policy is fixed independently of evolution.
    Pause callbacks block without stepping; cancellation discards pending actions.
    """
    if adapter is None:
        from .games import get_game
        adapter = get_game("snake")
    frozen_config = deepcopy(config or {})
    frozen_config.setdefault("board_size", size)
    frozen_config.setdefault("max_steps", max_steps)
    step_budget = frozen_config["max_steps"]
    if type(step_budget) is not int or step_budget < 1:
        raise ValueError("max_steps must be a positive integer")
    frozen_harness = deepcopy(harness)
    from .components import resolve_context_config
    tetris_context_config = resolve_context_config(frozen_harness, adapter.id)
    if tetris_context_config is not None:
        if adapter.id != "tetris":
            raise ValueError("Tetris context is only valid for the Tetris adapter")
        from .tetris_context import TetrisDecisionHarness, validate_config
        tetris_context_config = validate_config(tetris_context_config)
        provider = TetrisDecisionHarness(provider, tetris_context_config)
    frozen_context = deepcopy(adapter.context(frozen_config))
    _canonical(frozen_context)
    rules = frozen_context.get("rules", adapter.rules)
    if not isinstance(rules, str):
        raise ValueError("adapter rules must be text")
    instructions_by_role = {"candidate": _policy_instructions(frozen_harness.get("candidate", {}), rules)}
    opponent_policy = deepcopy(adapter.opponent_policy)
    if opponent_policy is not None:
        instructions_by_role["opponent"] = _policy_instructions(opponent_policy, rules)
    policy_hashes = {"candidate": _digest(frozen_harness.get("candidate", {}))}
    if opponent_policy is not None:
        policy_hashes["opponent"] = _digest(opponent_policy)
    trace, latencies = [], []
    model = failure = reason = request_id = pending_receipt = None
    expected_hash = None
    session = None
    last_state = {"game_id": adapter.id, "score": 0, "step": 0, "done": False, "reason": None}
    started = time.monotonic()
    log_file = None
    spectator_cursor = 0
    spectator_error = None

    def log(event):
        if log_file is not None:
            log_file.write(_canonical(event) + "\n")
            log_file.flush()

    def stopped():
        return bool(should_stop is not None and should_stop())

    def publish_spectator():
        # Presentation is a side channel: never merge it into state, instructions,
        # receipts or action selection. A failed viewer cannot choose or stop moves.
        nonlocal spectator_cursor, spectator_error
        if on_spectator is None or spectator_error is not None:
            return
        events = getattr(session, 'spectator_events', None)
        if events is None:
            return
        try:
            delta = events(spectator_cursor)
            on_spectator(deepcopy(delta))
            spectator_cursor = delta['cursor']
        except Exception as exc:
            spectator_error = {'type': type(exc).__name__, 'message': 'Public spectator stream unavailable'}

    def fail(exc, stage=None):
        details = {"type": type(exc).__name__, "message": str(exc),
                   "request_id": request_id, "observation_revision": last_state["step"]}
        if stage:
            details["stage"] = stage
        if pending_receipt is not None:
            try:
                _canonical(pending_receipt)
                details["rejected_or_pending_receipt"] = deepcopy(pending_receipt)
            except (TypeError, ValueError, RecursionError):
                details["receipt_unserializable"] = True
        if tetris_context_config is not None and getattr(provider, "pending_context", None) is not None:
            details["tetris_pending_context"] = deepcopy(provider.pending_context)
        return details

    try:
        session = adapter.create(seed, frozen_config)
        last_state = _snapshot(session)
        if log_path is not None:
            path = Path(log_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            log_file = path.open("x", encoding="utf-8")
        initial_catalog = _catalog(session) if not last_state["done"] else []
        expected_hash = _digest(initial_catalog)
        log({"type": "episode_start", "episode_id": episode_id, "seed": seed,
             "game_id": adapter.id, "harness_id": frozen_harness.get("id"),
             "rules_version": adapter.rules_version, "game_context": frozen_context,
             "policy_hashes": policy_hashes, "candidate_hash": expected_hash,
             "initial_state": last_state})
        publish_spectator()
        while not last_state["done"]:
            if stopped():
                reason = "stopped"
                break
            if wait_if_paused is not None and wait_if_paused() is False:
                reason = "stopped"
                break
            if stopped():
                reason = "stopped"
                break
            before = _snapshot(session)
            if before != last_state:
                raise ReceiptError("adapter observation advanced without an action")
            if before["step"] >= step_budget:
                raise ReceiptError("adapter did not terminate at its fixed step budget")
            criteria = _catalog(session)
            expected_hash = _digest(criteria)
            allowed_ids = {item["id"] for item in criteria}
            role = before.get("policy_role", "candidate")
            if role not in instructions_by_role:
                raise ReceiptError("opponent turn has no frozen opponent policy")
            actor = before.get("actor", before.get("turn", role))
            pending_receipt = None
            observation_hash = _digest(before)
            request_id = f"{episode_id}:step:{before['step']}:{observation_hash[:16]}"
            state = deepcopy(before)
            state.update({"episode_id": episode_id, "observation_hash": observation_hash,
                          "candidates": deepcopy(criteria), "remaining_steps": step_budget - before["step"]})
            state_copy = deepcopy(state)
            receipt = provider.decide(state, instructions_by_role[role], deepcopy(criteria), request_id)
            pending_receipt = receipt
            if stopped():
                reason = "stopped"
                break
            if wait_if_paused is not None and wait_if_paused() is False:
                reason = "stopped"
                break
            if stopped():
                reason = "stopped"
                break
            if state != state_copy or _snapshot(session) != before or _catalog(session) != criteria:
                raise ReceiptError("observation or action catalog changed while decision was pending")
            _validate_receipt(receipt, request_id, before["step"], expected_hash, allowed_ids)
            if model is not None and receipt["model"] != model:
                raise ReceiptError("provider model changed during frozen episode")
            receipt = deepcopy(receipt)
            model = receipt["model"]
            action = receipt["choice_id"]
            returned = session.step(action)
            after = _snapshot(session)
            if returned != after or after["step"] != before["step"] + 1:
                raise ReceiptError("adapter step did not advance exactly one observation")
            last_state = after
            record = {"before": before, "after": after, "action": action,
                      "applied_action": after.get("applied_action", action),
                      "policy_role": role, "actor": actor, "policy_hash": policy_hashes[role],
                      "criteria": criteria, "candidate_hash": expected_hash, "receipt": receipt}
            trace.append(record)
            latencies.append(receipt["elapsed_ms"])
            log({"type": "step", "episode_id": episode_id, **record})
            publish_spectator()
            if on_step is not None:
                on_step(deepcopy(record))
        if reason is None:
            reason = last_state.get("reason")
    except InterruptedError:
        reason = "stopped"
    except Exception as exc:
        reason, failure = "infra_error", fail(exc)
    except BaseException:
        if log_file is not None:
            log_file.close()
        raise
    finally:
        if session is not None:
            try:
                session.close()
            except Exception as exc:
                if failure is None:
                    reason, failure = "infra_error", fail(exc, "close_session")
    result = {
        "episode_id": episode_id, "seed": seed, "game_id": adapter.id,
        "harness_id": frozen_harness.get("id"), "rules_version": adapter.rules_version,
        "game_context": frozen_context, "config": frozen_config,
        "score": last_state["score"], "steps": last_state["step"],
        "termination_reason": reason, "model": model, "latencies_ms": latencies,
        "trace": trace, "infra_error": failure, "failure": failure,
        "final_state": last_state, "elapsed_ms": (time.monotonic() - started) * 1000,
        "candidate_hash": expected_hash, "policy_hashes": policy_hashes,
        "thinking_verification": (
            "reported_zero" if trace and all(
                r["receipt"].get("reasoning_tokens") == 0 and r["receipt"].get("think_used") is False
                for r in trace
            ) else "not_reported"
        ),
    }
    if tetris_context_config is not None:
        result["tetris_context_config"] = deepcopy(tetris_context_config)
    if spectator_error is not None:
        result['spectator_error'] = spectator_error
    try:
        log({"type": "episode_end", **{k: v for k, v in result.items() if k != "trace"}})
    except Exception as exc:
        result["termination_reason"] = "infra_error"
        result["infra_error"] = result["failure"] = fail(exc, "persist_episode_end")
    finally:
        if log_file is not None:
            log_file.close()
    return result
