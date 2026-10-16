"""Original three-level platform game; this is not Nintendo Super Mario."""
from .arcade import ArcadeAdapter, CONTROL_RULES


class PlatformerAdapter(ArcadeAdapter):
    id = "platformer"
    name = "原创平台跳跃"
    rules_version = "game-harness-lab/platformer-paused-v1-arena-4e2bda1"
    defaults = {"max_steps": 60, "train_games": 3, "validation_games": 3, "promotion_games": 6}
    scenario_policy = "seed%3+1选择三种固定关卡：教学障碍、坑洞、移动敌人。不同seed可能是同一关卡重复试验，不是无限随机地图。"
    evaluation = {"seed_modulus": 3, "minimum_pairs": 6,
                  "seed_semantics": "seed modulo three selects fixed level 1/2/3; repeated IDs do not create new maps"}
    score_spec = {"id": "arena-platformer-points-v1", "min": 0, "max": 11300,
                  "direction": "maximize", "unit": "原生进度积分", "minimum_mean_gain": 100,
                  "description": "points=floor(最远进度百分比×10)+金币×100+通关10000；最多3金币，存活不加分。"}
    rules = """这是一款原创MIT平台跳跃游戏，不是原版超级马里奥，不含任天堂素材。
三个固定关卡按seed%3+1选择：1教学障碍、2坑洞、3移动敌人；单关一命，最多7200tick。
位置单位像素，左上角原点，x右增y下增。60Hz整数物理，左右加速0.5px/tick²，最高3px/tick，
松键摩擦0.75px/tick²；重力0.5px/tick²，跳起速度-10.5px/tick，下落上限16px/tick。
跳跃仅着地且jump新按下时触发，空中按键不会等待落地后跳；没有二段跳或自动连跳。
碰敌、接触危险区或落坑即结束，不能踩敌。先判致死碰撞再判金币和出口，接触出口通关。
目标最大化points=floor(历史最远进度百分比×10)+金币数×100+通关奖励10000；三枚金币，最高11300。
当前位置、速度、地面状态、所有平台和坑洞边界、金币、敌人及其当前运动、终点全部来自原生观察。
已经获得的最远进度不会因后退而减少。\n""" + CONTROL_RULES
