"""Paused native Gen 3 battles, with poke-env maintaining each private view.

There is deliberately no poke-env Player: its default-action recovery paths are
unsuitable for a workload in which every submitted choice requires a receipt.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib.metadata
import json
import logging
from pathlib import Path

from .node_bridge import NodeBridge, RulesBridgeError

POKE_ENV_COMMIT = "7ff6212e4e738efd46f1ea52035b37a8bba1e386"
SHOWDOWN_COMMIT = "7244fe8e28fa8bc4d0809ae30d183c608b62bb2b"
RULES_VERSION = "game-harness-lab/pokemon-gen3-private-v1-7244fe8"
FORMAT_LABEL = "第三世代单打3v3"
RULES = """宝可梦第三世代单打3v3，由固定版本Pokemon Showdown执行完整原生对战规则，poke-env解析双方各自可见信息。
双方所有招式与换人选择都由Decision模型完成；候选使用可优化Harness，对手使用冻结参考Harness。等待推理时世界完全暂停。
每方有3只50级宝可梦，每只4招；没有队伍预览、太晶化、极巨化、Mega或Z招式。初始出场是环境生成队伍的第一只，不参与选队优化。
双方在同一回合分别秘密提交动作，再由原生优先级、速度和随机因素决定执行顺序。换人通常先于普通攻击；被击倒后出现仅换人的强制请求。等待方不需要行动。
完整合法目录列出move:<当前招式序号>和switch:<当前队伍序号>；序号随原生请求变化，必须只选择当前目录中的ID。无战术过滤、排序、推荐招式、伤害搜索、随机兜底或默认代走。
动作标签和team中的招式资料是规则事实，不是动作评分。第三世代物理/特殊分类由属性决定，不能套用第四世代以后各招式单独分类：普通、格斗、飞行、毒、地面、岩石、虫、幽灵、钢为物理，其余当时存在的属性为特殊。
属性相克、同属性加成、命中率、PP、状态、能力阶段与交换重置等均按第三世代原生规则执行。Protect连续使用可能失败；Recover回复自己；Calm Mind提高特攻与特防。Struggle仅当原生规则使其合法时列出，仍须Decision选择。
本方team包含自己的完整队伍、真实HP和能力值。opponent_team只含已登场或已由原生公开协议揭示的信息；未揭示的宝可梦、招式、物品、能力值及对方本回合选择不可读取或推断为事实。对方HP只提供公开百分比，不能访问对方真实精确HP。
recent_events只含最近公开对战事件，不含未执行的秘密选择。原生事件和模型提交次数不同；step计Decision提交，native_turn计原生回合。
胜100、负0、原生平局50。预算到达则result=unresolved、truncated=true、score=50仅为兼容存储的占位值，不是平局或能力得分，不得纳入评测、优化选优或发布验收。
私有种子改变环境队伍、出场顺序及原生随机序列，奇偶决定候选席位；不向模型或反思暴露种子或完整对手队伍。固定小型队伍池的实验不代表天梯实力或跨世代泛化。
"""
SCORE_SPEC = {
    "id": "pokemon-gen3-native-outcome-v1", "min": 0, "max": 100,
    "direction": "maximize", "minimum_mean_gain": 2.0, "perspective": "candidate_seat",
    "win": 100, "loss": 0, "draw": 50, "cutoff": 50, "cutoff_is_draw": False,
    "cutoff_is_scored": False, "requires_native_terminal": True,
    "description": "只比较原生胜/负/平局100/0/50。截断50仅存储占位，未完赛必须排除优化与验收，不采用血量或存活数代理分。",
}
OPPONENT_POLICY = {
    "decision_instructions": "你是固定参考对手，目标是赢得当前第三世代单打3v3。只依据本方team、已揭示的opponent_team和当前完整合法目录选择一项。考虑属性相克、同属性加成、第三世代物理/特殊分类、威力命中率、自身能力与HP；不利对位可换人，强制换人时选择能应对当前对手的存活队员。不要假设对方未揭示的招式或队伍，不读取私有种子，不故意让棋。",
    "strategy_notes": "pokemon-reference-v1：冻结参考Harness，双方动作均由相同Decision模型选出。保持合理进攻与换人；对手未公开数据保持未知。",
}


def _load_battle_class():
    try:
        distribution = importlib.metadata.distribution("poke-env")
        provenance = json.loads(distribution.read_text("direct_url.json") or "{}")
        if distribution.version != "0.16.1" or provenance.get("vcs_info", {}).get("commit_id") != POKE_ENV_COMMIT:
            raise RuntimeError("poke-env dependency provenance mismatch")
        from poke_env.battle import Battle
        return Battle
    except (ImportError, importlib.metadata.PackageNotFoundError, ValueError, RuntimeError) as exc:
        raise RulesBridgeError("需要固定版本poke-env及本地Pokemon Showdown；请运行 scripts/setup_pokemon.sh。") from exc


def _enum(value):
    return value.name.lower() if value is not None and hasattr(value, "name") else value


def _move_info(move, *, known_pp: bool) -> dict:
    result = {"id": move.id, "type": _enum(move.type), "category": _enum(move.category),
              "base_power": move.base_power, "accuracy": move.accuracy,
              "priority": move.priority, "target": _enum(move.target),
              "boosts": deepcopy(move.boosts), "status": _enum(move.status),
              "heal": move.heal, "drain": move.drain, "recoil": move.recoil,
              "protect": move.is_protect_move}
    if known_pp:
        result["pp"] = move.current_pp
        result["max_pp"] = move.max_pp
    return result


def _pokemon_info(ident, pokemon, *, private: bool, slot: int | None = None) -> dict:
    result = {"ident": ident, "species": pokemon.species, "active": pokemon.active,
              "fainted": pokemon.fainted, "hp_fraction": round(pokemon.current_hp_fraction, 6),
              "current_hp": pokemon.current_hp, "max_hp": pokemon.max_hp,
              "types": [_enum(t) for t in pokemon.types], "status": _enum(pokemon.status),
              "boosts": deepcopy(pokemon.boosts), "ability": pokemon.ability,
              "item": None if pokemon.item == "unknown_item" else pokemon.item,
              "moves": [_move_info(m, known_pp=private) for m in pokemon.moves.values()]}
    if private:
        result["stats"] = deepcopy(pokemon.stats)
        result["slot"] = slot
        result["hp_information"] = "own_exact"
    else:
        result["hp_information"] = "public_percentage"
        result["moves_information"] = "observed_moves_only"
    return result


class PokemonSession:
    def __init__(self, seed: int = 0, max_steps: int = 160):
        PokemonAdapter().validate_config({"max_steps": max_steps})
        if type(seed) is not int or not 0 <= seed <= 2**53 - 1:
            raise ValueError("seed must be a nonnegative safe integer")
        Battle = _load_battle_class()
        logger = logging.getLogger("game_harness.pokemon.protocol")
        self._views = {side: Battle("battle-gen3-v1", f"Seat {i+1}", logger, 3)
                       for i, side in enumerate(("p1", "p2"))}
        self._public = Battle("battle-gen3-v1", "Seat 1", logger, 3)
        self._recent_events = []
        self._candidate_seat = seed % 2
        self._max_steps = max_steps
        self._closed = False
        self._bridge = NodeBridge("pokemon_bridge.cjs", timeout=30)
        try:
            self._consume(self._bridge.request({"op": "init", "seed": seed, "max_steps": max_steps}))
        except Exception:
            self.close()
            raise

    @staticmethod
    def _parse(view, lines):
        for line in lines:
            parts = line.split("|")
            if len(parts) < 2 or not parts[1]:
                continue
            if parts[1] == "win":
                view.won_by(parts[2])
            elif parts[1] == "tie":
                view.tied()
            else:
                view.parse_message(parts)

    def _consume(self, native):
        self._native = native
        for batch in native["events"]:
            self._parse(self._public, batch["public"])
            for side, view in self._views.items():
                self._parse(view, batch[side])
            self._recent_events.extend(line for line in batch["public"]
                                       if line.split("|")[1] not in {"gametype", "player", "gen", "tier", "rule", "teamsize", "start", ""})
        self._recent_events = self._recent_events[-12:]
        if not native["native_terminal"]:
            for side, request in native["requests"].items():
                self._views[side].parse_request(deepcopy(request))
        self._state = self._build_state()

    def _build_state(self):
        native = self._native
        actor = native["actor"] or ("p1" if self._candidate_seat == 0 else "p2")
        seat = int(actor[1]) - 1
        view = self._views[actor]
        request = native["requests"].get(actor, {})
        order = {p["ident"]: i + 1 for i, p in enumerate(request.get("side", {}).get("pokemon", []))}
        own = [_pokemon_info(ident, pokemon, private=True, slot=order.get(ident))
               for ident, pokemon in view.team.items()]
        own.sort(key=lambda p: p["slot"] or 99)
        opponent = [_pokemon_info(ident, pokemon, private=False) for ident, pokemon in view.opponent_team.items()]
        public_players = [
            {"seat": 0, "team": [_pokemon_info(i, p, private=False) for i, p in self._public.team.items()],
             "unrevealed_count": max(0, 3 - len(self._public.team))},
            {"seat": 1, "team": [_pokemon_info(i, p, private=False) for i, p in self._public.opponent_team.items()],
             "unrevealed_count": max(0, 3 - len(self._public.opponent_team))},
        ]
        public = {"game_id": "pokemon", "format_label": FORMAT_LABEL, "native_turn": native["native_turn"],
                  "players": public_players, "recent_events": deepcopy(self._recent_events)}
        return {"game_id": "pokemon", "format": "gen3customgame", "format_label": FORMAT_LABEL,
                "rules_version": RULES_VERSION, "step": native["revision"], "max_steps": self._max_steps,
                "native_turn": native["native_turn"], "turn": seat, "actor": actor,
                "viewer_seat": seat, "candidate_seat": self._candidate_seat,
                "policy_role": "candidate" if seat == self._candidate_seat else "opponent",
                "team": own, "opponent_team": opponent, "opponent_unrevealed_count": max(0, 3-len(opponent)),
                "force_switch": bool(request.get("forceSwitch", [False])[0]),
                "trapped": bool(request.get("active", [{}])[0].get("trapped", False)),
                "weather": {_enum(k): v for k, v in view.weather.items()},
                "fields": {_enum(k): v for k, v in view.fields.items()},
                "side_conditions": {_enum(k): v for k, v in view.side_conditions.items()},
                "opponent_side_conditions": {_enum(k): v for k, v in view.opponent_side_conditions.items()},
                "recent_events": deepcopy(self._recent_events), "public_view": public,
                **{key: native[key] for key in ("done", "native_terminal", "truncated", "winner", "result", "reason", "score")}}

    def snapshot(self):
        return deepcopy(self._state)

    def candidates(self):
        if self._state["done"]:
            return []
        actor = self._native["actor"]
        request = self._native["requests"][actor]
        view = self._views[actor]
        if request.get("wait") or request.get("teamPreview"):
            raise RulesBridgeError("Unexpected Pokemon request; no automatic action is permitted")
        forced = bool(request.get("forceSwitch", [False])[0])
        active = request.get("active", [{}])[0]
        catalog = []
        if not forced:
            known = {move.id: move for move in view.available_moves}
            for index, move in enumerate(active.get("moves", []), 1):
                if move.get("disabled") or not (move.get("pp", 0) > 0 or move["id"] in {"struggle", "recharge"}):
                    continue
                if move["id"] not in known:
                    raise RulesBridgeError("poke-env/native move catalogs disagree; no fallback")
                fact = _move_info(known[move["id"]], known_pp=True)
                fact.update({"pp": move.get("pp"), "max_pp": move.get("maxpp")})
                catalog.append({"id": f"move:{index}", "label": f"使用{move['move']}；规则资料：" + json.dumps(fact, ensure_ascii=False, separators=(",", ":"))})
        if forced or not active.get("trapped"):
            switches = {id(p) for p in view.available_switches}
            for index, pokemon in enumerate(request["side"]["pokemon"], 1):
                if pokemon["active"] or "fnt" in pokemon["condition"]:
                    continue
                parsed = view.team[pokemon["ident"]]
                if id(parsed) not in switches:
                    raise RulesBridgeError("poke-env/native switch catalogs disagree; no fallback")
                catalog.append({"id": f"switch:{index}", "label": f"换上队伍第{index}只{parsed.species}；HP {pokemon['condition']}；属性{'/'.join(_enum(t) for t in parsed.types)}。占用本次选择，不同时使用招式。"})
        if not catalog:
            raise RulesBridgeError("Pokemon returned an empty legal catalog; no default action")
        return catalog

    def step(self, action_id):
        if self._closed:
            raise RulesBridgeError("Pokemon rules process is closed")
        if self._state["done"]:
            raise RuntimeError("episode already ended")
        if not isinstance(action_id, str) or action_id not in {c["id"] for c in self.candidates()}:
            raise ValueError("action must be in the complete current Pokemon legal catalog")
        response = self._bridge.request({"op": "step", "command": action_id.replace(":", " "),
                                         "actor": self._native["actor"], "expected_revision": self._state["step"]})
        self._consume(response)
        return self.snapshot()

    def close(self):
        if getattr(self, "_closed", True):
            return
        self._closed = True
        bridge = getattr(self, "_bridge", None)
        if bridge:
            bridge.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def __del__(self):
        self.close()


class PokemonAdapter:
    id = "pokemon"
    name = "宝可梦对战 · poke-env（第三世代3v3）"
    rules_version = RULES_VERSION
    rules = RULES
    score_spec = SCORE_SPEC
    defaults = {"max_steps": 160}
    opponent_policy = OPPONENT_POLICY
    information_policy = {"feedback_visibility": "candidate_before_only", "seed_is_private": True,
                          "public_projection": "public_view"}

    def validate_config(self, config):
        steps = config.get("max_steps", 160)
        if type(steps) is not int or not 1 <= steps <= 2000:
            raise ValueError("宝可梦max_steps必须为1到2000次Decision提交")

    def scope_config(self, config):
        self.validate_config(config)
        return {"max_steps": config.get("max_steps", 160), "generation": 3, "game_type": "singles",
                "team_size": 3, "fixture_pool": "six-species-disjoint-3v3-v1", "team_preview": False,
                "showdown_format": "gen3customgame", "debug": False, "hp_percentage_mod": True,
                "candidate_seat": "seed_parity", "reference_policy_version": "pokemon-reference-v1",
                "clock": "paused_native_request_barrier", "cutoff_policy": "unscored_unresolved"}

    def context(self, config):
        bridge = Path(__file__).with_name("pokemon_bridge.cjs")
        return {"game_id": self.id, "name": self.name, "rules_version": RULES_VERSION,
                "rules": RULES, "score_spec": deepcopy(SCORE_SPEC), "config": self.scope_config(config),
                "opponent_policy": deepcopy(OPPONENT_POLICY), "information_policy": deepcopy(self.information_policy),
                "evaluation": {"seed_modulus": 2, "require_native_terminal": True,
                               "primary_metric": "native_candidate_outcome", "exclude_truncated": True},
                "source": {"poke_env_repository": "https://github.com/hsahovic/poke-env", "poke_env_commit": POKE_ENV_COMMIT,
                           "poke_env_version": "0.16.1", "poke_env_license": "MIT",
                           "showdown_repository": "https://github.com/smogon/pokemon-showdown", "showdown_commit": SHOWDOWN_COMMIT,
                           "showdown_license": "MIT", "bridge_sha256": hashlib.sha256(bridge.read_bytes()).hexdigest()},
                "scenario_policy": "private_seeded_disjoint_three_of_six_species_parties_and_native_rng",
                "trial_design": "私有种子确定双方三只队伍、出场顺序、原生PRNG；奇偶决定候选席位；同种子新旧Harness配对。只覆盖固定六种宝可梦池，不构成天梯或跨世代泛化。",
                "action_policy": "全部合法招式与换人由Decision逐项选择。双边请求秘密屏障，强制换人仍须Decision；原生wait无需提交。无默认、脚本或自动战斗选手。",
                "limitations": "第三世代单打3v3固定小队伍池，不包含选队、队伍预览或现代特殊机制。未完赛不评分、不选优、不发布。"}

    def create(self, seed, config):
        self.validate_config(config)
        return PokemonSession(seed=seed, max_steps=config.get("max_steps", 160))
