"""Native fixtures verify rules/visibility only; scripted play is not model evidence."""
from copy import deepcopy
import importlib.metadata
import json
from pathlib import Path
import random
import subprocess

import pytest

from game_harness.games.pokemon import (
    PokemonAdapter, PokemonSession, POKE_ENV_COMMIT, SHOWDOWN_COMMIT, RULES_VERSION,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def native():
    try:
        importlib.metadata.version("poke-env")
    except importlib.metadata.PackageNotFoundError:
        pytest.skip("Optional pinned poke-env is not installed")
    if not (ROOT / ".runtime/pokemon-showdown/dist/sim/index.js").is_file():
        pytest.skip("Optional pinned Showdown simulator is not installed")
    # Installed but mismatched provenance is a failure, not a skipped test.
    with PokemonSession(seed=42, max_steps=2) as session:
        assert session.snapshot()["native_turn"] == 1


@pytest.mark.parametrize("value", [0, -1, 2001, True, "160", 160.0, None])
def test_invalid_budgets_rejected(value):
    with pytest.raises(ValueError):
        PokemonAdapter().validate_config({"max_steps": value})


@pytest.mark.parametrize("value", [-1, 2**53, True, 1.0, None])
def test_invalid_private_seed_rejected(value):
    with pytest.raises(ValueError):
        PokemonSession(seed=value)


def test_frozen_context_native_only_unscored_cutoff_and_privacy():
    context = PokemonAdapter().context({})
    assert context["config"]["max_steps"] == 160
    assert context["config"]["generation"] == 3
    assert context["config"]["team_size"] == 3
    assert context["config"]["debug"] is False
    assert context["config"]["team_preview"] is False
    assert context["evaluation"]["require_native_terminal"] is True
    assert context["evaluation"]["seed_modulus"] == 2
    assert context["score_spec"]["cutoff_is_scored"] is False
    assert context["source"]["poke_env_commit"] == POKE_ENV_COMMIT
    assert context["source"]["showdown_commit"] == SHOWDOWN_COMMIT
    assert context["information_policy"]["feedback_visibility"] == "candidate_before_only"


def test_no_implicit_player_or_native_default_choice_path():
    adapter = (ROOT / "src/game_harness/games/pokemon.py").read_text()
    bridge = (ROOT / "src/game_harness/games/pokemon_bridge.cjs").read_text()
    for forbidden in (".makeChoices(", ".autoChoose(", "chooseRandomMove", "RandomPlayer(", "DefaultBattleOrder("):
        assert forbidden not in adapter + bridge
    assert "strictChoices:true" in bridge
    assert "extractChannelMessages" in bridge


def test_initial_complete_legal_catalog_and_private_views(native):
    with PokemonSession(seed=42) as session:
        state = session.snapshot()
        assert state["rules_version"] == RULES_VERSION
        assert state["candidate_seat"] == state["turn"] == state["viewer_seat"] == 0
        assert state["actor"] == "p1" and state["policy_role"] == "candidate"
        assert len(state["team"]) == 3
        assert len(state["opponent_team"]) == 1
        assert state["opponent_unrevealed_count"] == 2
        assert state["opponent_team"][0]["moves"] == []
        assert "stats" not in state["opponent_team"][0]
        assert state["opponent_team"][0]["max_hp"] == 100
        assert state["team"][0]["max_hp"] == 130
        assert [c["id"] for c in session.candidates()] == ["move:1", "move:2", "move:3", "move:4", "switch:2", "switch:3"]
        assert all(set(c) == {"id", "label"} for c in session.candidates())
        assert "\"category\":\"physical\"" in session.candidates()[1]["label"]  # Gen3 Shadow Ball
        public = state["public_view"]
        assert [p["unrevealed_count"] for p in public["players"]] == [2, 2]
        for side in public["players"]:
            assert len(side["team"]) == 1
            assert side["team"][0]["max_hp"] == 100
            assert side["team"][0]["moves"] == []
            assert "stats" not in side["team"][0]
        text = json.dumps(state)
        for secret in ('"seed"', '"requests"', '"pending"', '"prng"', '"applied_action"'):
            assert secret not in text


def test_first_choice_is_secret_and_world_waits_for_both(native):
    with PokemonSession(seed=42) as a, PokemonSession(seed=42) as b:
        initial = a.snapshot()
        after_a = a.step("move:1")
        after_b = b.step("switch:2")
        assert after_a == after_b
        assert a.candidates() == b.candidates()
        assert after_a["step"] == 1 and after_a["native_turn"] == 1
        assert after_a["actor"] == "p2" and after_a["policy_role"] == "opponent"
        assert after_a["public_view"] == initial["public_view"]
        assert after_a["recent_events"] == initial["recent_events"]
        assert a.step("move:1") != b.step("move:1")


def test_native_forced_switch_requires_decision_and_wait_side_has_no_action(native):
    with PokemonSession(seed=42) as session:
        session.step("move:1")
        state = session.step("move:1")
        assert state["step"] == 2 and state["native_turn"] == 1
        assert state["actor"] == "p2" and state["force_switch"] is True
        assert [c["id"] for c in session.candidates()] == ["switch:2", "switch:3"]
        assert state["team"][0]["fainted"] is True
        state = session.step("switch:2")
        assert state["step"] == 3 and state["native_turn"] == 2
        assert state["force_switch"] is False
        assert state["actor"] == "p1"
        assert len(state["opponent_team"]) == 2


def test_invalid_action_and_catalog_mutation_do_not_advance(native):
    with PokemonSession(seed=42) as session:
        state = session.snapshot()
        session.candidates()[0]["id"] = "auto"
        session.snapshot()["team"][0]["moves"].clear()
        for invalid in ("auto", "move:99", "switch:1", "default", None, 1, []):
            with pytest.raises(ValueError):
                session.step(invalid)
            assert session.snapshot() == state


def test_native_replay_is_deterministic_and_seed_changes_fixture(native):
    with PokemonSession(seed=42) as a, PokemonSession(seed=42) as b, PokemonSession(seed=43) as c:
        assert a.snapshot() == b.snapshot()
        assert a.snapshot()["team"] != c.snapshot()["team"]
        assert c.snapshot()["candidate_seat"] == 1
        actions = []
        while not a.snapshot()["done"]:
            assert a.candidates() == b.candidates()
            action = a.candidates()[0]["id"]
            actions.append(action)
            assert a.step(action) == b.step(action)
        result = a.snapshot()
        assert result["native_terminal"] is True
        assert result["truncated"] is False
        assert result["result"] in ("win", "loss")
        assert result["reason"] == "native_win"
        assert result["score"] == (100 if result["result"] == "win" else 0)
        assert len(actions) == result["step"] < 160
        assert a.candidates() == []


@pytest.mark.parametrize("seed", range(8))
def test_varied_native_full_matches_complete_and_never_reveal_unseen_party(native, seed):
    with PokemonSession(seed=seed) as session:
        while not session.snapshot()["done"]:
            session.step(session.candidates()[0]["id"])
        final = session.snapshot()
        assert final["native_terminal"]
        assert final["winner"] in (0, 1)
        assert final["score"] == (100 if final["winner"] == seed % 2 else 0)
        for player in final["public_view"]["players"]:
            assert len(player["team"]) + player["unrevealed_count"] == 3
            assert all("stats" not in p and p["hp_information"] == "public_percentage" for p in player["team"])
        with pytest.raises(RuntimeError, match="ended"):
            session.step("move:1")


def test_pending_cutoff_is_unresolved_not_a_draw_or_committed_turn(native):
    with PokemonSession(seed=42, max_steps=1) as session:
        before = session.snapshot()
        after = session.step("move:1")
        assert after["public_view"] == before["public_view"]
        assert after["step"] == 1 and after["native_turn"] == 1
        assert after["done"] and after["truncated"] and not after["native_terminal"]
        assert after["result"] == "unresolved" and after["reason"] == "step_limit"
        assert after["winner"] is None and after["score"] == 50
        assert session.candidates() == []


def test_raw_bridge_stale_revision_rejects_without_advancing(native):
    payloads = [
        {"id": 1, "op": "init", "seed": 42, "max_steps": 160},
        {"id": 2, "op": "step", "actor": "p1", "command": "move 1", "expected_revision": 0},
        {"id": 3, "op": "step", "actor": "p2", "command": "move 1", "expected_revision": 0},
        {"id": 4, "op": "step", "actor": "p2", "command": "move 1", "expected_revision": 1},
    ]
    result = subprocess.run(["node", str(ROOT / "src/game_harness/games/pokemon_bridge.cjs")],
                            input="".join(json.dumps(p) + "\n" for p in payloads), text=True,
                            capture_output=True, check=True, timeout=30)
    outputs = [json.loads(line) for line in result.stdout.splitlines()]
    assert outputs[2] == {"id": 3, "ok": False, "error_code": "revision"}
    assert outputs[3]["value"]["revision"] == 2


def test_close_releases_native_process(native):
    session = PokemonSession(seed=42)
    process = session._bridge._process
    session.close()
    session.close()
    assert process.poll() is not None


def test_random_legal_fixture_sequences_do_not_need_automatic_recovery(native):
    chooser = random.Random(84)
    for seed in (41, 42, 43):
        with PokemonSession(seed=seed, max_steps=80) as session:
            while not session.snapshot()["done"]:
                catalog = session.candidates()
                action = chooser.choice(catalog)["id"]
                state = session.step(action)
                assert state["step"] <= 80
                assert "pending" not in state and "applied_action" not in state
