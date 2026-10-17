"""Public Pokemon presentation: never display a private request or uncommitted choice."""
from __future__ import annotations

import copy
import json

import pytest

from test_frontend_static import HTML, node_eval


def snapshot():
    return {
        "game_id": "pokemon", "step": 2, "native_turn": 2, "max_steps": 160,
        "score": 50, "candidate_seat": 0, "done": False, "native_terminal": False,
        "public_view": {
            "format_label": "第三世代单打 3v3", "native_turn": 2,
            "players": [
                {"seat": 0, "unrevealed_count": 2, "team": [
                    {"species": "Charizard", "active": True, "hp_fraction": .7,
                     "types": ["fire", "flying"], "status": "par", "moves": [{"id": "flamethrower"}]}]},
                {"seat": 1, "unrevealed_count": 2, "team": [
                    {"species": "Blastoise", "active": True, "hp_fraction": .45,
                     "types": ["water"], "status": "", "moves": []}]},
            ],
            "recent_events": ["|move|p1a: Charizard|Flamethrower|p2a: Blastoise"],
        },
        "observation": {"private_team": "PRIVATE_TEAM"},
        "party": [{"species": "SECRET_SPECIES"}],
        "criteria": [{"id": "move:secret"}], "pending_choice": "SECRET_CHOICE",
    }


def test_pokemon_catalog_and_form_use_battle_decisions_without_grid_fields():
    game = node_eval("fallbackGames.find(game=>game.id==='pokemon')")
    assert game["defaults"] == {"max_steps": 160}
    values = {"max_steps": "160", "seed": "42", "board_size": "8", "gomoku_size": "9", "overcooked_layout": "cramped_room"}
    payload = node_eval(f"formPayload('pokemon',{json.dumps(values)})")
    assert payload == {"game_id": "pokemon", "max_steps": 160, "seed": 42}
    assert node_eval(f"validFormPayload({json.dumps(payload)})")
    markup = HTML.read_text()
    assert '<option value="pokemon">宝可梦对战</option>' in markup
    assert "pokemon:renderPokemonBoard" in markup
    assert "第三世代单打 3v3" in markup


@pytest.mark.parametrize("projection", [None, False, [], "invalid"])
def test_missing_public_projection_never_uses_private_state(projection):
    data = snapshot()
    data["public_view"] = projection
    public = node_eval(f"publicPokemonView({json.dumps(data)})")
    assert public["available"] is False
    assert public["players"] == []
    assert public["events"] == []
    assert "PRIVATE" not in json.dumps(public)
    assert "SECRET" not in json.dumps(public)


def test_public_projection_preserves_only_revealed_fields_without_guessing_hidden_members():
    data = snapshot()
    data["public_view"]["players"][0]["team"][0]["request"] = {"private": "SECRET_REQUEST"}
    public = node_eval(f"publicPokemonView({json.dumps(data)})")
    assert public["available"] is True
    assert public["turn"] == 2
    assert public["players"][0]["unrevealed"] == 2
    assert len(public["players"][0]["team"]) == 1
    assert public["players"][0]["team"][0]["moves"] == ["flamethrower"]
    assert "PRIVATE" not in json.dumps(public)
    assert "SECRET" not in json.dumps(public)
    later = copy.deepcopy(data)
    later["step"] += 1
    later["pending_choice"] = "DIFFERENT_SECRET"
    assert public == node_eval(f"publicPokemonView({json.dumps(later)})")


def test_truncation_is_not_a_draw_and_has_no_valid_evaluation_score():
    result = node_eval("scorePresentation('pokemon',{truncated:true,score:50,result:null,native_terminal:false})")
    assert result["description"] == "未结束 · 不计入评测"
    assert "占位" in result["note"]
    assert "不是和局" in result["note"]
    actual = node_eval("scorePresentation('pokemon',{truncated:false,score:50,result:'draw',native_terminal:true})")
    assert actual["description"] == "实际结果：和棋"
    pending = node_eval("scorePresentation('pokemon',{truncated:false,score:50,result:null,native_terminal:false})")
    assert pending["description"] == "对战进行中 · 等待原生胜负"


def test_action_strip_never_displays_private_or_pending_choices():
    result = node_eval("actionPresentation('pokemon','move:SECRET_MOVE',{step:5})")
    assert "SECRET" not in json.dumps(result)
    assert "结算后公开" in result["label"]


def test_canvas_displays_only_public_hp_types_status_and_native_turn():
    result = node_eval(f"""(()=>{{
      const calls=[],nodes={{}},ctx=new Proxy({{}},{{get:(obj,key)=>obj[key]||((...args)=>calls.push([key,...args])),set:(obj,key,value)=>(obj[key]=value,true)}});
      const canvas={{getBoundingClientRect:()=>({{width:500,height:420}}),getContext:()=>ctx,setAttribute:(key,value)=>nodes[key]=value}};
      globalThis.window={{devicePixelRatio:1}};
      globalThis.document={{getElementById:id=>id==='board'?canvas:(nodes[id]??={{textContent:''}})}};
      renderPokemonBoard({json.dumps(snapshot())});
      return {{calls,nodes}};
    }})()""")
    texts = [call[1] for call in result["calls"] if call[0] == "fillText"]
    assert "Charizard" in texts and "Blastoise" in texts
    assert "HP 70% · 麻痹" in texts
    assert texts.count("尚未公开") == 4
    assert result["nodes"]["boardResult"]["textContent"] == "原生回合 2 · 公开战况"
    assert "2 名未公开" in result["nodes"]["aria-label"]
    assert "SECRET" not in json.dumps(result)
    assert "PRIVATE" not in json.dumps(result)
