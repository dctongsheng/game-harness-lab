"""StartLux platform CLI. Desktop operation stays independent of optimization."""
import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import sys

from .core.decision import StartLuxDecision
from .core.scenario import PolicyBundle
from .core.settings import DecisionSettings
from .core.storage import atomic_json
from .registry import load_scenario


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("scenarios")
    for command in ("run", "optimize", "promote", "rollback"):
        sub = commands.add_parser(command)
        sub.add_argument("--scenario", required=True)
        sub.add_argument("--config", type=Path, required=True)
        sub.add_argument("--output", type=Path, required=True)
        sub.add_argument("--fixture", action="store_true", help="explicit offline plumbing only")
        if command in {"promote", "rollback"}:
            sub.add_argument("--expected-active")
            if command == "promote":
                sub.add_argument("--optimization-run", type=Path, required=True)
        elif command == "run":
            sub.add_argument("--task", required=True)
            sub.add_argument("--policy", type=Path)
        else:
            sub.add_argument("--k", type=int, default=1)
            sub.add_argument("--mode", choices=["sequential", "parallel"], default="sequential")
            sub.add_argument("--max-improver-calls", type=int, default=32)
    trace = commands.add_parser("import-genshin")
    trace.add_argument("--run", type=Path, required=True)
    trace.add_argument("--output", type=Path, required=True)
    export = commands.add_parser("export-genshin")
    export.add_argument("--pack", type=Path, required=True)
    export.add_argument("--policy", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "scenarios":
        from importlib.metadata import entry_points
        print(json.dumps(["genshin.daily", "workspace.ledger", *sorted(ep.name for ep in entry_points(group="startlux.scenarios"))]))
        return
    if args.command == "import-genshin":
        from .scenarios.genshin.import_trace import import_run
        print(json.dumps(import_run(args.run, args.output)))
        return
    if args.command == "export-genshin":
        from .scenarios.genshin.taskpack import TaskPack, build_taskpack
        from .core.contracts import canonical_hash
        import shutil
        import tempfile
        pack = TaskPack.load(args.pack)
        policy = PolicyBundle.model_validate_json(args.policy.read_bytes())
        if (policy.scenario != "genshin.daily" or policy.model != pack.manifest.decision_model
                or policy.base_hash != pack.policy_base_hash):
            raise ValueError("policy does not match this exact Genshin pack and model")
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source"
            shutil.copytree(pack.root, source)
            (source / "instructions.txt").write_text(policy.instructions, encoding="utf-8")
            built = build_taskpack(source, args.output)
        print(json.dumps({"pack": str(built.root), "release_status": "development",
                          "requires_windows_validation": True}))
        return
    config = json.loads(args.config.read_text(encoding="utf-8"))
    # Resolve asset paths relative to the config, not the caller's shell directory.
    for field in ("pack", "suite", "dataset"):
        if field in config:
            config[field] = str((args.config.resolve().parent / config[field]).resolve())
    scenario = load_scenario(args.scenario, config)
    if args.command in {"promote", "rollback"}:
        from .core.releases import PolicyStore
        from .core.scenario import Trial
        from .core.contracts import canonical_hash
        store = PolicyStore(args.output)
        if args.command == "rollback":
            print(json.dumps(store.rollback(scenario.base_policy, expected_active=args.expected_active)))
            return
        run = args.optimization_run
        provenance = json.loads((run / "provenance.json").read_text())
        if provenance["scenario_identity"] != scenario.identity or provenance["provider_mode"] != "startlux":
            raise ValueError("promotion requires the same scenario and real model evidence")
        policy = PolicyBundle.model_validate_json((run / "candidate.json").read_bytes())
        trials = {}
        for kind in ("baseline", "candidate"):
            trials[kind] = [Trial.model_validate_json(
                (run / "validation" / kind / canonical_hash(task_id) / "trial.json").read_bytes())
                for task_id in scenario.tasks("heldout")]
            if [r.task_id for r in trials[kind]] != scenario.tasks("heldout"):
                raise ValueError("validation task identity changed")
        print(json.dumps(store.promote(scenario.base_policy, policy, trials["baseline"], trials["candidate"],
            expected_active=args.expected_active)))
        return
    settings = DecisionSettings.load()
    if args.fixture:
        from .testing import FixtureDecision
        if not scenario.base_policy.model.startswith("fixture-"):
            raise ValueError("fixture mode requires a fixture model")
        if getattr(getattr(scenario, "suite", None), "evidence_origin", "synthetic") != "synthetic":
            raise ValueError("fixture mode cannot evaluate captured game observations")
        provider_factory = FixtureDecision
    else:
        if scenario.base_policy.model.startswith(("fixture-", "configure-")):
            raise ValueError("configure the exact StartLux deployment before real requests")
        if not settings.decision_api_key_file:
            raise ValueError("set STARTLUX_DECISION_KEY_FILE")
        if scenario.id == "genshin.daily" and config.get("top_ranked_deployment_confirmed") is not True:
            raise ValueError("confirm the deployment maps to StartLux Decision v1.1 27B Instant")
        def provider_factory(model):
            return StartLuxDecision(replace(settings, decision_model=model))
    if args.command == "run":
        policy = PolicyBundle.model_validate_json(args.policy.read_bytes()) if args.policy else scenario.base_policy
        args.output.mkdir(parents=True, exist_ok=False)
        trial = scenario.run(args.task, policy, args.output, provider_factory)
        atomic_json(args.output / "trial.json", trial.model_dump())
        print(trial.model_dump_json(indent=2))
        if trial.missing or trial.success is False:
            raise SystemExit(1)
        return
    if sys.version_info < (3, 12) or os.name == "nt":
        raise SystemExit("Harness² optimization requires Python 3.12/3.13 on Linux, macOS or WSL")
    from .optimization.harness2_backend import optimize
    if args.fixture:
        from .testing import FixtureImprover
        proposer = FixtureImprover()
    else:
        from .optimization.improver import ChatImprover
        key_path = os.getenv("STARTLUX_IMPROVER_KEY_FILE", "")
        if not key_path:
            raise ValueError("set STARTLUX_IMPROVER_KEY_FILE")
        proposer = ChatImprover(base_policy=scenario.base_policy,
            model=os.environ["STARTLUX_IMPROVER_MODEL"], url=os.environ["STARTLUX_IMPROVER_URL"],
            api_key=Path(key_path).expanduser().read_text().strip(),
            audit_dir=args.output / "improver", max_calls=args.max_improver_calls)
    try:
        report = optimize(scenario, args.output, provider_factory, proposer, k=args.k, mode=args.mode, fixture=args.fixture)
    finally:
        proposer.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["workflow"]["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
