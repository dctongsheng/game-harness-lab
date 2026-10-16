# GEPA 与 JevHarness 源码调研

研究日期：2026-10-16（Asia/Shanghai）。只读核查官方仓库与固定提交源码；没有安装依赖、调用模型或运行游戏。下文“建议”为本项目设计，不是上游现成功能。

| 项目 | 核查版本 | 授权情况 |
| --- | --- | --- |
| GEPA | `f1cfdee2856e9dc4b5b4dc5cc2e391d0aa20d819` | [MIT](https://github.com/gepa-ai/gepa/blob/f1cfdee2856e9dc4b5b4dc5cc2e391d0aa20d819/LICENSE) |
| JevHarness | `d7676033c34c0e5636829b769d3641a2e61580f1` | 递归目录未发现 LICENSE/COPYING，项目元数据未声明许可证；本项目仅借鉴设计，不复制其实现 |

## 1. GEPA 可以作为底层，但不能直接套默认 Agent

**已验证：** `gepa.optimize` 接收 `seed_candidate: dict[str,str]`、`trainset`、`valset` 和自定义 `adapter`。因此可将打法说明、决策问题、状态描述模板、记忆规则分别表示为文本组件。GEPA 根据轨迹反思并搜索候选，不负责游戏运行或线上版本切换。默认 `DefaultAdapter` 会调用 `task_lm` 完成任务；把游戏动作接到这个默认路径，会让通用 LLM 参与局内选择。[API](https://github.com/gepa-ai/gepa/blob/f1cfdee2856e9dc4b5b4dc5cc2e391d0aa20d819/src/gepa/api.py) · [默认适配器](https://github.com/gepa-ai/gepa/blob/f1cfdee2856e9dc4b5b4dc5cc2e391d0aa20d819/src/gepa/adapters/default_adapter/default_adapter.py)

应显式实现 `GEPAAdapter`：`evaluate(batch,candidate,capture_traces)` 返回 `EvaluationBatch(outputs,scores,trajectories)`；分数越高越好，输出与轨迹必须逐局对齐。`make_reflective_dataset` 将轨迹转为按组件组织的 JSON 反馈。GEPA 不解释轨迹，状态、候选动作、模型输出、执行结果与死亡原因都要自己记录。[接口契约](https://github.com/gepa-ai/gepa/blob/f1cfdee2856e9dc4b5b4dc5cc2e391d0aa20d819/src/gepa/core/adapter.py)

GEPA 的验证集参与候选选择；未提供 `valset` 时会复用训练集。因此验证成绩不等于最终泛化成绩。建议另设不进入 GEPA 的审计测试集；日常发布门槛使用的场景长期参与选优后，也应视为开发数据。贪吃蛇、俄罗斯方块与马里奥分别搜索，不能把原始分数直接混合取均值。

评测还应区分策略失败与基础设施失败：撞墙、落坑等进入得分；服务断连、环境崩溃则重试或标记该批结果不可用，避免把网络问题误学成策略问题。日志同时保留原始游戏分数、用于搜索的固定换算分数、决策耗时和异常率。相同种子进行新旧配对比较，只能减少环境随机性，远程模型本身仍可能不完全可复现。

## 2. JevHarness 值得借鉴什么

**已验证：** 它把完整 PipelineSpec 序列化为 GEPA 的 `pipeline` 组件，允许同时改变节点、依赖图、Jev 问题、Python 特征代码、输出和记忆规则；模型标识被固定。反思只读取训练反馈，并归档发给 LLM 的实际输入与响应。[进化实现](https://github.com/TianyuCodings/JevHarness/blob/d7676033c34c0e5636829b769d3641a2e61580f1/auto_jev/evolution.py)

它的 `full_feedback` 保留完整轨迹，不静默裁剪；超过配置字节上限会报错，另支持无损压缩。这适合追溯遗漏了哪些证据。本项目面对实时游戏长轨迹，建议完整原始日志落盘，另生成标明抽样范围的失败窗口供反思，而不是声称 LLM 看过整局。[反思输入](https://github.com/TianyuCodings/JevHarness/blob/d7676033c34c0e5636829b769d3641a2e61580f1/auto_jev/reflection.py)

**需要更新此前判断：** 当前 Pokémon 入口已有 `train / validation / test / engineering` 分割、不同组合与种子、输入和源码哈希；两个实验分支都完成选优并冻结后才读取测试局。它还明确限定结论适用于预设队伍与对手策略。其独立测试设计比仅查看 README 中 Eval 胜率更完整，但源码存在不代表实验结果已由本项目复现。[实验入口](https://github.com/TianyuCodings/JevHarness/blob/d7676033c34c0e5636829b769d3641a2e61580f1/examples/pokemon/run.py)

最适合吸收的是：可信游戏桥接、环境确认终局分数、请求编号防过期、动作目录校验，以及动作被游戏拒绝时回滚局内记忆。[游戏适配器](https://github.com/TianyuCodings/JevHarness/blob/d7676033c34c0e5636829b769d3641a2e61580f1/examples/pokemon/adapter.py)

## 3. “所有决策由 Decision 模型完成”必须额外约束

**已验证：** JevHarness 运行时注入 Jev，未在此路径创建通用 LLM；但它允许 Python、表达式及最终 `output` 计算。Pokémon mixed 路径只核验最终动作是否合法，并未强制动作直接来自 Jev；另有明确禁止 Jev 的 code-only 对照分支。[运行时](https://github.com/TianyuCodings/JevHarness/blob/d7676033c34c0e5636829b769d3641a2e61580f1/auto_jev/runtime.py)

**设计判断：** “局内没有通用 LLM”仍不等于“局内每个选择由 Decision 模型做出”。生成代码可提前算好最佳动作，或把候选削成唯一答案。本项目应固定动作映射器：最终 action_id 只能取自 StartLux Decision/Jev 的合法响应；代码只能计算客观特征、列举规则允许的动作并执行。不得启发式排序选优、策略性删候选、自动重规划宏动作或失败后切到 LLM。选择失败应暂停、重试或结束该局并记录，不能悄悄换成规则玩家。

## 4. 恢复能力与版本兼容

GEPA 的 `run_dir` 保存 `gepa_state.bin` 并支持恢复；`write_agent_state` 可导出逐轮可读记录；适配器可通过 `get_adapter_state / set_adapter_state` 保存自己的状态。JevHarness 再增加反思响应日志、轮次统计、输入哈希和冻结产物；它明确不保证“付费响应返回但尚未记日志就崩溃”时绝不重复调用。[GEPA API](https://github.com/gepa-ai/gepa/blob/f1cfdee2856e9dc4b5b4dc5cc2e391d0aa20d819/src/gepa/api.py) · [JevHarness 恢复逻辑](https://github.com/TianyuCodings/JevHarness/blob/d7676033c34c0e5636829b769d3641a2e61580f1/auto_jev/evolution.py)

JevHarness 固定 `gepa==0.1.4`，其 `batch_evaluate(items)` 与核查的 GEPA main 中带 `capture_traces` 参数的接口有差异。实现时固定一个经验证版本，按其协议开发，不能直接复制不同版本的适配器。[依赖声明](https://github.com/TianyuCodings/JevHarness/blob/d7676033c34c0e5636829b769d3641a2e61580f1/pyproject.toml)

## 5. 具体接入建议

1. `GameGEPAAdapter` 的 DataInst 使用不可变 `EpisodeSpec`：游戏/关卡/种子/预算/观测协议版本；`evaluate` 只实例化固定 Harness + DecisionProvider，运行整局并从游戏环境取得分数。
2. Candidate 第一版只包含 `decision_instructions`、`state_template`、`strategy_notes`；规则、计分器、动作目录、模型 ID、数据划分及执行器均不在搜索空间。后续再受控开放特征代码。
3. `make_reflective_dataset` 必含触发优化的那一局及对应 Harness 哈希，再加入历史失败样本；不能仅依赖随机抽样而遗漏刚结束的局。反思用 `custom_candidate_proposer` 或 `reflection_lm`，游戏进程不持有教练模型客户端。
4. 每个正式训练局结束创建唯一优化作业，至少尝试一次反思；设置 `skip_perfect_score=False`，按已完成提案次数与预算停止。GEPA 内部评测局属于该作业，不递归创建优化作业。
5. 每个作业固定训练/验证清单并独立 `run_dir`；同一作业崩溃才恢复。新增数据建立新作业，以稳定版作为种子，外部版本库保存完整血缘；不要修改数据后继续套旧 checkpoint。候选通过配对评测才在下一局发布，否则保留稳定版。

以上建立的是可核验的 Harness 进化流程，不涉及模型权重训练，也不保证每局分数上升。
