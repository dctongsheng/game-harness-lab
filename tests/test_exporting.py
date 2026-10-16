"""Approved exports are immutable, portable, and isolated from unrelated Git work.

Git integration tests use only temporary local repositories and a local bare remote.
They never address GitHub or any other network service.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from game_harness.exporting import export_approved, read_bundle, sync_bundle


@pytest.fixture
def approved():
    candidate = {"decision_instructions": "从当前候选动作中选择方向。", "strategy_notes": "观察食物与身体位置。"}
    version = {"id": "h-approved", "scope": "scope-a", "parent_id": "h-parent",
               "candidate": candidate, "decision_model": "startlux-decision-test",
               "identity_verification": "service_reported_name"}
    job = {"id": "job-approved", "status": "promoted", "scope": "scope-a",
           "parent_id": "h-parent", "candidate_id": "h-approved",
           "created_at": "2026-10-17T10:00:00Z", "finished_at": "2026-10-17T10:10:00Z",
           "gate": {"minimum_pairs": 4, "minimum_mean_gain": 0.5, "no_pair_regression": True},
           "baseline_score": 1.5, "candidate_score": 2.5, "mean_gain": 1.0,
           "pairs": [{"seed": n, "baseline": n % 2 + 1, "candidate": n % 2 + 2,
                      "baseline_episode_id": f"ep-base-{n}", "candidate_episode_id": f"ep-new-{n}"}
                     for n in range(4)],
           "decision": "达到固定配对门槛", "config": {"game_id": "snake", "board_size": 8, "max_steps": 120},
           "gepa": {"engine": "gepa", "version": "0.1.4", "best_idx": 1, "best_score": 3.0,
                    "val_aggregate_scores": [2.0, 3.0], "candidate_count": 2, "total_metric_calls": 12}}
    context = {"game_id": "snake", "name": "贪吃蛇", "rules_version": "snake-v1", "rules": "吃食物得分。",
               "score_spec": {"id": "food-v1", "min": 0, "max": 61, "unit": "食物", "direction": "maximize",
                              "description": "实际吃到的食物数量", "minimum_mean_gain": 0.5},
               "config": {"board_size": 8, "max_steps": 120}, "opponent_policy": None,
               "scenario_policy": "种子控制食物", "action_policy": "四方向由模型决定"}
    brief = {"background": "网格游戏", "goal": "吃食物", "rules": ["碰撞结束"],
             "precautions": ["避免撞墙"], "strategy": "评估空间"}
    return version, job, context, brief


def export(root, approved):
    return export_approved(root, *approved)


def bundle_path(root, approved):
    version, _, context, _ = approved
    return root / context["game_id"] / version["id"]


@pytest.mark.parametrize("status", ["pending", "running", "retained", "failed", "interrupted"])
def test_only_promoted_evaluation_can_export(tmp_path, approved, status):
    approved[1]["status"] = status
    with pytest.raises(ValueError, match="approved"):
        export(tmp_path / "harnesses", approved)
    assert not (tmp_path / "harnesses").exists()


@pytest.mark.parametrize("field,value", [("candidate_id", "h-other"), ("scope", "other-scope")])
def test_approval_must_match_version_and_scope(tmp_path, approved, field, value):
    approved[1][field] = value
    with pytest.raises(ValueError, match="approved"):
        export(tmp_path, approved)


def test_four_complete_non_regressing_pairs_are_required(tmp_path, approved):
    approved[1]["pairs"].pop()
    with pytest.raises(ValueError, match="four"):
        export(tmp_path, approved)


def test_one_regression_rejects_even_with_positive_mean_gain(tmp_path, approved):
    approved[1]["pairs"][0]["candidate"] = 0
    approved[1]["pairs"][1]["candidate"] = 20
    with pytest.raises(ValueError, match="non-regressing"):
        export(tmp_path, approved)


def test_recorded_gate_is_recomputed_from_pairs_not_claimed_mean(tmp_path, approved):
    for pair in approved[1]["pairs"]:
        pair["candidate"] = pair["baseline"]
    approved[1]["mean_gain"] = 1000
    with pytest.raises(ValueError, match="gate"):
        export(tmp_path, approved)


def test_game_specific_minimum_gain_is_enforced(tmp_path, approved):
    approved[1]["gate"]["minimum_mean_gain"] = 2.0
    with pytest.raises(ValueError, match="gate"):
        export(tmp_path, approved)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_scores_cannot_satisfy_approval(tmp_path, approved, bad):
    approved[1]["pairs"][0]["candidate"] = bad
    with pytest.raises(ValueError):
        export(tmp_path, approved)


def test_complete_bundle_checksums_and_portable_content(tmp_path, approved):
    root = tmp_path / "harnesses"
    manifest = export(root, approved)
    path = bundle_path(root, approved)
    assert {item.name for item in path.iterdir()} == {
        "manifest.json", "game.json", "strategy.json", "evaluation.json", "README.md"}
    assert manifest["game_id"] == "snake"
    assert manifest["version_id"] == "h-approved"
    assert manifest["parent_id"] == "h-parent"
    assert manifest["approval_job_id"] == "job-approved"
    assert manifest["think_mode"] == "never"
    assert set(manifest["files"]) == {"game.json", "strategy.json", "evaluation.json", "README.md"}
    for filename, checksum in manifest["files"].items():
        assert checksum == hashlib.sha256((path / filename).read_bytes()).hexdigest()
    bundle = read_bundle(root, "snake", "h-approved")
    assert bundle["strategy"] == approved[0]["candidate"]
    assert bundle["game"]["brief"] == approved[3]
    assert bundle["game"]["context"] == approved[2]
    assert bundle["evaluation"]["pairs"] == approved[1]["pairs"]
    assert bundle["evaluation"]["raw_traces_included"] is False
    assert "not a generalization" in bundle["evaluation"]["evidence_type"]
    assert "不包含模型权重或原始 API 轨迹" in (path / "README.md").read_text()


def test_export_is_byte_identical_when_retried(tmp_path, approved):
    root = tmp_path / "harnesses"
    first = export(root, approved)
    path = bundle_path(root, approved)
    before = {p.name: p.read_bytes() for p in path.iterdir()}
    second = export(root, approved)
    assert second == first
    assert {p.name: p.read_bytes() for p in path.iterdir()} == before
    assert not list(path.parent.glob(".export-*"))


def test_same_version_cannot_be_replaced_by_other_strategy(tmp_path, approved):
    export(tmp_path, approved)
    approved[0]["candidate"]["strategy_notes"] = "另一个策略"
    with pytest.raises(ValueError, match="immutable"):
        export(tmp_path, approved)


@pytest.mark.parametrize("filename", ["game.json", "strategy.json", "evaluation.json", "README.md"])
def test_read_and_reexport_reject_modified_files(tmp_path, approved, filename):
    export(tmp_path, approved)
    (bundle_path(tmp_path, approved) / filename).write_text("tampered")
    with pytest.raises(ValueError, match="integrity"):
        read_bundle(tmp_path, "snake", "h-approved")
    with pytest.raises(ValueError, match="integrity"):
        export(tmp_path, approved)


def test_read_rejects_manifest_identity_change(tmp_path, approved):
    export(tmp_path, approved)
    path = bundle_path(tmp_path, approved) / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["version_id"] = "h-other"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="identity"):
        read_bundle(tmp_path, "snake", "h-approved")


def test_read_rejects_extra_files(tmp_path, approved):
    export(tmp_path, approved)
    (bundle_path(tmp_path, approved) / ".env").write_text("PRIVATE_TEST_VALUE")
    with pytest.raises(ValueError):
        read_bundle(tmp_path, "snake", "h-approved")


def test_metadata_is_whitelisted_recursively(tmp_path, approved):
    version, job, context, brief = approved
    secret = "DO_NOT_EXPORT_SYNTHETIC_SECRET"
    local = "/Users/private/model-data"
    version["provider_health"] = {"api_key": secret, "model_path": local}
    version["raw_response"] = {"authorization": secret}
    job["debug"] = {"api_key": secret, "local_path": local}
    job["run_dir"] = local
    job["gepa"]["run_dir"] = local
    job["gepa"]["api_key"] = secret
    job["config"]["api_key"] = secret
    job["config"]["decision_url"] = "http://private-host:1234/v1"
    job["gate"]["debug_path"] = local
    job["pairs"][0]["raw_response"] = {"authorization": secret}
    context["local_path"] = local
    context["api_key"] = secret
    context["config"]["local_path"] = local
    context["score_spec"]["debug_api_key"] = secret
    brief["internal_notes"] = {"api_key": secret, "path": local}
    try:
        export(tmp_path, approved)
    except ValueError:
        # Rejecting unexpected metadata is also safe; no partial package may survive.
        assert not bundle_path(tmp_path, approved).exists()
        return
    content = "\n".join(p.read_text() for p in bundle_path(tmp_path, approved).iterdir())
    assert secret not in content
    assert local not in content
    assert "raw_response" not in content
    assert "private-host" not in content
    bundle = read_bundle(tmp_path, "snake", "h-approved")
    assert bundle["strategy"] == version["candidate"]
    assert bundle["game"]["context"]["rules"] == context["rules"]


@pytest.mark.parametrize("bad", ["../outside", "a/b", "/tmp/escape", "..", "", "a\\b"])
def test_export_and_read_reject_path_traversal(tmp_path, approved, bad):
    approved[2]["game_id"] = bad
    with pytest.raises(ValueError, match="identifier"):
        export(tmp_path, approved)
    with pytest.raises(ValueError, match="identifier"):
        read_bundle(tmp_path, bad, "h-approved")
    with pytest.raises(ValueError, match="identifier"):
        read_bundle(tmp_path, "snake", bad)


def test_export_cannot_escape_root_through_game_symlink(tmp_path, approved):
    root = tmp_path / "harnesses"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "snake").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        export(root, approved)
    assert not list(outside.iterdir())


def test_read_rejects_symlinked_bundle_file_even_if_checksum_matches(tmp_path, approved):
    export(tmp_path / "harnesses", approved)
    strategy = bundle_path(tmp_path / "harnesses", approved) / "strategy.json"
    outside = tmp_path / "outside.json"
    outside.write_bytes(strategy.read_bytes())
    strategy.unlink()
    strategy.symlink_to(outside)
    with pytest.raises(ValueError):
        read_bundle(tmp_path / "harnesses", "snake", "h-approved")


def git(repo, *args):
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def local_repo(tmp_path):
    repo = tmp_path / "project"
    remote = tmp_path / "remote.git"
    repo.mkdir()
    remote.mkdir()
    git(remote, "init", "--bare", "--initial-branch=main")
    git(repo, "init", "--initial-branch=main")
    git(repo, "config", "user.name", "Export Test")
    git(repo, "config", "user.email", "export-test@example.invalid")
    git(repo, "config", "commit.gpgsign", "false")
    git(repo, "config", "core.hooksPath", str(tmp_path / "no-hooks"))
    (repo / "README.md").write_text("initial\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "Initial local test")
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-u", "origin", "main")
    return repo, remote


def test_sync_commits_only_bundle_and_preserves_unrelated_staged_work(local_repo, approved):
    repo, remote = local_repo
    export(repo / "harnesses", approved)
    path = bundle_path(repo / "harnesses", approved)
    (repo / "README.md").write_text("unrelated staged edit\n")
    (repo / "work-in-progress.txt").write_text("unrelated staged addition\n")
    (repo / ".env").write_text("SYNTHETIC_PRIVATE_KEY=do-not-push\n")
    git(repo, "add", "README.md", "work-in-progress.txt")
    staged_before = git(repo, "diff", "--cached")
    outcome = sync_bundle(repo, path)
    assert outcome["status"] == "synced"
    assert outcome["commit"] == git(repo, "rev-parse", "HEAD")
    assert outcome["commit"] == git(remote, "rev-parse", "refs/heads/main")
    changed = set(git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").splitlines())
    prefix = "harnesses/snake/h-approved/"
    assert changed == {prefix + name for name in ("manifest.json", "game.json", "strategy.json", "evaluation.json", "README.md")}
    assert git(repo, "diff", "--cached") == staged_before
    assert git(repo, "show", "HEAD:README.md") == "initial"
    assert ".env" not in git(remote, "ls-tree", "-r", "--name-only", "refs/heads/main").splitlines()
    assert "work-in-progress.txt" not in git(remote, "ls-tree", "-r", "--name-only", "refs/heads/main").splitlines()
    assert sync_bundle(repo, path)["commit"] == outcome["commit"]
    assert git(repo, "diff", "--cached") == staged_before


@pytest.mark.parametrize("change", ["tamper", "extra"])
def test_sync_rejects_tampered_or_extra_bundle_content(local_repo, approved, change):
    repo, remote = local_repo
    export(repo / "harnesses", approved)
    path = bundle_path(repo / "harnesses", approved)
    original = git(repo, "rev-parse", "HEAD")
    if change == "tamper":
        (path / "strategy.json").write_text('{}')
    else:
        (path / "secret.env").write_text("SYNTHETIC_SECRET")
    with pytest.raises(ValueError):
        sync_bundle(repo, path)
    assert git(repo, "rev-parse", "HEAD") == original
    assert git(remote, "rev-parse", "refs/heads/main") == original
    assert not git(repo, "diff", "--cached", "--name-only")


def test_sync_rejects_paths_outside_exact_package_shape(local_repo):
    repo, _ = local_repo
    for path in (repo.parent / "outside", repo / "README.md", repo / "harnesses", repo / "harnesses" / "snake"):
        with pytest.raises(ValueError):
            sync_bundle(repo, path)


def test_export_rejects_path_traversal_in_version_id(tmp_path, approved):
    approved[0]["id"] = "../outside"
    approved[1]["candidate_id"] = "../outside"
    with pytest.raises(ValueError, match="identifier"):
        export(tmp_path, approved)
    assert not list(tmp_path.iterdir())


def test_read_rejects_manifest_with_path_traversal_filename(tmp_path, approved):
    export(tmp_path, approved)
    path = bundle_path(tmp_path, approved) / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["files"]["../../outside.json"] = "0" * 64
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="file manifest"):
        read_bundle(tmp_path, "snake", "h-approved")
