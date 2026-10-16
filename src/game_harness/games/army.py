"""Hidden Army Chess adapter; private rules state stays inside a Node process."""
from __future__ import annotations

from copy import deepcopy
from .node_bridge import NodeBridge

SOURCE_COMMIT = '4e2bda1d6c9b2545837761e2c7d19e2ce546aa9d'
SOURCE_URL = 'https://github.com/dctongsheng/decison-arena'
RULES_VERSION = 'game-harness-lab/army-prearranged-hidden-v1-4e2bda1'
RULES = '''暗棋军棋对局任务：12×5棋盘，每方25子，蓝方（seat=0，上方）先手，红方（seat=1，下方）后手。
坐标A-E从左至右，1-12从上至下；cells按逐行排列。环境按私有种子生成固定合法布阵并锁定双方准备；
本任务从playing阶段开始，不优化布阵，setup的交换和ready不属于本任务动作空间，模型不能索取种子或全明棋。
军旗在己方大本营、地雷在后两排、炸弹不在前线、行营初始不布子。
等级依次为司令、军长、师长、旅长、团长、营长、连长、排长、工兵；大吃小，同级同毁。
军旗和地雷不能移动；进入大本营的棋子不能再走。军旗被吃立即判负；炸弹与目标同毁（含雷），
工兵排雷且存活，其他棋子碰雷阵亡而雷留存。道路走一步，行营连线可斜走，行营内棋子不能被攻击。
铁路直行不限步，工兵可转弯，均不能越子；中央仅A/C/E列互通。失旗或无合法动作判负，同局面同轮转三次判和。
对手身份只按战斗证据逐步揭示，司令阵亡亮旗；unknown或?不是可访问的隐藏身份。
必须根据当前方observation和完整合法move:<from>:<to>目录作决定，目录无战术筛选。
双方每一步均由同一Decision模型选择；对手使用固定参考策略。候选胜100、负0、真实和50。
到半回合预算时截断给中性50分，不判和棋，不根据暗子子力计分。等待模型时棋局完全暂停。
私有种子实际改变双方初始合法布阵，并以奇偶决定候选席位；不得从种子重建暗子或将特定布阵写入策略。
规则来自MIT Decision Arena提交4e2bda1d6c9b2545837761e2c7d19e2ce546aa9d的army-hidden-1。'''
SCORE_SPEC = {'id': 'army-visible-outcome-v1', 'min': 0, 'max': 100, 'direction': 'maximize',
              'minimum_mean_gain': 2.0, 'perspective': 'candidate_seat',
              'win': 100, 'loss': 0, 'draw': 50, 'cutoff': 50,
              'cutoff_is_draw': False, 'uses_hidden_material': False,
              'description': '真实胜负和100/0/50；预算截断中性50分，不使用隐藏子力代理分。'}
OPPONENT_POLICY = {
    'decision_instructions': '你是冻结参考对手，执observation.yourSeat对应颜色。只依据本方当前可见棋子、战斗证据及完整合法走法选一步。保护军旗，利用行营与铁路，合理探测对手并保留工兵排雷，不假设unknown棋子的真实身份，不主动让棋。',
    'strategy_notes': '固定参考策略army-reference-v1。只作本次合法对局走法选择，不参与布阵，不获取种子或全明棋，不能跨角色调用观察。以赢棋为目标。',
}


class ArmySession:
    def __init__(self, seed=0, max_steps=100):
        if type(seed) is not int or seed < 0:
            raise ValueError('seed must be a nonnegative integer')
        if type(max_steps) is not int or not 1 <= max_steps <= 2000:
            raise ValueError('max_steps must be 1..2000')
        self._bridge = NodeBridge('board_bridge.cjs')
        try:
            self._state = self._bridge.request({'op': 'init', 'game': 'army', 'seed': seed, 'max_steps': max_steps})
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
            raise ValueError('action must be in the complete current Army playing catalog')
        self._state = self._bridge.request({'op': 'step', 'action': action_id,
                                            'seat': self._state['snapshot']['turn']})
        return self.snapshot()

    def close(self):
        self._bridge.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class ArmyAdapter:
    id = 'army'
    name = '暗棋军棋'
    rules_version = RULES_VERSION
    rules = RULES
    score_spec = SCORE_SPEC
    defaults = {'max_steps': 100}
    opponent_policy = OPPONENT_POLICY
    information_policy = {'feedback_visibility': 'candidate_before_only', 'seed_is_private': True,
                          'public_projection': 'public_view'}

    def validate_config(self, config):
        steps = config.get('max_steps', 100)
        if type(steps) is not int or not 1 <= steps <= 2000:
            raise ValueError('军棋max_steps必须为1到2000的半回合数')

    def scope_config(self, config):
        self.validate_config(config)
        return {'max_steps': config.get('max_steps', 100), 'setup': 'seeded_legal_prearrangement',
                'setup_optimization': False, 'candidate_seat': 'seed_parity',
                'reference_policy_version': 'army-reference-v1', 'terminal_identity_reveal': False}

    def context(self, config):
        return {'game_id': self.id, 'name': self.name, 'rules_version': self.rules_version, 'rules': self.rules,
                'score_spec': self.score_spec, 'config': self.scope_config(config),
                'opponent_policy': self.opponent_policy, 'evaluation': {'seed_modulus': 2},
                'source': {'repository': SOURCE_URL, 'commit': SOURCE_COMMIT, 'license': 'MIT', 'rules_version': 'army-hidden-1'},
                'information_policy': self.information_policy,
                'scenario_policy': 'private_seed_changes_legal_arrangements_and_candidate_seat_parity',
                'trial_design': '不同私有种子实际改变合法布阵；偶数候选执蓝、奇数执红；同种子配对评测相同初始布阵与席位。',
                'action_policy': '对局阶段完整合法走法。初始化时环境锁定种子合法布阵；不进入可优化布阵阶段。',
                'limitations': '本版只优化预置布阵后的对局策略；不是自由布阵实验。公共UI不展示双方私有棋子身份，终局也不全明；截断50分不是和棋。'}

    def create(self, seed, config):
        self.validate_config(config)
        return ArmySession(seed=seed, max_steps=config.get('max_steps', 100))
