"""Development tools. Snapshot scores never stand in for Windows game results."""
from __future__ import annotations

import argparse
import hashlib
from dataclasses import replace
import json
import os
from pathlib import Path
import time

from ..providers import ROOT, Settings
from ..store import atomic_json
from .contracts import DecisionReceipt, canonical_hash, catalog_hash
from .decision import StartLuxRealtimeProvider
from .snapshot import SnapshotSuite, evaluate_snapshot_case
from .taskpack import TaskPack, build_taskpack


class SyntheticFixtureProvider:
    """Deterministic plumbing fixture, explicitly excluded from model evidence."""
    def __init__(self, model):
        self.model = model

    def decide(self, observation, instructions, options, request_id):
        abstained = observation.facts.get("fixture_abstain") is True
        return DecisionReceipt(request_id=request_id, observation_hash=observation.binding(),
                               catalog_hash=catalog_hash(options), model=self.model,
                               outcome="abstained" if abstained else "selected",
                               choice_id=None if abstained else options[0].id, elapsed_ms=0.0,
                               payload_hash=canonical_hash({"fixture": True}),
                               raw_response={"synthetic_fixture": True,
                                             "usage": {"input_tokens": 0, "output_tokens": 0}})

    def close(self):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate-pack")
    validate.add_argument("pack", type=Path)
    build = commands.add_parser("build-pack")
    build.add_argument("source", type=Path)
    build.add_argument("destination", type=Path)
    snapshot = commands.add_parser("snapshot")
    snapshot.add_argument("--pack", type=Path, required=True)
    snapshot.add_argument("--suite", type=Path, required=True)
    snapshot.add_argument("--split", choices=["evolve", "heldout"], default="evolve")
    snapshot.add_argument("--output", type=Path, required=True)
    snapshot.add_argument("--fixture-provider", action="store_true")
    live = commands.add_parser("run")
    live.add_argument("--pack", type=Path, required=True)
    live.add_argument("--pipe", default="startlux-genshin")
    live.add_argument("--trace", type=Path, required=True)
    live.add_argument("--max-decisions", type=int, default=200)
    live.add_argument("--max-seconds", type=float, default=1800)
    rrsi = commands.add_parser("rrsi")
    rrsi.add_argument("operation", choices=["baseline", "round", "status"])
    rrsi.add_argument("--suite", type=Path, required=True)
    rrsi.add_argument("--runs", type=Path, default=ROOT / "data" / "genshin-rrsi")
    rrsi.add_argument("--round", type=int, default=0)
    rrsi.add_argument("--fixture-provider", action="store_true")
    args = parser.parse_args()
    if args.command == "validate-pack":
        pack = TaskPack.load(args.pack)
        print(json.dumps({"valid": True, "manifest": pack.manifest.model_dump()}, ensure_ascii=False, indent=2))
        return
    if args.command == "build-pack":
        pack = build_taskpack(args.source, args.destination)
        print(json.dumps({"built": str(pack.root), "release_status": "development"}))
        return

    settings = Settings.load()

    def provider_factory(model):
        if getattr(args, "fixture_provider", False):
            return SyntheticFixtureProvider(model)
        if model.startswith("fixture-"):
            raise ValueError("the bundled fixture pack requires --fixture-provider; configure a real model pack first")
        configured = replace(settings, decision_model=model,
                             decision_url=os.getenv("GENSHIN_DECISION_URL", "https://api.bamboonode.dev/v1/decisions"))
        return StartLuxRealtimeProvider(configured)

    if args.command == "run":
        from .runner import AuditLog, RealtimeRunner
        from .windows_pipe import WindowsPipeSession

        if not 1 <= args.max_decisions <= 2000 or not 0 < args.max_seconds <= 7200:
            raise ValueError("invalid live execution budget")
        pack = TaskPack.load(args.pack)
        if pack.manifest.environment != "windows":
            raise ValueError("a synthetic fixture task pack cannot control the game")
        session = WindowsPipeSession(args.pipe)
        descriptor = session.describe()
        if (descriptor.get("manifest_file_sha256") != hashlib.sha256((pack.root / "manifest.json").read_bytes()).hexdigest()
                or descriptor.get("decision_model") != pack.manifest.decision_model
                or descriptor.get("environment") != "windows"):
            raise ValueError("agent and execution host must load the exact same task pack and model")
        provider = provider_factory(pack.manifest.decision_model)
        audit = AuditLog(args.trace)
        controller = RealtimeRunner(session, provider, pack.instructions,
                                    model=pack.manifest.decision_model, audit=audit)
        deadline = time.monotonic() + args.max_seconds
        decisions = 0
        result = {"status": "budget_exhausted"}
        termination = "budget_exhausted"
        try:
            session.arm()
            while time.monotonic() < deadline:
                deciding = controller.active is None
                if deciding and decisions >= args.max_decisions:
                    break
                result = controller.tick()
                decisions += int(deciding)
                if result["status"] not in {"accepted", "running", "succeeded", "reconciling", "stale"}:
                    termination = result["status"]
                    break
                time.sleep(0.1)
        except KeyboardInterrupt:
            result = {"status": "stopped"}
            termination = "stopped"
        except Exception:
            termination = "error"
            raise
        finally:
            try:
                controller.stop()
            finally:
                audit({"type": "run_ended", "termination_reason": termination, "last_result": result})
                provider.close()
                audit.close()
        print(json.dumps({"execution": result, "termination_reason": termination, "trace": str(args.trace),
                          "daily_completion_verified": False}, ensure_ascii=False, indent=2))
        return

    suite = SnapshotSuite.model_validate_json(args.suite.read_bytes())
    if args.fixture_provider and suite.evidence_origin != "synthetic":
        raise ValueError("fixture provider can evaluate synthetic suites only")
    if args.command == "snapshot":
        if args.output.exists():
            raise FileExistsError("snapshot evaluation output already exists")
        pack = TaskPack.load(args.pack)
        provider = provider_factory(pack.manifest.decision_model)
        try:
            records = [evaluate_snapshot_case(pack, case, provider, f"snapshot:{case.id}")
                       for case in suite.cases if case.split == args.split]
        finally:
            provider.close()
        report = {"evaluation_mode": "snapshot", "evidence_origin": suite.evidence_origin,
                  "provider_mode": "synthetic_fixture" if args.fixture_provider else "startlux",
                  "split": args.split, "suite_hash": canonical_hash(suite.model_dump()),
                  "pack_hash": canonical_hash(pack.manifest.model_dump()),
                  "eligible_for_client_release": False, "records": records,
                  "score": sum(item["reward"] for item in records) / len(records),
                  "missing": sum(item["missing"] for item in records)}
        atomic_json(args.output, report)
        print(json.dumps({key: value for key, value in report.items() if key != "records"}, indent=2))
        return

    from rrsi.config import RRSIConfig
    from rrsi.loop import Run
    from .rrsi_domain import GenshinSnapshotDomain

    if args.fixture_provider and args.operation != "baseline":
        raise ValueError("synthetic provider is for baseline plumbing only, not RRSI search")
    domain = GenshinSnapshotDomain(ROOT, suite, provider_factory)
    run = Run(domain, RRSIConfig.load(domain.root / "rrsi.json"), ROOT, args.runs)
    if args.operation == "baseline":
        result = run.baseline()
        print(json.dumps(result.to_json(), indent=2))
    elif args.operation == "round":
        run.round(args.round)
    else:
        print(json.dumps(run.frontier(), indent=2))


if __name__ == "__main__":
    main()
