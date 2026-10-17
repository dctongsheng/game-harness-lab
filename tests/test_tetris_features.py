"""Read-only visible-board geometry, using real adapter observations as inputs."""
from copy import deepcopy
import json
import shutil

import pytest

from game_harness.games.tetris import TetrisAdapter
from game_harness.tetris_features import FEATURE_VERSION, build_tetris_features


@pytest.fixture
def snapshot():
    if not shutil.which("node"):
        pytest.skip("Real Tetris adapter requires Node.js")
    with TetrisAdapter().create(12, {"max_steps": 80}) as game:
        return game.snapshot()


def set_cell(snapshot, x, y, value="J"):
    row = snapshot["observation"]["board"]["rows"][y + 2]
    row["cells"] = row["cells"][:x] + value + row["cells"][x + 1:]


def keys(value):
    if isinstance(value, dict):
        for name, child in value.items():
            yield name
            yield from keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from keys(child)


def test_real_empty_board_counts_exclude_active_and_are_json_serializable(snapshot):
    facts = build_tetris_features(snapshot)
    assert facts["schema_version"] == FEATURE_VERSION
    assert facts["total_locked_cells"] == facts["total_holes"] == 0
    assert len(facts["columns"]) == 10 and len(facts["rows"]) == 22
    assert [row["y"] for row in facts["rows"]] == list(range(-2, 20))
    assert all(row["occupied_count"] == 0 and row["missing_columns"] == list(range(10)) for row in facts["rows"])
    assert all(column == {"x": x, "top_occupied_y": None, "height": 0,
                          "occupied_count": 0, "hole_ys": [], "hole_count": 0}
               for x, column in enumerate(facts["columns"]))
    active = facts["active"]
    assert active["type"] == "O" and active["piece_id"] == 1
    assert active["origin"] == {"x": 3, "y": -2}
    assert active["bounds"] == {"min_x": 4, "max_x": 5, "min_y": -2, "max_y": -1}
    assert active["grounded"] is False
    assert active["below_exposed_edges"] == [
        {"x": 4, "active_y": -1, "below_y": 0, "kind": "empty"},
        {"x": 5, "active_y": -1, "below_y": 0, "kind": "empty"},
    ]
    assert json.loads(json.dumps(facts)) == facts


def test_hidden_rows_height_occupied_counts_and_holes_are_distinct(snapshot):
    for x, y in [(0, -2), (0, 19), (1, 17), (1, 19), (2, 18)]:
        set_cell(snapshot, x, y)
    facts = build_tetris_features(snapshot)
    assert facts["columns"][0] == {"x": 0, "top_occupied_y": -2, "height": 22,
                                   "occupied_count": 2, "hole_ys": list(range(-1, 19)), "hole_count": 20}
    assert facts["columns"][1] == {"x": 1, "top_occupied_y": 17, "height": 3,
                                   "occupied_count": 2, "hole_ys": [18], "hole_count": 1}
    assert facts["columns"][2]["hole_ys"] == [19]
    assert facts["total_locked_cells"] == 5 and facts["total_holes"] == 22
    assert facts["rows"][0] == {"y": -2, "occupied_count": 1, "missing_columns": list(range(1, 10))}
    assert facts["rows"][-1] == {"y": 19, "occupied_count": 2, "missing_columns": list(range(2, 10))}
    assert [pair["right_minus_left"] for pair in facts["adjacent_height_differences"]] == [-19, -1, -2, 0, 0, 0, 0, 0, 0]


def test_holes_use_only_locked_cells_not_active_or_side_reachability(snapshot):
    # An overhang at x=0 has empty neighbors: the same-column definition still
    # counts y=19 as a hole. The spawned O creates no holes under its own cells.
    set_cell(snapshot, 0, 18)
    facts = build_tetris_features(snapshot)
    assert facts["columns"][0]["hole_ys"] == [19]
    assert facts["columns"][4]["hole_count"] == facts["columns"][5]["hole_count"] == 0


@pytest.mark.parametrize("contact", ["floor", "locked"])
def test_current_exposed_edges_report_contacts_without_predicting_placement(snapshot, contact):
    observation = snapshot["observation"]
    y = 18 if contact == "floor" else 16
    observation["active"].update(y=y, cells=[{"x": x, "y": yy} for yy in (y, y + 1) for x in (4, 5)])
    if contact == "locked":
        set_cell(snapshot, 4, 18)
    observation["grounded"] = True
    active = build_tetris_features(snapshot)["active"]
    assert active["grounded"] is True
    assert active["below_exposed_edges"] == [
        {"x": 4, "active_y": y + 1, "below_y": y + 2, "kind": contact},
        {"x": 5, "active_y": y + 1, "below_y": y + 2, "kind": "floor" if contact == "floor" else "empty"},
    ]


def test_input_and_output_do_not_alias_and_row_input_order_is_not_a_policy(snapshot):
    original = deepcopy(snapshot)
    first = build_tetris_features(snapshot)
    assert snapshot == original
    snapshot["observation"]["board"]["rows"].reverse()
    assert build_tetris_features(snapshot) == first
    first["active"]["cells"][0]["x"] = 99
    first["columns"][0]["hole_ys"].append(19)
    assert snapshot["observation"]["active"] == original["observation"]["active"]
    assert build_tetris_features(snapshot)["columns"][0]["hole_ys"] == []


def test_private_extra_fields_candidates_and_render_cannot_affect_features(snapshot):
    first = build_tetris_features(snapshot)
    snapshot.update(seed=12345, rng=9000, private_state={"next": ["I"] * 100},
                    candidates=[{"id": "hard-drop:100", "rank": 1}], render={"cells": [7] * 200})
    snapshot["observation"].update(rng=19, seed="private", next=["L"] * 100)
    assert build_tetris_features(snapshot) == first
    prohibited = {"seed", "rng", "next", "private_state", "candidates", "action", "actions", "rank",
                  "score", "best_action", "recommended_action", "landing", "landing_positions"}
    assert not prohibited.intersection(keys(first))


@pytest.mark.parametrize("mutation, message", [
    (lambda s: s.pop("observation"), "observation.gameId"),
    (lambda s: s["observation"].pop("active"), "active field"),
    (lambda s: s["observation"].pop("grounded"), "grounded must"),
    (lambda s: s["observation"].update(width=True), "width must"),
    (lambda s: s["observation"]["board"].update(columns=[False, 1, 2, 3, 4, 5, 6, 7, 8, 9]), "board.columns"),
    (lambda s: s["observation"]["board"]["rows"].pop(0), "all 22"),
    (lambda s: s["observation"]["board"]["rows"][0].update(y=-1), "duplicate row"),
    (lambda s: s["observation"]["board"]["rows"][0].update(cells="0000000000"), "10 characters"),
    (lambda s: s["observation"]["active"].update(rotation=True), "rotation must"),
    (lambda s: s["observation"]["active"].update(cells=[{"x": 4, "y": -2}] * 4), "must be distinct"),
    (lambda s: s["observation"]["active"]["cells"][0].update(x=10), "within the full board"),
    (lambda s: s["observation"].update(grounded=True), "grounded disagrees"),
    (lambda s: set_cell(s, 4, -2), "overlap locked"),
    (lambda s: s["observation"].update(active=None), "null active"),
])
def test_missing_or_inconsistent_observation_fails_closed(snapshot, mutation, message):
    mutation(snapshot)
    with pytest.raises(ValueError, match=message):
        build_tetris_features(snapshot)


def test_finished_snapshot_may_have_no_active_piece(snapshot):
    snapshot["observation"].update(status="finished", active=None)
    assert build_tetris_features(snapshot)["active"] is None


@pytest.mark.skipif(not shutil.which("node"), reason="Real Tetris adapter requires Node.js")
def test_actual_adapter_actions_preserve_catalog_and_full_board_facts():
    # These are deterministic rule-fixture inputs, not evaluated model games.
    with TetrisAdapter().create(12, {"max_steps": 80}) as game:
        for action in ("rotate-cw:100", "left:300", "hard-drop:100", "hold:100", "soft-drop:1000"):
            before, catalog = game.snapshot(), game.candidates()
            build_tetris_features(before)
            assert game.snapshot() == before and game.candidates() == catalog
            after = game.step(action)
            facts = build_tetris_features(after)
            active = after["observation"]["active"]
            assert facts["active"]["cells"] == sorted(active["cells"], key=lambda p: (p["y"], p["x"]))
            assert facts["active"]["rotation"] == active["rotation"]
            assert facts["total_locked_cells"] == sum(
                char != "." for row in after["observation"]["board"]["rows"] for char in row["cells"])
