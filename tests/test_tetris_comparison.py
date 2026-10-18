"""Native-engine comparison integration tests, with no network or model claims."""
import json
from pathlib import Path
import threading

from fastapi.testclient import TestClient
import pytest

from game_harness import tetris_comparison as comparison
from game_harness.providers import Settings, digest
from game_harness.tetris_context import audit_context_episode
from game_harness.tetris_vision import render_tetris_png
from test_tetris_context import FixtureProvider


MODEL = "startlux-decision-27b-autothink"


class Providers:
    """Keep the real goal/action contracts, but return controlled fixture choices."""

    def __init__(self, monkeypatch, *, actions=None, fail=None, block_actions=False):
        self.actions = actions or {}
        self.fail = fail
        self.instances = {}
        self.closed = set()
        self.entered = {arm: threading.Event() for arm in comparison.LABELS}
        self.returned = {arm: threading.Event() for arm in comparison.LABELS}
        self.release = threading.Event()
        if not block_actions:
            self.release.set()
        owner = self

        class NativeFixture(FixtureProvider):
            def __init__(self, settings, *, enabled, image_store, archive_dir):
                arm = Path(archive_dir).parent.name
                if owner.fail == (arm, "construct"):
                    raise RuntimeError("fixture-private-key-do-not-expose")
                super().__init__(goals=["keep"] * 1100, action=owner.actions.get(arm, "noop:100"))
                self.settings, self.arm = settings, arm
                self.enabled = enabled
                self.image_store, self.archive_dir = Path(image_store), Path(archive_dir)
                owner.instances[arm] = self

            def health(self):
                return {"ok": True, "model": self.settings.decision_model}

            def decide(self, state, instructions, criteria, request_id):
                if not request_id.endswith(":goal"):
                    owner.entered[self.arm].set()
                    if not owner.release.wait(5):
                        raise TimeoutError("Test fixture was not released")
                    if owner.fail == (self.arm, "decide"):
                        raise RuntimeError("fixture-private-key-do-not-expose")
                receipt = super().decide(state, instructions, criteria, request_id)
                receipt["model"] = self.settings.decision_model
                receipt["raw_response"]["model"] = self.settings.decision_model
                receipt["response_hash"] = digest(receipt["raw_response"])
                if not request_id.endswith(":goal"):
                    owner.returned[self.arm].set()
                return receipt

            def close(self):
                owner.closed.add(self.arm)
                if owner.fail == (self.arm, "close"):
                    raise RuntimeError("fixture-private-key-do-not-expose")

        monkeypatch.setattr(comparison, "VisionDecisionProvider", NativeFixture)


@pytest.fixture
def settings():
    # Never load .env or credentials during protocol tests.
    return Settings("http://not-used.invalid/v1/systemone", MODEL, 1,
                    "http://not-used.invalid/v1", "fixture-coach", "", 1)


@pytest.fixture
def setup_comparison(monkeypatch, tmp_path, settings):
    running = []

    def create(**provider_options):
        providers = Providers(monkeypatch, **provider_options)
        lab = comparison.TetrisComparison(settings, tmp_path / f"run-{len(running)}")
        running.append((lab, providers))
        return lab, providers

    yield create
    for lab, providers in running:
        providers.release.set()
        lab.stop()
        if lab.worker is not None:
            lab.worker.join(5)
            assert not lab.worker.is_alive(), "Comparison worker leaked after cleanup"


def finish(lab):
    assert lab.worker is not None
    lab.worker.join(5)
    assert not lab.worker.is_alive(), "Comparison deadlocked"
    assert not lab.state()["busy"]
    return lab.state()


def read_result(lab, arm, pair_id=None):
    return json.loads((lab.root / "pairs" / (pair_id or lab.pair_id) / arm / "result.json").read_text())


def test_initial_boards_and_frozen_harness_contracts(setup_comparison):
    lab, _ = setup_comparison()
    state = lab.state()
    assert state["arms"]["baseline"]["snapshot"] == state["arms"]["planning"]["snapshot"]
    assert lab.frame("baseline") == lab.frame("planning")
    assert lab.frame("baseline").startswith(b"\x89PNG\r\n\x1a\n")
    assert state["model"] == MODEL and state["vision"] is True and state["think_mode"] == "never"
    before, after = lab.harnesses["baseline"], lab.harnesses["planning"]
    assert before == json.loads((comparison.ARTIFACT_DIR / "baseline-harness.json").read_text())
    assert after == json.loads((comparison.ARTIFACT_DIR / "research-harness.json").read_text())
    protocol = json.loads((comparison.ARTIFACT_DIR / "protocol.json").read_text())
    assert digest(before["candidate"]) == protocol["candidate_hash"]
    assert digest(before["brief"]) == protocol["brief_hash"]
    assert before["id"] == "exp-v6-repaired-da143ce409e94717"
    assert before["candidate"] == after["candidate"]
    assert before["game_context"] == after["game_context"]
    assert before["tetris_context_config"] == {
        "schema": "tetris-context-v2", "structured_facts": True, "decision_goal_memory": True,
        "replan_every_actions": 4, "control_feedback": True}
    assert after["tetris_context_config"] == {
        **before["tetris_context_config"], "schema": "tetris-context-v3",
        "goal_instructions": after["tetris_context_config"]["goal_instructions"]}
    state["arms"]["baseline"]["snapshot"]["step"] = 900
    assert lab.state()["arms"]["baseline"]["snapshot"]["step"] == 0


@pytest.mark.parametrize("change", ["candidate", "think_mode", "model", "context"])
def test_contract_changes_fail_closed(monkeypatch, tmp_path, settings, change):
    artifacts = {}
    for filename in ("baseline-harness.json", "research-harness.json"):
        artifacts[filename] = json.loads((comparison.ARTIFACT_DIR / filename).read_text())
    value = artifacts["baseline-harness.json"]
    if change == "candidate":
        value["candidate"]["strategy_notes"] += " different policy"
    elif change == "think_mode":
        value["think_mode"] = "auto"
    elif change == "model":
        value["decision_model"] = "other-model"
    else:
        value["tetris_context_config"]["control_feedback"] = False
    for filename, value in artifacts.items():
        (tmp_path / filename).write_text(json.dumps(value))
    monkeypatch.setattr(comparison, "ARTIFACT_DIR", tmp_path)
    with pytest.raises(ValueError):
        comparison.load_harnesses(settings)


def test_native_pair_preserves_metadata_catalogs_budget_and_audit(setup_comparison):
    lab, providers = setup_comparison()
    pair_id = lab.start({"seed": 1921416371, "max_steps": 8})["pair_id"]
    state = finish(lab)
    assert state["status"] == "completed"
    assert providers.closed == set(comparison.LABELS)
    assert providers.instances["baseline"].image_store != providers.instances["planning"].image_store
    assert providers.instances["baseline"].archive_dir != providers.instances["planning"].archive_dir
    first_calls, episodes = {}, {}
    for arm, provider in providers.instances.items():
        assert provider.enabled is True
        episode = read_result(lab, arm)
        episodes[arm] = episode
        assert episode["episode_id"] == pair_id
        assert episode["model"] == MODEL
        assert episode["steps"] == 8 and episode["termination_reason"] == "step_limit"
        assert episode["thinking_verification"] == "reported_zero"
        assert not episode["infra_error"]
        assert audit_context_episode(episode, lab.harnesses[arm])["actual_inputs_verified"] is True
        assert len(episode["trace"]) == 8
        first_calls[arm] = provider.calls[0]
        actions = [call for call in provider.calls if not call[3].endswith(":goal")]
        assert len(actions) == 8
        for index, (request, _, catalog, request_id) in enumerate(actions):
            assert request["episode_id"] == pair_id and request_id.startswith(pair_id + ":step:")
            assert request["remaining_steps"] == 8 - index
            assert catalog == episode["trace"][index]["criteria"]
            assert episode["trace"][index]["action"] == "noop:100"
    before, after = first_calls["baseline"], first_calls["planning"]
    assert before[0]["observation"] == after[0]["observation"]
    assert render_tetris_png(before[0]) == render_tetris_png(after[0])
    assert before[3] == after[3]
    assert [c["id"] for c in before[2]] == [c["id"] for c in after[2]]
    assert episodes["baseline"]["game_context"] == episodes["planning"]["game_context"]
    assert episodes["baseline"]["final_state"] == episodes["planning"]["final_state"]
    assert state["history"][0]["status"] == "completed"


def test_early_native_top_out_releases_peer_to_its_budget(setup_comparison):
    lab, _ = setup_comparison(actions={"baseline": "hard-drop:100"})
    lab.start({"seed": 1921416371, "max_steps": 80})
    state = finish(lab)
    before, after = read_result(lab, "baseline"), read_result(lab, "planning")
    assert before["termination_reason"] == "TOP_OUT" and before["steps"] < 80
    assert after["termination_reason"] == "step_limit" and after["steps"] == 80
    assert state["status"] == "completed"


@pytest.mark.parametrize("failure", ["construct", "decide", "close"])
def test_provider_errors_release_peer_and_invalidate_comparison(setup_comparison, failure):
    lab, providers = setup_comparison(fail=("baseline", failure))
    lab.start({"seed": 1921416371, "max_steps": 8})
    state = finish(lab)
    assert state["status"] == "error"
    assert state["arms"]["baseline"]["status"] == "error"
    assert state["history"][0]["status"] == "error"
    assert state["stop_requested"] is True
    assert "fixture-private-key" not in json.dumps(state)
    assert state["arms"]["planning"]["status"] in comparison.TERMINAL
    assert set(providers.instances) == providers.closed


def test_action_barrier_limits_lead_and_stop_releases_waiters(setup_comparison, monkeypatch):
    lab, _ = setup_comparison()
    original = lab._on_step
    at_step = {arm: threading.Event() for arm in comparison.LABELS}
    release = threading.Event()
    observed = []

    def on_step(arm, record):
        # Hold one side before publishing its first action. The other must wait
        # at its first action, rather than request or execute a second action.
        if arm == "planning" and record["after"]["step"] == 1:
            at_step[arm].set()
            assert release.wait(5)
        original(arm, record)
        with lab.condition:
            observed.append(tuple(lab.arms[x]["snapshot"]["step"] for x in comparison.LABELS))
        at_step[arm].set()

    monkeypatch.setattr(lab, "_on_step", on_step)
    try:
        lab.start({"seed": 77, "max_steps": 8})
        assert at_step["planning"].wait(5)
        with lab.condition:
            assert lab.condition.wait_for(lambda: lab.arms["baseline"]["snapshot"]["step"] == 1, 5)
            assert lab.arms["planning"]["snapshot"]["step"] == 0
        lab.stop()
        release.set()
        state = finish(lab)
        assert state["status"] == "stopped"
        assert all(abs(a - b) <= 1 for a, b in observed)
        assert all(arm["snapshot"]["step"] == 1 for arm in state["arms"].values())
    finally:
        release.set()


@pytest.mark.parametrize("finish_mode", ["resume", "stop"])
def test_pause_holds_pending_actions_and_stop_discards_them(setup_comparison, monkeypatch, finish_mode):
    lab, providers = setup_comparison(block_actions=True)
    paused_waits = []
    waiting = threading.Event()
    original = lab._wait

    def wait():
        with lab.condition:
            if lab.paused:
                paused_waits.append(threading.get_ident())
                if len(set(paused_waits)) == 2:
                    waiting.set()
        return original()

    monkeypatch.setattr(lab, "_wait", wait)
    lab.start({"seed": 77, "max_steps": 8})
    assert all(event.wait(5) for event in providers.entered.values())
    lab.pause()
    providers.release.set()
    assert waiting.wait(5)
    assert all(arm["snapshot"]["step"] == 0 for arm in lab.state()["arms"].values())
    if finish_mode == "resume":
        lab.resume()
    else:
        lab.stop()
    state = finish(lab)
    expected = 8 if finish_mode == "resume" else 0
    assert state["status"] == ("completed" if finish_mode == "resume" else "stopped")
    assert all(arm["snapshot"]["step"] == expected for arm in state["arms"].values())
    if finish_mode == "stop":
        assert all(not read_result(lab, arm)["trace"] for arm in comparison.LABELS)


def test_duplicate_start_rejected_and_restart_preserves_completed_evidence(setup_comparison):
    lab, providers = setup_comparison(block_actions=True)
    first_id = lab.start({"seed": 77, "max_steps": 8})["pair_id"]
    assert all(event.wait(5) for event in providers.entered.values())
    with pytest.raises(ValueError, match="还在运行"):
        lab.start({"seed": 99, "max_steps": 8})
    assert lab.pair_id == first_id and lab.seed == 77
    providers.release.set()
    finish(lab)
    directory = lab.root / "pairs" / first_id
    evidence = {p.relative_to(directory): p.read_bytes() for p in directory.rglob("*") if p.is_file()}
    second_id = lab.start({"seed": 77, "max_steps": 8})["pair_id"]
    finish(lab)
    assert first_id != second_id
    assert evidence == {p.relative_to(directory): p.read_bytes() for p in directory.rglob("*") if p.is_file()}
    assert len(lab.state()["history"]) == 2
    reopened = comparison.TetrisComparison(lab.settings, lab.root)
    assert len(reopened.state()["history"]) == 2
    assert reopened.state()["busy"] is False


def test_api_controls_origins_and_validation(monkeypatch, tmp_path, settings):
    providers = Providers(monkeypatch)
    app = comparison.create_app(tmp_path / "api", settings)
    lab = app.state.comparison
    try:
        with TestClient(app) as client:
            assert client.get("/api/compare/state").json()["status"] == "idle"
            first = client.get("/api/compare/frame/baseline")
            second = client.get("/api/compare/frame/planning")
            assert first.status_code == 200 and first.content == second.content
            assert first.headers["cache-control"] == "no-store"
            assert client.get("/api/compare/frame/missing").status_code == 400
            assert client.get("/api/compare/state", headers={"Host": "evil.example"}).status_code == 403
            assert client.post("/api/compare/start", json={}, headers={"Origin": "https://evil.example"}).status_code == 403
            for body in ({"seed": True}, {"seed": -1}, {"seed": 2**31}, {"seed": "77"},
                         {"max_steps": True}, {"max_steps": 7}, {"max_steps": 1001},
                         {"max_steps": 8.5}, {"harness": "other"}):
                assert client.post("/api/compare/start", json=body).status_code == 400
            assert client.post("/api/compare/pause").status_code == 400
            assert not providers.instances
            response = client.post("/api/compare/start", json={"seed": 77, "max_steps": 8},
                                   headers={"Origin": "http://testserver"})
            assert response.status_code == 200
            finish(lab)
            state = client.get("/api/compare/state").json()
            assert state["status"] == "completed" and len(state["history"]) == 1
            assert client.post("/api/compare/resume").status_code == 200
            assert client.post("/api/compare/stop").status_code == 200
    finally:
        providers.release.set()
        lab.stop()
        if lab.worker:
            lab.worker.join(5)
            assert not lab.worker.is_alive()
