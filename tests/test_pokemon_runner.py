"""Frozen Pokemon experiment protocol and fail-closed score consumption.

These tests use no model calls. Native replay is separately exercised by the
adapter/audit suites; here the audit boundary and experiment ordering are tested.
"""
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from game_harness.providers import ProviderError, Settings
from game_harness.scoring import IncompleteGameResult

_spec = importlib.util.spec_from_file_location("run_pokemon", Path(__file__).parents[1] / "scripts/run_pokemon.py")
runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runner)

MODEL = "fixture-decision-model"
CONTEXT = {"game_id": "pokemon", "evaluation": {"require_native_terminal": True},
           "score_spec": {"win": 100, "loss": 0, "draw": 50}}
FINGERPRINT = {"showdown_dist": {"files": 2, "sha256": "fixture-showdown"},
               "poke_env_package": {"files": 3, "sha256": "fixture-poke-env"},
               "node": "v22.18.0", "python": "3.13.0"}
original_runtime_fingerprint = runner.runtime_fingerprint


@pytest.fixture(autouse=True)
def no_live_model_calls(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Pokemon runner tests must not make live model requests")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(runner, "runtime_fingerprint", lambda: deepcopy(FINGERPRINT))


def episode(outcome="win"):
    score = {"win": 100, "loss": 0, "draw": 50, "unresolved": 50}[outcome]
    native = outcome != "unresolved"
    return {"episode_id": "fixture-episode", "score": score, "steps": 4,
            "infra_error": None, "game_context": deepcopy(CONTEXT),
            "termination_reason": "native_tie" if outcome == "draw" else "native_win" if native else "step_limit",
            "final_state": {"done": True, "native_terminal": native, "truncated": not native,
                            "result": outcome, "score": score}}


def lab_at(tmp_path):
    lab = object.__new__(runner.EvidenceLab)
    lab.store = SimpleNamespace(root=tmp_path)
    lab.frozen_protocol = {"source_hashes": {}, "runtime_fingerprint": deepcopy(FINGERPRINT)}
    return lab


@pytest.mark.parametrize("failure", ["infra_error", "stopped"])
def test_infrastructure_or_stopped_episode_rejected_before_audit(tmp_path, monkeypatch, failure):
    import audit_pokemon_run
    result = episode()
    if failure == "infra_error":
        result["infra_error"] = {"type": "FixtureProviderFailure"}
    result["termination_reason"] = failure
    monkeypatch.setattr(runner.Lab, "play", lambda *args, **kwargs: deepcopy(result))
    monkeypatch.setattr(audit_pokemon_run, "audit_episode",
                        lambda *args, **kwargs: pytest.fail("Incomplete game must not reach score audit"))
    with pytest.raises(ProviderError, match="Incomplete episode"):
        lab_at(tmp_path).play(None, {}, {}, 42, "search_train")
    assert not (tmp_path / "audits").exists()


@pytest.mark.parametrize("outcome", ["win", "loss", "draw"])
def test_native_result_reaches_consumer_only_after_successful_audit_and_gate(tmp_path, monkeypatch, outcome):
    import audit_pokemon_run
    events = []
    result = episode(outcome)
    harness = {"id": "frozen-fixture-harness"}
    evidence = {"score": result["score"], "result": outcome, "valid_for_evaluation": True}

    def play(*args, **kwargs):
        events.append("saved")
        return deepcopy(result)

    def audit(record, policy):
        assert events == ["saved"]
        assert record == result and policy == harness
        events.append("audited")
        return deepcopy(evidence)

    original_gate = runner.require_evaluable_result

    def gate(record, context):
        assert events == ["saved", "audited"]
        assert json.loads((tmp_path / "audits/fixture-episode.json").read_text()) == evidence
        original_gate(record, context)
        events.append("gated")

    monkeypatch.setattr(runner.Lab, "play", play)
    monkeypatch.setattr(audit_pokemon_run, "audit_episode", audit)
    monkeypatch.setattr(runner, "require_evaluable_result", gate)
    observed = lab_at(tmp_path).play(None, harness, {}, 42, "search_validation")
    events.append("returned")
    assert observed == result
    assert events == ["saved", "audited", "gated", "returned"]


def test_validly_replayed_cutoff_is_saved_but_never_returned_as_score(tmp_path, monkeypatch):
    import audit_pokemon_run
    result = episode("unresolved")
    evidence = {"score": None, "result": "unresolved", "valid_for_evaluation": False,
                "status": "excluded_cutoff", "observed_compatibility_score": 50}
    monkeypatch.setattr(runner.Lab, "play", lambda *args, **kwargs: deepcopy(result))
    monkeypatch.setattr(audit_pokemon_run, "audit_episode", lambda *args, **kwargs: deepcopy(evidence))
    with pytest.raises(IncompleteGameResult):
        lab_at(tmp_path).play(None, {}, {}, 42, "search_train")
    assert json.loads((tmp_path / "audits/fixture-episode.json").read_text()) == evidence


def test_audit_rejection_blocks_a_native_looking_result(tmp_path, monkeypatch):
    import audit_pokemon_run
    result = episode()
    monkeypatch.setattr(runner.Lab, "play", lambda *args, **kwargs: deepcopy(result))

    def reject(*args, **kwargs):
        raise audit_pokemon_run.AuditError("fixture changed rules or invalid request evidence")

    monkeypatch.setattr(audit_pokemon_run, "audit_episode", reject)
    with pytest.raises(audit_pokemon_run.AuditError, match="changed rules"):
        lab_at(tmp_path).play(None, {}, {}, 42, "search_validation")
    assert not (tmp_path / "audits").exists()


def fake_preflight(monkeypatch, *, creation_error=None):
    settings = Settings("http://private-fixture.invalid/v1/systemone", MODEL, 1,
                        "https://private-coach-fixture.invalid/v1", "fixture-coach", "synthetic-secret", 1)
    monkeypatch.setattr(runner.Settings, "load", lambda: settings)
    lifecycle = []

    def create(seed, config):
        lifecycle.append(("created", seed))
        if creation_error:
            raise creation_error
        return SimpleNamespace(close=lambda: lifecycle.append(("closed", seed)))

    adapter = SimpleNamespace(defaults={"max_steps": 160}, validate_config=lambda _: None,
                              context=lambda _: deepcopy(CONTEXT), create=create)
    monkeypatch.setattr(runner, "get_game", lambda game: adapter if game == "pokemon" else pytest.fail("wrong game"))
    return lifecycle


def test_protocol_freezes_all_sources_budget_and_seats_before_any_model_client(tmp_path, monkeypatch):
    class ProtocolFrozen(Exception):
        pass

    lifecycle = fake_preflight(monkeypatch)
    directory = tmp_path / "new-evidence"

    def stop_at_lab(*args, **kwargs):
        assert (directory / "protocol.json").is_file()
        raise ProtocolFrozen()

    monkeypatch.setattr(runner, "EvidenceLab", stop_at_lab)
    monkeypatch.setattr(runner, "RecordingDecision", lambda *args: pytest.fail("Decision constructed before protocol"))
    monkeypatch.setattr(runner, "RecordingCoach", lambda *args: pytest.fail("Coach constructed before protocol"))
    with pytest.raises(ProtocolFrozen):
        runner.run(directory, max_steps=160)
    assert lifecycle == [("created", 42), ("closed", 42)]
    manifest = json.loads((directory / "protocol.json").read_text())
    assert manifest["budget"] == {"max_episodes": 14, "decision_request_attempts_max": 2240,
                                   "coach_attempts_max": 4, "gepa_reflections": 1}
    assert manifest["schedule"] == {"train_seeds": [42], "validation_seeds": [4243],
                                    "promotion_seeds": [1042, 2043, 3042, 4043]}
    assert {seed % 2 for seed in manifest["schedule"]["promotion_seeds"]} == {0, 1}
    assert [seed % 2 for seed in manifest["schedule"]["promotion_seeds"]] == [0, 1, 0, 1]
    assert set(manifest["schedule"]["train_seeds"]).isdisjoint(manifest["schedule"]["validation_seeds"])
    assert set(manifest["schedule"]["promotion_seeds"]).isdisjoint(
        manifest["schedule"]["train_seeds"] + manifest["schedule"]["validation_seeds"])
    assert manifest["think_mode"] == "never"
    assert manifest["runtime_fingerprint"] == FINGERPRINT
    assert manifest["config"]["game_id"] == "pokemon" and manifest["config"]["max_steps"] == 160
    assert manifest["game_context"] == CONTEXT
    required = {"src/game_harness/runtime.py", "src/game_harness/providers.py", "src/game_harness/scoring.py",
                "src/game_harness/evolution.py", "src/game_harness/feedback.py", "src/game_harness/visibility.py",
                "src/game_harness/service.py", "src/game_harness/initialization_contract.py",
                "src/game_harness/games/pokemon.py", "src/game_harness/games/pokemon_bridge.cjs",
                "src/game_harness/games/node_bridge.py", "src/game_harness/games/__init__.py",
                "scripts/run_pokemon.py", "scripts/run_overcooked.py", "scripts/audit_pokemon_run.py",
                "scripts/setup_pokemon.sh", "requirements-pokemon.lock"}
    assert required.issubset(manifest["source_hashes"])
    for relative, frozen_hash in manifest["source_hashes"].items():
        assert not Path(relative).is_absolute()
        assert frozen_hash == hashlib.sha256((runner.ROOT / relative).read_bytes()).hexdigest()
    serialized = json.dumps(manifest)
    assert "fixture.invalid" not in serialized and "synthetic-secret" not in serialized
    assert not (directory / "calls.jsonl").exists()


def test_existing_evidence_directory_is_preserved_before_settings_or_preflight(tmp_path, monkeypatch):
    marker = tmp_path / "protocol.json"
    marker.write_text("existing frozen evidence")
    monkeypatch.setattr(runner.Settings, "load", lambda: pytest.fail("must reject before reading settings"))
    monkeypatch.setattr(runner, "get_game", lambda _: pytest.fail("must reject before native preflight"))
    with pytest.raises(ValueError, match="already exists"):
        runner.run(tmp_path)
    assert marker.read_text() == "existing frozen evidence"


def test_native_install_failure_creates_no_protocol_or_model_client(tmp_path, monkeypatch):
    directory = tmp_path / "unstarted"
    fake_preflight(monkeypatch, creation_error=RuntimeError("fixed dependency provenance mismatch"))
    monkeypatch.setattr(runner, "RecordingDecision", lambda *args: pytest.fail("native preflight must come first"))
    monkeypatch.setattr(runner, "RecordingCoach", lambda *args: pytest.fail("native preflight must come first"))
    with pytest.raises(RuntimeError, match="provenance"):
        runner.run(directory)
    assert not directory.exists()


def test_setup_script_preserves_mismatched_or_dirty_native_checkout():
    setup = (runner.ROOT / "scripts/setup_pokemon.sh").read_text()
    assert "7244fe8e28fa8bc4d0809ae30d183c608b62bb2b" in setup
    assert "requirements-pokemon.lock" in setup
    assert "status --porcelain --untracked-files=no" in setup
    assert "npm ci --ignore-scripts --omit=dev" in setup
    assert "git reset" not in setup and "git clean" not in setup
    assert setup.index("rev-parse HEAD") < setup.index("npm ci")
    assert setup.index("status --porcelain") < setup.index("npm ci")
    assert 'node "$pokemon_source/build" --force' in setup


def test_source_hash_verification_rejects_content_drift(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    source = tmp_path / "frozen.py"
    source.write_text("original")
    manifest = {"source_hashes": {"frozen.py": hashlib.sha256(source.read_bytes()).hexdigest()}}
    assert runner.verify_frozen_sources(manifest) is None
    source.write_text("changed")
    with pytest.raises(ValueError, match="Experiment source changed: frozen.py"):
        runner.verify_frozen_sources(manifest)


def test_runtime_change_before_play_prevents_any_game_or_model_work(tmp_path, monkeypatch):
    lab = lab_at(tmp_path)
    monkeypatch.setattr(runner, "runtime_fingerprint", lambda: {**FINGERPRINT, "node": "changed"})
    monkeypatch.setattr(runner.Lab, "play", lambda *args: pytest.fail("changed runtime must not play"))
    with pytest.raises(ValueError, match="Native runtime changed"):
        lab.play(None, {}, {}, 42, "search_train")


def test_runtime_change_during_episode_prevents_audit_and_score_consumption(tmp_path, monkeypatch):
    import audit_pokemon_run
    lab = lab_at(tmp_path)
    observed = []
    changed = {**FINGERPRINT, "node": "changed"}
    values = iter([deepcopy(FINGERPRINT), changed])
    monkeypatch.setattr(runner, "runtime_fingerprint", lambda: next(values))

    def play(*args, **kwargs):
        observed.append("played")
        return episode()

    monkeypatch.setattr(runner.Lab, "play", play)
    monkeypatch.setattr(audit_pokemon_run, "audit_episode", lambda *args: pytest.fail("runtime drift must block audit"))
    with pytest.raises(ValueError, match="Native runtime changed"):
        lab.play(None, {}, {}, 42, "search_train")
    assert observed == ["played"]
    assert not (tmp_path / "audits").exists()


def test_runtime_fingerprint_hashes_executed_files_and_rejects_dirty_source(tmp_path, monkeypatch):
    source = tmp_path / ".runtime/pokemon-showdown"
    dist = source / "dist"
    dist.mkdir(parents=True)
    (dist / "battle.js").write_text("frozen native rules")
    (dist / "dex.json").write_text('{"gen":3}')
    package = tmp_path / "site-packages/poke_env"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("# frozen parser")
    (package / "gen3.json").write_text('{"types":[]}')
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner.importlib.util, "find_spec", lambda _: SimpleNamespace(origin=str(package / "__init__.py")))
    dirty = False

    def native_command(args, **kwargs):
        if args[:3] == ["git", "-C", str(source)]:
            return " M sim/battle.ts\n" if dirty else ""
        assert args == ["node", "--version"]
        return "v22.18.0\n"

    monkeypatch.setattr(runner.subprocess, "check_output", native_command)
    first = original_runtime_fingerprint()
    assert first["showdown_dist"]["files"] == first["poke_env_package"]["files"] == 2
    assert str(tmp_path) not in json.dumps(first)
    (dist / "unrelated.txt").write_text("not executed")
    assert original_runtime_fingerprint() == first
    (dist / "battle.js").write_text("modified native rules")
    second = original_runtime_fingerprint()
    assert second["showdown_dist"] != first["showdown_dist"]
    assert second["poke_env_package"] == first["poke_env_package"]
    (package / "__init__.py").write_text("# modified parser")
    assert original_runtime_fingerprint()["poke_env_package"] != first["poke_env_package"]
    dirty = True
    with pytest.raises(ValueError, match="Showdown source has local edits"):
        original_runtime_fingerprint()


def test_audit_explicitly_excluded_result_cannot_reach_gepa_even_if_native_looking(tmp_path, monkeypatch):
    import audit_pokemon_run
    result = episode()
    evidence = {"score": None, "result": "win", "valid_for_evaluation": False,
                "status": "excluded_incomplete"}
    monkeypatch.setattr(runner.Lab, "play", lambda *args, **kwargs: deepcopy(result))
    monkeypatch.setattr(audit_pokemon_run, "audit_episode", lambda *args: deepcopy(evidence))
    with pytest.raises((ProviderError, IncompleteGameResult), match="evaluat|评测|审计|Incomplete"):
        lab_at(tmp_path).play(None, {}, {}, 42, "search_train")
