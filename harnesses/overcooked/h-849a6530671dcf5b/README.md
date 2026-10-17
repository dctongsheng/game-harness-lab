# Overcooked-AI 双人厨房 Harness · h-849a6530671dcf5b

此包已通过记录在 evaluation.json 的配对门槛；不是长期泛化证明。

- 游戏：`overcooked`；规则：`game-harness-lab/overcooked-joint-v1-739950a`。
- 原版本均分：0.0；候选均分：20.0；配对数：4。
- `game.json`：固定规则、评分、配置、对手策略和初始化资料。
- `strategy.json`：可供 Decision 使用的两段策略文字。
- `evaluation.json`：训练选优摘要与独立于本次搜索的配对验收。
- `manifest.json`：版本血缘、模型身份与文件 SHA-256。

本包需由兼容游戏适配器运行，不包含模型权重或原始 API 轨迹。
