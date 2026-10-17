"""Describe the real Decision boundary to the pre-game LLM, without playing.

Examples are fresh adapter observations, never evaluation episodes. Hidden games
contribute only a common-knowledge example, not either player's private catalog.
"""
from __future__ import annotations

from copy import deepcopy
import json
import secrets

from .games import get_game
from .visibility import public_snapshot


CONTRACT_VERSION = "decision-harness-initialization/v1"

_CONTEXT_FIELDS = {
    "game_id", "name", "rules_version", "rules", "score_spec", "config",
    "opponent_policy", "evaluation", "source", "information_policy",
    "scenario_policy", "trial_design", "action_policy", "limitations",
}
_PRIVATE_FIELDS = {
    "seed", "evaluation_seed", "train_seeds", "validation_seeds", "promotion_seeds",
    "rng", "rng_state", "private_state", "authoritative_state", "raw_request", "raw_response",
}


def _without_private_fields(value):
    if isinstance(value, list):
        return [_without_private_fields(item) for item in value]
    if isinstance(value, dict):
        return {
            key: _without_private_fields(item) for key, item in value.items()
            if isinstance(key, str) and key.lower() not in _PRIVATE_FIELDS
            and not any(word in key.lower() for word in ("api_key", "authorization", "password", "secret", "token"))
        }
    return deepcopy(value)


def initialization_context(context: dict) -> dict:
    """Keep immutable game documentation, not arbitrary application/settings data."""
    return _without_private_fields({key: value for key, value in context.items() if key in _CONTEXT_FIELDS})


def _shape(value):
    """A value-free structural description; an initial null is not a type promise."""
    if isinstance(value, dict):
        return {"type": "object", "properties": {key: _shape(item) for key, item in value.items()}}
    if isinstance(value, list):
        shapes = {json.dumps(_shape(item), sort_keys=True): _shape(item) for item in value}
        return {"type": "array", "observed_item_shapes": list(shapes.values())}
    return {"type": "null" if value is None else "boolean" if isinstance(value, bool)
            else "number" if isinstance(value, (float, int)) else "string"}


_GAME_GUIDANCE = {
    "snake": {
        "observations": "size为边长；snake是蛇头到蛇尾的[x,y]数组；food为当前食物或null；direction是实际行进方向。左上原点，x右增、y下增。",
        "actions": "up/right/down/left全四方向始终提供，包括可能致死的方向。每次推进一格；请求反向时继续原方向。未吃食物时尾格在本步腾出。",
        "design_focus": "把位置、方向和边界条件写成可由当前字段判断的短优先级，再考虑食物与空间；不假设存在safe_actions、路径搜索或未来食物。",
    },
    "xiangqi": {
        "observations": "fen、board_ascii、cells表示同一公开棋盘；cells从黑底线到红底线共10×9格，元素null或含type/color的棋子。turn为r/b，candidate_color为本Harness颜色；in_check是当前方被将军标记。",
        "actions": "完整规则合法ICCS走法，如h2h9。a-i列从红方左至右，0为红底线、9为黑底线；一步推进一个半回合。不提供搜索分、最佳走法或战术过滤。",
        "design_focus": "使用当前行棋颜色及坐标，区分将帅安全、吃子与发展；材料代理分只用于预算截断，不能替代真实胜负。没有历史棋谱输入。",
    },
    "gomoku": {
        "observations": "rows/cols为9或15；cells按左上开始逐行排列，元素null或含seat的stone。observation.board使用X=黑、O=白、.=空；stones为双方坐标列表，yourSeat/turn为0黑或1白。",
        "actions": "place:<index>覆盖所有空交叉点，index=row*cols+column，行列从0计；A1是左上。一次只落一子，没有战术剪枝或自动堵点。",
        "design_focus": "设计针对当前可见四个方向棋形的简短选择优先级；不假设已有winning_actions、threats或规则计算辅助字段。",
    },
    "army": {
        "observations": "实际Decision收到当前行棋方私有投影：cells与observation.pieces只公开己方身份和已知敌方战斗证据；unknown/标签?不是敌方真实身份。turn/actor/viewer_seat为当前方，candidate_seat为候选方。public_view仅含双方共同已知信息，不能用它推断未公开等级。",
        "actions": "move:<from>:<to>为完整当前合法移动目录；index=row*5+column，A-E列、1-12行从左上起。军旗/雷不可动，进入大本营后不可动；仅对局阶段，不含布阵动作。语法示例move:27:32不代表任何当前局面的推荐或保证合法动作。",
        "design_focus": "策略只能根据可见身份/等级证据和地形判断。没有跨步记忆、最近走法或重复计数；不能要求回忆前几步、合并双方观察、读取种子/全明棋或请求对手私有目录。样本仅为公共投影，不展示己方私子与私有动作目录。",
    },
    "tetris": {
        "observations": "observation.board.rows含y=-2..19的22行已锁定方块，当前active单独记录；active.cells为实际占用绝对格，active.x/y是局部框原点。next仅五块预览，holdAvailable决定暂存可用；grounded、fallTicks、lockTicks等是当前计时。render仅显示20行，不能代替完整observation。",
        "actions": "目录为原生按键与时长，如left:100、rotate-cw:100、hard-drop:100；100/300/1000毫秒推进6/18/60tick。硬降按当前横向位置/朝向锁块，不自动选落点；hold不是无限可用。旋转/硬降/hold每个动作只触发一次，每个动作结束自动松键；连续两次相同旋转/硬降/hold会产生新的按下边沿。hold已有块时换入hold中的块，只有空hold才消耗next[0]。noop:100也推进6tick。",
        "design_focus": "区分当前方块和锁定板、横移/旋转与最终硬降；依据当前可见结构设计策略，不假设height_profile、holes、落点枚举、评分器或未来随机序列存在。只有消行原生points计分。",
    },
    "platformer": {
        "observations": "observation给出player位置/宽高、vx/vy/onGround，以及platforms、hazards、enemies、coins、exit；像素左上原点，x右增y下增，速度为px/tick。maxX为历史最远位置不是当前位置。level是三固定关之一，不是可由模型改写的参数。",
        "actions": "完整原生按键时长目录含left/right及jump组合。100/300/1000毫秒推进6/18/60tick，到期松键；jump仅在着地且新按下时触发一次，空中输入不排队、不连跳。noop:100仍推进6tick；等待模型推进0tick。",
        "design_focus": "结合当前速度、地面状态与障碍几何设计动作时长选择，说明单次长按跨越多个物理tick的风险。无自动跳跃/寻路/安全动作过滤；原创平台游戏不是原版Mario。",
    },
}


def build_runtime_contract(context: dict, decision_model: str | None = None) -> dict:
    """Pure, sample-free player semantics shared by initialization and reflection.

    No adapter lookup, session, sample, seed, or trace is needed. Unknown games
    receive only the common protocol plus a conservative schema disclaimer.
    """
    return {
        "schema_version": "decision-harness-runtime/v1",
        "game_id": context.get("game_id"),
        "rules_version": context.get("rules_version"),
        "player": {
            "model": decision_model, "endpoint_protocol": "/v1/systemone",
            "question": "primary_action", "type": "choice", "think_mode": "never",
            "allow_none": False, "choices_per_call": 1,
            "input": "state为当前JSON观察序列化字符串；questions.primary_action.instructions接收冻结规则与Harness；criteria是完整动作ID到标签映射。",
            "output": "只选择本次criteria中的一个ID。调用方验证回执后执行；无模型选择则停止，不用代码代选。概率/置信度不是棋力保证。",
            "memory": "stateless：每次独立调用，只提供当前snapshot；无会话历史、跨步scratchpad或自动回忆。当前观察内已有的状态字段仍可使用。",
            "tools": [], "search_helpers": [], "in_game_coach_calls": False,
        },
        "runtime_fields": {
            "step": "0起始决策/半回合序号，不是过去动作序列",
            "remaining_steps": "固定预算减当前step",
            "candidates": "完整允许动作列表，每项只有id与label；允许不等于安全或有利",
            "episode_id": "不透明审计标识，不是策略条件或记忆",
            "observation_hash": "不透明一致性标识，不能据此恢复棋局历史或暗子",
            "policy_role": "candidate或opponent；本次只设计candidate策略，固定对手不修改",
            "actor": "若snapshot提供则为当前行动方；未提供时运行器内部识别角色，不应假设state必有actor",
        },
        "game_guidance": deepcopy(_GAME_GUIDANCE.get(context.get("game_id"), {
            "observations": "以适配器实际snapshot字段和固定规则解释当前观察。",
            "actions": "使用当前完整目录原样ID，一次执行一个动作，具体效果由适配器规则定义。",
            "design_focus": "为当前游戏设计可由现有观察判断的简短策略，不编造缺失字段、工具或记忆。",
        })),
        "feedback_boundary": "教练反馈是离线投影，不是玩家state schema。board_rows、selection_reasons、observation_context、public_followups等反馈字段不会自动出现在Decision输入中；不能把反馈布局、战术标签或统计结果写成玩家可读取的字段。运行时字段和动作语义以本合同及固定游戏规则为准。",
    }


def build_initialization_contract(context: dict, decision_model: str) -> dict:
    """Open one read-only fresh session, inspect its initial state, and close it."""
    adapter = get_game(context["game_id"])
    config = {**adapter.defaults, **context.get("config", {})}
    restricted = bool(getattr(adapter, "information_policy", {}).get("seed_is_private"))
    session = adapter.create(secrets.randbelow(2**31) if restricted else 0, config)
    try:
        snapshot = session.snapshot()
        schema = _shape(_without_private_fields(snapshot))
        # Never send private piece values or mobility catalogs from an Army
        # initialization to the coach. The public projection must fail closed.
        if restricted:
            if not isinstance(snapshot.get("public_view"), dict):
                raise ValueError("Hidden-information sample requires a public projection")
            sample = _without_private_fields(public_snapshot(snapshot))
            catalog = None
        else:
            sample = _without_private_fields(snapshot)
            catalog = session.candidates()
    finally:
        session.close()
    return {
        **build_runtime_contract(context, decision_model),
        "schema_version": CONTRACT_VERSION,
        "game_id": adapter.id,
        "rules_version": adapter.rules_version,
        "observation_schema": schema,
        "schema_note": "由真实初始snapshot生成的字段结构；null/空数组仅表示此示例，不保证后续总为空。具体含义见game_guidance和固定规则；没有列出的历史/辅助字段不能假定存在。",
        "sample": {"kind": "public_projection_only" if restricted else "fresh_adapter_initial_observation",
                   "state": sample, "action_catalog": catalog,
                   "note": "教学样本不是当前对局/评测状态，不记住坐标路线。暗棋公共样本不包含玩家私有目录；实际决策仍收到当前方完整合法目录。" if restricted
                   else "教学样本不是当前对局/评测状态，不记住具体坐标、开局或随机序列。目录只适用于此初始样本，运行时每步重新生成完整目录。"},
        "design_requirements": [
            "先解释游戏目标、关键观察字段及动作粒度，再设计适合单步choice/关闭思考的短而明确优先级，避免泛泛攻略或要求长篇推理。",
            "LLM只在局前生成/局后修改文本；不能替选手挑本局动作、写可执行代码、改变规则/目录/得分/对手或保证性能提升。",
            "不得要求读取不存在字段、调用外部工具、保存跨步记忆或执行多动作计划；条件只能引用现有观察。",
            "brief.strategy说明为何按这些输入和动作语义设计；brief.precautions写清能力边界、缺失信息及评分/截断含义。",
        ],
    }
