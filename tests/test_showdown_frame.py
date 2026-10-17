"""The spectator iframe consumes ordered public events without controlling play."""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
FRAME = ROOT / "src/game_harness/static/showdown-frame.js"
HTML = ROOT / "src/game_harness/static/showdown-frame.html"


def frame_eval(code: str):
    bootstrap = r"""
const fs=require('node:fs'),vm=require('node:vm');
const sent=[],listeners={},nodes={},battles=[];
for(const id of ['playback-status','viewer-error','toggle-playback','replay-animation',
  'animation-speed','battle-stage','battle'])nodes[id]={style:{},clientWidth:642,
  textContent:'',disabled:true,value:'1',events:{},addEventListener(t,f){this.events[t]=f;}};
const parent={postMessage:m=>sent.push(m)};
global.window={parent,addEventListener:(type,fn)=>listeners[type]=fn};
global.document={getElementById:id=>nodes[id]};global.$=x=>x;
global.Battle=class {
  constructor(options){this.options=options;this.stepQueue=options.log.slice();this.currentStep=0;
    this.paused=options.paused;this.atQueueEnd=false;this.turn=0;this.ended=false;
    this.scene={acceleration:1,updateAcceleration(){}};this.appended=[];this.seeks=[];battles.push(this);}
  setMute(value){this.muted=value;}destroy(){this.destroyed=true;}subscribe(fn){this.subscription=fn;}
  play(){this.paused=false;this.subscription?.('playing');}pause(){this.paused=true;this.subscription?.('paused');}
  addBatch(lines){this.appended.push(...lines);this.stepQueue.push(...lines);}
  seekTurn(turn){this.seeks.push(turn===Infinity?'Infinity':turn);}
};
vm.runInThisContext(fs.readFileSync(process.argv[1],'utf8'));
function receive(message,source=parent){listeners.message({source,data:message});}
function reset(id='ep-1',lines=['|gen|3','|turn|1'],finished=false){receive({type:'showdown-reset',episodeId:id,lines,cursor:lines.length,finished});}
function append(lines,start=2,id='ep-1',finished=false){receive({type:'showdown-append',episodeId:id,start_cursor:start,cursor:start+lines.length,lines,finished});}
"""
    result = subprocess.run(["node", "-e", bootstrap + "\nconsole.log(JSON.stringify(" + code + "));", str(FRAME)], text=True, capture_output=True, check=True)
    return json.loads(result.stdout)


def test_live_queue_uses_original_battle_without_premature_replay_end():
    result = frame_eval("(()=>{reset();append(['|move|p1a: Charizard|Flamethrower|p2a: Blastoise']);return {options:battles[0].options,appended:battles[0].appended,muted:battles[0].muted,ready:sent[0]}})()")
    assert result["options"]["isReplay"] is False
    assert result["options"]["$frame"] == "#battle"
    assert result["appended"] == ["|move|p1a: Charizard|Flamethrower|p2a: Blastoise"]
    assert result["muted"] is True
    assert result["ready"] == {"type": "showdown-ready"}


def test_duplicate_delta_is_idempotent_and_stale_battle_delta_is_ignored():
    result = frame_eval("(()=>{reset();append(['|turn|2']);append(['|turn|2']);append(['|turn|999'],2,'old-episode');return {appended:battles[0].appended,errors:sent.filter(m=>m.type==='showdown-error')}})()")
    assert result == {"appended": ["|turn|2"], "errors": []}


def test_out_of_order_delta_stops_renderer_without_guessing_missing_events():
    result = frame_eval("(()=>{reset();append(['|turn|4'],7);return {appended:battles[0].appended,paused:battles[0].paused,error:sent.at(-1)}})()")
    assert result["appended"] == []
    assert result["paused"] is True
    assert result["error"]["type"] == "showdown-error"


def test_wrong_source_and_private_request_never_reach_renderer():
    result = frame_eval("(()=>{receive({type:'showdown-reset',episodeId:'fake',cursor:1,lines:['|turn|8'],finished:false},{});reset('private',['|request|SECRET']);return {count:battles.length,error:sent.at(-1)}})()")
    assert result["count"] == 0
    assert result["error"]["type"] == "showdown-error"


def test_changing_episode_destroys_prior_scene_and_resets_cursor():
    result = frame_eval("(()=>{reset();append(['|turn|2']);reset('ep-2',['|gen|3']);return {oldDestroyed:battles[0].destroyed,newLog:battles[1].stepQueue,state:sent.at(-1)}})()")
    assert result["oldDestroyed"] is True
    assert result["newLog"] == ["|gen|3"]
    assert result["state"]["episodeId"] == "ep-2"
    assert result["state"]["cursor"] == 1


def test_polling_does_not_seek_or_discard_animation_and_explicit_seek_is_bound():
    result = frame_eval("(()=>{reset();append(['|turn|2']);const before=battles[0].seeks.slice();receive({type:'showdown-seek-live',episodeId:'other'});receive({type:'showdown-seek-live',episodeId:'ep-1'});return {before,after:battles[0].seeks}})()")
    assert result == {"before": [], "after": ["Infinity"]}


def test_pause_replay_speed_controls_do_not_send_gameplay_commands():
    result = frame_eval("(()=>{reset();nodes['toggle-playback'].events.click();const paused=battles[0].paused;nodes['replay-animation'].events.click();nodes['animation-speed'].value='4';nodes['animation-speed'].events.change();return {paused,seeks:battles[0].seeks,speed:battles[0].scene.acceleration,types:[...new Set(sent.map(x=>x.type))]}})()")
    assert result["paused"] is True
    assert result["seeks"] == [0]
    assert result["speed"] == 4
    assert set(result["types"]) <= {"showdown-ready", "showdown-state"}


def test_at_end_requires_all_queued_lines_to_finish_animating():
    result = frame_eval("(()=>{reset();const b=battles[0];b.atQueueEnd=true;b.currentStep=1;b.subscription('atqueueend');const early=sent.at(-1).atEnd;b.currentStep=2;b.subscription('atqueueend');return {early,complete:sent.at(-1).atEnd}})()")
    assert result == {"early": False, "complete": True}


def test_html_only_loads_local_scripts_and_does_not_start_full_showdown_client():
    html = HTML.read_text()
    scripts = re.findall(r'<script src="([^"]+)"', html)
    assert scripts and all(source.startswith("/showdown/") for source in scripts)
    assert not any("sockjs" in source or "client-connection" in source or "client-main" in source for source in scripts)
    assert "BattleSound.muted=true" in html
    assert "Dex.resourcePrefix='https://play.pokemonshowdown.com/'" in html
    bridge = FRAME.read_text()
    assert "fetch(" not in bridge and "WebSocket" not in bridge
    assert "event.source !== window.parent" in bridge


def test_oversized_and_embedded_newline_protocol_rejected():
    for lines in (["|turn|1\n|request|SECRET"], ["|raw|<script>bad</script>"], ["|x|" + "a" * 4097]):
        result = frame_eval(f"(()=>{{reset('ep-1',{json.dumps(lines)});return {{count:battles.length,error:sent.at(-1).type}}}})()")
        assert result == {"count": 0, "error": "showdown-error"}
