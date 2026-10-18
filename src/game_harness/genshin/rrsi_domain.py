"""Google RRSI Domain for frozen-model snapshot selection, not live game score.

Install the optional rrsi extra to use this module. The suite and grader stay
outside the evolvable harness. A research winner is never a client release.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import tempfile

from rrsi.domain import Domain
from rrsi.evaluate import TaskResult

from ..store import atomic_json
from .contracts import canonical_hash
from .snapshot import SnapshotSuite, evaluate_snapshot_case
from .taskpack import TaskPack, build_taskpack


class GenshinSnapshotDomain(Domain):
    name = "genshin_snapshot"
    harness_path = "harness"
    source_exts = {".txt", ".json"}
    briefs = {role: (
        "Improve StartLux skill selection on visible Genshin observations. "
        "The policy model is frozen. This domain measures snapshot choices only; "
        "no route, battle or reward-claim success can be inferred. Never embed "
        "case IDs, fixture accounts, game dates, expected actions or grading logic. "
        "Edit generic instructions and action descriptions/guards. Keep model, "
        "execution capabilities and release metadata fixed. Resource hashes are "
        "rebuilt by trusted tooling. Heldout observations are not available."
    ) for role in ("analyst", "digester", "proposer", "critic")}
    critic_patterns = [(r"acceptable_actions|expect_abstention|case_hash", "grading label reference"),
                       (r"fixture-account|fixture-server|snapshot-case-", "fixture-specific entity")]
    component_signals = [("prompt", [r"instructions\.txt", r'"label"']),
                         ("control_flow", [r'"preconditions"']),
                         ("skill", [r"actions\.json", r'"arguments"'])]

    def __init__(self, root: Path, suite: SnapshotSuite, provider_factory):
        self.root = Path(root) / "domains" / self.name
        self.suite = SnapshotSuite.model_validate(suite.model_dump())
        self.suite_hash = canonical_hash(self.suite.model_dump())
        self.cases = {case.id: case for case in self.suite.cases}
        self.provider_factory = provider_factory
        self.initial_pack = TaskPack.load(self.root / self.harness_path)
        self.frozen_metadata = self.initial_pack.manifest.model_dump(exclude={"files"})
        # Capabilities are frozen for snapshot comparisons. Real skill changes
        # require their own execution evaluations before entering this domain.
        self.capabilities = canonical_hash({option.id: [option.skill, option.skill_version, option.arguments]
                                            for option in self.initial_pack.options})

    def evolve_ids(self):
        return [case.id for case in self.suite.cases if case.split == "evolve"]

    def heldout_ids(self):
        return [case.id for case in self.suite.cases if case.split == "heldout"]

    def smoke_ids(self, incumbent_per_task=None):
        return self.evolve_ids()[:2]

    def _candidate_pack(self, root, destination):
        pack = build_taskpack(self.harness_dir(Path(root)), destination)
        if pack.manifest.model_dump(exclude={"files"}) != self.frozen_metadata:
            raise ValueError("RRSI candidate changed frozen task pack metadata")
        if pack.manifest.files.keys() != self.initial_pack.manifest.files.keys():
            raise ValueError("snapshot candidates cannot introduce unused resources")
        capabilities = canonical_hash({option.id: [option.skill, option.skill_version, option.arguments]
                                       for option in pack.options})
        if capabilities != self.capabilities:
            raise ValueError("snapshot candidates cannot change execution capabilities")
        return pack

    def _trial_path(self, runs_dir, job, task_id, trial):
        if not re.fullmatch(r"[a-zA-Z0-9_.-]+", job) or job in {".", ".."}:
            raise ValueError("invalid RRSI job name")
        identity = hashlib.sha256(task_id.encode()).hexdigest()[:24]
        return Path(runs_dir) / "jobs" / job / f"{identity}-{trial}.json"

    def run(self, root, runs_dir, job, ids, k, log_prefix=""):
        if type(k) is not int or k < 1 or not ids or len(set(ids)) != len(ids):
            raise ValueError("evaluation requires unique cases and a positive trial count")
        if any(task_id not in self.cases for task_id in ids):
            raise ValueError("unknown snapshot case")
        self._trial_path(runs_dir, job, ids[0], 0)
        with tempfile.TemporaryDirectory(prefix="genshin-rrsi-") as temporary:
            pack = self._candidate_pack(root, Path(temporary) / "pack")
            pack_hash = canonical_hash(pack.manifest.model_dump())
            provider = self.provider_factory(pack.manifest.decision_model)
            try:
                for task_id in ids:
                    case = self.cases[task_id]
                    for trial in range(k):
                        path = self._trial_path(runs_dir, job, task_id, trial)
                        if path.exists():
                            previous = json.loads(path.read_text())
                            if (previous.get("suite_hash") != self.suite_hash
                                    or previous.get("pack_hash") != pack_hash
                                    or previous.get("case_hash") != canonical_hash(case.model_dump())):
                                raise ValueError("cannot resume an evaluation with changed cases or policy")
                            continue
                        record = evaluate_snapshot_case(pack, case, provider, f"{job}:{task_id}:{trial}")
                        record.update(suite_hash=self.suite_hash, pack_hash=pack_hash,
                                      evidence_origin=self.suite.evidence_origin)
                        atomic_json(path, record)
            finally:
                provider.close()

    def score(self, runs_dir, job, ids, k):
        per_task = {}
        unknown_cost = 0
        for task_id in ids:
            rewards, tokens, missing = [], [], 0
            for trial in range(k):
                record = self.load_trial(runs_dir, job, task_id, trial)
                absent = record is None or record.get("missing") is not False
                reward = 0.0 if absent else record["reward"]
                token_count = None if absent else record.get("policy_tokens")
                if reward not in (0.0, 1.0) or (record is not None and record.get("suite_hash") != self.suite_hash):
                    raise ValueError("invalid or out-of-scope snapshot result")
                rewards.append(reward)
                tokens.append(token_count)
                missing += int(absent)
                unknown_cost += int(token_count is None)
            per_task[task_id] = TaskResult(rewards=rewards, tokens=tokens, missing=missing)
        return per_task, {"evaluation_mode": "snapshot", "suite_hash": self.suite_hash,
                          "evidence_origin": self.suite.evidence_origin,
                          "model": self.initial_pack.manifest.decision_model,
                          "unknown_cost_trials": unknown_cost, "eligible_for_client_release": False}

    def load_trial(self, runs_dir, job, task_id, trial):
        path = self._trial_path(runs_dir, job, task_id, trial)
        return json.loads(path.read_text()) if path.exists() else None

    def render_trace(self, rec, detail=False):
        # RRSI's proposer slices events; a snapshot has a single decision.
        value = {key: rec.get(key) for key in ("evaluation_mode", "observation", "catalog", "receipt",
                                              "reward", "missing", "error_type", "policy_tokens")}
        return json.dumps(value, ensure_ascii=False, indent=2 if detail else None)

    def task_row(self, task_id, rec, tr):
        return f"{task_id}: snapshot_reward={tr.mean:.3f}, missing={tr.missing}"

    def smoke(self, root, runs_dir, job, ids):
        try:
            with tempfile.TemporaryDirectory(prefix="genshin-smoke-") as temporary:
                pack = self._candidate_pack(root, Path(temporary) / "pack")
                empty = [task_id for task_id in ids if not pack.candidates(self.cases[task_id].observation)]
                return not empty, {"evaluation_mode": "static", "empty_catalog_cases": empty}
        except (ValueError, OSError):
            return False, {"evaluation_mode": "static", "error": "invalid_candidate_pack"}

    def guards(self, incumbent, candidate):
        violations = []
        for key in ("evaluation_mode", "suite_hash", "evidence_origin", "model"):
            if candidate.extra.get(key) != incumbent.extra.get(key):
                violations.append(f"changed_{key}")
        if candidate.missing:
            violations.append("missing_snapshot_trials")
        if candidate.extra.get("unknown_cost_trials", 1):
            violations.append("unknown_policy_cost")
        return violations
