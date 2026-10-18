"""Post-hoc reporting tests on native transitions, not model-strength evidence."""
from copy import deepcopy
import fcntl
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import analyze_tetris_landing_goals as analyzer
from game_harness.tetris_landing import build_landing_projections


@pytest.fixture(scope="module")
def native_episodes():
    """Deterministic arranged boards, executed by unchanged native rules.

    Goal receipts are explicitly test fixtures. Native state transitions and
    scores come from the real engine, without a model or network request.
    """
    if not shutil.which("node"):
        pytest.skip("Real native transition fixtures require Node.js")
    bridge = ROOT / "src/game_harness/games/arcade_bridge.cjs"
    program = "const {tetris}=require(" + json.dumps(str(bridge)) + ");\n" + r"""
function snapshot(s,step) { return {game_id:'tetris',step,score:s.score.points,metrics:{...s.score},
  observation:tetris.observe(s),reason:s.reason??'step_limit'}; }
function episode(kind) {
  let state=tetris.create({},'posthoc-fixture');
  let target;
  if(kind==='clear') {
    state.active={type:'I',x:2,y:-2,rotation:1};
    for(let y=16;y<20;y++)for(let x=0;x<10;x++)if(x!==4)state.board[(y+2)*10+x]=6;
    state.score.lines=9;target={leftmost_column:4,rotation:1};
  } else if(kind==='topout') {
    state.active={type:'O',x:-1,y:-2,rotation:0};state.board[20]=6;
    target={leftmost_column:0,rotation:0};
  } else {
    state.active={type:'O',x:3,y:-2,rotation:0};target={leftmost_column:9,rotation:0};
  }
  state=tetris.restore(state);
  const before=snapshot(state,0);
  state=tetris.step(tetris.input(state,{buttons:['hard-drop'],durationMs:100}),6);
  const after=snapshot(state,1);
  return {before,after,target};
}
console.log(JSON.stringify(Object.fromEntries(['clear','invalid','topout'].map(k=>[k,episode(k)]))));
"""
    result = subprocess.run([shutil.which("node"), "-e", program], check=True,
                            capture_output=True, text=True, timeout=10)
    fixtures = json.loads(result.stdout)
    episodes = {}
    for name, fixture in fixtures.items():
        before, after, target = fixture["before"], fixture["after"], fixture["target"]
        action_state = deepcopy(before)
        action_state["decision_goal_memory"] = {"target": deepcopy(target)}
        goal_state = deepcopy(before)
        goal_state["decision_goal_memory"] = {"target": None}
        choice = f"target:{target['leftmost_column']}:{target['rotation']}"
        calls = [
            {"purpose": "goal", "request_id": "fixture-goal", "input_state": goal_state,
             "receipt": {"choice_id": choice}},
            {"purpose": "game_action", "request_id": "fixture-action", "input_state": action_state,
             "receipt": {"choice_id": "hard-drop:100"}},
        ]
        step = {"before": before, "after": after, "action": "hard-drop:100",
            "receipt": {"request_id": "fixture-action", "response_hash": "fixture-response",
                "tetris_context": {"action_input_state": action_state, "calls": calls}}}
        episodes[name] = {"episode_id": f"fixture-{name}", "trace": [step], "steps": 1,
            "score": after["score"], "termination_reason": after["reason"],
            "initial_state": before, "final_state": after}
    return episodes


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def checksum(path):
    return {str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in path.rglob("*") if p.is_file()}


@pytest.fixture
def study(tmp_path, monkeypatch, native_episodes):
    path = tmp_path / "study"
    path.mkdir(); (path / ".run.lock").touch()
    scenarios = [{"id": f"evaluation-{i:02d}", "seed": i + 900, "order": list(analyzer.ARMS)} for i in range(12)]
    harness = {"id": "frozen-fixture-harness"}
    manifest = {"schema": "tetris-landing-experiment/v1", "id": "fixture-study", "arm_order": list(analyzer.ARMS),
        "arms": dict.fromkeys(analyzer.ARMS, harness), "scenarios": {"evaluation": scenarios}}
    result = {"per_seed": []}
    for index, scenario in enumerate(scenarios):
        pair = {**scenario, "arms": {}}
        for arm in analyzer.ARMS:
            kind = "clear" if (index in (0, 3) and arm == "planning") or (index == 1 and arm == "baseline") else (
                "topout" if index == 2 and arm == "planning" else "invalid")
            episode = deepcopy(native_episodes[kind])
            episode.update(episode_id=f"fixture-{index}", logical_trial=scenario["id"], seed=scenario["seed"],
                           purpose="evaluation", experiment_arm=arm)
            if arm == "planning":
                ledger = episode["trace"][0]["receipt"]["tetris_context"]
                for state in [ledger["action_input_state"], *[call["input_state"] for call in ledger["calls"]]]:
                    state["tetris_landing_projections"] = build_landing_projections(state)
            directory = path / "episodes" / arm / scenario["id"] / "attempt-01"
            write(directory / "episode.json", episode)
            write(directory / "complete.json", {"episode_hash": analyzer.digest(episode), "harness_hash": analyzer.digest(harness)})
            pair["arms"][arm] = {"points": episode["score"], "termination_reason": episode["termination_reason"]}
        result["per_seed"].append(pair)
    write(path / "manifest.json", manifest)
    write(path / "evaluation_result.json", result)
    write(path / "evaluation_result.sha256.json", {"sha256": analyzer.digest(result)})
    write(path / "episodes/planning/calibration-00/attempt-01/episode.json", {"must_not_read": True})
    audit_calls = []
    def full_audit_under_read_lock(received):
        assert received == path
        with (path / ".run.lock").open("rb") as other:
            with pytest.raises(BlockingIOError):
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        audit_calls.append(received)
        return {"status": "completed", "manifest_hash": analyzer.digest(manifest), "audit": {"fixture_only": True}}
    monkeypatch.setattr(analyzer, "audit_study", full_audit_under_read_lock)
    return path, audit_calls


def test_native_clear_outcome_and_reached_pose_are_separate_from_goal_claim(native_episodes):
    facts = analyzer.inspect_episode(native_episodes["clear"])
    counts = facts["counts"]
    assert counts["native_points_gained"] == counts["hard_drop_native_points"] == 800
    assert counts["native_lines_cleared"] == counts["hard_drop_actual_lines"] == 4
    assert counts["hard_drop_actions_at_target"] == counts["hard_drop_actions_clearing_lines"] == 1
    assert counts["goal_calls_with_conditional_clear_option"] == counts["goal_calls_selecting_conditional_clear_option"] == 1
    assert facts["explicit_target_outcome_counts"]["pose_valid"] == {"true": 1}
    assert facts["explicit_target_outcome_counts"]["lines_cleared"] == {"4": 1}
    assert facts["hard_drops"][0]["native_lines_cleared"] == 4
    assert facts["hard_drops"][0]["actual_pose_before_action"] == {"leftmost_column": 4, "rotation": 1}


def test_invalid_goal_prediction_is_null_even_when_native_harddrop_locks_elsewhere(native_episodes):
    facts = analyzer.inspect_episode(native_episodes["invalid"])
    assert facts["explicit_target_outcome_counts"]["pose_valid"] == {"false": 1}
    assert facts["explicit_target_outcome_counts"]["lines_cleared"] == {"null": 1}
    assert facts["explicit_target_outcome_counts"]["top_out"] == {"null": 1}
    assert facts["explicit_target_outcome_counts"]["invalid_reason"] == {"outside_horizontal_bounds": 1}
    assert facts["counts"]["hard_drop_actions"] == facts["counts"]["target_pose_invalid_actions"] == 1
    assert facts["counts"]["hard_drop_actions_at_target"] == 0
    assert facts["score"] == 0
    assert len(analyzer.lock_timeline(native_episodes["invalid"])) == 1


def test_keep_can_retain_clearing_target_but_is_not_explicit_new_target(native_episodes):
    episode = deepcopy(native_episodes["clear"])
    call = episode["trace"][0]["receipt"]["tetris_context"]["calls"][0]
    call["receipt"]["choice_id"] = "keep"
    call["input_state"]["decision_goal_memory"]["target"] = {"leftmost_column": 4, "rotation": 1}
    facts = analyzer.inspect_episode(episode)
    assert facts["goal_choice_counts"] == {"keep": 1}
    assert facts["counts"]["explicit_target_choices"] == 0
    assert facts["counts"]["goal_calls_selecting_conditional_clear_option"] == 1
    assert facts["explicit_target_outcome_counts"]["lines_cleared"] == {}
    assert facts["per_action_target_outcome_counts"]["lines_cleared"] == {"4": 1}


def test_clear_leaves_no_target_without_crediting_projected_opportunity(native_episodes):
    episode = deepcopy(native_episodes["clear"])
    ledger = episode["trace"][0]["receipt"]["tetris_context"]
    ledger["calls"][0]["receipt"]["choice_id"] = "clear"
    ledger["action_input_state"]["decision_goal_memory"]["target"] = None
    facts = analyzer.inspect_episode(episode)
    assert facts["counts"]["actions_without_target"] == 1
    assert facts["counts"]["goal_calls_with_conditional_clear_option"] == 1
    assert facts["counts"]["goal_calls_selecting_conditional_clear_option"] == 0
    assert facts["counts"]["hard_drop_actions_at_target"] == 0
    assert facts["hard_drops"][0]["target_reached_before_action"] is None
    assert facts["score"] == 800  # Only observed native score is the outcome.


def test_actual_topout_and_conditional_topout_are_explicit(native_episodes):
    facts = analyzer.inspect_episode(native_episodes["topout"])
    assert facts["termination_reason"] == "TOP_OUT"
    assert facts["explicit_target_outcome_counts"]["top_out"] == {"true": 1}
    assert facts["counts"]["hard_drop_actions_at_target"] == 1
    assert facts["score"] == 0


def test_full_audit_first_formal_only_fixed_cases_and_readonly(study):
    path, calls = study
    before = checksum(path)
    result = analyzer.analyze(path)
    assert checksum(path) == before and calls == [path]
    assert result["paired_bags"] == 12 and result["formal_episodes"] == 24
    assert result["post_hoc_exploratory"] is True
    assert result["arms"]["baseline"]["projection_facts_were_sent_to_model"] is False
    assert result["arms"]["planning"]["projection_facts_were_sent_to_model"] is True
    assert all(arm["episodes"] == 12 and arm["counts"]["actions"] == 12 for arm in result["arms"].values())
    categories = result["categories"]
    assert categories["positive_gain"][0]["pair_id"] == "evaluation-00"
    assert categories["positive_gain"][0]["eligible_pair_count"] == 2
    assert categories["negative_gain"][0]["pair_id"] == "evaluation-01"
    assert categories["zero_gain_planning_top_out"][0]["pair_id"] == "evaluation-02"
    event = categories["positive_gain"][0]["episodes"][1]["all_lock_events"][0]
    assert event["lines_cleared"] == 4 and event["native_points_delta"] == 800
    assert event["actual_active_before_action"]["cells"]
    assert event["goal_events_at_step"][0]["choice_id"] == "target:4:1"
    assert event["board_after"]["holes"] == 0
    assert "board" in categories["positive_gain"][0]["episodes"][1]["final_state"]
    assert str(path) not in json.dumps(result)
    assert "not independent samples" in json.dumps(result)
    assert "does NOT mean reachable" in json.dumps(result)


def test_noncomplete_audit_cannot_read_or_diagnose_scores(study, monkeypatch):
    path, _ = study
    monkeypatch.setattr(analyzer, "audit_study", lambda path: {"status": "incomplete"})
    with pytest.raises(analyzer.ReportError, match="complete audited"):
        analyzer.analyze(path)


def test_changed_episode_result_and_saved_projection_fail_closed(study, native_episodes):
    path, _ = study
    source = path / "episodes/planning/evaluation-00/attempt-01/episode.json"
    episode = analyzer.load(source); episode["score"] += 1; write(source, episode)
    with pytest.raises(analyzer.ReportError, match="Episode changed"):
        analyzer.analyze(path)
    state = deepcopy(native_episodes["clear"])
    action = state["trace"][0]["receipt"]["tetris_context"]["action_input_state"]
    action["tetris_landing_projections"] = build_landing_projections(action)
    action["tetris_landing_projections"]["candidates"][0]["points_delta"] = 999
    with pytest.raises(analyzer.ReportError, match="Saved planning facts differ"):
        analyzer.inspect_episode(state)


def test_lock_blocks_active_experiment_and_does_not_create_missing_lock(study, tmp_path):
    path, calls = study
    missing = tmp_path / "missing"; missing.mkdir()
    with pytest.raises(analyzer.ReportError, match="missing"):
        analyzer.analyze(missing)
    assert not (missing / ".run.lock").exists()
    with (path / ".run.lock").open("rb") as writer:
        fcntl.flock(writer, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(analyzer.ReportError, match="running"):
            analyzer.analyze(path)
    assert not calls
