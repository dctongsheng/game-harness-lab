"""Lean bundled Windows runtime; no RRSI/GEPA imports or teacher credentials."""
from __future__ import annotations

import argparse
import base64
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
import threading
import time
import uuid

from ..providers import Settings
from .contracts import AccountScope
from .daily import CommissionEvidence, RewardEvidence, verify_daily
from .decision import StartLuxRealtimeProvider
from .runner import AuditLog, RealtimeRunner
from .taskpack import TaskPack
from .windows_pipe import WindowsPipeSession


def prepare_daily_pack(source: Path, destination: Path, model: str) -> TaskPack:
    if not model.strip() or model.startswith(("fixture-", "configure-")):
        raise ValueError("an explicitly mapped top-ranked StartLux deployment is required")
    destination.mkdir(parents=True, exist_ok=False)
    files = {}
    for item in sorted(source.rglob("*")):
        if not item.is_file():
            continue
        name = item.relative_to(source).as_posix()
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        raw = item.read_bytes()
        target.write_bytes(raw)
        files[name] = hashlib.sha256(raw).hexdigest()
    common = {"region": "Mondstadt", "account_verified": True, "foreground": True}
    actions = [{"id": "scan", "label": "打开冒险之证并逐项识别四个真实每日委托及完成标记",
                "skill": "daily.scan", "skill_version": "1", "arguments": {},
                "preconditions": {**common, "daily_needs_scan": True}, "timeout_ms": 180000, "ttl_ms": 15000}]
    for slot in range(1, 5):
        actions.append({"id": f"quest-{slot}", "label": f"执行已经识别并支持的第 {slot} 项蒙德每日委托，结束后重新读取委托页",
                        "skill": "daily.execute", "skill_version": "1", "arguments": {"slot": slot},
                        "preconditions": {**common, f"daily_slot_{slot}_pending": True},
                        "timeout_ms": 300000, "ttl_ms": 15000})
    actions.append({"id": "claim", "label": "四项真实委托均已完成，前往蒙德凯瑟琳只领取每日委托奖励并检查冒险之证",
                    "skill": "daily.claim", "skill_version": "1", "arguments": {},
                    "preconditions": {**common, "daily_needs_scan": False, "daily_all_complete": True,
                                      "daily_reward_claimed": False}, "timeout_ms": 180000, "ttl_ms": 15000})
    content = {"actions.json": json.dumps(actions, ensure_ascii=False, indent=2).encode(),
               "instructions.txt": ("用户要求完成今天四个真实每日委托并领取凯瑟琳奖励。只选择可用目录内的动作。"
                   "每次执行委托后必须先扫描冒险之证；脚本返回不等于委托完成，历练点4/4不等于真实委托完成。"
                   "观察 daily_evidence 中的任务名称、状态和截图。无法判断时弃权。禁止花原石、体力或购买物品。"
                   "模型固定为经配置映射的排行榜第一 StartLux Decision v1.1 27B Instant；不得替换为其他模型。").encode()}
    for name, raw in content.items():
        (destination / name).write_bytes(raw)
        files[name] = hashlib.sha256(raw).hexdigest()
    manifest = {"schema_version": 2, "id": "startlux.mondstadt.daily", "version": "0.2.0", "game_id": "genshin",
                "environment": "windows", "release_status": "development", "decision_model": model,
                "regions": ["Mondstadt"], "files": files,
                "attributions": ["AutoCommission LX666-666 and contributors, GPL-3.0, f9b5a49942b613c545e9bea3bed4d015424844d1",
                                 "BetterGI babalae and contributors, GPL-3.0; StartLux daily adapter"]}
    (destination / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return TaskPack.load(destination)


def report_from_observation(observation):
    """Require archived PNGs for every accepted piece of journal evidence."""
    data = observation.facts.get("daily_evidence", {})
    ids = data.get("expected_quest_ids", [])
    if len(ids) != 4:
        return {"status": "unverified", "commission_completed_count": 0, "daily_reward_claimed": None}
    images = {hashlib.sha256(base64.b64decode(image.split(",", 1)[1], validate=True)).hexdigest()
              for image in observation.images}
    commissions = [CommissionEvidence.model_validate(row) for row in data.get("commissions", [])]
    reward = RewardEvidence.model_validate(data["reward"]) if data.get("reward") else None
    for evidence in [*(row.evidence for row in commissions), *([reward.evidence] if reward else [])]:
        if evidence.screenshot_sha256 not in images:
            raise ValueError("journal evidence is missing its screenshot")
    return verify_daily(observation.scope, ids, commissions, reward).model_dump(mode="json")


def emit(event):
    print(json.dumps(event, ensure_ascii=False), flush=True)


def wait_for_game_ready(session, uid, *, seconds=180):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            state = session.observe()
            if state.scope.account_id != uid:
                raise ValueError("game UID differs from the requested account")
            if (state.facts.get("account_verified") is True and state.facts.get("foreground") is True
                    and state.facts.get("scene") == "world"):
                return
        except (OSError, TimeoutError, ConnectionError, RuntimeError):
            pass  # Read-only startup/capture failures can be retried before arming.
        time.sleep(1)
    raise TimeoutError("game did not reach the verified account's world view")


class ExecutionHeartbeat:
    """Keep the native lease alive independently of OCR and screenshot transport."""
    def __init__(self, controller):
        self.controller = controller
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        while not self.stopped.wait(0.5):
            active = self.controller.active
            if active is not None and not self.controller.stopped.is_set():
                try:
                    self.controller.session.reconcile(active.command_id)
                except (ConnectionError, TimeoutError, OSError):
                    pass  # Native watchdog releases inputs if this connection is lost.

    def close(self):
        self.stopped.set()
        self.thread.join(timeout=1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["run", "stop", "self-test"])
    parser.add_argument("--config", type=Path)
    parser.add_argument("--pipe", default="startlux-genshin")
    args = parser.parse_args()
    if args.operation == "self-test":
        # Imports and DTO validation in the actual frozen executable.
        AccountScope(account_id="123456789", server_id="cn", game_day="2026-01-01")
        emit({"status": "runtime_ready", "platform": sys.platform})
        return
    if args.operation == "stop":
        for attempt in range(3):
            try:
                WindowsPipeSession(args.pipe).stop()
                break
            except (OSError, TimeoutError, ConnectionError):
                if attempt == 2:
                    raise
                time.sleep(0.05)
        emit({"status": "stop_sent"})
        return
    if args.config is None:
        raise ValueError("desktop configuration is required")
    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    if config.get("top_ranked_deployment_confirmed") is not True:
        raise ValueError("confirm the deployment mapping to StartLux Decision v1.1 27B Instant")
    # Credential comes through an anonymous inherited stdin pipe; never CLI/env/file.
    key = sys.stdin.readline().strip()
    if not key or any(char.isspace() for char in key):
        raise ValueError("invalid Decision credential")
    home = args.config.resolve().parent
    run = home / "runs" / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
    run.mkdir(parents=True)
    assets = Path(sys.executable).parent.parent / "task-source" if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[3] / "integrations" / "desktop" / "task-source"
    pack = prepare_daily_pack(assets, run / "pack", config["model_id"])
    host_config = {"binding": {"account_id": config["uid"], "server_id": config["server_id"],
                    "server_utc_offset_minutes": config["server_utc_offset_minutes"], "region": "Mondstadt"},
                   "pack_directory": str(pack.root)}
    host_path = home / "bridge.json"
    host_path.write_text(json.dumps(host_config), encoding="utf-8")
    emit({"status": "pack_ready", "bridge_config": str(host_path), "pipe": args.pipe})
    # The UI starts BetterGI only after pack/config exist and acknowledges via stdin.
    if sys.stdin.readline().strip() != "host_started":
        raise ValueError("execution host was not acknowledged")
    session = WindowsPipeSession(args.pipe)
    descriptor = None
    connect_deadline = time.monotonic() + 90
    while time.monotonic() < connect_deadline:
        try:
            descriptor = session.describe()
            break
        except (OSError, TimeoutError, ConnectionError):
            time.sleep(0.5)
    if (descriptor is None or descriptor.get("decision_model") != pack.manifest.decision_model
            or descriptor.get("manifest_file_sha256") != hashlib.sha256((pack.root / "manifest.json").read_bytes()).hexdigest()):
        raise ValueError("execution host task pack/model mismatch")
    settings = replace(Settings.load(), decision_api_key_file="", decision_model=config["model_id"],
                       decision_url="https://api.bamboonode.dev/v1/decisions")
    provider = StartLuxRealtimeProvider(settings)
    provider._headers["Authorization"] = "Bearer " + key
    key = ""
    audit = AuditLog(run / "trace.jsonl")
    controller = RealtimeRunner(session, provider, pack.instructions, model=pack.manifest.decision_model, audit=audit)
    heartbeat = ExecutionHeartbeat(controller)
    heartbeat.thread.start()
    termination = "error"
    try:
        emit({"status": "waiting_for_game"})
        wait_for_game_ready(session, config["uid"])
        session.arm()
        emit({"status": "running", "trace": str(run / "trace.jsonl")})
        deadline = time.monotonic() + 3600
        decisions = 0
        while time.monotonic() < deadline and decisions < 100:
            deciding = controller.active is None
            result = controller.tick()
            decisions += int(deciding)
            emit({"status": "progress", "execution": result})
            if result["status"] not in {"accepted", "running", "succeeded", "reconciling", "stale"}:
                termination = result["status"]
                break
            time.sleep(0.2)
        else:
            termination = "budget_exhausted"
        observation = session.observe()
        audit({"type": "final_observation", "observation": audit.archive_observation(observation)})
        report = report_from_observation(observation)
        report.update(termination_reason=termination, trace=str(run / "trace.jsonl"),
                      evidence_origin="windows_live", model=config["model_id"])
        (run / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        audit({"type": "daily_report", "report": report})
        emit({"status": "finished", "report": report})
    except Exception as error:
        termination = "error"
        report = {"status": "unverified", "termination_reason": termination,
                  "error_type": type(error).__name__, "trace": str(run / "trace.jsonl"),
                  "evidence_origin": "windows_live", "model": config["model_id"]}
        (run / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        audit({"type": "daily_report", "report": report})
        raise
    finally:
        try:
            controller.stop()
        finally:
            heartbeat.close()
            audit({"type": "run_ended", "termination_reason": termination})
            provider.close()
            audit.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Never surface credential-bearing HTTP or native exception strings.
        emit({"status": "error", "error_type": type(error).__name__})
        raise SystemExit(1)
