"""Conditional landing consequences from public observations, never a controller.

Shape coordinates and clockwise rotation follow the MIT Decision Arena snapshot
at ``games/vendor/arena-arcade/packages/games/src/arcade/tetris.ts`` (4e2bda1).
The vendored license and attribution are preserved alongside that source.
This independently expressed geometry is checked against the unmodified native
engine's cells, hard-drop, line-clear, points and top-out in the module tests.

All forty declared targets are returned in their original catalog order. A
collision-free assumed pose is NOT proof that real timed inputs can reach it.
There is no path search, candidate selection, recommendation or game execution.
"""
from __future__ import annotations

from .tetris_features import build_tetris_features


LANDING_VERSION = "tetris-conditional-landings/v1"
LANDING_CONTRACT = """可选目标后果合同：
tetris_landing_projections 仅用当前公开 observation，完整保留 target:列:朝向 的40项目标，按列0..9、朝向0..3固定顺序。
每项是假设当前块能立即到达指定朝向与最左占用列，且 active 的局部框原点 y 保持不变，再竖直硬降到碰撞前的位置。
pose_valid 只表示这一假设的起始姿态在棋盘内且不与已锁定格相撞；不等于能用真实按键到达。
invalid_reason 不为空时该假设不能投影，其后果字段为 null；程序仍不删除该目标。native_reachability 始终为 not_proven。
landing_cells 是假设硬降后、消行前的四格；lines_cleared、points_delta 与 top_out 按原生规则计算，包含两行生成区，积分按消行前等级。
top_out 仅表示锁定并消行后两行生成区仍有锁定格；未模拟下一块生成，因此 false 不是下一块可生成的保证。
holes_after、holes_delta、max_height、total_height、bumpiness 均针对假设消行后的已锁定棋盘；holes_before 是当前已锁定棋盘空洞数。
空洞是同列上方已有锁定格的空格，包含生成区；列高是20减去最高锁定格y，空列为0；bumpiness是相邻列高差绝对值之和。
这不是动作评分、路径或推荐；未模拟到达目标所需的横移、SRS踢墙、按键时长、重力、锁定倒计时、hold或后续块。
目标选择和每一次实际游戏按键仍须分别由Decision决定；程序不会代选、代执行，也不保证目标安全或可达。
"""

# Spawn-relative coordinates and frame sizes exactly match the MIT native
# source. O keeps its occupied cells in every orientation; I uses a 4x4 frame.
_SHAPES = {
    "I": ((0, 1), (1, 1), (2, 1), (3, 1)),
    "O": ((1, 0), (2, 0), (1, 1), (2, 1)),
    "T": ((1, 0), (0, 1), (1, 1), (2, 1)),
    "S": ((1, 0), (2, 0), (0, 1), (1, 1)),
    "Z": ((0, 0), (1, 0), (1, 1), (2, 1)),
    "J": ((0, 0), (0, 1), (1, 1), (2, 1)),
    "L": ((2, 0), (0, 1), (1, 1), (2, 1)),
}


def _offsets(piece: str, rotation: int) -> list[tuple[int, int]]:
    result = []
    for x, y in _SHAPES[piece]:
        if piece != "O":
            for _ in range(rotation):
                x, y = (3 if piece == "I" else 2) - y, x
        result.append((x, y))
    return result


def _cells(offsets, x, y):
    return [(x + dx, y + dy) for dx, dy in offsets]


def _invalid_pose(cells, board):
    if any(x < 0 or x >= 10 for x, _ in cells):
        return "outside_horizontal_bounds"
    if any(y < -2 or y >= 20 for _, y in cells):
        return "outside_vertical_bounds"
    if any(board[y + 2][x] != "." for x, y in cells):
        return "start_pose_collision"
    return None


def _board_facts(board):
    heights, holes = [], 0
    for x in range(10):
        occupied = [row for row in range(22) if board[row][x] != "."]
        top = occupied[0] if occupied else None
        heights.append(0 if top is None else 22 - top)
        if top is not None:
            holes += sum(board[row][x] == "." for row in range(top + 1, 22))
    return {"holes": holes, "max_height": max(heights), "total_height": sum(heights),
            "bumpiness": sum(abs(a - b) for a, b in zip(heights, heights[1:])),
            "column_heights": heights}


def build_landing_projections(state: dict) -> dict:
    """Describe every declared pose without mutating ``state`` or using secrets.

    The current board and piece must be complete and internally consistent. We
    deliberately do not read seed, RNG, private state, candidates or future queue.
    Validation failures raise rather than returning an assumed empty board.
    """
    features = build_tetris_features(state)
    observation = state["observation"]
    active = observation["active"]
    if observation["status"] != "playing" or active is None:
        raise ValueError("Conditional landings require a currently playing piece")
    actual_cells = {(cell["x"], cell["y"]) for cell in active["cells"]}
    native_cells = set(_cells(_offsets(active["type"], active["rotation"]), active["x"], active["y"]))
    if actual_cells != native_cells:
        raise ValueError("Active cells disagree with the native shape and rotation")
    score = observation.get("score")
    lines_before = score.get("lines") if isinstance(score, dict) else None
    if type(lines_before) is not int or not 0 <= lines_before <= 28800:
        raise ValueError("Conditional landing points require native cumulative line count")
    by_y = {row["y"]: row["cells"] for row in observation["board"]["rows"]}
    board = [by_y[y] for y in range(-2, 20)]
    before = _board_facts(board)
    # The shared fact implementation is independently useful validation that
    # hidden-row and hole definitions have not silently diverged.
    if before["holes"] != features["total_holes"]:
        raise ValueError("Locked-board hole definitions disagree")
    level = 1 + lines_before // 10
    candidates = []
    for column in range(10):
        for rotation in range(4):
            offsets = _offsets(active["type"], rotation)
            x, y = column - min(dx for dx, _ in offsets), active["y"]
            cells = _cells(offsets, x, y)
            reason = _invalid_pose(cells, board)
            candidate = {
                "id": f"target:{column}:{rotation}", "leftmost_column": column,
                "rotation": rotation, "pose_valid": reason is None, "invalid_reason": reason,
                "native_reachability": "not_proven", "landing_origin": None,
                "landing_cells": None, "lines_cleared": None, "points_delta": None,
                "holes_before": before["holes"], "holes_after": None, "holes_delta": None,
                "max_height": None, "total_height": None, "bumpiness": None, "top_out": None,
            }
            if reason is None:
                while _invalid_pose(_cells(offsets, x, y + 1), board) is None:
                    y += 1
                cells = _cells(offsets, x, y)
                locked = [list(row) for row in board]
                for cx, cy in cells:
                    locked[cy + 2][cx] = active["type"]
                remaining = [row for row in locked if "." in row]
                cleared = 22 - len(remaining)
                after_board = [["."] * 10 for _ in range(cleared)] + remaining
                after = _board_facts(after_board)
                candidate.update({
                    "landing_origin": {"x": x, "y": y},
                    "landing_cells": [{"x": cx, "y": cy}
                                      for cx, cy in sorted(cells, key=lambda cell: (cell[1], cell[0]))],
                    "lines_cleared": cleared,
                    "points_delta": ([0, 100, 300, 500, 800][cleared] if cleared <= 4 else 0) * level,
                    "holes_after": after["holes"], "holes_delta": after["holes"] - before["holes"],
                    "max_height": after["max_height"], "total_height": after["total_height"],
                    "bumpiness": after["bumpiness"],
                    "top_out": any(cell != "." for row in after_board[:2] for cell in row),
                })
            candidates.append(candidate)
    return {
        "schema": LANDING_VERSION,
        "assumptions": {
            "method": "conditional_vertical_hard_drop_from_assumed_pose",
            "active_origin_y": active["y"], "piece_type": active["type"], "level_before": level,
            "pose": "Assume target rotation and leftmost occupied column can be attained instantly; retain current active frame origin y.",
            "reachability": "not_proven: no timed-input, gravity, lock-delay, SRS-path or hold simulation",
            "cells": "landing_cells are the four pre-clear locked cells, including exposed spawn rows -2 and -1",
            "top_out": "Locked cells remain in spawn rows after lock and line clear only; next-piece spawn is not simulated, so false does not guarantee spawning succeeds.",
            "board_metrics": "after line clear; heights include spawn rows; holes have a locked cell above in the same column",
            "ordering": "all 40 original targets; column 0..9 then rotation 0..3; no ranking or filtering",
        },
        "before": before, "candidates": candidates,
    }
