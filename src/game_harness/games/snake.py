"""Snake adapter: deterministic rules and observations, no player or network."""
from copy import deepcopy
from ..snake import SnakeGame, GAME_RULES, RULES_VERSION, ACTION_CANDIDATES


class SnakeSession:
    def __init__(self, seed, config):
        self.engine = SnakeGame(size=config.get('board_size',8), seed=seed, max_steps=config.get('max_steps',120))

    def snapshot(self):
        return {**self.engine.snapshot(), 'game_id':'snake', 'policy_role':'candidate', 'actor':'player', 'applied_action':self.engine.direction}

    def candidates(self):
        return [] if self.engine.done else deepcopy(ACTION_CANDIDATES)

    def step(self, action_id):
        self.engine.step(action_id)
        return self.snapshot()

    def close(self):
        pass


class SnakeAdapter:
    id = 'snake'
    name = '贪吃蛇'
    rules_version = RULES_VERSION
    rules = GAME_RULES
    defaults = {'board_size':8,'max_steps':120}
    opponent_policy = None
    score_spec = {'id':'snake-food-v1','min':0,'direction':'maximize','unit':'食物',
                  'description':'吃到一个食物得1分；生存和步数不加分。', 'minimum_mean_gain':0.5}

    def validate_config(self, config):
        size, steps = config.get('board_size',8),config.get('max_steps',120)
        if type(size) is not int or not 4 <= size <= 20:
            raise ValueError('贪吃蛇棋盘必须为4到20的整数')
        if type(steps) is not int or not 1 <= steps <= 1000:
            raise ValueError('max_steps必须为1到1000的整数')

    def scope_config(self, config):
        return {'board_size':config.get('board_size',8),'max_steps':config.get('max_steps',120)}

    def context(self, config):
        return {'game_id':self.id,'name':self.name,'rules_version':self.rules_version,'rules':self.rules,
                'score_spec':{**self.score_spec,'max':config.get('board_size',8)**2-3},
                'config':self.scope_config(config),'opponent_policy':None,
                'scenario_policy':'种子决定食物生成序列；未来食物不出现在玩家观察中。',
                'action_policy':'全部四个方向始终提供；不得用安全过滤或自动寻路接管决策。'}

    def create(self, seed, config):
        self.validate_config(config)
        return SnakeSession(seed,config)
