"""Check public information boundaries and geometric fidelity of vision input."""
from copy import deepcopy
from io import BytesIO
import shutil

import pytest

pytest.importorskip('PIL', reason='Install .[vision] to run visual tests')
from PIL import Image, ImageColor

from game_harness.games.tetris import TetrisAdapter
from game_harness.tetris_vision import (
    ACTIVE_OUTLINE, BOARD_X, BOARD_Y, CELL_SIZE, EMPTY_COLOR, IMAGE_HEIGHT,
    IMAGE_WIDTH, LOCKED_OUTLINE, PALETTE, SPAWN_COLOR, render_tetris_png,
)


@pytest.fixture
def game():
    if not shutil.which("node"):
        pytest.skip("Real Tetris observations require Node.js")
    with TetrisAdapter().create(77, {"max_steps": 80}) as session:
        yield session


def image(state):
    return Image.open(BytesIO(render_tetris_png(state)))


def cell_pixel(png, x, y, offset=12):
    return png.getpixel((BOARD_X + x * CELL_SIZE + offset, BOARD_Y + (y + 2) * CELL_SIZE + offset))


def rgb(color):
    return ImageColor.getrgb(color)


def assert_board_matches(png, state):
    observation = state["observation"]
    active = observation["active"]
    active_cells = set() if active is None else {(p["x"], p["y"]) for p in active["cells"]}
    for row in observation["board"]["rows"]:
        y = row["y"]
        for x, piece in enumerate(row["cells"]):
            if (x, y) in active_cells:
                assert cell_pixel(png, x, y) == rgb(PALETTE[active["type"]])
                assert cell_pixel(png, x, y, 2) == rgb(ACTIVE_OUTLINE)
            elif piece != ".":
                assert cell_pixel(png, x, y) == rgb(PALETTE[piece])
                assert cell_pixel(png, x, y, 1) == rgb(LOCKED_OUTLINE)
            else:
                assert cell_pixel(png, x, y) == rgb(SPAWN_COLOR if y < 0 else EMPTY_COLOR)


def test_real_initial_board_includes_public_spawn_cells_and_no_metadata(game):
    snapshot = game.snapshot()
    untouched = deepcopy(snapshot)
    png = image(snapshot)
    assert png.format == "PNG" and png.mode == "RGB"
    assert png.size == (IMAGE_WIDTH, IMAGE_HEIGHT) == (448, 768)
    assert png.info == {}
    assert snapshot == untouched
    assert render_tetris_png(snapshot) == render_tetris_png(untouched)
    assert_board_matches(png, snapshot)
    assert snapshot["observation"]["active"]["type"] == "T"


def test_rotation_locking_and_hold_follow_real_observation(game):
    previous = render_tetris_png(game.snapshot())
    for action in ("rotate-cw:100", "left:100", "hard-drop:100", "hold:100", "hard-drop:100"):
        state = game.step(action)
        current = render_tetris_png(state)
        assert current != previous
        assert_board_matches(Image.open(BytesIO(current)), state)
        previous = current
    assert any(set(row["cells"]) != {"."} for row in state["observation"]["board"]["rows"])
    assert state["observation"]["hold"] is not None


def test_extra_fields_goal_memory_and_future_queue_do_not_change_image(game):
    state = game.snapshot()
    expected = render_tetris_png(state)
    state.update(episode_id="secret-episode", seed=8877, rng=44,
                 receipt={"choice_id": "hard-drop:100"},
                 render={"cells": [7] * 200},
                 candidates=[{"id": "left:100", "rank": 1}],
                 tetris_features={"best_action": "left:100"},
                 decision_goal_memory={"target": {"leftmost_column": 0, "rotation": 2}})
    state["observation"].update(seed="private", rng=5, future_queue=["I"] * 100,
                                ghost=[{"x": 0, "y": 19}], target={"x": 7},
                                recommended_action="hard-drop:100")
    state["observation"]["next"] += ["NOT_A_PUBLIC_PIECE"] * 20
    state["observation"]["board"]["private"] = "unread"
    assert render_tetris_png(state) == expected


@pytest.mark.parametrize("mutate", [
    *[(lambda s, index=index: s["observation"]["next"].__setitem__(index, "Z")) for index in range(5)],
    lambda s: s["observation"].update(hold="I"),
    lambda s: s["observation"].update(holdAvailable=False),
    lambda s: s["observation"]["score"].update(points=100),
    lambda s: s["observation"]["score"].update(lines=1),
])
def test_each_public_preview_hold_and_score_is_visible_but_not_on_board(game, mutate):
    state = game.snapshot()
    initial = image(state)
    changed = deepcopy(state)
    mutate(changed)
    updated = image(changed)
    assert initial.tobytes() != updated.tobytes()
    assert initial.crop((BOARD_X, BOARD_Y, BOARD_X + 240, BOARD_Y + 528)).tobytes() == updated.crop(
        (BOARD_X, BOARD_Y, BOARD_X + 240, BOARD_Y + 528)).tobytes()


def test_same_cells_different_input_order_does_not_change_bytes(game):
    state = game.snapshot()
    original = render_tetris_png(state)
    state["observation"]["board"]["rows"].reverse()
    state["observation"]["active"]["cells"].reverse()
    assert render_tetris_png(state) == original


def test_finished_without_active_renders_only_remaining_locked_board(game):
    state = game.step("hard-drop:100")
    state["observation"].update(status="finished", active=None)
    assert_board_matches(image(state), state)


@pytest.mark.parametrize("mutate, message", [
    (lambda s: s.update(game_id="snake"), "game_id"),
    (lambda s: s.pop("observation"), "observation.gameId"),
    (lambda s: s["observation"].update(width=True), "width"),
    (lambda s: s["observation"]["board"]["rows"].pop(), "22 board rows"),
    (lambda s: s["observation"]["board"]["rows"][0].update(cells="??????????"), "ten"),
    (lambda s: s["observation"]["board"]["rows"][0].update(y=-1), "duplicate"),
    (lambda s: s["observation"].pop("active"), "active is required"),
    (lambda s: s["observation"].update(active=None), "requires active"),
    (lambda s: s["observation"]["active"]["cells"][0].update(x=10), "within"),
    (lambda s: s["observation"]["active"].update(type=[]), "unknown active"),
    (lambda s: s["observation"]["active"].update(rotation=True), "rotation"),
    (lambda s: s["observation"].update(next=["I"]), "five public"),
    (lambda s: s["observation"].update(hold="X"), "hold piece"),
    (lambda s: s["observation"].update(holdAvailable=1), "boolean"),
    (lambda s: s["observation"]["score"].update(points=float("inf")), "score"),
])
def test_malformed_observation_fails_closed(game, mutate, message):
    snapshot = game.snapshot()
    mutate(snapshot)
    with pytest.raises(ValueError, match=message):
        render_tetris_png(snapshot)
