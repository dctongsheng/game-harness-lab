"""Freestyle Gomoku adapter over unchanged MIT Decision Arena game rules."""
from __future__ import annotations

from copy import deepcopy
from .node_bridge import NodeBridge

SOURCE_COMMIT = '4e2bda1d6c9b2545837761e2c7d19e2ce546aa9d'
SOURCE_URL = 'https://github.com/dctongsheng/decison-arena'
RULES_VERSION = 'game-harness-lab/gomoku-freestyle-v1-4e2bda1'
RULES = '''自由五子棋：支持9×9或15×15棋盘，黑方（seat=0）先手，白方（seat=1）后手。
双方轮流在空交叉点落一子。横、竖或任一斜线连续五子及以上获胜；无禁手、无提子、无交换开局。
棋盘满且无人连五则和棋。A1位于左上角，列从左至右，行从上至下。cells按从上至下逐行排列。
每次必须从完整合法动作目录选择place:<index>，不按战术优劣过滤空位。代码仅执行模型选择。
种子奇偶决定候选执黑或白；所有试验从标准空盘开始，种子不同不代表不同开局。
双方均使用同一Decision模型，候选使用当前Harness，对手使用冻结参考策略。
目标是赢棋，候选胜100分、负0分、实际和棋50分；到固定半回合预算时截断，给中性50分而不判和棋。
等待模型时游戏暂停，不使用搜索、随机走子或脚本代打。
规则来自MIT Decision Arena提交4e2bda1d6c9b2545837761e2c7d19e2ce546aa9d的freestyle-1。'''
SCORE_SPEC = {'id': 'gomoku-outcome-v1', 'min': 0, 'max': 100, 'direction': 'maximize',
              'minimum_mean_gain': 2.0, 'perspective': 'candidate_seat',
              'win': 100, 'loss': 0, 'draw': 50, 'cutoff': 50,
              'cutoff_is_draw': False,
              'description': '真实胜负和100/0/50；达到预算仅中性50分，明确截断，不判和棋。'}
OPPONENT_POLICY = {
    'decision_instructions': '你是冻结参考对手，执observation.yourSeat对应颜色。从完整合法空位候选选一步。优先完成己方连五，其次防止对手下一步连五；协调进攻与防守，观察四条方向的威胁，不故意让候选方获胜。',
    'strategy_notes': '固定参考策略gomoku-reference-v1；以赢棋为目标，依据当前棋盘逐步选择。没有搜索工具、自动落子或额外信息。',
}


class GomokuSession:
    def __init__(self, seed=0, max_steps=81, size=9):
        if type(seed) is not int or seed < 0:
            raise ValueError('seed must be a nonnegative integer')
        if type(size) is not int or size not in (9, 15):
            raise ValueError('gomoku_size must be 9 or 15')
        if type(max_steps) is not int or not 1 <= max_steps <= 2000:
            raise ValueError('max_steps must be 1..2000')
        self._bridge = NodeBridge('board_bridge.cjs')
        try:
            self._state = self._bridge.request({'op': 'init', 'game': 'gomoku', 'seed': seed,
                                                'max_steps': max_steps, 'gomoku_size': size})
        except Exception:
            self._bridge.close()
            raise

    def snapshot(self):
        return {**deepcopy(self._state['snapshot']), 'rules_version': RULES_VERSION}

    def candidates(self):
        return deepcopy(self._state['candidates'])

    def step(self, action_id):
        if self._state['snapshot']['done']:
            raise RuntimeError('episode already ended')
        if not isinstance(action_id, str) or action_id not in {action['id'] for action in self.candidates()}:
            raise ValueError('action must be in the complete current Gomoku catalog')
        self._state = self._bridge.request({'op': 'step', 'action': action_id,
                                            'seat': self._state['snapshot']['turn']})
        return self.snapshot()

    def close(self):
        self._bridge.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class GomokuAdapter:
    id = 'gomoku'
    name = '五子棋'
    rules_version = RULES_VERSION
    rules = RULES
    score_spec = SCORE_SPEC
    defaults = {'gomoku_size': 9, 'max_steps': 81}
    opponent_policy = OPPONENT_POLICY

    def validate_config(self, config):
        if type(config.get('gomoku_size', 9)) is not int or config.get('gomoku_size', 9) not in (9, 15):
            raise ValueError('五子棋gomoku_size必须为9或15')
        steps = config.get('max_steps', 81)
        if type(steps) is not int or not 1 <= steps <= 2000:
            raise ValueError('五子棋max_steps必须为1到2000的半回合数')

    def scope_config(self, config):
        self.validate_config(config)
        return {'gomoku_size': config.get('gomoku_size', 9), 'max_steps': config.get('max_steps', 81),
                'start_position': 'empty_board', 'candidate_seat': 'seed_parity',
                'reference_policy_version': 'gomoku-reference-v1'}

    def context(self, config):
        return {'game_id': self.id, 'name': self.name, 'rules_version': self.rules_version, 'rules': self.rules,
                'score_spec': self.score_spec, 'config': self.scope_config(config),
                'opponent_policy': self.opponent_policy, 'evaluation': {'seed_modulus': 2},
                'source': {'repository': SOURCE_URL, 'commit': SOURCE_COMMIT, 'license': 'MIT', 'rules_version': 'freestyle-1'},
                'scenario_policy': 'standard_empty_board_repeated_trials_seat_by_seed_parity',
                'trial_design': '标准空盘重复试验；偶数候选执黑，奇数执白。不同种子不是不同开局。',
                'action_policy': '完整空交叉点集合，未用启发式或搜索剪枝。双方每次落子都由Decision选择。',
                'limitations': '自由五子棋，无禁手、交换开局；截断中性分不是和棋或胜率证据。'}

    def create(self, seed, config):
        self.validate_config(config)
        return GomokuSession(seed=seed, max_steps=config.get('max_steps', 81), size=config.get('gomoku_size', 9))
