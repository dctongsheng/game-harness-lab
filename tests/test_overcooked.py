"""Real native rules fixtures, not model-play or benchmark evidence.

The scripted delivery below exists only to verify engine integration and sparse
reward accounting. Production adapters never call or import these fixture moves.
"""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

from game_harness.games.overcooked import (
    ACTION_IDS, CATALOG, LAYOUTS, OvercookedAdapter, OvercookedEngineError,
    OvercookedSession, UPSTREAM_COMMIT, _python_path,
)
from game_harness.providers import digest
from game_harness.runtime import run_episode

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "src/game_harness/games/overcooked_worker.py"


@pytest.fixture(scope="module")
def native_python():
    binary = Path(_python_path())
    if not binary.is_file():
        pytest.skip("Optional pinned Overcooked Python3.10 runtime is not installed")
    probe = subprocess.run([str(binary), "-c", "import importlib.metadata; print(importlib.metadata.version('overcooked-ai'))"],
                           text=True, capture_output=True, timeout=30)
    if probe.returncode:
        pytest.skip("Optional native overcooked-ai package is not installed")
    # Present but wrong provenance is a failing test, never silently skipped.
    with OvercookedSession(max_steps=2, python_binary=str(binary)) as session:
        assert session.snapshot()["native_tick"] == 0
    return str(binary)


@pytest.mark.parametrize("value", [0, 1, 3, 1001, 1002, -2, True, 4.0, "240"])
def test_reject_incomplete_or_invalid_joint_budget(value):
    with pytest.raises(ValueError, match="偶数"):
        OvercookedAdapter().validate_config({"max_steps": value})


@pytest.mark.parametrize("layout", ["../cramped_room", "random", None, [], {}])
def test_layout_allowlist_rejects_other_inputs(layout):
    with pytest.raises(ValueError):
        OvercookedAdapter().validate_config({"overcooked_layout": layout})


def test_context_explains_no_fake_seed_randomization_and_sparse_only():
    context = OvercookedAdapter().context({})
    assert context["config"]["max_steps"] == 240
    assert context["config"]["decisions_per_tick"] == 2
    assert context["source"]["upstream_commit"] == UPSTREAM_COMMIT
    assert context["opponent_policy"] is None
    assert context["evaluation"]["seed_modulus"] == 1
    assert context["score_spec"]["score_kind"] == "native_sparse_reward"
    assert [item["id"] for item in CATALOG] == list(ACTION_IDS)
    assert all(set(item) == {"id", "label"} for item in CATALOG)


def test_missing_dependency_is_informative(tmp_path):
    with pytest.raises(OvercookedEngineError, match="独立Python环境"):
        OvercookedSession(python_binary=str(tmp_path / "missing-python"))


def joint(session, first, second="stay"):
    session.step(first)
    return session.step(second)


def test_atomic_pair_hides_pending_action_and_only_then_advances_world(native_python):
    with OvercookedSession(max_steps=4, python_binary=native_python) as session:
        before = session.snapshot()
        first = session.step("north")
        changed = {key for key in before if first[key] != before[key]}
        assert changed == {"step", "acting_player", "actor"}
        assert first["step"] == 1 and first["native_tick"] == 0
        assert first["last_joint_actions"] is None
        assert "pending" not in json.dumps(first) and "applied_action" not in first
        assert session.candidates() == CATALOG
        committed = session.step("stay")
        assert committed["native_tick"] == 1 and committed["step"] == 2
        assert committed["players"][0]["position"] == [1, 1]
        assert committed["last_joint_actions"] == ["north", "stay"]
        assert committed["last_transition"]["sparse_reward"] == 0
        assert committed["policy_role"] == first["policy_role"] == "candidate"
        assert joint(session, "stay")["done"]
        assert session.snapshot()["reason"] == "step_limit"
        assert session.candidates() == []
        with pytest.raises(RuntimeError, match="ended"):
            session.step("stay")


def test_pending_choice_is_indistinguishable_to_second_chef(native_python):
    with OvercookedSession(max_steps=4, python_binary=native_python) as left:
        with OvercookedSession(max_steps=4, python_binary=native_python) as right:
            assert left.step("north") == right.step("interact")
            assert left.candidates() == right.candidates()
            assert left.step("stay") != right.step("stay")


def test_invalid_actions_preserve_pending_and_revision(native_python):
    with OvercookedSession(max_steps=4, python_binary=native_python) as session:
        before = session.snapshot()
        for bad in ["auto_soup", "NORTH", "", None, [], 0]:
            with pytest.raises(ValueError):
                session.step(bad)
            assert session.snapshot() == before
        staged = session.step("north")
        with pytest.raises(ValueError):
            session.step("auto_soup")
        assert session.snapshot() == staged
        assert session.step("stay")["players"][0]["position"] == [1, 1]


def test_worker_stale_revision_rejects_without_native_advance(native_python):
    payloads = [
        {"id": 1, "op": "init", "seed": 1, "max_steps": 4, "layout": "cramped_room"},
        {"id": 2, "op": "step", "action": "north", "expected_revision": 0},
        {"id": 3, "op": "step", "action": "stay", "expected_revision": 0},
        {"id": 4, "op": "snapshot"},
        {"id": 5, "op": "step", "action": "stay", "expected_revision": 1},
    ]
    result = subprocess.run([native_python, str(WORKER)],
                            input="".join(json.dumps(x) + "\n" for x in payloads),
                            text=True, capture_output=True, check=True, timeout=30)
    responses = [json.loads(line) for line in result.stdout.splitlines()]
    assert responses[2] == {"id": 3, "ok": False, "error_code": "revision"}
    assert responses[3]["value"] == responses[1]["value"]
    assert responses[4]["value"]["native_tick"] == 1
    assert responses[4]["value"]["players"][0]["position"] == [1, 1]


def test_native_collision_not_avoided_or_ranked(native_python):
    with OvercookedSession(max_steps=6, python_binary=native_python) as session:
        first = joint(session, "north", "west")
        assert [p["position"] for p in first["players"]] == [[1, 1], [2, 1]]
        collision = joint(session, "east", "west")
        assert [p["position"] for p in collision["players"]] == [[1, 1], [2, 1]]
        assert [p["orientation"] for p in collision["players"]] == [[1, 0], [-1, 0]]
        assert session.candidates() == CATALOG
        assert collision["score"] == 0


def test_native_soup_delivery_trajectory_has_no_intermediate_score(native_python):
    # Explicit test-only fixture from the native standard start; chef1 stays.
    first_onion = ["north", "west", "interact", "east", "north", "interact"]
    more_onion = ["west", "west", "interact", "east", "north", "interact"]
    with OvercookedSession(max_steps=120, python_binary=native_python) as session:
        for action in first_onion + more_onion * 2:
            state = joint(session, action)
            assert state["score"] == state["deliveries"] == 0
        assert state["pots"][0]["ingredients"] == ["onion"] * 3
        assert state["pots"][0]["is_cooking"] is False  # Full does not auto-start.
        state = joint(session, "interact")
        assert state["pots"][0]["is_cooking"] is True
        for action in ["west", "south", "south", "interact", "north", "east", "north"] + ["stay"] * 12:
            state = joint(session, action)
            assert state["score"] == 0
        assert state["pots"][0]["is_ready"] is True
        assert state["pots"][0]["cook_time_remaining"] == 0
        for action in ["interact", "south", "east", "south"]:
            state = joint(session, action)
            assert state["score"] == 0
        assert state["players"][0]["held_object"]["name"] == "soup"
        state = joint(session, "interact")
        assert state["score"] == 20
        assert state["deliveries"] == 1
        assert state["delivery_counts"] == [1, 0]
        assert state["last_transition"] == {
            "native_tick": 43, "sparse_reward": 20.0,
            "sparse_reward_by_player": [20.0, 0.0], "delivery_by_player": [True, False]}
        assert state["players"][0]["held_object"] is None
        assert len(state["joint_history"]) == 4
        assert all("planner" not in key for key in state)


@pytest.mark.parametrize("layout", LAYOUTS)
def test_all_layouts_use_identical_native_start_across_seeds(native_python, layout):
    with OvercookedSession(seed=1, max_steps=2, layout=layout, python_binary=native_python) as first:
        with OvercookedSession(seed=20261017, max_steps=2, layout=layout, python_binary=native_python) as second:
            assert first.snapshot() == second.snapshot()
            assert first.snapshot()["layout"] == layout
            assert joint(first, "stay") == joint(second, "stay")


def test_returns_deep_copies_and_closed_session_cannot_step(native_python):
    session = OvercookedSession(max_steps=2, python_binary=native_python)
    before = session.snapshot()
    mutated = session.snapshot()
    mutated["players"][0]["position"][0] = 999
    catalog = session.candidates()
    catalog[0]["id"] = "autoplay"
    assert session.snapshot() == before
    assert session.candidates() == CATALOG
    process = session._process
    session.close()
    session.close()
    assert process.poll() is not None
    with pytest.raises(OvercookedEngineError, match="closed"):
        session.step("stay")


class FixtureProvider:
    def __init__(self, stale=False):
        self.calls = []
        self.stale = stale

    def decide(self, state, instructions, criteria, request_id):
        self.calls.append(deepcopy(state))
        return {"choice_id": "north" if state["acting_player"] == 0 else "stay",
                "model": "fixture-model", "request_id": request_id,
                "observation_revision": 0 if self.stale else state["step"],
                "candidate_hash": digest(criteria), "elapsed_ms": 0.1,
                "think_mode": "never", "think_used": False, "reasoning_tokens": 0,
                "raw_response": {"fixture": True}}


@pytest.mark.parametrize("stale", [False, True])
def test_generic_runtime_receipts_bind_both_chefs_before_native_tick(native_python, monkeypatch, stale):
    monkeypatch.setenv("OVERCOOKED_PYTHON", native_python)
    provider = FixtureProvider(stale)
    result = run_episode(provider, {"id": "fixture", "candidate": {
        "decision_instructions": "test only", "strategy_notes": ""}},
        adapter=OvercookedAdapter(), episode_id="overcooked-fixture", seed=0,
        config={"max_steps": 2, "overcooked_layout": "cramped_room"})
    assert len(provider.calls) == 2
    assert [s["actor"] for s in provider.calls] == ["chef_0", "chef_1"]
    assert [s["native_tick"] for s in provider.calls] == [0, 0]
    assert [s["policy_role"] for s in provider.calls] == ["candidate", "candidate"]
    assert all(s["last_joint_actions"] is None for s in provider.calls)
    if stale:
        assert result["termination_reason"] == "infra_error"
        assert result["steps"] == 1
        assert result["final_state"]["native_tick"] == 0
    else:
        assert result["infra_error"] is None
        assert result["steps"] == 2
        assert result["final_state"]["native_tick"] == 1
        assert result["trace"][0]["after"]["native_tick"] == 0
        assert result["trace"][1]["after"]["native_tick"] == 1


def test_native_worker_does_not_import_or_construct_planners(native_python):
    code = f"""
import importlib.util, json, sys
spec=importlib.util.spec_from_file_location('overcooked_worker_fixture', {str(WORKER)!r})
worker=importlib.util.module_from_spec(spec); spec.loader.exec_module(worker)
session=worker.NativeSession(0, 8, 'cramped_room')
for i, action in enumerate(['north','west','east','west','stay','interact','south','stay']):
    session.step(action, i)
assert not any(k.startswith('overcooked_ai_py.planning') for k in sys.modules)
print(json.dumps({{'native_tick':session.snapshot()['native_tick']}}))
"""
    result = subprocess.run([native_python, "-c", code], text=True, capture_output=True, timeout=30, check=True)
    assert json.loads(result.stdout) == {"native_tick": 4}


def test_core_transition_matches_upstream_env_wrapper(native_python):
    # The upstream wrapper invokes mp even for display_phi=False. Supply an inert
    # sentinel to its lazy cache; no planner is constructed or queried by rules.
    code = f"""
import importlib.util, json
spec=importlib.util.spec_from_file_location('overcooked_worker_fixture', {str(WORKER)!r})
worker=importlib.util.module_from_spec(spec); spec.loader.exec_module(worker)
from overcooked_ai_py.mdp.overcooked_mdp import OvercookedGridworld
from overcooked_ai_py.mdp.overcooked_env import OvercookedEnv
fixture=['north','west','interact','east','north','interact']+['west','west','interact','east','north','interact']*2+['interact']+['west','south','south','interact','north','east','north']+['stay']*12+['interact','south','east','south','interact']
session=worker.NativeSession(0,120,'cramped_room')
env=OvercookedEnv.from_mdp(OvercookedGridworld.from_layout_name('cramped_room'), horizon=60, info_level=0)
env._mp=object()
score=0
for a in fixture:
    session.step(a,session.revision); state=session.step('stay',session.revision)
    native,reward,done,info=env.step((worker.ACTIONS[a],worker.ACTIONS['stay']),display_phi=False)
    score+=reward
    assert native.to_dict()==session.state.to_dict()
    assert reward==state['last_transition']['sparse_reward']
    assert score==state['score']
assert score==20
print(json.dumps({{'native_tick':state['native_tick'],'score':score}}))
"""
    result = subprocess.run([native_python, "-c", code], text=True, capture_output=True, timeout=60, check=True)
    assert json.loads(result.stdout) == {"native_tick": 43, "score": 20}


@pytest.mark.parametrize("mutation", ["commit", "url", "python"])
def test_dependency_provenance_and_python_are_checked(monkeypatch, mutation):
    spec = importlib.util.spec_from_file_location("overcooked_worker_fixture", WORKER)
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    provenance = {"url": "https://github.com/HumanCompatibleAI/overcooked_ai",
                  "vcs_info": {"vcs": "git", "commit_id": UPSTREAM_COMMIT}}
    if mutation == "commit":
        provenance["vcs_info"]["commit_id"] = "bad"
    elif mutation == "url":
        provenance["url"] = "https://example.com/other/overcooked_ai"
    class Distribution:
        def read_text(self, name):
            assert name == "direct_url.json"
            return json.dumps(provenance)
    monkeypatch.setattr(worker.sys, "version_info", (3, 11) if mutation == "python" else (3, 10))
    monkeypatch.setattr(worker.importlib.metadata, "distribution", lambda _: Distribution())
    with pytest.raises(worker.WorkerError) as error:
        worker.verify_dependency()
    assert error.value.code == ("dependency" if mutation == "python" else "provenance")


def test_invalid_recipe_delivery_remains_zero_native_score(native_python):
    # Native rules permit early cooking; one onion is not a valid order here.
    fixture = ["north", "west", "interact", "east", "north", "interact", "interact"]
    fixture += ["west", "south", "south", "interact", "north", "east", "north"]
    fixture += ["stay"] * 12 + ["interact", "south", "east", "south", "interact"]
    with OvercookedSession(max_steps=80, python_binary=native_python) as session:
        for action in fixture:
            state = joint(session, action)
            assert state["score"] == 0
        assert state["deliveries"] == 1
        assert state["delivery_counts"] == [1, 0]
        assert state["last_transition"]["delivery_by_player"] == [True, False]
        assert state["last_transition"]["sparse_reward"] == 0
