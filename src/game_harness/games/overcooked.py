"""Paused two-chef Overcooked-AI; the runtime selects every primitive action.

A joint tick has two independently receipted Decision calls. The first choice
stays private until the second is committed; neither chef sees a future action.
Native rules run in a pinned Python 3.10 process, never in the application venv.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import selectors
import subprocess
import threading
import time

UPSTREAM_COMMIT = "739950a079cdaed5a44fcc662efc40244c205d06"
UPSTREAM_REPOSITORY = "https://github.com/HumanCompatibleAI/overcooked_ai"
RULES_VERSION = "game-harness-lab/overcooked-joint-v1-739950a"
LAYOUTS = ("cramped_room", "asymmetric_advantages", "coordination_ring")
ACTION_IDS = ("north", "south", "east", "west", "stay", "interact")
CATALOG = [
    {"id": "north", "label": "朝北(0,-1)走一格并面朝北；遇阻挡仍转向。"},
    {"id": "south", "label": "朝南(0,1)走一格并面朝南；遇阻挡仍转向。"},
    {"id": "east", "label": "朝东(1,0)走一格并面朝东；遇阻挡仍转向。"},
    {"id": "west", "label": "朝西(-1,0)走一格并面朝西；遇阻挡仍转向。"},
    {"id": "stay", "label": "原地等待一个联合tick，保持朝向；烹饪时钟照常推进。"},
    {"id": "interact", "label": "与当前朝向的相邻格交互一次：拿取、放下、放食材、空手开火、盛汤或上菜，具体效果遵循原生规则。"},
]
SCORE_SPEC = {
    "id": "overcooked-native-sparse-v1", "direction": "maximize",
    "higher_is_better": True, "min": 0, "minimum_mean_gain": 1.0,
    "perspective": "cooperative_team", "score_kind": "native_sparse_reward",
    "description": "只累计原生 sparse_reward_by_agent；完成有效订单上菜才有分。食材入锅、取盘、盛汤、移动、存活均无代理奖励。",
}
RULES = """双厨师合作厨房，所有原始动作都由Decision选择；两位厨师共享候选Harness，没有脚本队友。
坐标(x,y)从左上(0,0)开始，x向右、y向下。terrain公开整张地图：空格可走、X台面、O洋葱、T番茄、D盘子、P锅、S上菜口。
每位厨师可选north/south/east/west/stay/interact。方向动作移动一格并改变朝向；遇墙或碰撞可能不移动但仍转向；interact只作用于当前面朝的相邻格。
交互效果由原生规则决定：空手从食材源拿食材，从盘子源拿盘子；持食材向锅交互添加食材；空手朝含食材的未开火锅交互启动烹饪；持盘子朝煮熟锅交互盛汤；持汤向S交互上菜。台面可暂存/交换物品。
锅最多容纳3个食材。原生允许少量食材提前开火，但当前3张地图的有效订单均为3个洋葱，错误配方上菜可为0分；请以orders的原生配方、时间和分数为准。满锅不会自动开火，空手interact才开火。煮熟的汤不会烧焦。
动作采用原生同时联合步进：先问chef_0，再问chef_1；双方看同一个已完成tick的世界，chef_1看不到chef_0待提交选择；第二次合法选择后两动作一起提交并推进1tick。原生交互按厨师索引处理，再统一处理移动、碰撞和烹饪。
等待模型时0tick推进。step/max_steps计Decision动作次数，native_tick/max_ticks计联合游戏步；2次Decision动作=1tick。所有动作候选始终完整，无寻路、候选推荐、战术评分、动作过滤或失败代走。
deliveries/delivery_counts统计原生上菜事件，包括错误配方的零分上菜，不能直接当作有效订单数。只有有效订单上菜所得原生稀疏分进入score；不采用原生shaped reward，不用接近锅、食材搬运或生存奖励。预算到达记step_limit截断，不当成胜负。
seed只标记相同标准开局的重复试验，不随机地图，不调换厨师。不同布局须单独比较。joint_history仅记录已完成的最近4个tick事实。
"""


class OvercookedEngineError(RuntimeError):
    """Native dependency or IPC failure; never substitute an action."""


def _python_path() -> str:
    override = os.environ.get("OVERCOOKED_PYTHON")
    if override:
        return str(Path(override).expanduser())
    return str(Path(__file__).resolve().parents[3] / ".venv-overcooked/bin/python")


class OvercookedSession:
    def __init__(self, seed: int = 0, max_steps: int = 240,
                 layout: str = "cramped_room", *, python_binary: str | None = None,
                 timeout: float = 30):
        OvercookedAdapter().validate_config({"max_steps": max_steps, "overcooked_layout": layout})
        if type(seed) is not int or not -(2**53 - 1) <= seed <= 2**53 - 1:
            raise ValueError("seed must be a safe integer trial identifier")
        python = python_binary or _python_path()
        if not Path(python).is_file():
            raise OvercookedEngineError("缺少Overcooked独立Python环境；请运行 scripts/setup_overcooked.sh，或设置OVERCOOKED_PYTHON指向已安装固定版本的Python3.10。")
        self.timeout = timeout
        self._serial = 0
        self._buffer = b""
        self._closed = False
        self._lock = threading.Lock()
        worker = Path(__file__).with_name("overcooked_worker.py")
        try:
            # Native rules must not inherit model credentials or application imports.
            self._process = subprocess.Popen(
                [python, "-u", str(worker)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, bufsize=0, cwd=worker.parent,
                env={"PATH": os.environ.get("PATH", ""), "LANG": "C.UTF-8",
                     "PYTHONDONTWRITEBYTECODE": "1"},
            )
            self._selector = selectors.DefaultSelector()
            self._selector.register(self._process.stdout, selectors.EVENT_READ)
            self._state = self._request({"op": "init", "seed": seed,
                                         "max_steps": max_steps, "layout": layout})
        except Exception:
            self.close()
            raise

    def _request(self, payload: dict) -> dict:
        with self._lock:
            if self._closed:
                raise OvercookedEngineError("Overcooked rules process is closed")
            self._serial += 1
            try:
                message = json.dumps({**payload, "id": self._serial}, allow_nan=False).encode() + b"\n"
                self._process.stdin.write(message)
                self._process.stdin.flush()
                deadline = time.monotonic() + self.timeout
                while b"\n" not in self._buffer:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not self._selector.select(remaining):
                        raise OvercookedEngineError("Overcooked rules response timed out")
                    part = os.read(self._process.stdout.fileno(), 65536)
                    if not part:
                        raise OvercookedEngineError("Overcooked rules process exited unexpectedly")
                    self._buffer += part
                    if len(self._buffer) > 2_000_000:
                        raise OvercookedEngineError("Overcooked rules response exceeds size limit")
                line, self._buffer = self._buffer.split(b"\n", 1)
                response = json.loads(line)
                if not isinstance(response, dict) or response.get("id") != self._serial:
                    raise OvercookedEngineError("Overcooked response sequence mismatch")
                if response.get("ok") is not True:
                    code = response.get("error_code")
                    messages = {
                        "dependency": "Overcooked独立环境不可用：需要Python3.10及固定上游提交739950a的overcooked-ai。请运行 scripts/setup_overcooked.sh。",
                        "provenance": "Overcooked依赖来源不匹配：必须安装固定上游提交739950a079cdaed5a44fcc662efc40244c205d06。",
                        "revision": "Overcooked rejected a stale observation revision",
                        "action": "Overcooked rejected an unknown primitive action",
                    }
                    raise OvercookedEngineError(messages.get(code, "Overcooked rules rejected the operation"))
                if not isinstance(response.get("value"), dict):
                    raise OvercookedEngineError("Overcooked response omitted state")
                return response["value"]
            except (BrokenPipeError, OSError, ValueError) as exc:
                self.close()
                raise OvercookedEngineError("Overcooked rules communication failed") from exc
            except OvercookedEngineError:
                self.close()
                raise

    def snapshot(self) -> dict:
        return deepcopy(self._state)

    def candidates(self) -> list[dict[str, str]]:
        return [] if self._state["done"] else deepcopy(CATALOG)

    def step(self, action_id: str) -> dict:
        if self._closed:
            raise OvercookedEngineError("Overcooked rules process is closed")
        if self._state["done"]:
            raise RuntimeError("episode already ended")
        if not isinstance(action_id, str) or action_id not in ACTION_IDS:
            raise ValueError("action must be one of the complete six native primitives")
        self._state = self._request({"op": "step", "action": action_id,
                                     "expected_revision": self._state["step"]})
        return self.snapshot()

    def close(self) -> None:
        if getattr(self, "_closed", True):
            return
        self._closed = True
        selector = getattr(self, "_selector", None)
        if selector:
            selector.close()
        process = getattr(self, "_process", None)
        if process:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
            for stream in (process.stdin, process.stdout):
                if stream:
                    stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def __del__(self):
        self.close()


class OvercookedAdapter:
    id = "overcooked"
    name = "Overcooked-AI 双人厨房"
    rules_version = RULES_VERSION
    rules = RULES
    score_spec = SCORE_SPEC
    defaults = {"max_steps": 240, "overcooked_layout": "cramped_room"}
    opponent_policy = None

    def validate_config(self, config: dict) -> None:
        steps = config.get("max_steps", self.defaults["max_steps"])
        if type(steps) is not int or not 2 <= steps <= 1000 or steps % 2:
            raise ValueError("Overcooked max_steps必须为2到1000的偶数；两次Decision选择推进一个联合tick")
        if config.get("overcooked_layout", self.defaults["overcooked_layout"]) not in LAYOUTS:
            raise ValueError("Overcooked布局必须为cramped_room、asymmetric_advantages或coordination_ring")

    def scope_config(self, config: dict) -> dict:
        self.validate_config(config)
        return {"max_steps": config.get("max_steps", self.defaults["max_steps"]),
                "overcooked_layout": config.get("overcooked_layout", self.defaults["overcooked_layout"]),
                "clock": "paused_atomic_joint_tick", "decisions_per_tick": 2,
                "start_position": "native_standard", "chef_control": "both_decision_candidate"}

    def context(self, config: dict) -> dict:
        return {
            "game_id": self.id, "name": self.name, "rules_version": RULES_VERSION,
            "rules": RULES, "score_spec": deepcopy(SCORE_SPEC), "config": self.scope_config(config),
            "opponent_policy": None,
            "scenario_policy": "fixed_layout_standard_start_repeated_trials",
            "trial_design": "同布局原生标准开局重复试验；seed只标识重复，不随机地图或交换厨师；跨布局另作评测。",
            "evaluation": {"seed_semantics": "repeat identifier only; identical native initial state",
                           "seed_modulus": 1,
                           "primary_metric": "native_sparse_reward",
                           "secondary_metrics": ["deliveries", "delivery_counts", "native_tick"],
                           "delivery_semantics": "deliveries/delivery_counts count native soup_delivery events, including invalid recipes worth zero; score is the valid-order outcome",
                           "limitations": "重复种子不构成新地图泛化证据；双厨师共享同一候选Harness。"},
            "source": {"upstream_repository": UPSTREAM_REPOSITORY,
                       "upstream_commit": UPSTREAM_COMMIT, "license": "MIT",
                       "worker_sha256": hashlib.sha256(Path(__file__).with_name("overcooked_worker.py").read_bytes()).hexdigest()},
        }

    def create(self, seed: int, config: dict) -> OvercookedSession:
        self.validate_config(config)
        return OvercookedSession(seed=seed, max_steps=config.get("max_steps", self.defaults["max_steps"]),
                                 layout=config.get("overcooked_layout", self.defaults["overcooked_layout"]))
