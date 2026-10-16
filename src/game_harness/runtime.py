"""Receipt-checked model-only episode execution; no heuristic fallback."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import time

from .snake import ACTION_CANDIDATES, ACTION_IDS, GAME_RULES, RULES_VERSION, SnakeGame, candidate_hash


class ReceiptError(ValueError):
    """A decision cannot be bound to this observation and action catalog."""


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _validate_receipt(receipt, request_id, revision, expected_hash):
    if not isinstance(receipt, dict):
        raise ReceiptError("provider receipt must be an object")
    if receipt.get("request_id") != request_id:
        raise ReceiptError("receipt request_id mismatch")
    if type(receipt.get("observation_revision")) is not int or receipt["observation_revision"] != revision:
        raise ReceiptError("receipt observation_revision mismatch")
    if receipt.get("candidate_hash") != expected_hash:
        raise ReceiptError("receipt candidate_hash mismatch")
    if receipt.get("choice_id") not in ACTION_IDS:
        raise ReceiptError("receipt choice_id outside fixed action catalog")
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
    _canonical(receipt)  # Receipts must be persistable JSON, including raw_response.


def run_episode(provider, harness, *, episode_id, seed, size, max_steps,
                on_step=None, should_stop=None, wait_if_paused=None, log_path=None) -> dict:
    """Run one frozen harness version with a real receipt required for each move.

    `criteria` passed to the provider is the complete fixed candidate list.
    Pause callbacks block without stepping; returning False or raising
    InterruptedError stops the episode. KeyboardInterrupt/SystemExit propagate.
    """
    game = SnakeGame(size=size, seed=seed, max_steps=max_steps)
    frozen_harness = deepcopy(harness)
    candidate = frozen_harness.get("candidate", {})
    decision_instructions = candidate.get("decision_instructions", "")
    notes = candidate.get("strategy_notes", "")
    if not isinstance(decision_instructions, str) or not isinstance(notes, str):
        raise ValueError("harness candidate instructions and strategy_notes must be strings")
    instructions = GAME_RULES + "\n本局冻结的决策指导：\n" + decision_instructions + "\n策略参考：\n" + notes
    criteria = deepcopy(ACTION_CANDIDATES)
    expected_hash = candidate_hash(criteria)
    trace, latencies = [], []
    model = None
    failure = None
    reason = None
    request_id = None
    pending_receipt = None
    started = time.monotonic()
    log_file = None

    def log(event):
        if log_file is not None:
            log_file.write(_canonical(event) + "\n")
            log_file.flush()

    def stopped():
        return bool(should_stop is not None and should_stop())

    try:
        if log_path is not None:
            path = Path(log_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            log_file = path.open("x", encoding="utf-8")
        log({"type": "episode_start", "episode_id": episode_id, "seed": seed,
             "harness_id": frozen_harness.get("id"), "rules_version": RULES_VERSION,
             "candidate_hash": expected_hash, "initial_state": game.snapshot()})
        while not game.done:
            if stopped():
                reason = "stopped"
                break
            if wait_if_paused is not None and wait_if_paused() is False:
                reason = "stopped"
                break
            if stopped():
                reason = "stopped"
                break
            before = game.snapshot()
            pending_receipt = None
            observation_hash = hashlib.sha256(_canonical(before).encode("utf-8")).hexdigest()
            request_id = f"{episode_id}:step:{before['step']}:{observation_hash[:16]}"
            state = deepcopy(before)
            state.update({"episode_id": episode_id, "observation_hash": observation_hash,
                          "candidates": deepcopy(criteria), "remaining_steps": max_steps - before["step"]})
            state_copy = deepcopy(state)
            receipt = provider.decide(state, instructions, deepcopy(criteria), request_id)
            pending_receipt = receipt
            # A stop requested during an in-flight model call must not execute its action.
            if stopped():
                reason = "stopped"
                break
            if wait_if_paused is not None and wait_if_paused() is False:
                reason = "stopped"
                break
            if stopped():
                reason = "stopped"
                break
            if state != state_copy or game.snapshot() != before:
                raise ReceiptError("observation changed while decision was pending")
            _validate_receipt(receipt, request_id, before["step"], expected_hash)
            if model is not None and receipt["model"] != model:
                raise ReceiptError("provider model changed during frozen episode")
            receipt = deepcopy(receipt)
            model = receipt["model"]
            action = receipt["choice_id"]
            after = game.step(action)
            record = {"before": before, "after": after, "action": action,
                      "applied_action": after["direction"], "receipt": receipt}
            trace.append(record)
            latencies.append(receipt["elapsed_ms"])
            log({"type": "step", "episode_id": episode_id, **record})
            if on_step is not None:
                on_step(deepcopy(record))
        if reason is None:
            reason = game.reason
    except InterruptedError:
        reason = "stopped"
    except Exception as exc:
        reason = "infra_error"
        failure = {"type": type(exc).__name__, "message": str(exc),
                   "request_id": request_id, "observation_revision": game.step_count}
        if pending_receipt is not None:
            try:
                _canonical(pending_receipt)
                failure["rejected_or_pending_receipt"] = deepcopy(pending_receipt)
            except (TypeError, ValueError, RecursionError):
                failure["receipt_unserializable"] = True
    result = {
        "episode_id": episode_id,
        "seed": seed,
        "harness_id": frozen_harness.get("id"),
        "rules_version": RULES_VERSION,
        "score": game.score,
        "steps": game.step_count,
        "termination_reason": reason,
        "model": model,
        "latencies_ms": latencies,
        "trace": trace,
        "infra_error": failure,
        "failure": failure,
        "final_state": game.snapshot(),
        "elapsed_ms": (time.monotonic() - started) * 1000,
        "candidate_hash": expected_hash,
        "thinking_verification": (
            "reported_zero" if trace and all(
                r["receipt"].get("reasoning_tokens") == 0 and r["receipt"].get("think_used") is False
                for r in trace
            ) else "not_reported"
        ),
    }
    try:
        log({"type": "episode_end", **{k: v for k, v in result.items() if k != "trace"}})
    except Exception as exc:
        result["termination_reason"] = "infra_error"
        result["infra_error"] = result["failure"] = {
            "type": type(exc).__name__, "message": str(exc), "stage": "persist_episode_end"}
    finally:
        if log_file is not None:
            log_file.close()
    return result
