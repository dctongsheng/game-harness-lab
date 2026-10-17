"""Game registry. Register an adapter; shared runtime/evolution need no changes."""
import re
from .snake import SnakeAdapter
from .xiangqi import XiangqiAdapter
from .army import ArmyAdapter
from .gomoku import GomokuAdapter
from .tetris import TetrisAdapter
from .platformer import PlatformerAdapter
from .overcooked import OvercookedAdapter

_REGISTRY = {}


def register_game(adapter):
    if not isinstance(adapter.id,str) or not re.fullmatch(r'[a-z][a-z0-9_-]*',adapter.id):
        raise ValueError('Adapter ID must be a portable lowercase identifier')
    if adapter.id in _REGISTRY:
        raise ValueError('Adapter ID is already registered: '+adapter.id)
    _REGISTRY[adapter.id] = adapter


def get_game(game_id='snake'):
    try:
        return _REGISTRY[game_id]
    except (KeyError,TypeError):
        raise ValueError('未知游戏：' + str(game_id)) from None


def game_catalog():
    return [{'id':a.id,'name':a.name,'rules_version':a.rules_version,'rules':a.rules,
             'score_spec':a.score_spec,'defaults':a.defaults,
             'information_policy':getattr(a,'information_policy',{})} for a in _REGISTRY.values()]


register_game(SnakeAdapter())
register_game(XiangqiAdapter())
register_game(ArmyAdapter())
register_game(GomokuAdapter())
register_game(TetrisAdapter())
register_game(PlatformerAdapter())
register_game(OvercookedAdapter())
