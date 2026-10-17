"""Pure observer transport tests: no model, game, browser or external requests."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest


CONTROLLER = Path(__file__).resolve().parents[1] / "src/game_harness/static/spectate.js"


def node_eval(source):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for spectator controller tests")
    program = (f"const {{FeedCursor,pendingEpisodes,playbackComplete}}=require({json.dumps(str(CONTROLLER))});\n"
               "console.log(JSON.stringify((()=>{\n" + source + "\n})()));")
    result = subprocess.run([node, "-e", program], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_cursor_counts_lines_including_identical_repeated_native_events():
    result = node_eval("""
      const c=new FeedCursor(); c.select('ep-a'); const epoch=c.epoch;
      const first=c.accept({available:true,episode_id:'ep-a',start_cursor:0,cursor:2,
        lines:['|move|same','|move|same']},epoch);
      const repeatedPoll=c.accept({available:true,episode_id:'ep-a',start_cursor:2,cursor:2,lines:[]},epoch);
      const next=c.accept({available:true,episode_id:'ep-a',start_cursor:2,cursor:3,lines:['|move|same']},epoch);
      return {first,repeatedPoll,next,cursor:c.cursor};
    """)
    assert result == {"first": True, "repeatedPoll": True, "next": True, "cursor": 3}


def test_episode_switch_discards_inflight_old_response_without_changing_cursor():
    result = node_eval("""
      const c=new FeedCursor(); c.select('ep-a'); const old=c.epoch;
      c.select('ep-b');
      const accepted=c.accept({available:true,episode_id:'ep-a',start_cursor:0,cursor:1,lines:['|win|Seat 1']},old);
      const wrongId=c.accept({available:true,episode_id:'ep-a',start_cursor:0,cursor:1,lines:['|win|Seat 1']},c.epoch);
      return {accepted,wrongId,cursor:c.cursor,id:c.id};
    """)
    assert result == {"accepted": False, "wrongId": False, "cursor": 0, "id": "ep-b"}


@pytest.mark.parametrize("feed", [
    {"start_cursor": 1, "cursor": 3, "lines": ["|turn|1", "|turn|2"]},
    {"start_cursor": 2, "cursor": 3, "lines": []},
    {"start_cursor": 2, "cursor": 1, "lines": []},
    {"start_cursor": 2, "cursor": -1, "lines": []},
    {"start_cursor": 2, "cursor": 3.5, "lines": ["|turn|2"]},
    {"start_cursor": "2", "cursor": 3, "lines": ["|turn|2"]},
    {"start_cursor": 2, "cursor": 3, "lines": [None]},
    {"start_cursor": 1, "cursor": 1, "lines": [], "reset": True},
    {"start_cursor": 0, "cursor": 3, "lines": ["|turn|2"], "reset": True},
])
def test_invalid_or_out_of_order_suffix_is_rejected_without_advancing(feed):
    result = node_eval("""
      const c=new FeedCursor();c.select('ep-a');c.cursor=2;
      let rejected=false;try{c.accept(""" + json.dumps({"available": True, "episode_id": "ep-a", **feed}) + """,c.epoch)}catch{rejected=true}
      return {rejected,cursor:c.cursor};
    """)
    assert result == {"rejected": True, "cursor": 2}


def test_reconnect_reset_requires_complete_stream_from_zero():
    result = node_eval("""
      const c=new FeedCursor();c.select('ep-a');c.cursor=100;
      const accepted=c.accept({available:true,episode_id:'ep-a',start_cursor:0,cursor:2,
        lines:['|gen|3','|start'],reset:true},c.epoch);
      return {accepted,cursor:c.cursor};
    """)
    assert result == {"accepted": True, "cursor": 2}


def test_unavailable_historical_record_never_advances_cursor():
    assert node_eval("""
      const c=new FeedCursor();c.select('ep-old');
      return {accepted:c.accept({available:false,episode_id:'ep-old',cursor:5},c.epoch),cursor:c.cursor};
    """) == {"accepted": False, "cursor": 0}


def test_queue_is_chronological_and_skips_current_visited_and_unavailable():
    assert node_eval("""
      const records=[
        {episode_id:'ep-new',created_at:'2026-10-17T12:04:00'},
        {episode_id:'ep-b',created_at:'2026-10-17T12:03:00'},
        {episode_id:'ep-a',created_at:'2026-10-17T12:03:00'},
        {episode_id:'ep-current',created_at:'2026-10-17T12:02:00'},
        {episode_id:'ep-missing',created_at:'2026-10-17T12:01:00',available:false},
        {episode_id:'ep-seen',created_at:'2026-10-17T12:00:00'}];
      return pendingEpisodes(records,new Set(['ep-seen']),'ep-current').map(row=>row.episode_id);
    """) == ["ep-a", "ep-b", "ep-new"]


@pytest.mark.parametrize("finished,animation,cursor,expected", [
    (True, {"cursor": 8, "atEnd": True, "paused": False}, 8, True),
    (False, {"cursor": 8, "atEnd": True, "paused": False}, 8, False),
    (True, {"cursor": 7, "atEnd": True, "paused": False}, 8, False),
    (True, {"cursor": 8, "atEnd": False, "paused": False}, 8, False),
    (True, {"cursor": 8, "atEnd": True, "paused": True}, 8, False),
    (True, {"atEnd": True, "paused": False}, 8, False),
    (True, {"cursor": "8", "atEnd": True, "paused": False}, 8, False),
])
def test_auto_next_requires_final_cursor_acknowledgment_and_finished_animation(finished, animation, cursor, expected):
    expression = f"playbackComplete({json.dumps(finished)},{json.dumps(animation)},{cursor})"
    assert node_eval(f"return {expression};") is expected


def test_explicit_seek_latest_discards_skipped_queue_and_initial_skips_missing_records():
    """Execute controller startup and its real click handler in a minimal DOM."""
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for spectator controller tests")
    source = r"""
      const vm=require('node:vm'),fs=require('node:fs');
      const nodes={},listeners={},timers=[],posted=[];
      function node(id){return nodes[id]||(nodes[id]={id,value:'',checked:id==='follow',hidden:false,
        textContent:'',handlers:{},addEventListener(type,fn){this.handlers[type]=fn;},
        replaceChildren(fragment){this.children=fragment.children;}});}
      const child={postMessage:value=>posted.push(value)};node('viewer').contentWindow=child;
      let records=[{episode_id:'ep-broken',created_at:'04',available:false,finished:true},
        {episode_id:'ep-old',created_at:'01',available:true,finished:true}];
      const context={console,URLSearchParams,AbortSignal,Date,
        location:{search:''},document:{getElementById:node,createElement:()=>({}),
          createDocumentFragment:()=>({children:[],append(x){this.children.push(x);}})},
        window:{addEventListener:(kind,fn)=>listeners[kind]=fn},setTimeout:fn=>timers.push(fn),
        fetch:async path=>({ok:true,json:async()=>path==='/api/showdown-viewer'?{available:true}:
          path==='/api/spectator/episodes'?{episodes:records}:
          (()=>{const url=new URL(path,'http://127.0.0.1'),after=Number(url.searchParams.get('after'));
            const all=['|gen|3','|start'];return {available:true,episode_id:url.pathname.split('/').pop(),
              start_cursor:after,cursor:2,lines:all.slice(after),finished:true,native_turn:1,status:'complete'};})()})};
      vm.runInNewContext(fs.readFileSync(CONTROLLER,'utf8'),context);
      const flush=async()=>{for(let n=0;n<12;n++)await Promise.resolve();};
      await flush();
      const initiallySelected=node('identity').textContent;
      listeners.message({source:child,origin:'null',data:{type:'showdown-ready'}});
      records=[{episode_id:'ep-latest',created_at:'03',available:true,finished:true},
        {episode_id:'ep-middle',created_at:'02',available:true,finished:true},records[1]];
      await timers.shift()();await flush();
      await node('latest').handlers.click();await flush();
      listeners.message({source:child,origin:'null',data:{type:'showdown-state',episodeId:'ep-latest',
        cursor:2,atEnd:true,paused:false,animatedTurn:1}});
      return {initiallySelected,selected:node('identity').textContent,nextDisabled:node('next').disabled,
        seek:posted.some(message=>message.type==='showdown-seek-live'&&message.episodeId==='ep-latest')};
    """
    program = f"const CONTROLLER={json.dumps(str(CONTROLLER))};\n(async()=>{{{source}}})().then(value=>console.log(JSON.stringify(value)));"
    result = subprocess.run([node, "-e", program], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"initiallySelected": "ep-old", "selected": "ep-latest",
                                       "nextDisabled": True, "seek": True}


def test_observer_has_no_game_control_calls_or_unsandboxed_frame():
    source = CONTROLLER.read_text()
    for endpoint in ("/api/run", "/api/pause", "/api/resume", "/api/stop", "/api/rollback"):
        assert endpoint not in source
    page = CONTROLLER.with_name("spectate.html").read_text()
    assert 'sandbox="allow-scripts"' in page
    assert "allow-same-origin" not in page
    assert "event.source !== frame.contentWindow" in source
    assert "event.origin !== 'null'" in source
