"""Local persistent artifacts, with atomic JSON writes and a small SQLite registry."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2, allow_nan=False)
        file.flush()
        os.fsync(file.fileno())
    temporary.replace(path)


class Store:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(root / "registry.sqlite", check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS records (kind TEXT, id TEXT, created TEXT, body TEXT, PRIMARY KEY(kind,id))")
        self.db.execute("CREATE TABLE IF NOT EXISTS active (scope TEXT PRIMARY KEY, version TEXT)")
        self.db.commit()

    def put(self, kind, item):
        value = dict(item)
        value.setdefault("created_at", utcnow())
        identity = value.get("id", value.get("episode_id"))
        if not identity:
            raise ValueError("record requires id")
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO records VALUES (?,?,?,?)",
                            (kind, identity, value["created_at"], json.dumps(value, ensure_ascii=False, allow_nan=False)))
            self.db.commit()
        return value

    def get(self, kind, identity):
        with self.lock:
            row = self.db.execute("SELECT body FROM records WHERE kind=? AND id=?", (kind, identity)).fetchone()
            return json.loads(row[0]) if row else None

    def list(self, kind, limit=100):
        with self.lock:
            rows = self.db.execute("SELECT body FROM records WHERE kind=? ORDER BY created DESC LIMIT ?", (kind, limit)).fetchall()
            return [json.loads(row[0]) for row in rows]

    def active(self, scope):
        with self.lock:
            row = self.db.execute("SELECT version FROM active WHERE scope=?", (scope,)).fetchone()
            return self.get("version", row[0]) if row else None

    def activate(self, scope, version_id, expected_parent, publication=None):
        with self.lock:
            current = self.active(scope)
            if (current or {}).get("id") != expected_parent:
                raise RuntimeError("活动版本已经变化，拒绝发布过期候选")
            version = self.get("version", version_id)
            if not version or version["scope"] != scope:
                raise RuntimeError("版本不属于当前游戏配置")
            if publication is not None and (publication.get("status") != "promoted"
                    or publication.get("candidate_id") != version_id or publication.get("scope") != scope):
                raise ValueError("发布记录必须与活动版本一致")
            # Champion pointer and its approval evidence must survive a crash together.
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO active VALUES (?,?)", (scope, version_id))
                if publication is not None:
                    self.db.execute("INSERT OR REPLACE INTO records VALUES (?,?,?,?)",
                                    ("job", publication["id"], publication["created_at"],
                                     json.dumps(publication, ensure_ascii=False, allow_nan=False)))

    def save_episode(self, result):
        identity = result["episode_id"]
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", identity):
            raise ValueError("invalid episode id")
        atomic_json(self.root / "episodes" / f"{identity}.json", result)
        return self._index_episode(result)

    def _index_episode(self, result):
        identity = result["episode_id"]
        summary = {k: v for k, v in result.items() if k not in {"trace", "latencies_ms"}}
        latency = sorted(result.get("latencies_ms", []))
        summary["latency_mean_ms"] = round(sum(latency) / len(latency), 2) if latency else None
        summary["latency_p95_ms"] = latency[min(len(latency) - 1, int(len(latency) * .95))] if latency else None
        summary["reason"] = result.get("termination_reason")
        summary["trace_path"] = f"episodes/{identity}.json"
        return self.put("episode", summary)

    def recover_episode_indexes(self):
        """Repair the file-before-registry crash window without replaying a game."""
        indexed = {row[0] for row in self.db.execute("SELECT id FROM records WHERE kind='episode'")}
        recovered = 0
        for path in (self.root / "episodes").glob("*.json"):
            if path.stem in indexed:
                continue
            result = json.loads(path.read_text(encoding="utf-8"))
            if result.get("episode_id") != path.stem:
                raise ValueError("回合文件与 ID 不匹配，拒绝恢复")
            self._index_episode(result)
            recovered += 1
        return recovered

    def load_episode(self, identity):
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", identity):
            return None
        path = self.root / "episodes" / f"{identity}.json"
        return json.loads(path.read_text()) if path.is_file() else None
