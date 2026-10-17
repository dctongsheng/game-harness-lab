"""Read-only spectator API and pinned-asset security using local fixtures only."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from game_harness.app import create_app
from game_harness.providers import Settings
from game_harness import showdown_assets


@pytest.fixture
def web(tmp_path):
    settings = Settings("http://127.0.0.1:1/v1/systemone", "decision-test", 1,
                        "http://127.0.0.1:1/v1", "coach-test", "", 1)
    app = create_app(tmp_path / "data", settings)
    try:
        with TestClient(app, base_url="http://127.0.0.1") as client:
            yield client, app.state.lab
    finally:
        app.state.lab.store.db.close()


@pytest.fixture
def asset_root(tmp_path, monkeypatch):
    root = tmp_path / "viewer"
    root.mkdir()
    content = b"window.UpstreamBattleFixture = true;\n"
    (root / "battle.js").write_bytes(content)
    data = {"schema_version": 1, "client": {"commit": showdown_assets.CLIENT_COMMIT},
            "files": {"battle.js": {"sha256": hashlib.sha256(content).hexdigest(), "mime": "text/javascript"}}}
    (root / "manifest.json").write_text(json.dumps(data))
    monkeypatch.setattr(showdown_assets, "VIEWER_ROOT", root)
    return root, data


def initial_delta():
    lines = ["|gen|3", "|player|p1|Seat 1|", "|player|p2|Seat 2|", "|start",
             "|switch|p1a: Charizard|Charizard, L50, M|100/100",
             "|switch|p2a: Blastoise|Blastoise, L50, M|100/100", "|turn|1"]
    return {"start_cursor": 0, "cursor": len(lines), "lines": lines,
            "step": 0, "native_turn": 1, "native_terminal": False, "truncated": False,
            "result": "ongoing", "done": False}


def test_asset_status_pinned_file_and_missing_names(web, asset_root):
    client, _ = web
    response = client.get("/api/showdown-viewer")
    assert response.json()["available"] is True
    assert response.json()["client"]["commit"] == showdown_assets.CLIENT_COMMIT
    license_manifest = client.get("/showdown/manifest.json")
    assert license_manifest.status_code == 200
    assert license_manifest.json()["client"]["commit"] == showdown_assets.CLIENT_COMMIT
    response = client.get("/showdown/assets/battle.js")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/javascript")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"
    for name in ("manifest.json", ".env", "package.json", "unlisted.js"):
        assert client.get("/showdown/assets/" + name).status_code == 404


def test_tampered_asset_and_wrong_revision_are_unavailable(web, asset_root):
    client, _ = web
    root, manifest = asset_root
    (root / "battle.js").write_text("tampered")
    assert client.get("/showdown/assets/battle.js").status_code == 404
    manifest["client"]["commit"] = "0" * 40
    (root / "manifest.json").write_text(json.dumps(manifest))
    assert client.get("/api/showdown-viewer").json()["available"] is False
    assert client.get("/showdown/assets/battle.js").status_code == 404


@pytest.mark.parametrize("name", ["../secret", "a/../../secret", "/secret", "a/./battle.js", "a\\secret"])
def test_asset_path_traversal_rejected_before_filesystem_access(asset_root, name):
    with pytest.raises(ValueError):
        showdown_assets.asset(name)


def test_manifest_cannot_whitelist_symlink_escape_or_html(asset_root):
    root, manifest = asset_root
    outside = root.parent / "outside.js"
    outside.write_text("private outside artifact")
    (root / "escaped.js").symlink_to(outside)
    manifest["files"]["escaped.js"] = {
        "sha256": hashlib.sha256(outside.read_bytes()).hexdigest(), "mime": "text/javascript"}
    manifest["files"]["battle.js"]["mime"] = "text/html"
    (root / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(FileNotFoundError):
        showdown_assets.asset("escaped.js")
    with pytest.raises(ValueError, match="asset type"):
        showdown_assets.asset("battle.js")


def test_spectator_suffix_and_reset_preserve_every_repeated_event(web):
    client, lab = web
    delta = initial_delta()
    lab.spectators.begin("ep-live", purpose="play", harness_id="h-initial")
    lab.spectators.append("ep-live", delta)
    initial = client.get("/api/spectator/ep-live").json()
    assert initial["lines"] == delta["lines"]
    assert initial["available"] is True
    lines = ["|move|p1a: Charizard|Protect|p1a: Charizard"] * 2
    lab.spectators.append("ep-live", {**delta, "start_cursor": delta["cursor"],
                                     "cursor": delta["cursor"] + 2, "lines": lines, "step": 2})
    suffix = client.get(f"/api/spectator/ep-live?after={delta['cursor']}").json()
    assert suffix["lines"] == lines
    assert suffix["start_cursor"] == delta["cursor"]
    empty = client.get(f"/api/spectator/ep-live?after={suffix['cursor']}").json()
    assert empty["lines"] == []
    reset = client.get("/api/spectator/ep-live?after=9999").json()
    assert reset["reset"] is True and reset["start_cursor"] == 0
    assert reset["lines"] == delta["lines"] + lines
    listed = client.get("/api/spectator/episodes").json()["episodes"]
    assert [row["episode_id"] for row in listed] == ["ep-live"]
    assert all("lines" not in row for row in listed)


def test_sidecar_api_is_allowlist_and_missing_native_history_is_explicit(web):
    client, lab = web
    lab.spectators.begin("ep-private")
    lab.spectators.append("ep-private", initial_delta())
    path = lab.spectators.root / "ep-private.json"
    stored = json.loads(path.read_text())
    stored.update(seed=12345, request={"team": "PRIVATE_TEAM"}, raw_response="PRIVATE_RECEIPT")
    path.write_text(json.dumps(stored))
    response = client.get("/api/spectator/ep-private")
    assert response.status_code == 200
    assert "PRIVATE" not in response.text and '"seed"' not in response.text
    lab.store.put("episode", {"episode_id": "ep-old", "game_id": "pokemon"})
    missing = client.get("/api/spectator/ep-old").json()
    assert missing["available"] is False
    assert missing["reason"] == "missing_native_public_sidecar"
    assert missing["lines"] == []
    assert client.get("/api/spectator/ep-unknown").status_code == 404
    lab.store.put("episode", {"episode_id": "ep-snake", "game_id": "snake"})
    assert client.get("/api/spectator/ep-snake").status_code == 404


def test_episode_listing_marks_initialization_failure_unavailable_for_queue(web):
    client, lab = web
    lab.spectators.begin("ep-init-failed")
    lab.spectators.finish("ep-init-failed", error=True)
    lab.spectators.begin("ep-valid")
    lab.spectators.append("ep-valid", initial_delta())
    rows = {row["episode_id"]: row for row in client.get("/api/spectator/episodes").json()["episodes"]}
    assert rows["ep-init-failed"]["finished"] is True
    assert rows["ep-init-failed"]["available"] is False
    assert rows["ep-valid"]["available"] is True
    assert rows["ep-valid"]["complete_from_start"] is True


@pytest.mark.parametrize("cursor,expected", [("-1", 400), ("not-an-int", 422), ("1.5", 422)])
def test_invalid_http_cursor_cannot_advance_or_read_unrelated_files(web, cursor, expected):
    client, lab = web
    lab.spectators.begin("ep-live")
    lab.spectators.append("ep-live", initial_delta())
    assert client.get(f"/api/spectator/ep-live?after={cursor}").status_code == expected


def test_observer_frame_and_assets_are_read_only_and_origin_isolated(web):
    client, lab = web
    response = client.get("/spectate")
    assert response.status_code == 200
    assert 'sandbox="allow-scripts"' in response.text
    assert "allow-same-origin" not in response.text
    assert client.get("/showdown/spectate.js").status_code == 200
    frame = client.get("/showdown/frame")
    assert frame.status_code == 200
    policy = frame.headers["content-security-policy"]
    for restriction in ("connect-src 'none'", "frame-ancestors 'self'", "base-uri 'none'", "form-action 'none'"):
        assert restriction in policy
    assert "https://play.pokemonshowdown.com" in policy
    assert "window.parent.postMessage" in client.get("/showdown/frame.js").text
    assert client.get("/showdown/index.html").status_code == 404
    assert client.post("/api/spectator/episodes").status_code == 405
    for endpoint in ("pause", "resume", "stop"):
        assert client.post("/api/" + endpoint, headers={"Origin": "null"}).status_code == 403
    assert not lab.stop_event.is_set() and not lab.paused


def test_cross_host_request_cannot_read_observer_api(web):
    client, _ = web
    assert client.get("/api/spectator/episodes", headers={"Host": "attacker.invalid"}).status_code == 403
