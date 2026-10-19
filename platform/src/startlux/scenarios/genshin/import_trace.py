"""Convert desktop evidence into unlabelled replay tasks with verified PNGs."""
import base64
import hashlib
import json
from pathlib import Path

from ...core.contracts import canonical_hash, catalog_hash
from ...core.storage import atomic_json
from .contracts import Observation
from .snapshot import SnapshotCase, SnapshotSuite
from .taskpack import TaskPack


def import_run(run: Path, output: Path):
    run = run.resolve()
    if output.exists():
        raise FileExistsError("replay dataset already exists")
    pack = TaskPack.load(run / "pack")
    if pack.manifest.environment != "windows":
        raise ValueError("only Windows capture runs can be imported")
    cases, seen = [], set()
    with (run / "trace.jsonl").open(encoding="utf-8") as source:
        for line in source:
            record = json.loads(line)
            if record.get("type") != "decision_requested":
                continue
            if record.get("requested_model") != pack.manifest.decision_model:
                raise ValueError("trace model does not match the frozen pack")
            value = record["observation"]
            images = []
            for image in value["images"]:
                identity = image["sha256"]
                if image["path"] != "images/" + identity + ".png" or len(identity) != 64:
                    raise ValueError("invalid archived screenshot path")
                path = run / image["path"]
                if path.is_symlink() or not path.resolve().is_relative_to(run):
                    raise ValueError("screenshot escapes the run")
                raw = path.read_bytes()
                if hashlib.sha256(raw).hexdigest() != identity or not raw.startswith(b"\x89PNG\r\n\x1a\n"):
                    raise ValueError("archived screenshot hash mismatch")
                uri = "data:image/png;base64," + base64.b64encode(raw).decode()
                if hashlib.sha256(uri.encode()).hexdigest() != image["data_uri_sha256"]:
                    raise ValueError("screenshot URI binding mismatch")
                images.append(uri)
            state = Observation.model_validate({**value, "images": images})
            if state.binding() != record["observation_hash"]:
                raise ValueError("observation binding mismatch")
            options = pack.candidates(state)
            if catalog_hash(options) != record["catalog_hash"]:
                raise ValueError("trace action catalog differs from the frozen pack")
            # Keep identical facts/images together; frame counters do not make new tasks.
            group = canonical_hash({"facts": state.facts, "images": images, "scope": state.scope.model_dump()})
            if group in seen:
                continue
            seen.add(group)
            cases.append(SnapshotCase(id="capture-" + group[:24], split="evolve", observation=state,
                                      acceptable_actions=[], label_available=False))
    if len(cases) < 2:
        raise ValueError("need at least two distinct captured observations")
    boundary = max(1, len(cases) * 4 // 5)
    cases = [case.model_copy(update={"split": "evolve" if i < boundary else "heldout"})
             for i, case in enumerate(cases)]
    suite = SnapshotSuite(evidence_origin="captured_game", cases=cases)
    atomic_json(output, suite.model_dump())
    return {"cases": len(cases), "labels": "unlabelled", "evidence_origin": "captured_game",
            "eligible_for_client_release": False}
