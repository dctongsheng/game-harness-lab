"""Durable, cursor-based native public protocol, separate from model observations.

Only a live PokemonSession's channel-0 messages are accepted here. Historical
``recent_events`` observations cannot recover an animation stream: they omit the
initial protocol and can truncate a whole turn, so missing sidecars stay missing.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import re
import threading

from .store import atomic_json, utcnow


SCHEMA = "pokemon-public-spectator-v1"
_PRIVATE_TAGS = {"request", "split", "seed", "team", "showteam", "sentchoice",
                 "choice", "choices", "debug", "inputlog", "sideupdate", "end"}
_PROGRESS = ("step", "native_turn", "native_terminal", "truncated", "result", "done")
_STATUSES = {"waiting", "live", "complete", "stopped", "truncated", "interrupted", "error"}


def _identity(episode_id):
    if not isinstance(episode_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", episode_id):
        raise ValueError("invalid spectator episode id")
    return episode_id


def _cursor(value):
    if type(value) is not int or value < 0:
        raise ValueError("spectator cursor must be a nonnegative integer")
    return value


def _lines(value):
    if not isinstance(value, list) or len(value) > 100000:
        raise ValueError("invalid spectator protocol lines")
    for line in value:
        if (not isinstance(line, str) or not line.startswith("|") or len(line) > 16384
                or any(character in line for character in ("\n", "\r", "\0"))):
            raise ValueError("invalid spectator protocol line")
        if line.split("|", 2)[1] in _PRIVATE_TAGS:
            raise ValueError("private protocol is forbidden in spectator feed")
    return deepcopy(value)


def _progress(value):
    result = {key: deepcopy(value[key]) for key in _PROGRESS if key in value}
    for key in ("step", "native_turn"):
        if type(result.get(key)) is not int or result[key] < 0:
            raise ValueError("invalid spectator progress")
    for key in ("native_terminal", "truncated", "done"):
        if type(result.get(key)) is not bool:
            raise ValueError("invalid spectator terminal metadata")
    if (not isinstance(result.get("result"), str)
            or result["result"] not in {"ongoing", "win", "loss", "draw", "unresolved"}):
        raise ValueError("invalid spectator result")
    return result


def _metadata(purpose, harness_id):
    if purpose is not None and (not isinstance(purpose, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", purpose)):
        raise ValueError("invalid spectator purpose")
    if harness_id is not None:
        _identity(harness_id)


def _initial_protocol(lines):
    tags = {line.split("|", 2)[1] for line in lines}
    return ({"gen", "start"}.issubset(tags)
            and any(line.startswith("|player|p1|") for line in lines)
            and any(line.startswith("|player|p2|") for line in lines))


class SpectatorStore:
    """Atomically persist the full public stream; readers request any suffix.

    Cursors count native lines, including repeated identical messages. A Decision
    submission with no resolved native events has an unchanged cursor. Each sidecar
    is an independently readable snapshot, so polling gaps and process restarts do
    not remove animation events.
    """

    def __init__(self, root: Path):
        self.root = Path(root) / "spectators"
        self.lock = threading.RLock()

    def _path(self, episode_id):
        return self.root / (_identity(episode_id) + ".json")

    def begin(self, episode_id, *, purpose=None, harness_id=None):
        path = self._path(episode_id)
        _metadata(purpose, harness_id)
        with self.lock:
            if path.exists():
                raise ValueError("spectator episode already exists")
            atomic_json(path, {"schema": SCHEMA, "episode_id": episode_id, "game_id": "pokemon",
                               "source": "native_public_channel_0", "complete_from_start": False,
                               "lines": [], "cursor": 0, "status": "waiting",
                               "finished": False, "created_at": utcnow(), "updated_at": utcnow(),
                               "purpose": purpose, "harness_id": harness_id})

    def exists(self, episode_id):
        return self._path(episode_id).is_file()

    def episodes(self):
        with self.lock:
            result = []
            for path in self.root.glob("*.json"):
                try:
                    value = self._load(path.stem)
                except (OSError, ValueError, TypeError):
                    # A damaged file must not break watching unrelated games.
                    continue
                entry = {key: deepcopy(value[key]) for key in (
                    "episode_id", "purpose", "harness_id", "created_at", "finished",
                    "status", "native_turn", "cursor", "complete_from_start") if key in value}
                entry["available"] = value["complete_from_start"]
                result.append(entry)
            return sorted(result, key=lambda item: (item["created_at"], item["episode_id"]), reverse=True)

    def recover(self):
        """A new Lab has no surviving live runner; retain logs, close lifecycle."""
        for episode in self.episodes():
            if not episode["finished"]:
                self.finish(episode["episode_id"])

    def _load(self, episode_id):
        path = self._path(episode_id)
        if not path.is_file():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(value, dict) or value.get("schema") != SCHEMA
                or value.get("episode_id") != episode_id or value.get("game_id") != "pokemon"
                or value.get("source") != "native_public_channel_0"):
            raise ValueError("invalid spectator sidecar")
        lines = _lines(value.get("lines"))
        if _cursor(value.get("cursor")) != len(lines):
            raise ValueError("spectator cursor does not match stored lines")
        if (type(value.get("finished")) is not bool or type(value.get("complete_from_start")) is not bool
                or not isinstance(value.get("status"), str) or value["status"] not in _STATUSES):
            raise ValueError("invalid spectator lifecycle metadata")
        _metadata(value.get("purpose"), value.get("harness_id"))
        for key in ("created_at", "updated_at"):
            if not isinstance(value.get(key), str):
                raise ValueError("invalid spectator timestamp")
            datetime.fromisoformat(value[key])
        if value["complete_from_start"]:
            _progress(value)
            if not _initial_protocol(lines):
                raise ValueError("missing spectator initial protocol")
        elif lines:
            raise ValueError("incomplete spectator source")
        if value["status"] == "complete" and not value.get("native_terminal"):
            raise ValueError("spectator completion is not a native outcome")
        return value

    def append(self, episode_id, delta):
        if not isinstance(delta, dict):
            raise ValueError("invalid spectator delta")
        lines = _lines(delta.get("lines"))
        start, end = _cursor(delta.get("start_cursor")), _cursor(delta.get("cursor"))
        if end != start + len(lines):
            raise ValueError("spectator delta is not contiguous")
        progress = _progress(delta)
        with self.lock:
            value = self._load(episode_id)
            if value is None:
                raise FileNotFoundError("spectator episode does not exist")
            if value["finished"]:
                raise ValueError("spectator episode is finished")
            if start != value["cursor"]:
                raise ValueError("spectator cursor gap or duplicate append")
            if progress["step"] < value.get("step", 0):
                raise ValueError("spectator progress moved backwards")
            if not value["complete_from_start"]:
                if start != 0 or not _initial_protocol(lines):
                    raise ValueError("spectator feed requires native initial protocol")
                value["complete_from_start"] = True
            value.update(progress)
            value["lines"].extend(lines)
            value.update(cursor=end, status="live", updated_at=utcnow())
            atomic_json(self._path(episode_id), value)

    def finish(self, episode_id, *, termination_reason=None, error=False):
        """Mark observation lifecycle without manufacturing a native win/tie."""
        with self.lock:
            value = self._load(episode_id)
            if value is None:
                return
            # Never publish arbitrary provider/exception strings: they may contain
            # private observations or request bodies. The API uses fixed statuses.
            status = ("error" if error else "complete" if value.get("native_terminal")
                      else "stopped" if termination_reason == "stopped"
                      else "truncated" if value.get("truncated") else "interrupted")
            value.update(status=status, finished=True, updated_at=utcnow())
            atomic_json(self._path(episode_id), value)

    def feed(self, episode_id, after=0):
        after = _cursor(after)
        _identity(episode_id)
        with self.lock:
            value = self._load(episode_id)
            if value is None:
                return {"schema": SCHEMA, "episode_id": episode_id, "game_id": "pokemon",
                        "available": False, "reason": "missing_native_public_sidecar",
                        "complete_from_start": False, "cursor": 0, "start_cursor": 0,
                        "lines": [], "reset": False, "status": "unavailable", "finished": True}
            reset = after > value["cursor"]
            start = 0 if reset else after
            # Explicit allowlist: neither future internal fields nor accidental
            # private keys in a sidecar may cross the public API boundary.
            result = {key: deepcopy(value[key]) for key in (
                "schema", "episode_id", "game_id", "source", "complete_from_start", "cursor",
                "status", "finished", "created_at", "updated_at", "purpose", "harness_id", *_PROGRESS) if key in value}
            result.update(available=value["complete_from_start"], start_cursor=start,
                          lines=deepcopy(value["lines"][start:]), reset=reset)
            return result
