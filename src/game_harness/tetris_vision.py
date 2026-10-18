"""Deterministic PNG of the same public Tetris observation given as text.

This is a rendering adapter, not a policy. It draws occupied cells at their
actual coordinates, the five public previews and hold slot. The two spawn rows
are already part of ``observation.board`` and are labelled separately. It never
reads a seed, hidden queue, candidate, goal memory or derived placement score;
there is no ghost, landing simulation, recommended action or selected target.
"""
from __future__ import annotations

from io import BytesIO

from PIL import Image, ImageDraw, ImageFont


VISION_RENDERER_VERSION = "tetris-public-observation-png/v1"
IMAGE_WIDTH, IMAGE_HEIGHT = 448, 768
CELL_SIZE = 24
BOARD_X, BOARD_Y = 36, 104
BOARD_WIDTH, BOARD_HEIGHT = 10, 22
PALETTE = {
    "I": "#68CDD4", "O": "#E3C66B", "T": "#B79DE4", "S": "#A9D184",
    "Z": "#D78A83", "J": "#80A9DB", "L": "#E3AE74",
}
EMPTY_COLOR = "#101E2B"
SPAWN_COLOR = "#262339"
ACTIVE_OUTLINE = "#FFFFFF"
LOCKED_OUTLINE = "#405364"
_SHAPES = {
    "I": ((0, 1), (1, 1), (2, 1), (3, 1)),
    "O": ((1, 0), (2, 0), (1, 1), (2, 1)),
    "T": ((1, 0), (0, 1), (1, 1), (2, 1)),
    "S": ((1, 0), (2, 0), (0, 1), (1, 1)),
    "Z": ((0, 0), (1, 0), (1, 1), (2, 1)),
    "J": ((0, 0), (0, 1), (1, 1), (2, 1)),
    "L": ((2, 0), (0, 1), (1, 1), (2, 1)),
}


def _invalid(message):
    raise ValueError(f"Invalid public Tetris image input: {message}")


def _integer(value, low, high):
    return type(value) is int and low <= value <= high


def _public_view(state):
    """Copy only display fields; extra fields cannot change the PNG bytes."""
    if not isinstance(state, dict) or state.get("game_id") != "tetris":
        _invalid("game_id must be tetris")
    observation = state.get("observation")
    if not isinstance(observation, dict) or observation.get("gameId") != "tetris":
        _invalid("observation.gameId must be tetris")
    for key, expected in (("width", 10), ("height", 20), ("hiddenRows", 2)):
        if type(observation.get(key)) is not int or observation[key] != expected:
            _invalid(f"{key} must be {expected}")
    status = observation.get("status")
    if status not in ("playing", "finished"):
        _invalid("unknown status")
    board = observation.get("board")
    rows = board.get("rows") if isinstance(board, dict) else None
    if not isinstance(rows, list) or len(rows) != 22:
        _invalid("all 22 board rows are required")
    by_y = {}
    for row in rows:
        if not isinstance(row, dict) or not _integer(row.get("y"), -2, 19):
            _invalid("row y must be an integer in -2..19")
        y, cells = row["y"], row.get("cells")
        if y in by_y:
            _invalid("duplicate row y")
        if not isinstance(cells, str) or len(cells) != 10 or set(cells) - (PALETTE.keys() | {"."}):
            _invalid("rows must contain ten .IOTSZJL cells")
        by_y[y] = cells
    if set(by_y) != set(range(-2, 20)):
        _invalid("rows must cover -2..19")
    if "active" not in observation:
        _invalid("active is required, even when null")
    active = observation["active"]
    if active is None:
        if status == "playing":
            _invalid("playing observation requires active")
    else:
        if (not isinstance(active, dict) or not isinstance(active.get("type"), str)
                or active["type"] not in PALETTE):
            _invalid("unknown active piece")
        if not _integer(active.get("rotation"), 0, 3):
            _invalid("active rotation must be 0..3")
        raw_cells = active.get("cells")
        if not isinstance(raw_cells, list) or len(raw_cells) != 4:
            _invalid("active requires four actual cells")
        cells = set()
        for cell in raw_cells:
            if (not isinstance(cell, dict) or not _integer(cell.get("x"), 0, 9)
                    or not _integer(cell.get("y"), -2, 19)):
                _invalid("active cells must lie within the observed board")
            cells.add((cell["x"], cell["y"]))
        if len(cells) != 4:
            _invalid("active cells must be distinct")
        if status == "playing" and any(by_y[y][x] != "." for x, y in cells):
            _invalid("active overlaps locked cells")
        active = {"type": active["type"], "rotation": active["rotation"],
                  "cells": sorted(cells, key=lambda c: (c[1], c[0]))}
    preview = observation.get("next")
    if not isinstance(preview, list) or len(preview) < 5:
        _invalid("five public next pieces are required")
    # Never inspect or render anything past the engine's five public previews.
    preview = preview[:5]
    if any(not isinstance(piece, str) or piece not in PALETTE for piece in preview):
        _invalid("unknown public preview piece")
    if "hold" not in observation:
        _invalid("hold field is required")
    hold = observation["hold"]
    if hold is not None and (not isinstance(hold, str) or hold not in PALETTE):
        _invalid("unknown hold piece")
    if type(observation.get("holdAvailable")) is not bool:
        _invalid("holdAvailable must be boolean")
    score = observation.get("score")
    if (not isinstance(score, dict) or not _integer(score.get("points"), 0, 16594560000)
            or not _integer(score.get("lines"), 0, 28800)):
        _invalid("score must contain nonnegative native points and lines")
    return {"rows": by_y, "active": active, "next": preview, "hold": hold,
            "hold_available": observation["holdAvailable"], "status": status,
            "points": score["points"], "lines": score["lines"]}


def render_tetris_png(state: dict) -> bytes:
    """Render public observation as a metadata-free, fixed-size RGB PNG.

    Input may contain ``tetris_features`` and ``decision_goal_memory``; these
    are intentionally ignored. Missing or malformed display data is an error,
    never a fabricated empty board. Fonts are bundled with Pillow, not loaded
    from a host-specific system path. Identical display fields produce identical
    bytes within the pinned rendering dependency version.
    """
    view = _public_view(state)
    image = Image.new("RGB", (IMAGE_WIDTH, IMAGE_HEIGHT), "#0B1420")
    draw = ImageDraw.Draw(image)
    fonts = {size: ImageFont.load_default(size=size) for size in (12, 14, 16, 20, 24)}

    def text(x, y, value, size=14, fill="#DFE9F4", anchor=None):
        draw.text((x, y), value, fill=fill, font=fonts[size], anchor=anchor)

    def block(x, y, piece, active=False, cell=CELL_SIZE):
        draw.rectangle((x + 1, y + 1, x + cell - 2, y + cell - 2), fill=PALETTE[piece],
                       outline=ACTIVE_OUTLINE if active else LOCKED_OUTLINE, width=3 if active else 1)

    def preview(piece, x, y):
        if piece is None:
            text(x, y + 5, "EMPTY", size=12, fill="#8998AA")
            return
        shape = _SHAPES[piece]
        min_x, min_y = min(p[0] for p in shape), min(p[1] for p in shape)
        for px, py in shape:
            block(x + (px - min_x) * 14, y + (py - min_y) * 14, piece, cell=14)

    text(24, 22, "TETRIS | PUBLIC OBSERVATION", size=20)
    text(24, 54, "x right; y down. White outline = active.", size=14)
    text(BOARD_X - 21, BOARD_Y - 25, "y", size=14)
    for x in range(10):
        text(BOARD_X + x * CELL_SIZE + CELL_SIZE / 2, BOARD_Y - 17, str(x), size=14, anchor="mm")
    for y in range(-2, 20):
        py = BOARD_Y + (y + 2) * CELL_SIZE
        text(BOARD_X - 12, py + CELL_SIZE / 2, str(y), size=14, anchor="rm")
        for x in range(10):
            px = BOARD_X + x * CELL_SIZE
            draw.rectangle((px, py, px + CELL_SIZE - 1, py + CELL_SIZE - 1),
                           fill=SPAWN_COLOR if y < 0 else EMPTY_COLOR, outline="#293B4D", width=1)
            if view["rows"][y][x] != ".":
                block(px, py, view["rows"][y][x])
    active = view["active"]
    if active is not None:
        for x, y in active["cells"]:
            block(BOARD_X + x * CELL_SIZE, BOARD_Y + (y + 2) * CELL_SIZE, active["type"], active=True)
    draw.rectangle((BOARD_X - 1, BOARD_Y - 1, BOARD_X + 10 * CELL_SIZE,
                    BOARD_Y + 22 * CELL_SIZE), outline="#7A8C9E", width=1)
    draw.line((BOARD_X, BOARD_Y + 2 * CELL_SIZE, BOARD_X + 10 * CELL_SIZE,
               BOARD_Y + 2 * CELL_SIZE), fill="#C4B0ED", width=2)
    side_x = 296
    text(side_x, 104, "SPAWN: -2, -1", size=12, fill="#C4B0ED")
    text(side_x, 123, "Visible: y=0..19", size=12, fill="#A5B7C9")
    text(side_x, 160, "ACTIVE", size=14, fill="#A5B7C9")
    text(side_x, 182, "NONE" if active is None else f"{active['type']}  rotation {active['rotation']}", size=16)
    text(side_x, 222, "NEXT (in order)", size=14)
    for index, piece in enumerate(view["next"]):
        y = 251 + index * 39
        text(side_x, y + 5, f"{index + 1}. {piece}", size=14)
        preview(piece, side_x + 45, y)
    text(side_x, 465, "HOLD", size=14)
    preview(view["hold"], side_x + 45, 491)
    text(side_x, 496, view["hold"] or "-", size=16)
    text(side_x, 529, "Available" if view["hold_available"] else "Used this piece", size=12,
         fill="#A5B7C9")
    text(side_x, 570, "POINTS", size=12, fill="#A5B7C9")
    text(side_x, 590, str(view["points"]), size=16)
    text(side_x, 619, f"LINES  {view['lines']}", size=14)
    block(36, 657, "T", active=True, cell=20)
    text(65, 660, "Active", size=14)
    block(156, 657, "J", cell=20)
    text(185, 660, "Locked", size=14)
    draw.rectangle((276, 658, 293, 675), fill=EMPTY_COLOR, outline="#405364")
    text(304, 660, "Empty", size=14)
    text(36, 700, view["status"].upper(), size=14, fill="#A5B7C9")
    text(36, 727, "Actual cells only. No ghost or target overlay.", size=12, fill="#8998AA")
    output = BytesIO()
    image.save(output, format="PNG", optimize=False, compress_level=9)
    return output.getvalue()
