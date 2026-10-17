"""Isolated JSONL process for the pinned native Overcooked-AI transition rules.

No policy, planner, path search, network client, generated code or fallback lives
here. Two complete native primitives from Decision are applied atomically.
"""
from __future__ import annotations

from contextlib import redirect_stdout
from copy import deepcopy
import importlib.metadata
import json
import sys

UPSTREAM_COMMIT = "739950a079cdaed5a44fcc662efc40244c205d06"
RULES_VERSION = "game-harness-lab/overcooked-joint-v1-739950a"
LAYOUTS = ("cramped_room", "asymmetric_advantages", "coordination_ring")
ACTIONS = {"north": (0, -1), "south": (0, 1), "east": (1, 0),
           "west": (-1, 0), "stay": (0, 0), "interact": "interact"}
LEGEND = {" ": "floor", "X": "counter", "O": "onion_dispenser", "T": "tomato_dispenser",
          "D": "dish_dispenser", "P": "pot", "S": "serving_location"}


class WorkerError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def verify_dependency():
    if sys.version_info[:2] != (3, 10):
        raise WorkerError("dependency")
    try:
        distribution = importlib.metadata.distribution("overcooked-ai")
        provenance = json.loads(distribution.read_text("direct_url.json") or "{}")
    except (importlib.metadata.PackageNotFoundError, ValueError):
        raise WorkerError("dependency")
    vcs = provenance.get("vcs_info", {})
    url = provenance.get("url", "").removesuffix(".git").removesuffix("/")
    if (vcs.get("vcs") != "git" or vcs.get("commit_id") != UPSTREAM_COMMIT
            or url != "https://github.com/HumanCompatibleAI/overcooked_ai"):
        raise WorkerError("provenance")


def public_object(obj):
    if obj is None:
        return None
    result = {"name": obj.name, "position": list(obj.position)}
    if obj.name == "soup":
        result.update({"ingredients": list(obj.ingredients), "is_cooking": bool(obj.is_cooking),
                       "is_ready": bool(obj.is_ready), "is_idle": bool(obj.is_idle),
                       "cook_time_remaining": None if obj.is_idle else int(obj.cook_time_remaining),
                       "cooking_tick": int(obj._cooking_tick)})
    return result


class NativeSession:
    def __init__(self, seed, max_steps, layout):
        if type(seed) is not int or not -(2**53 - 1) <= seed <= 2**53 - 1:
            raise WorkerError("config")
        if type(max_steps) is not int or not 2 <= max_steps <= 1000 or max_steps % 2:
            raise WorkerError("config")
        if layout not in LAYOUTS:
            raise WorkerError("config")
        verify_dependency()
        from overcooked_ai_py.mdp.overcooked_mdp import OvercookedGridworld
        self.mdp = OvercookedGridworld.from_layout_name(layout)
        self.state = self.mdp.get_standard_start_state().deepcopy()
        self.layout = layout
        self.max_steps = max_steps
        self.revision = 0
        self.score = 0
        self.delivery_counts = [0, 0]
        self.last_transition = None
        self.joint_history = []
        self._pending = None

    def snapshot(self):
        objects = [public_object(obj) for _, obj in sorted(self.state.objects.items())]
        pots = []
        for y, row in enumerate(self.mdp.terrain_mtx):
            for x, terrain in enumerate(row):
                if terrain == "P":
                    soup = self.state.objects.get((x, y))
                    pots.append({"position": [x, y], "capacity": 3,
                                 "ingredients": list(soup.ingredients) if soup else [],
                                 "is_cooking": bool(soup.is_cooking) if soup else False,
                                 "is_ready": bool(soup.is_ready) if soup else False,
                                 "cook_time_remaining": (None if soup.is_idle else int(soup.cook_time_remaining)) if soup else None})
        done = self.revision >= self.max_steps or self.mdp.is_terminal(self.state)
        return {
            "game_id": "overcooked", "rules_version": RULES_VERSION, "layout": self.layout,
            "step": self.revision, "max_steps": self.max_steps,
            "native_tick": int(self.state.timestep), "max_ticks": self.max_steps // 2,
            "actor": "chef_" + str(self.revision % 2), "acting_player": self.revision % 2,
            "policy_role": "candidate", "score": self.score,
            "score_kind": "native_sparse_reward", "deliveries": sum(self.delivery_counts),
            "delivery_counts": list(self.delivery_counts), "done": bool(done),
            "reason": "step_limit" if self.revision >= self.max_steps else "native_terminal" if done else None,
            "truncated": self.revision >= self.max_steps, "result": None,
            "rows": len(self.mdp.terrain_mtx), "cols": len(self.mdp.terrain_mtx[0]),
            "terrain": ["".join(row) for row in self.mdp.terrain_mtx], "terrain_legend": LEGEND.copy(),
            "players": [{"index": i, "position": list(p.position), "orientation": list(p.orientation),
                         "held_object": public_object(p.held_object)} for i, p in enumerate(self.state.players)],
            "objects": objects, "pots": pots,
            "orders": [{"ingredients": list(recipe.ingredients), "cook_ticks": int(recipe.time),
                        "native_reward": self.mdp.get_recipe_value(self.state, recipe),
                        "bonus": recipe in self.state.bonus_orders} for recipe in self.state.all_orders],
            "last_joint_actions": deepcopy(self.joint_history[-1]["actions"]) if self.joint_history else None,
            "last_transition": deepcopy(self.last_transition), "joint_history": deepcopy(self.joint_history),
            "trial_design": "fixed_layout_standard_start_repeated_trials_seed_is_identifier_only",
        }

    def step(self, action, expected_revision):
        if type(expected_revision) is not int or expected_revision != self.revision:
            raise WorkerError("revision")
        if not isinstance(action, str) or action not in ACTIONS:
            raise WorkerError("action")
        if self.snapshot()["done"]:
            raise WorkerError("done")
        if self.revision % 2 == 0:
            self._pending = action
            self.revision += 1
        else:
            if self._pending not in ACTIONS:
                raise WorkerError("barrier")
            chosen = [self._pending, action]
            # Same native core called by OvercookedEnv.step; skip its unconditional
            # MotionPlanner initialization, which isn't needed when display_phi=False.
            new_state, info = self.mdp.get_state_transition(
                self.state, tuple(ACTIONS[x] for x in chosen), display_phi=False, motion_planner=None)
            reward_by_player = [float(x) for x in info["sparse_reward_by_agent"]]
            reward = sum(reward_by_player)
            delivered = [bool(x) for x in info["event_infos"]["soup_delivery"]]
            self.state = new_state
            self.score += reward
            self.delivery_counts = [old + int(new) for old, new in zip(self.delivery_counts, delivered)]
            self.last_transition = {"native_tick": int(self.state.timestep), "sparse_reward": reward,
                                    "sparse_reward_by_player": reward_by_player, "delivery_by_player": delivered}
            self.joint_history.append({"tick": int(self.state.timestep), "actions": chosen,
                                       "reward": reward, "delivery_by_player": delivered})
            self.joint_history = self.joint_history[-4:]
            self._pending = None
            self.revision += 1
        return self.snapshot()


def main():
    session = None
    for line in sys.stdin:
        request_id = None
        try:
            if len(line) > 65536:
                raise WorkerError("size")
            request = json.loads(line)
            if not isinstance(request, dict):
                raise WorkerError("request")
            request_id = request.get("id")
            # Third-party warnings/prints can never contaminate the JSON protocol.
            with redirect_stdout(sys.stderr):
                if request.get("op") == "init" and session is None:
                    session = NativeSession(request.get("seed"), request.get("max_steps"), request.get("layout"))
                    value = session.snapshot()
                elif request.get("op") == "step" and session is not None:
                    value = session.step(request.get("action"), request.get("expected_revision"))
                elif request.get("op") == "snapshot" and session is not None:
                    value = session.snapshot()
                else:
                    raise WorkerError("operation")
            response = {"id": request_id, "ok": True, "value": value}
        except WorkerError as exc:
            response = {"id": request_id, "ok": False, "error_code": exc.code}
        except ImportError:
            response = {"id": request_id, "ok": False, "error_code": "dependency"}
        except Exception:
            response = {"id": request_id, "ok": False, "error_code": "native"}
        sys.stdout.write(json.dumps(response, ensure_ascii=False, allow_nan=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
