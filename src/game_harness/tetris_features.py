"""Current-board facts derived only from a public Tetris snapshot.

This module never opens a game, advances physics, enumerates placements, scores
an action, or reads a random seed / future queue. It counts the locked cells
already present in ``observation.board.rows`` and describes the active piece's
current cells. Missing or inconsistent required inputs raise ``ValueError``;
there is no fallback to the cropped render or an assumed empty board.
"""
from __future__ import annotations


FEATURE_VERSION = "tetris-visible-facts/v1"
_PIECES = frozenset("IOTSZJL")
_WIDTH, _HEIGHT, _HIDDEN_ROWS = 10, 20, 2


def _integer(value, lower: int, upper: int) -> bool:
    return type(value) is int and lower <= value <= upper


def _invalid(reason: str):
    raise ValueError(f"Invalid public Tetris snapshot: {reason}")


def build_tetris_features(snapshot: dict) -> dict:
    """Return JSON-compatible facts without changing or retaining ``snapshot``.

    ``height`` measures from y=19 to the top locked cell, inclusive; it is not
    the number of occupied cells. A hole is an empty cell with at least one
    locked cell above it in the same column, including the two spawn rows.
    Active cells are excluded from all locked-board counts and hole detection.
    """
    if not isinstance(snapshot, dict) or snapshot.get("game_id") != "tetris":
        _invalid("game_id must be tetris")
    observation = snapshot.get("observation")
    if not isinstance(observation, dict) or observation.get("gameId") != "tetris":
        _invalid("observation.gameId must be tetris")
    for name, expected in (("width", _WIDTH), ("height", _HEIGHT), ("hiddenRows", _HIDDEN_ROWS)):
        if type(observation.get(name)) is not int or observation[name] != expected:
            _invalid(f"{name} must be {expected}")
    if observation.get("status") not in ("playing", "finished"):
        _invalid("status must be playing or finished")
    if not _integer(observation.get("pieceId"), 1, 20000):
        _invalid("pieceId must be a positive rule-valid integer")
    if type(observation.get("grounded")) is not bool:
        _invalid("grounded must be boolean")
    board = observation.get("board")
    if (not isinstance(board, dict) or board.get("columns") != list(range(_WIDTH))
            or any(type(x) is not int for x in board["columns"])):
        _invalid("board.columns must be 0..9")
    rows = board.get("rows")
    if not isinstance(rows, list) or len(rows) != _HEIGHT + _HIDDEN_ROWS:
        _invalid("board.rows must include all 22 rows")
    by_y = {}
    for row in rows:
        if not isinstance(row, dict) or not _integer(row.get("y"), -_HIDDEN_ROWS, _HEIGHT - 1):
            _invalid("each row requires an integer y in -2..19")
        y, cells = row["y"], row.get("cells")
        if y in by_y:
            _invalid("duplicate row y")
        if not isinstance(cells, str) or len(cells) != _WIDTH or set(cells) - (_PIECES | {"."}):
            _invalid("row cells must be exactly 10 characters from .IOTSZJL")
        by_y[y] = cells
    if set(by_y) != set(range(-_HIDDEN_ROWS, _HEIGHT)):
        _invalid("board rows must cover y=-2..19 exactly")

    columns = []
    for x in range(_WIDTH):
        occupied_y = [y for y in range(-_HIDDEN_ROWS, _HEIGHT) if by_y[y][x] != "."]
        top = occupied_y[0] if occupied_y else None
        holes = [] if top is None else [y for y in range(top + 1, _HEIGHT) if by_y[y][x] == "."]
        columns.append({
            "x": x, "top_occupied_y": top, "height": 0 if top is None else _HEIGHT - top,
            "occupied_count": len(occupied_y), "hole_ys": holes, "hole_count": len(holes),
        })
    row_facts = [
        {"y": y, "occupied_count": _WIDTH - by_y[y].count("."),
         "missing_columns": [x for x, cell in enumerate(by_y[y]) if cell == "."]}
        for y in range(-_HIDDEN_ROWS, _HEIGHT)
    ]

    if "active" not in observation:
        _invalid("active field is required, even when null")
    raw_active = observation["active"]
    active = None
    if raw_active is None:
        if observation["status"] == "playing" or observation["grounded"]:
            _invalid("null active is inconsistent with playing or grounded")
    else:
        if (not isinstance(raw_active, dict) or not isinstance(raw_active.get("type"), str)
                or raw_active["type"] not in _PIECES):
            _invalid("active requires a valid tetromino type")
        if not _integer(raw_active.get("rotation"), 0, 3):
            _invalid("active rotation must be 0..3")
        if not _integer(raw_active.get("x"), -3, 9) or not _integer(raw_active.get("y"), -2, 19):
            _invalid("active origin must be rule-valid integer coordinates")
        cells = raw_active.get("cells")
        if not isinstance(cells, list) or len(cells) != 4:
            _invalid("active requires four actual cells")
        coordinates = set()
        for cell in cells:
            if not isinstance(cell, dict) or not _integer(cell.get("x"), 0, 9) or not _integer(cell.get("y"), -2, 19):
                _invalid("active cells must be integer coordinates within the full board")
            coordinates.add((cell["x"], cell["y"]))
        if len(coordinates) != 4:
            _invalid("active cells must be distinct")
        if observation["status"] == "playing" and any(by_y[y][x] != "." for x, y in coordinates):
            _invalid("playing active cells overlap locked cells")
        # Inspect only the immediately adjacent cell beneath the current piece's
        # exposed lower edges. This is contact geometry, not a simulated move.
        below_edges = []
        for x, y in sorted(coordinates, key=lambda point: (point[1], point[0])):
            if (x, y + 1) in coordinates:
                continue
            kind = "floor" if y == _HEIGHT - 1 else "empty" if by_y[y + 1][x] == "." else "locked"
            below_edges.append({"x": x, "active_y": y, "below_y": y + 1, "kind": kind})
        has_contact = any(edge["kind"] != "empty" for edge in below_edges)
        if observation["status"] == "playing" and has_contact != observation["grounded"]:
            _invalid("grounded disagrees with the current visible contact geometry")
        xs, ys = [x for x, _ in coordinates], [y for _, y in coordinates]
        active = {
            "piece_id": observation["pieceId"], "type": raw_active["type"],
            "rotation": raw_active["rotation"],
            "origin": {"x": raw_active["x"], "y": raw_active["y"]},
            "cells": [{"x": x, "y": y} for x, y in sorted(coordinates, key=lambda point: (point[1], point[0]))],
            "bounds": {"min_x": min(xs), "max_x": max(xs), "min_y": min(ys), "max_y": max(ys)},
            "grounded": observation["grounded"], "below_exposed_edges": below_edges,
        }
    return {
        "schema_version": FEATURE_VERSION,
        "coordinates": {"x_min": 0, "x_max": 9, "y_min": -2, "y_max": 19,
                        "visible_y_min": 0, "x_direction": "right", "y_direction": "down"},
        "definitions": {
            "board": "Locked cells only; active cells are excluded from columns, rows and holes.",
            "height": "0 for an empty column; otherwise 20 - top_occupied_y, including spawn rows (maximum 22). Not occupied_count.",
            "hole": "An empty cell with at least one locked cell above it in the same column, within y=-2..19. Does not imply that it is unreachable from the side.",
            "adjacent_height_difference": "height of right column minus height of left column; signed, no score or ranking.",
            "below_exposed_edges": "Cell immediately below each active cell with no active cell below it: empty, locked, or floor at y=20. Current contact only, no landing prediction.",
            "rotation": "0/1/2/3 are the current spawn-relative clockwise quarter turns. Origin is the local shape frame, not its leftmost occupied cell.",
        },
        "columns": columns, "rows": row_facts,
        "adjacent_height_differences": [
            {"left_x": x, "right_x": x + 1, "right_minus_left": columns[x + 1]["height"] - columns[x]["height"]}
            for x in range(_WIDTH - 1)
        ],
        "total_locked_cells": sum(column["occupied_count"] for column in columns),
        "total_holes": sum(column["hole_count"] for column in columns),
        "active": active,
    }
