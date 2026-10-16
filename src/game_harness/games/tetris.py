"""Falling-block rules adapter; every native input is chosen by Decision."""
from .arcade import ArcadeAdapter, CONTROL_RULES


class TetrisAdapter(ArcadeAdapter):
    id = "tetris"
    name = "俄罗斯方块"
    rules_version = "game-harness-lab/tetris-paused-v1-arena-4e2bda1"
    defaults = {"max_steps": 80}
    scenario_policy = "整数seed转字符串，驱动参考引擎的确定性7-bag；仅可见五块预览，没有rng或其余队列。"
    evaluation = {"seed_modulus": 1, "minimum_pairs": 4,
                  "seed_semantics": "seed controls deterministic seven-bag piece order"}
    score_spec = {"id": "arena-tetris-points-v1", "min": 0, "max": 16594560000,
                  "direction": "maximize", "unit": "原生消行积分", "minimum_mean_gain": 100,
                  "description": "只使用原生消行points；1/2/3/4行得100/300/500/800乘消行前等级。软硬降和存活不加分。",
                  "upper_bound_basis": "保守上界：7200tick×每tick最多一次锁块×800×最高等级2881；不是实际可达到的目标。"}
    rules = """俄罗斯方块为Decision Arena的MIT规则实现，10×20可见格，上方两行生成区。
7-bag，预览五块，每块hold一次。初始每48tick下一行，每10行加快3tick，最低6tick；
接地30tick锁定，最多15次移动/旋转重置锁定计时。左右按住9tick后每3tick重复，软降每3tick一行。
SRS顺/逆时针旋转，硬降只将当前横向位置与朝向下的方块落到底并锁定，不自动寻找落点。
消1/2/3/4行得100/300/500/800乘消行前等级；等级=1+累计消行数整除10。
没有软硬降分、T-spin或combo奖励。顶部锁块或生成受阻结束，引擎最多7200tick。
观察包含完整当前棋盘（含两行生成区）、当前方块、五块可见预览、暂存、重力与锁定计时；隐藏随机状态不暴露。
目标是在决策预算与物理时钟内最大化原生points。\n""" + CONTROL_RULES
