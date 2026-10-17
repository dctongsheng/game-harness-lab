# Game Harness Lab

**通用游戏 Harness 进化框架：游戏适配器 + Decision 玩家 + GEPA 局后优化 + 验收后导出。** 当前支持贪吃蛇、中国象棋、军棋、五子棋、俄罗斯方块、原创平台跳跃、Overcooked-AI 双人厨房和宝可梦对战（poke-env）。

所有局内动作都由 StartLux Decision 选择，显式 `think_mode=never`、`allow_none=false`。GLM-5.3 负责游戏资料、策略设计、历史经验提取与局后反思。适配器负责规则、完整动作集和计分，运行器负责模型调用与执行校验。

## 启动

需要 Python 3.11+；中国象棋、军棋、五子棋、俄罗斯方块和平台跳跃的规则引擎需要 Node.js 18+。Overcooked 另用独立 Python 3.10 环境：主环境装好后运行 `bash scripts/setup_overcooked.sh`（需要 uv），不会更改主环境的 Python。宝可梦另需 Node.js 22.18+，运行 `bash scripts/setup_pokemon.sh` 安装固定 poke-env 和本地 Showdown。已在 macOS 验证；六款基础游戏的已打包引擎不需要 npm 安装。

```sh
./setup.sh
# 在 .env 配置自己的模型地址和 COACH_API_KEY；不要提交 .env
./run.sh
```

打开 **http://127.0.0.1:18765**，选择游戏，点击开始实验。支持暂停、继续、停止、历史回放、资料/策略对比以及已批准版本回滚。

本机配置的玩家为 `http://127.0.0.1:18080/v1/systemone` / `startlux-decision-27b-autothink`，教练为 `glm-5.3`。需要认证时，在本地 `.env` 设置 `DECISION_API_KEY_FILE=~/.config/startlux-decision/api.key`；密钥只进入 HTTP Authorization，不进入模型 state、实验 manifest 或 Git。模型服务的全局思考默认值未改，每次玩家请求覆盖为 `never` 并验证遥测。

## 如何进化

```mermaid
flowchart LR
    A[游戏资料与初版 Harness] --> B[Decision 玩一局]
    B --> C[保存轨迹和得分]
    C --> D[GEPA + GLM 反思与选优]
    D --> E[新旧版本配对验收]
    E -->|通过| F[自动导出 + 下一局使用]
    E -->|未通过| G[保留原版]
    F --> B
    G --> B
```

每个正式回合创建一个优化任务，任务中的评测局不会递归优化。每局冻结策略；GEPA 只修改两个文本组件，不能改规则、分数、参考对手或执行器。GEPA 固定源码 commit，Python 依赖固定在 `requirements.lock`。

初始化时，LLM 会收到当前游戏的真实观察样本、动作语义和 Decision 能力合同：单步 choice、关闭思考、没有跨步会话记忆或搜索辅助。它先生成游戏资料和初始策略，再由真实对局检验；合同适配并不保证初版一定正确。模型、规则和合同哈希随版本保存。

局后反思默认使用 `complete_events_v1`：在预算内保留完整紧凑事件，优先提供终局、漏胜和循环等关键片段，明确哪些步骤被省略。军棋只包含候选方合法可见观察与公共后果。旧字符摘录 `legacy_excerpt_v1` 仅为可复现对照保留；两者不改变局内观察、动作集合或计分。

| 游戏 | 动作与得分 | 额外说明 |
| --- | --- | --- |
| 贪吃蛇 | 上下左右四方向始终提供；食物数为得分 | 不用寻路器或安全动作过滤代打 |
| 中国象棋 | 每步完整规则合法 ICCS 走法；双方均由 Decision 下棋 | 候选与固定参考策略对战；真实胜负与步数截断代理分明确区分 |
| 暗棋军棋 | 完整合法对局走法；真实胜/负/和为100/0/50 | 环境预置合法布阵，不优化布阵；候选、对手、教练与公共页面分开管理可见信息 |
| 五子棋 | 全部空位，9×9或15×15自由五子棋 | 真实胜/负/和100/0/50；种子交换候选黑白角色 |
| 俄罗斯方块 | 原生按键及100/300/1000ms持续时间 | 原生消行积分，保留重力/SRS/hold；不给存活加分 |
| 原创平台跳跃 | 原生左右/跳跃及按住时长 | 三个固定关卡，进度/金币/通关原生积分；不是原版超级马里奥 |
| 宝可梦 poke-env | 第三世代单打3v3，双方完整合法招式与换人均由 Decision 选择；原生胜/负/和100/0/50 | 双方秘密提交后结算；隐藏队伍不泄漏；预算截断不计分、不选优、不发布 |
| Overcooked-AI | 两名厨师均由 Decision 从六个原生动作选择；正确送餐才计原生分 | 两次选择同步推进一 tick；三张官方布局；独立 Python 3.10 规则进程，不用规划器 |

中国象棋参考 `decision-arena` 的规则集，使用固定 BSD 引擎。标准局面重复试验，种子奇偶交换候选执红黑；不同种子不代表不同开局。到步数上限只按固定子力差评分，不能当作获胜或胜率。

## Harness 保存在哪里

0.5.0 新增俄罗斯方块的实验性上下文组件：可独立启用结构化棋盘事实，以及由 Decision 自己更新的每块目标与短期动作记忆。四组对照命令及真实结果见 [V5 实验文档](docs/tetris-context-validation-v5.md)。这些选项通过实验运行器显式启用，网页常规运行默认关闭。LLM 可参与上下文整理；本轮在对局前完成历史经验提取并冻结，以隔离两个实验因素。

运行中的全部版本与轨迹位于本地 `data/`，不提交 Git。**通过配对验收的版本自动导出到：**

```text
harnesses/<游戏>/<版本>/
  manifest.json
  game.json
  strategy.json
  evaluation.json
  README.md
```

包包含游戏资料、固定规则与评分、策略、版本血缘、验收结果和文件哈希。未通过候选不会导出成批准版本。[查看已导出 Harness](harnesses/)。

`HARNESS_GIT_SYNC=1` 可在导出后自动提交该包并推送 `origin`，不暂存其他源码或用户改动；示例配置默认关闭。网络失败保留本地产物并标记待重试。详情见 [框架设计](docs/generic-framework-v2.md)。

## 验证与文档

```sh
.venv/bin/python -m pytest -q
.venv/bin/python scripts/audit_saved_run.py
```

审计脚本根据保存的动作重放各游戏，核对完整候选目录、模型回执、思考模式、双方角色和最终得分，不再次调用模型。

- [V2 通用框架与新增游戏接口](docs/generic-framework-v2.md)
- [V2 实测结果](docs/validation-v2.md)
- [V3 四游戏适配与验证边界](docs/additional-games-v3.md)
- [六游戏 RSI 诊断与实验设计](docs/rsi-diagnosis-v1.md)
- [V4 完整反馈与真实对照实验](docs/feedback-validation-v4.md)
- [V5 俄罗斯方块结构事实与目标记忆](docs/tetris-context-validation-v5.md)
- [V6 俄罗斯方块按键控制、LLM 修订与配对实验](docs/tetris-control-validation-v6.md)
- [宝可梦 poke-env 接入与真实实验](docs/pokemon-integration-v1.md)
- [Overcooked-AI 双人厨房接入与真实实验](docs/overcooked-integration-v1.md)
- [V1 总结](docs/project-summary-v1.md)、[V1 历史实测](docs/validation-v1.md)
- [原始长期架构提案](docs/architecture.md)、[路线图](docs/roadmap.md)
- [第三方来源与许可证](THIRD_PARTY_NOTICES.md)

`contracts/` 和 `examples/` 中的完整声明式配置仍是长期设计提案；当前可运行接口在 `games/base.py`。原版马里奥、真实时间持续推进控制和权重训练尚未实现。Overcooked 的真实回放、同步时间和计分显示已在浏览器验证；这不代表其余游戏的所有页面交互均已验收。
