"""Real rule snapshots + synthetic coach HTTP, never real model calls."""
from copy import deepcopy
import json
import shutil

import httpx
import pytest

from game_harness.games import get_game
from game_harness.initialization_contract import (
    CONTRACT_VERSION, build_initialization_contract, build_runtime_contract, initialization_context,
)
from game_harness.providers import CoachProvider, ProviderError, Settings, digest


BRIEF = {key: "fixture " + key for key in ("background", "goal", "rules", "precautions", "strategy")}
CANDIDATE = {"decision_instructions": "Choose from the current catalog.", "strategy_notes": "Fixture strategy."}
SECRET = "fixture-secret-in-header-only"


@pytest.mark.parametrize('game_id', ['gomoku', 'tetris', 'army', 'unregistered-custom-game'])
def test_runtime_contract_is_pure_shared_and_never_reads_samples(monkeypatch, game_id):
    def forbidden(*args, **kwargs):
        pytest.fail('Runtime contract must not look up adapters or create sessions')
    monkeypatch.setattr('game_harness.initialization_contract.get_game', forbidden)
    source = {'game_id': game_id, 'rules_version': 'frozen-fixture-v1',
              'seed': 'NEVER_PRIVATE_SEED', 'sample': {'piece': 'NEVER_PRIVATE_PIECE'},
              'config': {'authorization': 'NEVER_AUTHORIZATION'}}
    original = deepcopy(source)
    contract = build_runtime_contract(source, 'fixture-decision')
    assert source == original
    assert contract['game_id'] == game_id and contract['rules_version'] == 'frozen-fixture-v1'
    assert contract['player']['model'] == 'fixture-decision'
    assert 'sample' not in contract and 'observation_schema' not in contract
    assert 'NEVER_' not in json.dumps(contract)
    assert 'board_rows' not in contract['runtime_fields']
    assert '不是玩家state schema' in contract['feedback_boundary']
    if game_id == 'gomoku':
        assert 'observation.board使用X=黑、O=白、.=空' in contract['game_guidance']['observations']
    if game_id == 'tetris':
        actions = contract['game_guidance']['actions']
        assert '每个动作结束自动松键' in actions and '新的按下边沿' in actions
        assert '已有块时换入hold中的块' in actions and '只有空hold才消耗next[0]' in actions
    if game_id == 'unregistered-custom-game':
        assert '实际snapshot' in contract['game_guidance']['observations']
        assert 'snake' not in json.dumps(contract)
    contract['player']['tools'].append('MUTATED')
    assert build_runtime_contract(source, 'fixture-decision')['player']['tools'] == []


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("No real network/model calls are allowed")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)


def coach_fixture():
    captured = []
    content = json.dumps({"brief": BRIEF, "candidate": CANDIDATE})

    def transport(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"model": "coach-returned-fixture", "choices": [{
            "finish_reason": "stop", "message": {"content": content}}]})

    settings = Settings("http://private-decision.invalid/v1/systemone", "decision-model-fixture", 1,
                        "https://private-coach.invalid/v1", "coach-requested-fixture", SECRET, 1)
    coach = CoachProvider(settings, client=httpx.Client(transport=httpx.MockTransport(transport)))
    return coach, captured, content


@pytest.mark.parametrize("game_id,semantic", [
    ("snake", "反向"), ("xiangqi", "ICCS"), ("gomoku", "place:<index>"),
    ("army", "move:<from>:<to>"), ("tetris", "硬降"), ("platformer", "jump"),
])
def test_each_game_gets_real_schema_specific_semantics_and_versioned_provenance(game_id, semantic):
    if game_id != "snake" and not shutil.which("node"):
        pytest.skip("Node.js is required for the real rules adapter")
    adapter = get_game(game_id)
    config = {**adapter.defaults, "max_steps": 12}
    context = adapter.context(config)
    original = deepcopy(context)
    coach, captured, response = coach_fixture()
    try:
        assert coach.initialize(game_context=context) == (BRIEF, CANDIDATE)
        assert context == original
        contract = coach.initialization_contract
        assert contract["schema_version"] == CONTRACT_VERSION
        assert contract["game_id"] == game_id
        assert contract["rules_version"] == adapter.rules_version
        assert contract["player"]["model"] == "decision-model-fixture"
        assert contract["player"]["type"] == "choice"
        assert contract["player"]["think_mode"] == "never"
        assert contract["player"]["choices_per_call"] == 1
        assert contract["player"]["allow_none"] is False
        assert contract["player"]["tools"] == contract["player"]["search_helpers"] == []
        assert contract["player"]["in_game_coach_calls"] is False
        assert "stateless" in contract["player"]["memory"]
        assert semantic in contract["game_guidance"]["actions"]
        assert "recent_actions" not in contract["observation_schema"]["properties"]
        assert len(captured) == 1
        prompt = captured[0]["messages"][0]["content"]
        assert json.dumps(contract, ensure_ascii=False, allow_nan=False) in prompt
        assert "brief.strategy解释设计依据" in prompt
        assert "brief.precautions明确" in prompt
        assert "不能替选手选本局动作" in prompt
        assert SECRET not in prompt and "private-decision.invalid" not in prompt
        assert "private-coach.invalid" not in prompt
        provenance = coach.initialization_provenance
        assert provenance["contract_schema_version"] == CONTRACT_VERSION
        assert provenance["contract_hash"] == digest(contract)
        assert provenance["observation_schema_hash"] == digest(contract["observation_schema"])
        assert provenance["coach_requested_model"] == "coach-requested-fixture"
        assert provenance["coach_returned_model"] == "coach-returned-fixture"
        assert provenance["coach_response_hash"] == digest(response)
        assert provenance["decision_model"] == "decision-model-fixture"
        assert provenance["identity_verification"] == "service_reported_name"
        assert SECRET not in json.dumps(provenance)
        if game_id == "army":
            assert contract["sample"]["kind"] == "public_projection_only"
            assert contract["sample"]["action_catalog"] is None
            cells = contract["sample"]["state"]["cells"]
            assert all(cell is None or cell["kind"] == "unknown" for cell in cells)
        else:
            session = adapter.create(0, config)
            try:
                assert contract["sample"]["state"] == session.snapshot()
                assert contract["sample"]["action_catalog"] == session.candidates()
                assert set(contract["observation_schema"]["properties"]) == set(session.snapshot())
            finally:
                session.close()
    finally:
        coach.close()


@pytest.mark.skipif(not shutil.which("node"), reason="Node.js required")
def test_gomoku_sample_uses_configured_board_and_all_225_choices():
    adapter = get_game("gomoku")
    contract = build_initialization_contract(adapter.context({"gomoku_size": 15, "max_steps": 225}), "fixture")
    assert contract["sample"]["state"]["cols"] == 15
    assert len(contract["sample"]["state"]["cells"]) == 225
    assert [item["id"] for item in contract["sample"]["action_catalog"]] == [f"place:{i}" for i in range(225)]


class PrivateSession:
    def __init__(self, public=True, broken=False):
        self.closed = False
        self.public, self.broken = public, broken

    def snapshot(self):
        if self.broken:
            raise RuntimeError("private snapshot unavailable")
        result = {"game_id": "army", "step": 0, "actor": 0, "turn": 0,
                  "cells": [{"kind": "PRIVATE_KIND_SENTINEL", "seat": 0}],
                  "observation": {"pieces": [{"kind": "PRIVATE_OBSERVATION_SENTINEL"}]},
                  "seed": "PRIVATE_SEED_SENTINEL", "rng_state": "PRIVATE_RNG_SENTINEL"}
        if self.public:
            result["public_view"] = {"rows": 1, "cols": 1, "cells": [{"kind": "unknown", "label": "?", "seat": 0}]}
        return result

    def candidates(self):
        pytest.fail("Private initialization catalogs must not be read")

    def step(self, action):
        pytest.fail("Initialization must never play an action")

    def close(self):
        self.closed = True


class PrivateAdapter:
    id, rules_version = "army", "fixture/private-v1"
    defaults = {"max_steps": 12}
    information_policy = {"seed_is_private": True}

    def __init__(self, session):
        self.session = session

    def create(self, seed, config):
        return self.session


def test_hidden_sample_only_sends_public_values_and_always_closes(monkeypatch):
    session = PrivateSession()
    monkeypatch.setattr("game_harness.initialization_contract.get_game", lambda _: PrivateAdapter(session))
    context = {"game_id": "army", "rules": "Hidden game", "config": {"max_steps": 12}}
    contract = build_initialization_contract(context, "fixture")
    serialized = json.dumps(contract)
    assert "PRIVATE_" not in serialized
    assert session.closed
    assert contract["sample"]["action_catalog"] is None
    assert "seed" not in contract["observation_schema"]["properties"]
    assert "rng_state" not in contract["observation_schema"]["properties"]
    assert "observation" in contract["observation_schema"]["properties"]  # shape, never private values


@pytest.mark.parametrize("public,broken", [(False, False), (True, True)])
def test_missing_public_projection_or_sample_failure_stops_before_coach(monkeypatch, public, broken):
    session = PrivateSession(public=public, broken=broken)
    monkeypatch.setattr("game_harness.initialization_contract.get_game", lambda _: PrivateAdapter(session))
    coach, captured, _ = coach_fixture()
    try:
        with pytest.raises(ProviderError, match="初始化合同"):
            coach.initialize(game_context={"game_id": "army", "rules": "fixture"})
        assert captured == [] and session.closed
        assert coach.initialization_contract is coach.initialization_provenance is None
    finally:
        coach.close()


def test_context_filters_seeds_credentials_and_unrelated_application_state_without_mutation():
    context = {"game_id": "army", "rules": "fixed", "config": {"max_steps": 12, "seed": 787878,
               "api_key": SECRET}, "source": {"commit": "abc", "authorization": SECRET},
               "private_episode": {"cells": "private"}, "evaluation": {"seed_modulus": 2, "promotion_seeds": [7, 9]}}
    original = deepcopy(context)
    clean = initialization_context(context)
    assert context == original
    assert clean == {"game_id": "army", "rules": "fixed", "config": {"max_steps": 12},
                     "source": {"commit": "abc"}, "evaluation": {"seed_modulus": 2}}


def test_unknown_game_is_not_given_a_snake_contract_or_sent_to_the_llm():
    coach, captured, _ = coach_fixture()
    try:
        with pytest.raises(ProviderError, match="初始化合同"):
            coach.initialize(game_context={"game_id": "unregistered-game", "rules": "fixture"})
        assert captured == []
        assert coach.initialization_provenance is None
    finally:
        coach.close()
