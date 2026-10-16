"""Static markup and pure presentation tests; no browser, server, or model calls."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

import pytest

HTML = Path(__file__).resolve().parents[1] / "src/game_harness/static/index.html"


class Markup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.labels = []
        self.resources = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.append(attrs["id"])
        if tag == "label" and "for" in attrs:
            self.labels.append(attrs["for"])
        if tag in ("script", "img", "link"):
            self.resources.append(attrs.get("src", attrs.get("href", "")))


def script():
    return re.search(r"<script>([\s\S]*)</script>", HTML.read_text()).group(1)


def test_markup_has_unique_ids_valid_references_and_no_remote_assets():
    text = HTML.read_text()
    parsed = Markup()
    parsed.feed(text)
    assert not [key for key, count in Counter(parsed.ids).items() if count > 1]
    assert set(re.findall(r"\$\('([^']+)'\)", text)) <= set(parsed.ids)
    assert set(parsed.labels) <= set(parsed.ids)
    assert not any(re.match(r"(?:https?:)?//", src) for src in parsed.resources)
    assert 'id="level"' not in text
    assert 'id="gomoku_size"' in text


def node_eval(expression):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js required for pure JavaScript presentation checks")
    definitions = script().split("    $('settingsForm').addEventListener('submit'", 1)[0]
    assert definitions != script(), "Keep test execution separate from DOM startup"
    source = definitions + "\nconsole.log(JSON.stringify(" + expression + "));\n"
    result = subprocess.run([node, "-e", source], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_javascript_syntax(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js not installed")
    path = tmp_path / "frontend.js"
    path.write_text(script())
    result = subprocess.run([node, "--check", str(path)], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_catalog_contains_six_games_and_platformer_evaluation_defaults():
    games = node_eval("fallbackGames")
    assert {game["id"] for game in games} == {"snake", "xiangqi", "army", "gomoku", "tetris", "platformer"}
    platformer = next(game for game in games if game["id"] == "platformer")
    assert platformer["defaults"]["promotion_games"] == 6
    gomoku = next(game for game in games if game["id"] == "gomoku")
    assert gomoku["defaults"]["gomoku_size"] == 9


@pytest.mark.parametrize("game", ["army", "gomoku"])
def test_duel_budget_cutoff_is_not_reported_as_draw(game):
    result = node_eval(f"scorePresentation({json.dumps(game)},{{truncated:true,result:null,score:50}})")
    assert "未分胜负" in result["description"]
    assert "不代表和棋" in result["note"]
    assert "实际结果" not in result["description"]
    actual_draw = node_eval(f"scorePresentation({json.dumps(game)},{{truncated:false,result:'draw',score:50}})")
    assert actual_draw["description"] == "实际结果：和棋"


def test_xiangqi_cutoff_remains_distinct_material_surrogate():
    result = node_eval("scorePresentation('xiangqi',{truncated:true,result:null,score:52})")
    assert "子力代理分" in result["description"]
    assert "非胜负" in result["description"]


def test_arcade_scores_are_actual_metrics_and_cutoff_is_not_completion():
    tetris = node_eval("scorePresentation('tetris',{truncated:true,metrics:{lines:3,points:400}})")
    assert "3 行" in tetris["description"]
    assert "预算截断" in tetris["description"]
    platformer = node_eval("scorePresentation('platformer',{truncated:true,metrics:{progress:35.5,coins:1,completed:false}})")
    assert "35.5%" in platformer["description"]
    assert "金币 1" in platformer["description"]
    assert "已通关" not in platformer["description"]
    assert "不等于通关" in platformer["note"]


def test_army_renderer_prefers_public_projection_and_masks_hidden_labels():
    snapshot = {
        "cells": [{"seat": 0, "kind": "commander", "label": "私有司令", "hidden": False}],
        "observation": {"private": "DO_NOT_DISPLAY_PRIVATE_OBSERVATION"},
        "public_view": {"rows": 12, "cols": 5, "turn": 1,
                        "cells": [{"seat": 0, "kind": "commander", "label": "绝不可泄漏", "hidden": True},
                                  {"seat": 1, "kind": "engineer", "label": "工兵", "hidden": False}]},
    }
    result = node_eval(f"publicArmyView({json.dumps(snapshot, ensure_ascii=False)})")
    encoded = json.dumps(result, ensure_ascii=False)
    assert "私有司令" not in encoded
    assert "绝不可泄漏" not in encoded
    assert "commander" not in encoded
    assert "DO_NOT_DISPLAY_PRIVATE_OBSERVATION" not in encoded
    assert result["cells"][0]["label"] == "?"
    assert result["cells"][1]["label"] == "工兵"


def test_army_projection_does_not_retain_previous_frame_identities():
    result = node_eval("(()=>{publicArmyView({public_view:{cells:[{seat:0,label:'司令',hidden:false}]}});return publicArmyView({public_view:{cells:[{seat:0,label:'私有司令',hidden:true}]}});})()")
    assert result["cells"] == [{"seat": 0, "label": "?", "hidden": True}]


@pytest.mark.parametrize("game,action,snapshot,expected", [
    ("snake", "left", {}, "向左"),
    ("xiangqi", "h2h9", {}, "h2 → h9"),
    ("gomoku", "place:14", {"cols": 9}, "落子 F2"),
    ("army", "move:0:59", {}, "A1 → E12"),
    ("tetris", "rotate-cw:100", {}, "顺时针旋转 · 100 ms"),
    ("platformer", "right+jump:300", {}, "向右 + 跳跃 · 300 ms"),
])
def test_action_labels_preserve_game_coordinates_and_duration(game, action, snapshot, expected):
    result = node_eval(f"actionPresentation({json.dumps(game)},{json.dumps(action)},{json.dumps(snapshot)})")
    assert result["label"] == expected


@pytest.mark.parametrize("game", ["snake", "xiangqi", "army", "gomoku", "tetris", "platformer"])
def test_run_payload_only_includes_current_game_specific_fields(game):
    values = {"episodes": "1", "seed": "42", "max_steps": "80", "train_games": "3", "validation_games": "3",
              "promotion_games": "6", "reflection_rounds": "1", "board_size": "8", "gomoku_size": "15"}
    result = node_eval(f"formPayload({json.dumps(game)},{json.dumps(values)})")
    assert result["game_id"] == game
    assert ("board_size" in result) == (game == "snake")
    assert ("gomoku_size" in result) == (game == "gomoku")
    assert result["promotion_games"] == 6
    assert all(isinstance(value, int) for key, value in result.items() if key != "game_id")


def test_episode_history_uses_final_state_metrics_and_actual_outcome():
    tetris = node_eval("scorePresentation('tetris',episodeOutcome({score:300,termination_reason:'step_limit',final_state:{score:300,metrics:{lines:2},truncated:true}}))")
    assert "2 行" in tetris["description"]
    platformer = node_eval("scorePresentation('platformer',episodeOutcome({score:11200,final_state:{metrics:{progress:100,coins:2,completed:true},truncated:false}}))")
    assert "100%" in platformer["description"]
    assert "金币 2" in platformer["description"]
    assert "已通关" in platformer["description"]
    army = node_eval("scorePresentation('army',episodeOutcome({score:100,termination_reason:'FLAG_CAPTURED',final_state:{result:'win',truncated:false}}))")
    assert army["description"] == "实际结果：候选方胜"


@pytest.mark.parametrize("projection", [None, False, [], "invalid"])
def test_army_missing_or_invalid_public_projection_never_falls_back_to_private_board(projection):
    snapshot = {"cells": [{"seat": 0, "label": "PRIVATE_COMMANDER", "hidden": False}],
                "observation": {"secret": "PRIVATE_OBSERVATION"}, "public_view": projection,
                "turn": 0, "terrain": {"private": "PRIVATE_TERRAIN"}}
    result = node_eval(f"publicArmyView({json.dumps(snapshot)})")
    assert result["available"] is False
    assert result["cells"] == []
    assert result["terrain"] == {}
    assert "PRIVATE" not in json.dumps(result)
