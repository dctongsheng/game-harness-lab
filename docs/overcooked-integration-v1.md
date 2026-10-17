# Overcooked-AI 接入 v1

本版把原生双人厨房接入共用 Harness 进化引擎。两位厨师共享一份冻结的 Harness，每次基础动作均由 Decision 选择；LLM 负责局前设计与局后反思，GEPA 负责候选搜索和选择。2026-10-17 的真实实验中，4 对额外验收的平均分从 **0 提高到 20**，但候选仍有 **2/4 局零分**。已通过现有工程门槛并导出，尚不能称为稳定完成送餐。

## 环境与实现

采用 [HumanCompatibleAI/overcooked_ai](https://github.com/HumanCompatibleAI/overcooked_ai)，固定提交 [`739950a079cdaed5a44fcc662efc40244c205d06`](https://github.com/HumanCompatibleAI/overcooked_ai/tree/739950a079cdaed5a44fcc662efc40244c205d06)，许可证为 [MIT](https://github.com/HumanCompatibleAI/overcooked_ai/blob/739950a079cdaed5a44fcc662efc40244c205d06/LICENSE)。本机安装包的 `direct_url.json` 与 LICENSE 已核对；worker 每次初始化检查 Python 3.10 与安装来源提交。

| 部件 | 职责 |
|---|---|
| [`games/overcooked.py`](../src/game_harness/games/overcooked.py) | 游戏合同、完整动作目录、配置验证及原生进程通信 |
| [`games/overcooked_worker.py`](../src/game_harness/games/overcooked_worker.py) | 在独立 Python 3.10 环境调用原生规则，保存同时动作的提交屏障 |
| [`runtime.py`](../src/game_harness/runtime.py) | 对每次 Decision 回执绑定观察、动作目录和请求编号，验证后执行 |
| [`service.py`](../src/game_harness/service.py)、[`evolution.py`](../src/game_harness/evolution.py) | 复用初始化、GEPA、配对验收、版本登记及导出流程 |
| [`feedback.py`](../src/game_harness/feedback.py) | 向局后 LLM 保留厨房布局、厨师、持有物、锅及联合动作等完整事件 |
| [`run_overcooked.py`](../scripts/run_overcooked.py) | 有固定预算的真实实验入口，保存模型请求与响应证据 |
| [`audit_overcooked_run.py`](../scripts/audit_overcooked_run.py) | 不调用模型，读取记录并重新执行原生规则，核验请求、动作和积分 |

原生核心与应用环境分开，安装到 `.venv-overcooked/`；应用继续使用现有 `.venv/`。worker 不接收模型密钥，不创建规划器，也不选择动作。它直接调用原生 `get_state_transition`，关闭势函数计算；游戏的交互、碰撞和烹饪规则由上游执行。

## 动作、时间与计分

每位厨师始终得到 `north / south / east / west / stay / interact` 六个完整选项。方向动作遇阻也可能改变朝向；交互作用于面前相邻格。没有自动寻路、安全动作过滤、宏动作或脚本队友。

一个厨房 tick 分为两次模型调用：

1. `chef_0` 选择动作，暂存于 worker。世界、积分和烹饪时间不变。
2. `chef_1` 看同一个已完成 tick 的世界，不能看到厨师 0 的待提交选择。
3. 两个合法动作一起提交给原生规则，推进一个 tick。

因此 `step/max_steps` 统计 Decision 次数，`native_tick/max_ticks` 统计厨房时间。默认 **240 次决策 = 120 个 tick**，预算必须是偶数。等待模型不推进游戏；任一模型调用失败便停止，不用默认动作补齐。

双方请求均固定 `think_mode=never`。真实实验入口还要求响应明确报告 `thought.used=false`、`thought.tokens=0`。模型标识按服务返回名称核对，不能据此验证服务内部的权重文件。

主指标 `score` 只累计原生 `sparse_reward_by_agent`，不加入拿取、备菜、移动或存活奖励。`deliveries/delivery_counts` 是原生上菜事件数，**包括错误配方的零分上菜，不能直接当作有效订单数**。预算结束属于截断，不记胜负。

当前支持 `cramped_room`、`asymmetric_advantages`、`coordination_ring` 三张固定地图。`seed` 只是重复试验编号，各编号使用同地图的原生标准开局；它不生成新地图，也不控制远端模型随机性。

## 安装与启动

以下命令在仓库根目录运行，要求现有应用 `.venv/` 可用，并已安装 `uv`：

```bash
bash scripts/setup_overcooked.sh
```

脚本使用 [`requirements-overcooked.txt`](../requirements-overcooked.txt) 固定依赖，创建独立 Python 3.10 环境，最后检查原生初始状态。若使用别处已安装的相同环境，可设置 `OVERCOOKED_PYTHON` 为其 Python 可执行文件路径。

模型沿用本地 `.env` 的 `DECISION_URL`、`DECISION_MODEL`、`DECISION_API_KEY_FILE`、`COACH_BASE_URL`、`COACH_MODEL`、`COACH_API_KEY` 等设置。Decision 密钥由本地密钥文件读取；不要把密钥写入文档或提交 Git。当前方案使用 Startlux Decision 与 GLM-5.3。

运行一次有证据记录的初始化与 GEPA 循环：

```bash
.venv/bin/python scripts/run_overcooked.py run \
  --data-dir data/experiments/overcooked-v1 \
  --layout cramped_room \
  --max-steps 240
```

输出目录必须尚不存在，脚本不会覆盖、续跑或隐式重试。CLI 的固定预算为：正式触发局 1 局、训练编号 1 个、选优编号 1 个、GEPA 反思 1 轮；若选中不同候选，追加 4 对交替顺序验收。上限为 14 局、`14 × max_steps` 次 Decision 请求尝试、4 次教练请求尝试。未必耗尽上限；GEPA 保留父版本时，不强行增加发布对局。

配对发布门槛为至少 4 对、平均增益至少 1 分、没有单对退步。这是工程上的小样本经验门槛，不代表统计显著性或跨地图泛化。两位厨师都采用被测 Harness；没有固定强队友帮助候选拿分。

## 界面与回放

CLI 实验结束后，启动该目录的回放与交互界面：

```bash
.venv/bin/python scripts/run_overcooked.py serve \
  --data-dir data/experiments/overcooked-v1 \
  --port 18766
```

打开 [本地实验台](http://127.0.0.1:18766/)，选择 **Overcooked-AI 双人厨房**，可查看厨师位置、朝向、持有物、锅内食材与烹饪状态，以及原生送餐分、tick、当前等待决策的厨师。逐步回放忠实展示已存状态：第一位厨师决策后，棋盘仍冻结。

`serve` 是可发起新实验的普通实验台，不是只读服务。**不要同时对同一目录运行 CLI 实验与 `serve`**：普通实验台启动会恢复登记并处理未完成作业。用于严格证据实验时优先使用上面的 CLI；界面发起的普通运行沿用共用服务，其回执不包含 CLI 额外记录的完整 HTTP 请求。

## 证据、审计与 Harness 保存

实验目录保存 `protocol.json`、源代码哈希、初始化合同、教练输入输出、`calls.jsonl`、`episodes/`、`traces/`、`audits/`、`jobs/` 和 `registry.sqlite`。完成后写入 `summary.json`，失败则写入 `failed.json`。不完整对局不得作为策略零分参与评测。

独立重放与核验：

```bash
.venv/bin/python scripts/audit_overcooked_run.py \
  data/experiments/overcooked-v1 \
  > data/experiments/overcooked-v1/audit.json
```

审计打开只读 SQLite，启动新的原生规则进程，核对每一步前后状态、完整六动作目录、双方 Decision 回执、实际 HTTP 请求内容、固定模型与思考关闭证据、联合提交和原生积分。这里的“独立”指与在线决策分离的重放检查，仍使用同一固定上游规则实现，不是第二套游戏引擎。

初始与候选 Harness 保存在该实验目录的版本登记中。CLI 只有通过验收的候选才导出到 `harnesses/overcooked/<版本>/`，包含游戏资料、策略和评测证据；未通过时保留旧版，不伪造“通过”包。隔离实验目录不启用生产注册表的自动 Git 同步，代码、报告及确实生成的导出包需在审查后单独提交同步。原始 `data/` 与密钥文件不提交。

## 真实实验结果

本地证据目录为 `data/experiments/overcooked-v1/`。开跑前固定预算与 22 个源码文件哈希，结束后全部匹配，没有按得分中途改规则、策略、地图或预算。配置为 `cramped_room`、每局 240 次 Decision 选择 / 120 tick，玩家 `startlux-decision-27b-autothink`，教练 `glm-5.3`。LLM 共调用 2 次：一次初始化、一次 GEPA 反思；无局内教练调用。

初始版本 `h-f5e090c70ba7232a` 与候选 `h-849a6530671dcf5b` 均由实际 LLM 返回，未人工改写。GEPA 0.1.4 真实调用 `gepa.optimize`，完成 4 次搜索计分；正式触发局、搜索局和验收局共 **13 局**。

| 阶段 | 初版原生分 | 候选原生分 | 解释 |
|---|---:|---:|---|
| 正式触发局 | 0 | — | 供局后反思使用 |
| 搜索训练 | 0 | 60 | 候选送出 3 碗有效汤，参与优化 |
| 搜索选优 | 0 | 40 | 参与 GEPA 选版，不能当最终验收 |
| 额外验收 1，编号 1042 | 0 | 40 | 候选送出 2 碗有效汤 |
| 额外验收 2，编号 2042 | 0 | 40 | 候选送出 2 碗有效汤 |
| 额外验收 3，编号 3042 | 0 | 0 | 汤煮熟后卡住，没有送出 |
| 额外验收 4，编号 4042 | 0 | 0 | 汤煮熟后卡住，没有送出 |
| **额外验收均分** | **0** | **20** | **平均增益 +20，候选有分局 2/4** |

验收按旧/新、新/旧交替顺序执行。不同编号对应同一确定性标准开局，既不是新地图，也不是远端模型随机数种子。4 对样本不支持统计显著性声明。

现有门槛要求四对、均分提高至少 1、每对不退步；它允许 `0→0`，所以本轮判定 `promoted`，并自动导出 [h-849a6530671dcf5b](../harnesses/overcooked/h-849a6530671dcf5b/README.md)。**通过该门槛不等于每局都会成功，也不等于候选文字完全符合输入合同。** 本轮保留了已冻结门槛，没有看到结果后调整标准。

### 从真实轨迹看，改进和问题分别在哪里

初版正式局两位厨师的 240 个选择全部为 `interact`，位置和空手状态始终未变，锅内没有食材。初版文字其实已经要求先移动到目标旁再交互，因此不能把失败简单描述为“原提示没有移动规则”。它说明这套表达与 Decision 的实际单步选择没有配合好，尚不能孤立归因于模型本身。

LLM 候选补充了当前公开地图的站位、朝向、厨师分工和交互前置条件。真实训练局在 tick 43、80、116 送出有效汤。四局额外验收中，候选均完成三洋葱入锅、开火和煮熟，初版均未开始装锅，表明备菜环节的行为确实改善。

但两局失败都发生在盛汤前的协作。编号 3042 的局中，tick 42 汤已熟；tick 45，持洋葱厨师位于 `[2,1]` 朝锅，持盘厨师位于 `[1,1]` 朝空台面，二者仍选择无效交互。随后反复交互、南移、北移，形成三 tick 循环直至预算结束。编号 4042 从 tick 46 起出现同类循环。对应完整局面摘录保存在 [results.json](experiments/overcooked-v1/results.json) 的 `failure_example`。

候选还要求比较“当前状态与上一 tick 是否相同”，但 `last_joint_actions` 只有动作，`joint_history` 只有动作和奖励等事实，没有上一 tick 的完整位置、持有物或锅状态。该条件仍缺少可计算输入，不能宣称已经实现状态记忆或可靠反循环。候选也明确写入了 `cramped_room` 坐标，属于当前地图的策略；本轮未验证另外两张地图的模型表现。

下一轮最值得验证的是：向 Decision 提供可核验的最近状态变化摘要，并让 LLM 明确“持食材者让出熟锅入口、持盘者负责盛汤”的条件。应与本轮候选做固定预算对照，主指标仍为原生送餐分，同时预先规定有分局比例的可靠性要求；不能在运行时由代码或 LLM 代选移动动作。本轮没有做这些额外改动，也没有消融区分坐标、优先级和分工各自的贡献。

### 审计与回归验证

- **3120/3120 次实际 HTTP 请求与原始响应通过审计**，全部明确返回思考关闭、推理 token 为 0；双方各 1560 次动作，没有脚本队友或代走。
- **13/13 局完整、1560 个联合 tick 全量原生重放一致**；无基础设施失败、缺失请求或被记成零分的中断局。
- 同一玩家原生动作、完整六选项、冻结策略、请求绑定、首位厨师选择不泄露和稀疏积分均逐步核对。总体单次 Decision 平均耗时 146.43 ms，只作本轮运行观测，不是吞吐基准。
- 本次提交涉及的现有框架与新增测试 **618 项通过，另 9 个 subtest 通过**。命令和范围保存在 results；未将工作区另行开发的王者荣耀文件纳入本次提交。
- 浏览器实际验证了地图菜单、真实回放、首位厨师选择后 tick 不变、第二位选择后 tick 加 1，以及 40 分 / 2 碗的完成记录；检查时没有页面 console error。未通过浏览器额外发起模型实验。

可提交的 [冻结协议](experiments/overcooked-v1/protocol.json)、[结果与失败摘录](experiments/overcooked-v1/results.json)、[独立审计摘要](experiments/overcooked-v1/audit.json)、[LLM 初始 Harness](experiments/overcooked-v1/initial-harness.json) 已保存；完整请求、响应和轨迹留在被 Git 忽略的本地 `data/`。协议哈希为 `316dbdf2b0eb999c0b52a382ab9d8fcc565e06676f690029521bd5778696d259`。
