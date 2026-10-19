"""Protocol and reward regressions; fixtures do not measure game capability."""
from copy import deepcopy
import base64
import hashlib
import json
import threading
import struct

import httpx
import pytest

from startlux.scenarios.genshin.contracts import (AccountScope, ActionOption, ActionResult,
                                          DecisionReceipt, Observation, catalog_hash)
from startlux.scenarios.genshin.daily import (CommissionEvidence, RewardEvidence, VisualEvidence,
                                      verify_daily)
from startlux.scenarios.genshin.decision import StartLuxRealtimeProvider
from startlux.scenarios.genshin.desktop import prepare_daily_pack, report_from_observation, wait_for_game_ready
from startlux.scenarios.genshin.runner import AuditLog, RealtimeRunner
from startlux.scenarios.genshin.taskpack import TaskPack, build_taskpack, export_taskpack, portable_resource_path
from startlux.core.settings import ProviderError, DecisionSettings as Settings


MODEL = "fixture-startlux-pinned"
SCOPE = AccountScope(account_id="fixture-account", server_id="fixture-server", game_day="2026-10-18")
OPTION = ActionOption(id="talk", label="Talk to Katheryne", skill="dialogue",
                      skill_version="1", arguments={"target": "katheryne"},
                      preconditions={"scene": "world", "target": "katheryne", "region": "mondstadt"})


def observation(**updates):
    values = dict(session_id="fixture-session", scope=SCOPE, frame_id=0, control_generation=0,
                  facts={"scene": "world", "target": "katheryne", "region": "mondstadt", "animation": 0})
    values.update(updates)
    return Observation(**values)


class FixtureSession:
    def __init__(self):
        self.current = observation()
        self.options = [OPTION]
        self.starts = []
        self.results = {}
        self.stops = 0
        self.start_status = "succeeded"
        self.lose_response = False
        self.cancels = []

    def observe(self):
        return self.current.model_copy(deep=True)

    def candidates(self, state):
        return [item for item in self.options if item.available(state)]

    def start(self, command):
        if command.command_id in self.results:
            return self.results[command.command_id]
        if not command.applicable(self.current):
            return ActionResult(command_id=command.command_id, status="cancelled", reason="stale")
        self.starts.append(command)
        result = ActionResult(command_id=command.command_id, status=self.start_status)
        self.results[command.command_id] = result
        if self.lose_response:
            raise ConnectionError("fixture response lost after acceptance")
        return result

    def reconcile(self, command_id):
        return self.results.get(command_id, ActionResult(command_id=command_id, status="unknown"))

    def cancel(self, command_id):
        self.cancels.append(command_id)
        self.results[command_id] = ActionResult(command_id=command_id, status="cancelled")
        return self.results[command_id]

    def stop(self):
        self.stops += 1
        self.current = self.current.model_copy(update={"control_generation": self.current.control_generation + 1})

    def close(self):
        pass


class FixtureProvider:
    def __init__(self, during=None, transform=None):
        self.calls = 0
        self.during = during
        self.transform = transform

    def decide(self, state, instructions, options, request_id):
        self.calls += 1
        receipt = DecisionReceipt(request_id=request_id, observation_hash=state.binding(),
                                  catalog_hash=catalog_hash(options), model=MODEL,
                                  outcome="selected", choice_id=options[0].id, elapsed_ms=1.0,
                                  payload_hash="fixture-payload", raw_response={"fixture": True})
        if self.during:
            self.during()
        if self.transform:
            receipt = self.transform(receipt)
        return receipt


def runner(session, provider=None, **kwargs):
    return RealtimeRunner(session, provider or FixtureProvider(), "Fixture instructions",
                          model=MODEL, **kwargs)


def test_animation_frames_may_advance_during_inference():
    session = FixtureSession()

    def advance():
        facts = dict(session.current.facts, animation=99)
        session.current = session.current.model_copy(update={"frame_id": 10, "facts": facts})

    events = []
    result = runner(session, FixtureProvider(during=advance), audit=events.append).tick()
    assert result["status"] == "succeeded"
    assert len(session.starts) == 1
    assert session.starts[0].source_frame_id == 0
    assert events[0]["action_granularity"] == "skill"


@pytest.mark.parametrize("change", ["target", "generation", "account", "session", "arguments", "skill_version"])
def test_changed_action_basis_never_executes(change):
    session = FixtureSession()

    def advance():
        if change == "target":
            session.current = session.current.model_copy(update={"facts": dict(session.current.facts, target="enemy")})
        elif change == "generation":
            session.stop()
        elif change == "account":
            session.current = session.current.model_copy(update={"scope": SCOPE.model_copy(update={"account_id": "other"})})
        elif change == "session":
            session.current = session.current.model_copy(update={"session_id": "other-session"})
        else:
            changes = {"arguments": {"target": "other"}} if change == "arguments" else {"skill_version": "2"}
            session.options = [OPTION.model_copy(update=changes)]

    assert runner(session, FixtureProvider(during=advance)).tick()["status"] == "stale"
    assert session.starts == []


def test_catalog_hash_covers_action_semantics():
    assert catalog_hash([OPTION]) != catalog_hash([OPTION.model_copy(update={"arguments": {"target": "other"}})])


@pytest.mark.parametrize("field,value", [("request_id", "wrong"), ("observation_hash", "wrong"),
                                        ("catalog_hash", "wrong"), ("model", "other-model"),
                                        ("choice_id", "unoffered")])
def test_tampered_receipt_stops_without_action(field, value):
    session = FixtureSession()
    provider = FixtureProvider(transform=lambda receipt: receipt.model_copy(update={field: value}))
    with pytest.raises(ValueError):
        runner(session, provider).tick()
    assert session.starts == []
    assert session.stops == 1


def test_stop_during_blocked_inference_releases_host_and_discards_late_response():
    session = FixtureSession()
    entered, finish = threading.Event(), threading.Event()

    def block():
        entered.set()
        assert finish.wait(2)

    controller = runner(session, FixtureProvider(during=block))
    results = []
    worker = threading.Thread(target=lambda: results.append(controller.tick()))
    worker.start()
    try:
        assert entered.wait(2)
        controller.stop()
        assert session.stops == 1
    finally:
        finish.set()
        worker.join(2)
    assert not worker.is_alive()
    assert results == [{"status": "stopped"}]
    assert session.starts == []


def test_expired_response_is_discarded_using_local_monotonic_time():
    session = FixtureSession()
    now = [0.0]
    provider = FixtureProvider(during=lambda: now.__setitem__(0, 4.0))
    assert runner(session, provider, clock=lambda: now[0]).tick()["status"] == "stale"
    assert session.starts == []


def test_ambiguous_transport_is_reconciled_without_resubmitting():
    session = FixtureSession()
    session.lose_response = True
    provider = FixtureProvider()
    controller = runner(session, provider)
    assert controller.tick()["status"] == "reconciling"
    assert controller.tick()["status"] == "succeeded"
    assert len(session.starts) == 1
    assert provider.calls == 1


def test_unknown_command_blocks_further_decisions():
    session = FixtureSession()
    session.start_status = "running"
    provider = FixtureProvider()
    controller = runner(session, provider)
    first = controller.tick()
    session.results.clear()
    assert controller.tick()["status"] == "unknown"
    assert controller.tick()["status"] == "stopped"
    assert provider.calls == 1
    assert len(session.starts) == 1


def test_running_skill_has_no_parallel_decision_and_timeout_cancels():
    session = FixtureSession()
    session.start_status = "running"
    provider = FixtureProvider()
    now = [0.0]
    controller = runner(session, provider, clock=lambda: now[0])
    first = controller.tick()
    assert controller.tick()["status"] == "running"
    now[0] = 31.0
    assert controller.tick()["status"] == "timed_out"
    assert session.cancels == [first["command_id"]]
    assert session.stops == 1
    assert provider.calls == 1


def test_account_switch_between_actions_stops_episode():
    session = FixtureSession()
    controller = runner(session)
    assert controller.tick()["status"] == "succeeded"
    session.current = session.current.model_copy(update={"scope": SCOPE.model_copy(update={"game_day": "2026-10-19"})})
    assert controller.tick()["status"] == "scope_changed"
    assert len(session.starts) == 1


def provider_settings():
    return Settings("https://fixture.invalid/v1/decisions", MODEL, 3.0)


def decision_response(**answer_updates):
    answer = {"choice": "talk", "probabilities": {"talk": 1.0}, "confidence": 1.0,
              "none_probability": 0.01, "details": {"thought": {"used": False, "tokens": 0}}}
    answer.update(answer_updates)
    return {"model": MODEL, "answers": {"primary_action": answer},
            "usage": {"input_tokens": 120, "output_tokens": 0}}


@pytest.mark.parametrize("updates,outcome", [({}, "selected"), ({"choice": None}, "abstained"),
                                            ({"none_probability": 0.9}, "abstained")])
def test_startlux_transport_supports_null_and_none_warning(updates, outcome):
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=decision_response(**updates))

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        provider = StartLuxRealtimeProvider(provider_settings(), client)
        receipt = provider.decide(observation(), "Fixture", [OPTION], "request-1")
    assert receipt.outcome == outcome
    assert requests[0]["allow_none"] is True
    assert requests[0]["think_mode"] == "never"
    assert json.loads(requests[0]["state"])["actions"][0]["arguments"] == OPTION.arguments


@pytest.mark.parametrize("response", [decision_response(choice="unknown"),
                                      decision_response(probabilities={"talk": 0.2}),
                                      decision_response(confidence=True),
                                      decision_response(details={"thought": {"used": True, "tokens": 2}}),
                                      {**decision_response(), "model": "wrong-model"}])
def test_invalid_startlux_response_never_selects_action(response):
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response))) as client:
        provider = StartLuxRealtimeProvider(provider_settings(), client)
        with pytest.raises(ProviderError):
            provider.decide(observation(), "Fixture", [OPTION], "request-1")


def test_image_markers_and_local_image_audit(tmp_path):
    raw = b"\x89PNG\r\n\x1a\nfixture-pixels"
    image = "data:image/png;base64," + base64.b64encode(raw).decode()
    session = FixtureSession()
    session.current = observation(images=[image])
    path = tmp_path / "trace.jsonl"
    audit = AuditLog(path)
    try:
        runner(session, audit=audit).tick()
    finally:
        audit.close()
    events = [json.loads(line) for line in path.read_text().splitlines()]
    reference = events[0]["observation"]["images"][0]
    assert (tmp_path / reference["path"]).read_bytes() == raw
    assert reference["sha256"] == hashlib.sha256(raw).hexdigest()
    assert image not in path.read_text()
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=decision_response())

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        StartLuxRealtimeProvider(provider_settings(), client).decide(session.current, "Fixture", [OPTION], "image-1")
    assert requests[0]["state"].count("<image>") == len(requests[0]["images"]) == 1


def evidence(source, frame=1, **updates):
    values = dict(scope=SCOPE, frame_id=frame, screenshot_sha256="a" * 64, source=source)
    values.update(updates)
    return VisualEvidence(**values)


def test_encounter_credit_does_not_prove_commissions_or_primogems():
    report = verify_daily(SCOPE, ["q1", "q2", "q3", "q4"], [], None, daily_credit_progress=4)
    assert report.status == "unverified"
    assert report.commission_completed_count == 0
    assert report.daily_reward_claimed is None
    assert report.observed_primogems_delta is None


def test_desktop_daily_catalog_requires_each_scan_and_four_completed_commissions(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "agent-main.js").write_text("// development fixture")
    pack = prepare_daily_pack(source, tmp_path / "pack", "explicit-startlux-deployment")
    facts = {"region": "Mondstadt", "account_verified": True, "foreground": True,
             "daily_needs_scan": True, "daily_all_complete": False, "daily_reward_claimed": False}
    assert [item.id for item in pack.candidates(observation(facts=facts))] == ["scan"]
    facts.update(daily_needs_scan=False, daily_slot_2_pending=True)
    assert [item.id for item in pack.candidates(observation(facts=facts))] == ["quest-2"]
    facts.update(daily_slot_2_pending=False, daily_all_complete=True)
    assert [item.id for item in pack.candidates(observation(facts=facts))] == ["claim"]
    facts["daily_reward_claimed"] = True
    assert pack.candidates(observation(facts=facts)) == []
    with pytest.raises(ValueError, match="deployment"):
        prepare_daily_pack(source, tmp_path / "bad", "fixture-model")


def test_vendored_commission_routes_and_recognition_assets_match_pinned_source():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2] / "integrations/auto_commission"
    source = json.loads((root / "source.json").read_text(encoding="utf-8"))
    files = source["files"]
    assert "assets/邪恶的扩张/望风山地-1.json" in files
    assert "Data/RecognitionObject/Completed.png" in files
    for name, expected in files.items():
        assert hashlib.sha256((root / "upstream" / name).read_bytes()).hexdigest() == expected


def test_desktop_waits_for_world_and_never_arms_a_different_account(monkeypatch):
    session = FixtureSession()
    ready = observation(facts={"account_verified": True, "foreground": True, "scene": "world"})
    states = iter([observation(facts={"scene": "loading"}), ready])
    session.observe = lambda: next(states)
    monkeypatch.setattr("startlux.scenarios.genshin.desktop.time.sleep", lambda seconds: None)
    wait_for_game_ready(session, SCOPE.account_id)
    assert session.starts == []
    session.observe = lambda: ready
    with pytest.raises(ValueError, match="UID differs"):
        wait_for_game_ready(session, "another-account")


def test_desktop_report_requires_journal_screenshots_and_independent_reward():
    image = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\nfixture").decode()
    sha = hashlib.sha256(base64.b64decode(image.split(",")[1])).hexdigest()
    ids = [f"{i}:fixture-quest" for i in range(1, 5)]
    items = [CommissionEvidence(quest_id=quest, completed=True,
             evidence=evidence("commission_journal", screenshot_sha256=sha)).model_dump(mode="json") for quest in ids]
    data = {"expected_quest_ids": ids, "commissions": items, "reward": None}
    state = observation(facts={"daily_evidence": data}, images=[image])
    assert report_from_observation(state)["status"] == "commissions_complete"
    data["reward"] = RewardEvidence(claimed=True, evidence=evidence("daily_reward_ui", screenshot_sha256=sha)).model_dump(mode="json")
    report = report_from_observation(observation(facts={"daily_evidence": data}, images=[image]))
    assert report["status"] == "complete"
    assert report["observed_primogems_delta"] is None
    with pytest.raises(ValueError, match="missing its screenshot"):
        report_from_observation(observation(facts={"daily_evidence": data}))
    data["commissions"][0]["completed"] = False
    assert report_from_observation(observation(facts={"daily_evidence": data}, images=[image]))["status"] == "incomplete"


def test_commission_and_reward_verification_have_independent_evidence():
    items = [CommissionEvidence(quest_id=f"q{i}", completed=True, evidence=evidence("commission_journal"))
             for i in range(4)]
    ids = [item.quest_id for item in items]
    assert verify_daily(SCOPE, ids, items, None).status == "commissions_complete"
    reward = RewardEvidence(claimed=True, evidence=evidence("daily_reward_ui"))
    report = verify_daily(SCOPE, ids, items, reward)
    assert report.status == "complete"
    assert report.observed_primogems_delta is None
    popup = RewardEvidence(claimed=True, primogems=20, evidence=evidence("reward_popup", frame=2))
    assert verify_daily(SCOPE, ids, items, popup).observed_primogems_delta == 20


def test_other_account_day_evidence_and_unseen_primogems_are_rejected():
    wrong = SCOPE.model_copy(update={"game_day": "2026-10-19"})
    item = CommissionEvidence(quest_id="q1", completed=True,
                              evidence=evidence("commission_journal", scope=wrong))
    with pytest.raises(ValueError, match="account/day"):
        verify_daily(SCOPE, ["q1", "q2", "q3", "q4"], [item], None)
    reward = RewardEvidence(claimed=True, primogems=60, evidence=evidence("daily_reward_ui"))
    with pytest.raises(ValueError, match="popup"):
        verify_daily(SCOPE, ["q1", "q2", "q3", "q4"], [], reward)


def test_partial_quest_list_cannot_be_reported_as_complete_day():
    with pytest.raises(ValueError, match="four"):
        verify_daily(SCOPE, ["q1"], [], None)


def make_pack(root):
    root.mkdir()
    resources = {"actions.json": json.dumps([OPTION.model_dump()]).encode(),
                 "instructions.txt": b"Fixture instructions", "routes/example.json": b"{}"}
    for name, raw in resources.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    manifest = {"schema_version": 2, "id": "fixture-daily", "version": "0.1.0", "game_id": "genshin",
                "environment": "fixture", "release_status": "development", "decision_model": MODEL,
                "regions": ["mondstadt"], "files": {name: hashlib.sha256(raw).hexdigest()
                                                     for name, raw in resources.items()},
                "attributions": ["Synthetic fixture, no live game routes"]}
    (root / "manifest.json").write_text(json.dumps(manifest))
    return manifest


def test_v2_taskpack_exports_resources_and_filters_by_guards(tmp_path):
    source = tmp_path / "source"
    make_pack(source)
    pack = export_taskpack(source, tmp_path / "export")
    assert pack.manifest.schema_version == 2
    assert (pack.root / "routes/example.json").read_bytes() == b"{}"
    assert pack.candidates(observation()) == [OPTION]
    assert pack.candidates(observation(facts={"region": "inazuma"})) == []
    with pytest.raises(FileExistsError):
        export_taskpack(source, tmp_path / "export")


@pytest.mark.parametrize("kind", ["changed", "unlisted", "symlink", "traversal", "case_collision"])
def test_corrupt_or_unsafe_packs_are_rejected(tmp_path, kind):
    root = tmp_path / "pack"
    manifest = make_pack(root)
    if kind == "changed":
        (root / "instructions.txt").write_text("Changed after hashing")
    elif kind == "unlisted":
        (root / "extra.txt").write_text("Unlisted")
    elif kind == "symlink":
        (root / "link").symlink_to(tmp_path)
    else:
        name = "../outside.txt" if kind == "traversal" else "ACTIONS.json"
        manifest["files"][name] = "a" * 64
        (root / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        TaskPack.load(root)


@pytest.mark.parametrize("name", ["../x", "/x", "C:/x", "a\\b", "a//b", "NUL.json", "a/COM1.txt", "a.", "a ", "a?b"])
def test_windows_unsafe_resource_paths_are_rejected(name):
    with pytest.raises(ValueError):
        portable_resource_path(name)


def test_authoring_build_rehashes_policy_then_runtime_detects_tampering(tmp_path):
    source = tmp_path / "source"
    make_pack(source)
    (source / "instructions.txt").write_text("Revised policy")
    with pytest.raises(ValueError, match="hash"):
        TaskPack.load(source)
    pack = build_taskpack(source, tmp_path / "built")
    assert pack.instructions == "Revised policy"
    (pack.root / "instructions.txt").write_text("Tampered after build")
    with pytest.raises(ValueError, match="hash"):
        TaskPack.load(pack.root)


def test_external_takeover_between_actions_requires_new_controller():
    session = FixtureSession()
    provider = FixtureProvider()
    controller = runner(session, provider)
    controller.tick()
    session.stop()
    assert controller.tick()["status"] == "control_changed"
    assert len(session.starts) == provider.calls == 1


def test_external_takeover_cancels_active_control_without_another_decision():
    session = FixtureSession()
    session.start_status = "running"
    provider = FixtureProvider()
    controller = runner(session, provider)
    controller.tick()
    session.stop()
    assert controller.tick()["status"] == "control_changed"
    assert provider.calls == 1


def test_snapshot_grading_hides_labels_and_checks_skill_semantics(tmp_path):
    from startlux.scenarios.genshin.snapshot import ExpectedAction, SnapshotCase, evaluate_snapshot_case

    make_pack(tmp_path / "pack")
    pack = TaskPack.load(tmp_path / "pack")
    expected = ExpectedAction(id="talk", skill="dialogue", skill_version="1", arguments=OPTION.arguments)
    case = SnapshotCase(id="private-label", split="evolve", observation=observation(), acceptable_actions=[expected])
    seen = []

    class CapturingProvider(FixtureProvider):
        def decide(self, state, instructions, options, request_id):
            seen.append(json.dumps({"state": state.model_dump(), "instructions": instructions,
                                    "options": [option.model_dump() for option in options]}))
            return super().decide(state, instructions, options, request_id)

    record = evaluate_snapshot_case(pack, case, CapturingProvider(), "snapshot-request")
    assert record["reward"] == 1.0
    assert record["evaluation_mode"] == "snapshot"
    assert "private-label" not in seen[0]
    assert "acceptable_actions" not in seen[0]
    # Reusing a correct action ID for a different side effect cannot pass.
    pack.options = [OPTION.model_copy(update={"arguments": {"target": "other"}})]
    assert evaluate_snapshot_case(pack, case, FixtureProvider(), "snapshot-2")["reward"] == 0.0



def test_pipe_requests_bind_responses_and_use_an_independent_stop_channel():
    from startlux.scenarios.genshin.windows_pipe import WindowsPipeSession

    connections = []

    class Pipe:
        def __init__(self, name, timeout):
            connections.append((name, timeout))

        def write_all(self, raw):
            count = struct.unpack("<I", raw[:4])[0]
            request = json.loads(raw[4:])
            assert count == len(raw) - 4
            data = observation().model_dump() if request["operation"] == "observe" else {}
            body = json.dumps({"version": 1, "request_id": request["request_id"],
                               "success": True, "data": data}).encode()
            self.buffer = struct.pack("<I", len(body)) + body

        def read_exact(self, count):
            result, self.buffer = self.buffer[:count], self.buffer[count:]
            return result

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    session = WindowsPipeSession(connector=Pipe)
    assert session.observe() == observation()
    session.stop()
    assert connections == [("startlux-genshin", 5.0), ("startlux-genshin.control", 0.2)]


@pytest.mark.parametrize("defect", ["wrong_request", "wrong_version", "oversized", "rejected"])
def test_invalid_pipe_responses_are_not_accepted(defect):
    from startlux.scenarios.genshin.windows_pipe import MAX_PAYLOAD_BYTES, WindowsPipeSession

    class Pipe:
        def __init__(self, name, timeout):
            pass

        def write_all(self, raw):
            request = json.loads(raw[4:])
            response = {"version": 1, "request_id": request["request_id"], "success": True, "data": {}}
            if defect == "wrong_request":
                response["request_id"] = "unrelated"
            elif defect == "wrong_version":
                response["version"] = True
            elif defect == "rejected":
                response["success"] = False
            body = json.dumps(response).encode()
            length = MAX_PAYLOAD_BYTES + 1 if defect == "oversized" else len(body)
            self.buffer = struct.pack("<I", length) + body

        def read_exact(self, count):
            result, self.buffer = self.buffer[:count], self.buffer[count:]
            return result

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    with pytest.raises((ValueError, RuntimeError)):
        WindowsPipeSession(connector=Pipe).describe()


def test_python_runner_controls_the_dotnet_fixture_host_over_real_ipc():
    import os
    import queue
    import socket
    import subprocess
    import sys
    import uuid
    from startlux.scenarios.genshin.windows_pipe import WindowsPipeSession

    dll = os.environ.get("GENSHIN_BRIDGE_FIXTURE_DLL")
    if not dll:
        pytest.skip("set GENSHIN_BRIDGE_FIXTURE_DLL to run cross-language IPC integration")
    name = "g" + uuid.uuid4().hex[:12]
    process = subprocess.Popen([os.environ.get("GENSHIN_DOTNET", "dotnet"), dll, name],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    ready = queue.Queue()
    reader = threading.Thread(target=lambda: ready.put(process.stdout.readline()), daemon=True)
    reader.start()
    try:
        metadata = json.loads(ready.get(timeout=10))

        class UnixPipe:
            def __init__(self, pipe_name, timeout):
                self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.socket.settimeout(timeout)
                path = metadata["unix_pipe_path"] + (".control" if pipe_name.endswith(".control") else "")
                self.socket.connect(path)

            def write_all(self, raw):
                self.socket.sendall(raw)

            def read_exact(self, count):
                raw = b""
                while len(raw) < count:
                    part = self.socket.recv(count - len(raw))
                    if not part:
                        raise ConnectionError("fixture pipe closed")
                    raw += part
                return raw

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.socket.close()

        session = WindowsPipeSession(name, connector=None if sys.platform == "win32" else UnixPipe)
        session.arm()
        provider = FixtureProvider()
        controller = runner(session, provider)
        first = controller.tick()
        assert first["status"] == "running"
        final = controller.tick()
        import time
        deadline = time.monotonic() + 3
        while final["status"] in {"running", "reconciling"} and time.monotonic() < deadline:
            threading.Event().wait(0.01)
            final = controller.tick()
        assert final["status"] == "succeeded"
        assert final["evidence"]["synthetic_fixture"] is True
        assert final["evidence"]["executions"] == 1
        assert controller.tick()["status"] == "no_candidates"
        session.stop()
        assert session.candidates(session.observe()) == []
    finally:
        if process.poll() is None:
            process.stdin.write("stop\n")
            process.stdin.flush()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        reader.join(timeout=1)
