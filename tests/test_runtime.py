"""Synthetic receipt fixtures test protocol integrity, not model capability."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from game_harness.runtime import run_episode
from game_harness.snake import ACTION_CANDIDATES, candidate_hash


HARNESS = {"id": "fixture-harness-1", "candidate": {
    "decision_instructions": "Fixture instructions", "strategy_notes": "Fixture notes"}}


class ReceiptFixtureProvider:
    def __init__(self, choice="right", transform=None):
        self.choice = choice
        self.transform = transform
        self.calls = []

    def decide(self, state, instructions, criteria, request_id):
        self.calls.append(deepcopy((state, instructions, criteria, request_id)))
        receipt = {"choice_id": self.choice, "model": "unit-test-fixture",
                   "elapsed_ms": 2.5, "raw_response": {"fixture": True},
                   "request_id": request_id, "observation_revision": state["step"],
                   "candidate_hash": candidate_hash(criteria), "think_mode": "never",
                   "reasoning_tokens": 0, "think_used": False}
        if self.transform:
            self.transform(receipt, state)
        return receipt


def run(provider, **kwargs):
    return run_episode(provider, HARNESS, episode_id="fixture-episode", seed=2,
                       size=4, max_steps=10, **kwargs)


class RuntimeTests(unittest.TestCase):
    def test_every_executed_move_has_bound_receipt_and_complete_catalog(self):
        provider = ReceiptFixtureProvider()
        records = []
        result = run(provider, on_step=records.append)
        self.assertEqual(result["termination_reason"], "wall_collision")
        self.assertIsNone(result["infra_error"])
        self.assertEqual(result["steps"], 2)
        self.assertEqual(len(result["trace"]), result["steps"])
        self.assertEqual(len(records), 2)
        self.assertEqual(result["thinking_verification"], "reported_zero")
        for index, record in enumerate(records):
            self.assertEqual(record["receipt"]["observation_revision"], index)
            self.assertEqual(record["before"]["step"], index)
            self.assertEqual(record["after"]["step"], index + 1)
            self.assertEqual(provider.calls[index][2], ACTION_CANDIDATES)
        self.assertIn("Fixture instructions", provider.calls[0][1])
        self.assertIn("Fixture notes", provider.calls[0][1])

    def test_tampered_receipts_never_advance_game(self):
        wrong_values = {"choice_id": "safest", "request_id": "stale-request",
                        "observation_revision": 99, "candidate_hash": "pruned-candidates",
                        "think_mode": "auto", "reasoning_tokens": 12, "think_used": True,
                        "elapsed_ms": float("nan"), "model": ""}
        for key, value in wrong_values.items():
            with self.subTest(field=key):
                provider = ReceiptFixtureProvider(transform=lambda r, s: r.update({key: value}))
                result = run(provider)
                self.assertEqual(result["termination_reason"], "infra_error")
                self.assertEqual(result["steps"], 0)
                self.assertEqual(result["trace"], [])
                self.assertEqual(result["infra_error"]["type"], "ReceiptError")

    def test_rejected_receipt_is_preserved_for_audit(self):
        provider = ReceiptFixtureProvider(transform=lambda r, s: r.update({"request_id": "wrong"}))
        result = run(provider)
        self.assertEqual(result["infra_error"]["rejected_or_pending_receipt"]["request_id"], "wrong")

    def test_model_cannot_change_mid_episode(self):
        provider = ReceiptFixtureProvider(transform=lambda r, s: r.update({"model": f"model-{s['step']}"}))
        result = run(provider)
        self.assertEqual(result["steps"], 1)
        self.assertEqual(result["termination_reason"], "infra_error")
        self.assertIn("model changed", result["infra_error"]["message"])

    def test_observation_mutation_is_rejected(self):
        provider = ReceiptFixtureProvider(transform=lambda r, s: s.update({"step": 22}))
        result = run(provider)
        self.assertEqual(result["steps"], 0)
        self.assertEqual(result["termination_reason"], "infra_error")

    def test_provider_failure_keeps_prior_trace_and_does_not_fallback(self):
        provider = ReceiptFixtureProvider()
        original = provider.decide

        def fail_second(*args):
            if provider.calls:
                raise ConnectionError("fixture endpoint unavailable")
            return original(*args)

        provider.decide = fail_second
        result = run(provider)
        self.assertEqual(result["steps"], 1)
        self.assertEqual(len(result["trace"]), 1)
        self.assertEqual(result["infra_error"]["type"], "ConnectionError")
        self.assertIn(":step:1:", result["infra_error"]["request_id"])

    def test_pause_has_no_game_steps_and_stop_prevents_request(self):
        provider = ReceiptFixtureProvider()
        paused = []
        result = run(provider, wait_if_paused=lambda: paused.append(True) or False)
        self.assertEqual(paused, [True])
        self.assertEqual(result["termination_reason"], "stopped")
        self.assertEqual(provider.calls, [])
        self.assertEqual(result["steps"], 0)

    def test_stop_during_request_discards_pending_action(self):
        provider = ReceiptFixtureProvider()
        result = run(provider, should_stop=lambda: bool(provider.calls))
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(result["steps"], 0)
        self.assertEqual(result["termination_reason"], "stopped")

    def test_pause_after_response_precedes_action_execution(self):
        provider = ReceiptFixtureProvider()
        events = []

        def pause_gate():
            events.append(("pause_gate", len(provider.calls)))
            return True

        result = run(provider, wait_if_paused=pause_gate,
                     on_step=lambda record: events.append(("step", record["after"]["step"])))
        self.assertIsNone(result["infra_error"])
        self.assertEqual(events[:3], [("pause_gate", 0), ("pause_gate", 1), ("step", 1)])

    def test_stop_after_completed_step_preserves_that_step(self):
        provider = ReceiptFixtureProvider()
        stop = []
        result = run(provider, on_step=lambda record: stop.append(True), should_stop=lambda: bool(stop))
        self.assertEqual(result["steps"], 1)
        self.assertEqual(result["termination_reason"], "stopped")
        self.assertEqual(len(result["trace"]), 1)

    def test_interrupted_pause_is_stop_not_infrastructure_failure(self):
        def interrupted():
            raise InterruptedError("cancelled")

        result = run(ReceiptFixtureProvider(), wait_if_paused=interrupted)
        self.assertEqual(result["termination_reason"], "stopped")
        self.assertIsNone(result["infra_error"])

    def test_callback_cannot_mutate_audit_trace(self):
        def mutate(record):
            record["receipt"]["choice_id"] = "forged"
            record["after"]["score"] = 999

        result = run(ReceiptFixtureProvider(), on_step=mutate)
        self.assertEqual(result["trace"][0]["receipt"]["choice_id"], "right")
        self.assertNotEqual(result["trace"][0]["after"]["score"], 999)

    def test_jsonl_has_start_steps_and_end_with_raw_receipts(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "episode.jsonl"
            result = run(ReceiptFixtureProvider(), log_path=path)
            events = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual([e["type"] for e in events], ["episode_start", "step", "step", "episode_end"])
            self.assertEqual(events[-1]["score"], result["score"])
            self.assertEqual(events[1]["receipt"]["raw_response"], {"fixture": True})
            second = run(ReceiptFixtureProvider(), log_path=path)
            self.assertEqual(second["infra_error"]["type"], "FileExistsError")
            self.assertEqual(second["steps"], 0)
            self.assertEqual(len(path.read_text().splitlines()), 4)

    def test_missing_thinking_telemetry_is_not_claimed_as_verified(self):
        def omit(receipt, state):
            receipt.pop("reasoning_tokens")
            receipt.pop("think_used")

        result = run(ReceiptFixtureProvider(transform=omit))
        self.assertIsNone(result["infra_error"])
        self.assertEqual(result["thinking_verification"], "not_reported")


if __name__ == "__main__":
    unittest.main()
