"""Observer fixtures exercise native rules/transport, not model playing strength."""
from copy import deepcopy
import importlib.metadata
import json
from pathlib import Path

import pytest

from game_harness.games.pokemon import PokemonAdapter, PokemonSession
from game_harness.providers import Settings, digest
from game_harness.service import Lab
from game_harness.runtime import run_episode
from game_harness.spectator import SpectatorStore
from game_harness.store import atomic_json


ROOT = Path(__file__).resolve().parents[1]
HEADER = ["|gametype|singles", "|player|p1|Seat 1||", "|player|p2|Seat 2||", "|gen|3", "|start",
          "|switch|p1a: Alakazam|Alakazam, L50, M|100/100", "|turn|1"]


def delta(lines=None, *, start=0, step=0, terminal=False, truncated=False):
    lines = HEADER if lines is None else lines
    return {"start_cursor": start, "cursor": start + len(lines), "lines": deepcopy(lines),
            "step": step, "native_turn": 1 + step // 2, "native_terminal": terminal,
            "truncated": truncated, "result": "win" if terminal else "unresolved" if truncated else "ongoing",
            "done": terminal or truncated}


@pytest.fixture(scope="module")
def native():
    try:
        importlib.metadata.version("poke-env")
    except importlib.metadata.PackageNotFoundError:
        pytest.skip("Optional pinned poke-env is not installed")
    if not (ROOT / ".runtime/pokemon-showdown/dist/sim/index.js").is_file():
        pytest.skip("Optional pinned Showdown simulator is not installed")
    with PokemonSession(seed=42, max_steps=2) as session:
        assert session.snapshot()["native_turn"] == 1


def test_full_native_stream_survives_skipped_polls_reload_and_identical_lines(tmp_path):
    store = SpectatorStore(tmp_path)
    store.begin("ep-one", purpose="play", harness_id="h-fixture")
    assert store.feed("ep-one")["available"] is False
    store.append("ep-one", delta())
    first = store.feed("ep-one")
    # A player submits a hidden choice: cursor stays fixed while step advances.
    store.append("ep-one", delta([], start=len(HEADER), step=1))
    assert store.feed("ep-one", after=first["cursor"])["lines"] == []
    repeated = ["|move|p1a: Alakazam|Psychic|p2a: Golem", "|-damage|p2a: Golem|50/100"] * 3
    store.append("ep-one", delta(repeated, start=len(HEADER), step=2))
    restored = SpectatorStore(tmp_path)
    late_join = restored.feed("ep-one")
    assert late_join["lines"] == HEADER + repeated
    assert restored.feed("ep-one", first["cursor"])["lines"] == repeated
    assert late_join["complete_from_start"] and late_join["available"]
    assert late_join["cursor"] == len(HEADER + repeated)
    late_join["lines"].clear()
    assert restored.feed("ep-one")["lines"] == HEADER + repeated
    reset = restored.feed("ep-one", after=999)
    assert reset["reset"] and reset["start_cursor"] == 0 and reset["lines"] == HEADER + repeated


@pytest.mark.parametrize("value", [-1, True, 1.2, "0", None])
def test_invalid_public_cursors_rejected(tmp_path, value):
    with pytest.raises(ValueError, match="cursor"):
        SpectatorStore(tmp_path).feed("ep-one", value)


@pytest.mark.parametrize("value", ["../private", "", "ep/a", "ep%2fsecret", None])
def test_invalid_public_identity_rejected(tmp_path, value):
    store = SpectatorStore(tmp_path)
    with pytest.raises(ValueError, match="episode id"):
        store.feed(value)
    with pytest.raises(ValueError, match="episode id"):
        store.begin(value)


@pytest.mark.parametrize("tag", ["request", "split", "seed", "team", "showteam", "sentchoice", "choice", "debug", "sideupdate", "end"])
def test_private_protocol_never_enters_feed(tmp_path, tag):
    store = SpectatorStore(tmp_path)
    store.begin("ep-one")
    with pytest.raises(ValueError, match="private protocol"):
        store.append("ep-one", delta(HEADER + [f"|{tag}|PRIVATE_SECRET"]))
    assert "PRIVATE_SECRET" not in json.dumps(store.feed("ep-one"))
    assert store.feed("ep-one")["cursor"] == 0


def test_delta_requires_initial_headers_and_strict_contiguity(tmp_path):
    store = SpectatorStore(tmp_path)
    store.begin("ep-one")
    with pytest.raises(ValueError, match="initial protocol"):
        store.append("ep-one", delta(["|move|p1a: Alakazam|Psychic|p2a: Golem"]))
    store.append("ep-one", delta())
    with pytest.raises(ValueError, match="gap or duplicate"):
        store.append("ep-one", delta())
    malformed = delta([], start=len(HEADER))
    malformed["cursor"] += 1
    with pytest.raises(ValueError, match="contiguous"):
        store.append("ep-one", malformed)


def test_stored_unknown_keys_and_failure_details_are_not_published(tmp_path):
    store = SpectatorStore(tmp_path)
    store.begin("ep-one")
    initial = delta()
    initial.update(seed=123, request_payload="PRIVATE_SECRET", pending_choice="move:2")
    store.append("ep-one", initial)
    path = tmp_path / "spectators/ep-one.json"
    value = json.loads(path.read_text())
    value.update(seed=123, request_payload="PRIVATE_SECRET", pending_choice="move:2")
    atomic_json(path, value)
    store.finish("ep-one", termination_reason="PRIVATE_SECRET", error=True)
    output = json.dumps(store.feed("ep-one"))
    assert "PRIVATE_SECRET" not in output and "pending_choice" not in output and '"seed"' not in output
    assert store.feed("ep-one")["status"] == "error"


@pytest.mark.parametrize("key,value", [("status", {"private": "SECRET"}), ("result", ["SECRET"]),
                                       ("created_at", "SECRET"), ("finished", "SECRET"),
                                       ("purpose", {"private": "SECRET"})])
def test_malformed_allowed_sidecar_metadata_fails_closed(tmp_path, key, value):
    store = SpectatorStore(tmp_path)
    store.begin("ep-one")
    store.append("ep-one", delta())
    path = tmp_path / "spectators/ep-one.json"
    item = json.loads(path.read_text())
    item[key] = value
    atomic_json(path, item)
    with pytest.raises(ValueError):
        store.feed("ep-one")
    assert store.episodes() == []


def test_cutoff_finish_and_restart_do_not_invent_native_result(tmp_path):
    store = SpectatorStore(tmp_path)
    store.begin("ep-cutoff")
    store.append("ep-cutoff", delta())
    store.append("ep-cutoff", delta([], start=len(HEADER), step=1, truncated=True))
    store.finish("ep-cutoff", termination_reason="step_limit")
    feed = store.feed("ep-cutoff")
    assert feed["finished"] and feed["status"] == "truncated"
    assert feed["result"] == "unresolved" and not feed["native_terminal"]
    assert not any(line.startswith(("|win|", "|tie")) for line in feed["lines"])
    with pytest.raises(ValueError, match="finished"):
        store.append("ep-cutoff", delta([], start=len(HEADER), step=1))
    store.begin("ep-live")
    store.append("ep-live", delta())
    SpectatorStore(tmp_path).recover()
    assert store.feed("ep-live")["status"] == "interrupted"
    assert store.feed("ep-live")["lines"] == HEADER


def test_episode_list_order_and_legacy_unavailability_are_explicit(tmp_path):
    store = SpectatorStore(tmp_path)
    for identity in ("ep-a", "ep-b"):
        store.begin(identity, purpose="play", harness_id="h-fixture")
        path = tmp_path / f"spectators/{identity}.json"
        item = json.loads(path.read_text())
        item.update(created_at="2026-01-01T00:00:00Z", secret="PRIVATE")
        atomic_json(path, item)
    assert [item["episode_id"] for item in store.episodes()] == ["ep-b", "ep-a"]
    assert "PRIVATE" not in json.dumps(store.episodes())
    missing = store.feed("ep-old")
    assert missing["available"] is False and not missing["complete_from_start"]
    assert missing["reason"] == "missing_native_public_sidecar" and missing["lines"] == []


def test_episode_list_marks_empty_failed_stream_unavailable(tmp_path):
    store = SpectatorStore(tmp_path)
    store.begin("ep-empty")
    store.finish("ep-empty", error=True)
    store.begin("ep-ready")
    store.append("ep-ready", delta())
    entries = {entry["episode_id"]: entry for entry in store.episodes()}
    assert entries["ep-empty"]["status"] == "error"
    assert entries["ep-empty"]["available"] is False
    assert entries["ep-empty"]["complete_from_start"] is False
    assert entries["ep-ready"]["available"] is True
    assert entries["ep-ready"]["complete_from_start"] is True


def test_session_full_channel_zero_stream_is_outside_model_observation(native):
    with PokemonSession(seed=42, max_steps=160) as first, PokemonSession(seed=42, max_steps=160) as other:
        initial = first.snapshot()
        public = first.spectator_events()
        assert "|start" in public["lines"] and "|gen|3" in public["lines"]
        assert any(line.startswith("|player|p1|") for line in public["lines"])
        assert "130/130" not in "\n".join(public["lines"]) and "155/155" not in "\n".join(public["lines"])
        assert all("spectator" not in key for key in initial)
        assert first.snapshot() == other.snapshot()
        first.step("move:1")
        other.step("switch:2")
        # Different secret pending choices do not alter spectator protocol.
        assert first.spectator_events() == other.spectator_events()
        assert first.spectator_events(public["cursor"])["lines"] == []
        first.step("move:1")
        resolved = first.spectator_events(public["cursor"])
        assert any(line.startswith("|move|") for line in resolved["lines"])
        assert any(line.startswith("|-damage|") for line in resolved["lines"])
        while not first.snapshot()["done"]:
            first.step(first.candidates()[0]["id"])
        final = first.spectator_events()
        assert len(final["lines"]) > 12
        assert final["lines"][:len(public["lines"])] == public["lines"]
        assert any(line.startswith("|win|") for line in final["lines"])
        assert final["native_terminal"]
        assert set(PokemonAdapter().context({})).isdisjoint({"spectator", "spectator_events", "spectator_log"})


class FixtureDecision:
    """Receipt-bound test fixture only; production never invokes this helper."""
    def __init__(self):
        self.states = []
        self.calls = []

    def decide(self, state, instructions, criteria, request_id):
        self.states.append(deepcopy(state))
        self.calls.append(deepcopy({"state": state, "instructions": instructions,
                                    "criteria": criteria, "request_id": request_id}))
        return {"choice_id": criteria[0]["id"], "request_id": request_id,
                "observation_revision": state["step"], "candidate_hash": digest(criteria),
                "model": "fixture", "think_mode": "never", "think_used": False,
                "reasoning_tokens": 0, "elapsed_ms": 1, "raw_response": {"fixture": True}}


@pytest.mark.parametrize("observer_kind", ["collect", "fail"])
def test_observer_cannot_change_model_inputs_choices_or_results(native, observer_kind):
    baseline, observed = FixtureDecision(), FixtureDecision()
    options = {"episode_id": "ep-fixture", "seed": 42, "max_steps": 4,
               "adapter": PokemonAdapter(), "config": {"max_steps": 4}}
    harness = {"id": "h-fixture", "candidate": {"decision_instructions": "fixture", "strategy_notes": "fixture"}}
    before = run_episode(baseline, harness, **options)
    events = []

    def observer(event):
        events.append(deepcopy(event))
        event["lines"].clear()  # Mutating observer input cannot alter native session.
        if observer_kind == "fail":
            raise RuntimeError("PRIVATE_PROVIDER_SECRET")

    after = run_episode(observed, harness, **options, on_spectator=observer)
    assert baseline.calls == observed.calls
    assert before["trace"] == after["trace"]
    assert before["final_state"] == after["final_state"]
    assert after["infra_error"] is None and after["steps"] == 4
    assert events[0]["start_cursor"] == 0 and "|start" in events[0]["lines"]
    if observer_kind == "fail":
        assert len(events) == 1
        assert after["spectator_error"]["type"] == "RuntimeError"
        assert "PRIVATE_PROVIDER_SECRET" not in json.dumps(after)
    else:
        assert len(events) == 5
        assert "spectator_error" not in after


def test_lab_records_public_sidecar_with_unchanged_receipt_bound_decisions(native, tmp_path):
    settings = Settings("http://invalid", "fixture", 1, "http://invalid", "fixture", "", 1)
    lab = Lab(settings, tmp_path)
    provider = FixtureDecision()
    result = lab.play(provider, {"id": "h-fixture", "scope": "fixture", "candidate": {
        "decision_instructions": "fixture", "strategy_notes": "fixture"}},
        {"game_id": "pokemon", "board_size": 8, "max_steps": 4}, 42, "fixture")
    assert result["infra_error"] is None and result["steps"] == 4
    assert len(provider.states) == 4
    assert all(not any("spectator" in key for key in state) for state in provider.states)
    feed = lab.spectator_feed(result["episode_id"])
    assert feed["finished"] and feed["complete_from_start"] and feed["truncated"]
    assert "|start" in feed["lines"]
    assert lab.spectator_episodes()[0]["episode_id"] == result["episode_id"]
    lab.store.put("episode", {"episode_id": "ep-old", "game_id": "pokemon"})
    assert not lab.spectator_feed("ep-old")["available"]
    with pytest.raises(FileNotFoundError):
        lab.spectator_feed("ep-unknown")
    lab.store.db.close()
