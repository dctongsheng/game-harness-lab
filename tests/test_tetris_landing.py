"""Conditional geometry is audited against the unchanged native rules, offline."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from game_harness.tetris_landing import LANDING_CONTRACT, LANDING_VERSION, build_landing_projections


BRIDGE = Path(__file__).resolve().parents[1] / "src/game_harness/games/arcade_bridge.cjs"


@pytest.fixture(scope="module")
def native_cases():
    """Only tests can align a hypothetical pose; production never does this.

    All collision checks and landing cells use native restore/observe. Actual
    consequences use native input+step, without a copied lock/score function.
    """
    if not shutil.which("node"):
        pytest.skip("Native Tetris parity requires Node.js")
    program = "const {tetris} = require(" + json.dumps(str(BRIDGE)) + ");\n" + r"""
const output=[];
function run(name, type, boardRows, options={}) {
  let original=tetris.create({}, 'landing-native-parity');
  original.active={type,rotation:0,x:3,y:-2};
  original.board=boardRows.flatMap(row=>[...row].map(cell=>cell==='.'?0:'IOTSZJL'.indexOf(cell)+1));
  original.score.lines=options.lines??0;
  if(options.active) original.active=options.active;
  original=tetris.restore(original);
  const before=tetris.observe(original), candidates=[];
  for(let column=0;column<10;column++) for(let rotation=0;rotation<4;rotation++) {
    let pose={type,rotation,x:0,y:original.active.y};
    const local=tetris.observe({...original,active:pose}).active.cells;
    pose.x=column-Math.min(...local.map(c=>c.x));
    const id=`target:${column}:${rotation}`;
    let start;
    try {start=tetris.restore({...original,active:pose});}
    catch {candidates.push({id,valid:false});continue;}
    let landing=start;
    while(true) {
      try {landing=tetris.restore({...landing,active:{...landing.active,y:landing.active.y+1}});}
      catch {break;}
    }
    const cells=tetris.observe(landing).active.cells;
    const after=tetris.step(tetris.input(start,{buttons:['hard-drop'],durationMs:100}),1);
    candidates.push({id,valid:true,origin:{x:landing.active.x,y:landing.active.y},cells,
      board:tetris.observe(after).board.rows,lines:after.score.lines-before.score.lines,
      points:after.score.points-before.score.points,topOut:after.reason==='TOP_OUT'});
  }
  output.push({name,state:{game_id:'tetris',observation:before},candidates});
}
for(const type of 'IOTSZJL') {
  run(`empty-${type}`,type,Array(22).fill('..........'));
  let jagged=Array(22).fill('..........').map(row=>[...row]);
  for(let x=0;x<10;x++) for(let y=20-(x*3%7);y<20;y++) if(!(y===18 && x%3===0)) jagged[y+2][x]='J';
  run(`jagged-${type}`,type,jagged.map(row=>row.join('')));
  let well=Array(22).fill('..........');
  for(let y=16;y<20;y++) well[y+2]='JJJJ.JJJJJ';
  run(`four-line-well-${type}`,type,well,{lines:9});
  let high=Array(22).fill('..........');
  high[2]='JJJ....JJJ'; high[3]='JJJJJJJJJJ';
  run(`high-${type}`,type,high);
  // Only some target rotations fit when the active frame already sits low.
  if(type!=='I') run(`low-${type}`,type,Array(22).fill('..........'),{active:{type,rotation:0,x:3,y:18}});
}
let horizontal=Array(22).fill('..........');horizontal[21]='JJJ....JJJ';
run('one-line-level-two','I',horizontal,{lines:10});
let hidden=Array(22).fill('..........');hidden[1]='JJJ....JJJ';hidden[2]='JJJJJJJJJJ';
run('clear-hidden-row','I',hidden);
let topout=Array(22).fill('..........');topout[2]='J.........';
run('hidden-top-out','O',topout);
let double=Array(22).fill('..........');double[20]=double[21]='JJJJ..JJJJ';
run('double-line','O',double);
let triple=Array(22).fill('..........');for(let y=17;y<20;y++)triple[y+2]='JJJJ.JJJJJ';
run('triple-line','I',triple);
console.log(JSON.stringify(output));
"""
    result = subprocess.run([shutil.which("node"), "-e", program], check=True,
                            capture_output=True, text=True, timeout=20)
    return json.loads(result.stdout)


def independent_board_facts(rows):
    board = {row["y"]: row["cells"] for row in rows}
    heights, holes = [], 0
    for x in range(10):
        seen = False
        height = 0
        for y in range(-2, 20):
            if board[y][x] != ".":
                if not seen:
                    height = 20 - y
                seen = True
            elif seen:
                holes += 1
        heights.append(height)
    return {"holes": holes, "max_height": max(heights), "total_height": sum(heights),
            "column_heights": heights,
            "bumpiness": sum(abs(heights[i] - heights[i + 1]) for i in range(9))}


def test_every_piece_rotation_column_and_native_lock_consequence(native_cases):
    reasons, clear_counts, topouts = set(), set(), 0
    for case in native_cases:
        source = deepcopy(case["state"])
        facts = build_landing_projections(source)
        assert source == case["state"]
        assert facts["schema"] == LANDING_VERSION
        assert len(facts["candidates"]) == 40
        assert facts["before"] == independent_board_facts(source["observation"]["board"]["rows"])
        assert [row["id"] for row in facts["candidates"]] == [
            f"target:{column}:{rotation}" for column in range(10) for rotation in range(4)]
        for candidate, expected in zip(facts["candidates"], case["candidates"], strict=True):
            message = f"{case['name']} {candidate['id']}"
            assert candidate["id"] == expected["id"], message
            assert candidate["pose_valid"] is expected["valid"], message
            assert candidate["native_reachability"] == "not_proven"
            if not expected["valid"]:
                reasons.add(candidate["invalid_reason"])
                assert candidate["invalid_reason"] is not None
                for field in ("landing_origin", "landing_cells", "lines_cleared", "points_delta",
                              "holes_after", "holes_delta", "max_height", "total_height", "bumpiness", "top_out"):
                    assert candidate[field] is None, (message, field)
                continue
            assert candidate["invalid_reason"] is None
            assert candidate["landing_origin"] == expected["origin"], message
            expected_cells = sorted(expected["cells"], key=lambda cell: (cell["y"], cell["x"]))
            assert candidate["landing_cells"] == expected_cells, message
            assert candidate["lines_cleared"] == expected["lines"], message
            assert candidate["points_delta"] == expected["points"], message
            assert candidate["top_out"] is expected["topOut"], message
            after = independent_board_facts(expected["board"])
            assert candidate["holes_after"] == after["holes"], message
            assert candidate["holes_delta"] == after["holes"] - facts["before"]["holes"], message
            for field in ("max_height", "total_height", "bumpiness"):
                assert candidate[field] == after[field], (message, field)
            clear_counts.add(candidate["lines_cleared"])
            topouts += candidate["top_out"]
    assert reasons == {"outside_horizontal_bounds", "outside_vertical_bounds", "start_pose_collision"}
    assert clear_counts.issuperset({0, 1, 2, 3, 4})
    assert topouts > 0


def test_current_frame_y_is_preserved_and_no_path_claim_is_made(native_cases):
    case = next(row for row in native_cases if row["name"] == "low-T")
    facts = build_landing_projections(case["state"])
    assert facts["assumptions"]["active_origin_y"] == 18
    assert next(c for c in facts["candidates"] if c["id"] == "target:3:1")["pose_valid"] is False
    assert "not_proven" in LANDING_CONTRACT
    assert "40" in LANDING_CONTRACT and "不会代选" in LANDING_CONTRACT
    assert "no timed-input" in facts["assumptions"]["reachability"]


def test_line_level_is_before_clearing_and_spawn_rows_are_cleared(native_cases):
    well = next(row for row in native_cases if row["name"] == "four-line-well-I")
    candidates = build_landing_projections(well["state"])["candidates"]
    clear_four = next(c for c in candidates if c["id"] == "target:4:1")
    assert clear_four["lines_cleared"] == 4 and clear_four["points_delta"] == 800
    second = next(row for row in native_cases if row["name"] == "one-line-level-two")
    clear_one = next(c for c in build_landing_projections(second["state"])["candidates"] if c["id"] == "target:3:0")
    assert clear_one["lines_cleared"] == 1 and clear_one["points_delta"] == 200
    hidden = next(row for row in native_cases if row["name"] == "clear-hidden-row")
    clear_hidden = next(c for c in build_landing_projections(hidden["state"])["candidates"] if c["id"] == "target:3:0")
    assert clear_hidden["lines_cleared"] == 2
    assert clear_hidden["landing_cells"][0]["y"] == -1
    assert clear_hidden["top_out"] is False


def test_private_fields_and_row_order_cannot_affect_output_and_no_alias(native_cases):
    source = deepcopy(native_cases[0]["state"])
    before = deepcopy(source)
    facts = build_landing_projections(source)
    assert source == before
    source.update(seed=999, rng=123, private_state={"next": ["O"] * 100}, candidates=[{"id": "hold:100"}])
    source["observation"].update(rng=555, next=["L"] * 100)
    source["observation"]["board"]["rows"].reverse()
    assert build_landing_projections(source) == facts
    facts["candidates"][0]["landing_cells"][0]["x"] = 99
    assert build_landing_projections(source)["candidates"][0]["landing_cells"][0]["x"] != 99
    serialized = json.dumps(facts)
    for prohibited in ('"rng"', '"seed"', '"recommendation"', '"best_action"', '"path"', '"actions"'):
        assert prohibited not in serialized


@pytest.mark.parametrize("change,match", [
    (lambda s: s["observation"]["active"]["cells"].__setitem__(0, {"x": 8, "y": -1}), "native shape"),
    (lambda s: s["observation"]["score"].__setitem__("lines", True), "cumulative line"),
    (lambda s: s["observation"]["score"].__setitem__("lines", -1), "cumulative line"),
    (lambda s: s["observation"].__setitem__("score", {}), "cumulative line"),
    (lambda s: s["observation"]["board"]["rows"].pop(), "22 rows"),
    (lambda s: s["observation"].__setitem__("status", "finished"), "currently playing"),
])
def test_malformed_or_inconsistent_observation_fails_closed(native_cases, change, match):
    source = deepcopy(native_cases[0]["state"])
    change(source)
    with pytest.raises(ValueError, match=match):
        build_landing_projections(source)
