"""Overcooked presentation checks; snapshots are displayed without simulating actions."""
from __future__ import annotations

import copy
import json

import pytest

from test_frontend_static import HTML, node_eval


def snapshot():
    return {
        "game_id": "overcooked", "layout": "cramped_room", "rows": 3, "cols": 5,
        "terrain": ["XOPDX", "X   X", "XXSXX"], "native_tick": 0, "step": 0,
        "max_ticks": 120, "max_steps": 240, "score": 0, "deliveries": 0,
        "acting_player": 0,
        "players": [
            {"index": 0, "position": [1, 1], "orientation": [0, -1], "held_object": None},
            {"index": 1, "position": [3, 1], "orientation": [0, 1], "held_object": {"name": "dish"}},
        ],
        "pots": [{"position": [2, 0], "ingredients": ["onion", "onion", "onion"],
                  "is_cooking": True, "is_ready": False, "cook_time_remaining": 8, "capacity": 3}],
        "objects": [{"position": [0, 0], "name": "onion"}],
    }


def test_overcooked_catalog_and_layout_default_match_two_decisions_per_tick():
    game = node_eval("fallbackGames.find(game=>game.id==='overcooked')")
    assert game["defaults"] == {"max_steps": 240, "overcooked_layout": "cramped_room"}
    markup = HTML.read_text()
    assert 'id="overcookedLayoutField"' in markup
    assert 'name="overcooked_layout"' in markup
    assert "overcooked:renderOvercookedBoard" in markup


@pytest.mark.parametrize("layout", ["cramped_room", "asymmetric_advantages", "coordination_ring"])
def test_overcooked_form_retains_string_layout_without_leaking_other_game_settings(layout):
    values = {"max_steps": "240", "seed": "42", "overcooked_layout": layout,
              "board_size": "8", "gomoku_size": "15"}
    result = node_eval(f"formPayload('overcooked',{json.dumps(values)})")
    assert result == {"game_id": "overcooked", "max_steps": 240, "seed": 42, "overcooked_layout": layout}
    assert node_eval(f"validFormPayload({json.dumps(result)})")
    other = node_eval(f"formPayload('snake',{json.dumps(values)})")
    assert "overcooked_layout" not in other
    assert other["board_size"] == 8


@pytest.mark.parametrize("payload", [
    {"game_id": "overcooked", "overcooked_layout": "invented"},
    {"game_id": "overcooked", "max_steps": "240"},
    {"game_id": "snake", "overcooked_layout": "cramped_room"},
    {"game_id": "overcooked", "max_steps": 1.5},
    {"game_id": "overcooked", "max_steps": 239},
])
def test_invalid_layout_and_noninteger_budget_are_rejected(payload):
    assert node_eval(f"validFormPayload({json.dumps(payload)})") is False


def test_score_label_is_sparse_delivery_reward_and_budget_remains_explicit():
    result = node_eval("scorePresentation('overcooked',{score:40,deliveries:2,native_tick:120,truncated:true})")
    assert result["label"] == "原生送餐积分"
    assert "2 份" in result["description"]
    assert "120 厨房 tick" in result["description"]
    assert "预算截断" in result["description"]
    assert "不计备菜" in result["note"]
    assert "两位厨师各决策一次" in result["note"]
    assert result["unit"] == "次决策"


@pytest.mark.parametrize("action,label", [
    ("north", "向北"), ("south", "向南"), ("east", "向东"),
    ("west", "向西"), ("stay", "等待"), ("interact", "交互"),
])
def test_all_six_primitive_actions_have_labels(action, label):
    result = node_eval(f"actionPresentation('overcooked',{json.dumps(action)})")
    assert result["label"] == label


def test_first_staged_decision_changes_actor_but_never_predicts_world_motion():
    before = snapshot()
    staged = copy.deepcopy(before)
    staged.update(step=1, acting_player=1)
    committed = copy.deepcopy(staged)
    committed.update(step=2, native_tick=1, acting_player=0)
    committed["players"][0]["position"] = [2, 1]
    views = node_eval(f"{json.dumps([before, staged, committed])}.map(overcookedView)")
    assert views[0]["actor"] == 0
    assert views[1]["actor"] == 1
    assert views[0]["players"] == views[1]["players"]
    assert views[0]["pots"] == views[1]["pots"]
    assert views[0]["tick"] == views[1]["tick"] == 0
    assert views[2]["tick"] == 1
    assert views[2]["players"][0]["position"] == [2, 1]


def test_canvas_renderer_shows_native_pots_objects_orientation_and_next_actor():
    encoded = json.dumps(snapshot())
    result = node_eval(f"""(()=>{{
      const calls=[],nodes={{}},ctx=new Proxy({{}},{{get:(obj,key)=>obj[key]||((...args)=>calls.push([key,...args])),set:(obj,key,value)=>(obj[key]=value,true)}});
      const canvas={{getBoundingClientRect:()=>({{width:500,height:400}}),getContext:()=>ctx,setAttribute:(key,value)=>nodes[key]=value}};
      globalThis.window={{devicePixelRatio:1}};
      globalThis.document={{getElementById:id=>id==='board'?canvas:(nodes[id]??={{textContent:''}})}};
      renderOvercookedBoard({encoded});
      return {{calls,nodes}};
    }})()""")
    texts = [call[1] for call in result["calls"] if call[0] == "fillText"]
    assert "3/3" in texts and "烹饪 8" in texts
    assert "洋葱" in texts and "盘" in texts
    assert "cramped_room" in texts
    assert result["nodes"]["boardResult"]["textContent"] == "TICK 0/120 · 厨师 1 决策"
    assert "厨师 1 位于 1,1，朝向 0,-1，持有空手" in result["nodes"]["aria-label"]
    assert "厨师 2 位于 3,1，朝向 0,1，持有盘" in result["nodes"]["aria-label"]


def test_replay_uses_stored_after_snapshot_instead_of_interpolating_moves():
    initial = snapshot()
    staged = copy.deepcopy(initial)
    staged.update(step=1, acting_player=1)
    entry = {"before": initial, "after": staged, "action": "east", "receipt": {"model": "decision"}}
    result = node_eval(f"""(()=>{{
      const nodes={{}},frames=[];
      globalThis.document={{getElementById:id=>nodes[id]??={{classList:{{add:()=>{{}},remove:()=>{{}}}},setAttribute:()=>{{}}}}}};
      renderEpisode=(snapshot,...rest)=>frames.push({{snapshot,action:rest[1]}});
      replay={{trace:[{json.dumps(entry)}],purpose:'play'}};
      replayIndex=0;renderReplay();replayIndex=1;renderReplay();
      return frames;
    }})()""")
    assert result[0]["snapshot"] == initial
    assert result[1]["snapshot"] == staged
    assert result[1]["snapshot"]["players"] == initial["players"]
    assert result[1]["action"] == "east"
