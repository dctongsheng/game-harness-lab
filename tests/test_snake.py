import unittest

from game_harness.snake import ACTION_IDS, RULES_VERSION, SnakeGame, candidate_hash


class SnakeGameTests(unittest.TestCase):
    def test_seeded_food_and_replay_are_repeatable(self):
        left, right = SnakeGame(seed=73), SnakeGame(seed=73)
        self.assertEqual(left.snapshot(), right.snapshot())
        for action in ("up", "left", "down", "right", "up"):
            self.assertEqual(left.step(action), right.step(action))
        self.assertEqual(left.snapshot()["rules_version"], RULES_VERSION)

    def test_snapshots_are_detached(self):
        game = SnakeGame()
        state = game.snapshot()
        state["snake"][0][0] = 900
        state["food"][0] = 900
        self.assertNotEqual(game.snapshot()["snake"][0][0], 900)
        self.assertNotEqual(game.snapshot()["food"][0], 900)
        self.assertEqual(game.step_count, 0)

    def test_eating_grows_and_counts_raw_food(self):
        game = SnakeGame()
        game.food = (5, 4)
        after = game.step("right")
        self.assertEqual(after["score"], 1)
        self.assertEqual(len(after["snake"]), 4)
        self.assertNotIn(after["food"], after["snake"])

    def test_reverse_input_keeps_current_direction(self):
        game = SnakeGame()
        game.food = (0, 0)
        after = game.step("left")
        self.assertEqual(after["direction"], "right")
        self.assertEqual(after["snake"][0], [5, 4])

    def test_dangerous_wall_action_is_executed(self):
        game = SnakeGame(size=4)
        game.food = (0, 0)
        game.step("right")
        after = game.step("right")
        self.assertEqual(after["reason"], "wall_collision")
        self.assertEqual(after["step"], 2)
        self.assertEqual(after["score"], 0)
        with self.assertRaises(RuntimeError):
            game.step("up")

    def test_self_collision_is_not_masked(self):
        game = SnakeGame()
        game.snake = [(2, 2), (2, 3), (3, 3), (3, 2), (4, 2)]
        game.direction = "up"
        game.food = (0, 0)
        self.assertEqual(game.step("right")["reason"], "self_collision")

    def test_vacating_tail_can_be_entered(self):
        game = SnakeGame()
        game.snake = [(1, 1), (1, 2), (2, 2), (2, 1)]
        game.direction = "up"
        game.food = (0, 0)
        after = game.step("right")
        self.assertFalse(after["done"])
        self.assertEqual(after["snake"], [[2, 1], [1, 1], [1, 2], [2, 2]])

    def test_full_board_finishes_without_food_sampling_error(self):
        game = SnakeGame(size=4)
        game.snake = [(2, 3), (2, 2), (3, 2), (3, 1), (3, 0), (2, 0), (2, 1),
                      (1, 1), (1, 0), (0, 0), (0, 1), (0, 2), (1, 2), (1, 3), (0, 3)]
        game.direction = "down"
        game.food = (3, 3)
        game.score = 12
        after = game.step("right")
        self.assertEqual(after["reason"], "board_filled")
        self.assertEqual(after["score"], 13)
        self.assertIsNone(after["food"])

    def test_limit_is_separate_from_collision(self):
        game = SnakeGame(max_steps=1)
        self.assertEqual(game.step("up")["reason"], "step_limit")

    def test_invalid_action_does_not_advance_or_get_replaced(self):
        game = SnakeGame()
        initial = game.snapshot()
        with self.assertRaises(ValueError):
            game.step("safest")
        self.assertEqual(game.snapshot(), initial)
        self.assertEqual(ACTION_IDS, ("up", "right", "down", "left"))
        self.assertEqual(len(candidate_hash()), 64)


if __name__ == "__main__":
    unittest.main()
