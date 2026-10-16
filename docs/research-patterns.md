# 可借鉴的开源机制与架构约束

核查日期：2026-10-16。已阅读下列项目默认分支的官方说明、关键源码与许可证；没有安装运行，也未验证项目方效果数字。下面把源码事实与本项目设计建议分开。

## 已核实的机制

**Continual Harness：借鉴轨迹驱动的记忆维护，不照搬局内自修改。**

[`HarnessEvolver`](https://github.com/sethkarten/continual-harness/blob/main/agents/utils/harness_evolver.py) 读取最近轨迹，顺序更新提示词、子智能体、技能和记忆，各步骤单独捕获错误并保存日志。当前 `should_evolve` 忽略调用方传入的频率，实际采用早期每 25 步、后期每 100 步的自适应安排。更新路径直接写存储；在该路径中没有看到完整的新旧版本独立对照验收。技能提示允许代码读取游戏状态并调用按键工具，子智能体也能循环行动。因此不能直接迁入“局内只有 Decision/Jev”的运行环境。许可证：[MIT](https://github.com/sethkarten/continual-harness/blob/main/LICENSE)。

**Voyager：借鉴可检索技能库与成功经验沉淀，不照搬执行循环。**

[`SkillManager`](https://github.com/MineDojo/Voyager/blob/main/voyager/agents/skill.py) 保存技能代码及描述，用向量库检索；描述生成会调用 LLM。[`Voyager.step/learn`](https://github.com/MineDojo/Voyager/blob/main/voyager/voyager.py) 在游戏运行中调用 LLM 生成动作程序，执行后交给 critic 检查，成功任务才进入技能库。它证明了经验可以整理为复用资产，但此处的生成、判断和代码行动均不能原样用作本项目选手。许可证：[MIT](https://github.com/MineDojo/Voyager/blob/main/LICENSE)。

**OpenEvolve：借鉴候选档案和分级评测，不再叠一套优化器。**

[`database.py`](https://github.com/algorithmicsuperintelligence/openevolve/blob/main/openevolve/database.py) 保存父代、指标、种群和岛屿，支持探索与优胜候选采样；[`controller.py`](https://github.com/algorithmicsuperintelligence/openevolve/blob/main/openevolve/controller.py) 管理并行运行、检查点和最优程序。GEPA 已承担搜索职责，本项目只借鉴这些工程机制。[`evaluator.py`](https://github.com/algorithmicsuperintelligence/openevolve/blob/main/openevolve/evaluator.py) 支持阶段评测与错误工件；但开启 LLM feedback 后，存在把 `combined_score` 改为原分数与 LLM 评分混合值的路径。游戏原始得分应由环境直接产生，不能照搬这条配置。许可证：[Apache-2.0](https://github.com/algorithmicsuperintelligence/openevolve/blob/main/LICENSE)。

## 本项目提案：必须落成接口约束

**1. 把“游戏能力”与“偷偷代打”分清。**

局内链路固定为：游戏观测 → 状态整理 → Decision/Jev 选择 → 确定性执行。记忆检索只提供证据，不能检索到某条记录便直接按键。特征代码可以计算距离、碰撞、空洞等事实；不能输出推荐动作、剪掉战略上不喜欢的合法动作，或替模型给候选排序。全候选共享同一固定枚举规则，不能通过候选生成器植入解题策略。

V1 的“技能”采用文字策略卡与有界动作模板；模型必须选择动作、目标及持续时间。模板只展开该选择，不读新状态分支决策。蛇每个移动节拍由模型选择；马里奥可选“右＋跳，持续 N 帧”，N 也由模型选；方块若用“指定旋转与列”的复合动作，必须单独记录其与逐键模式的差异。新观测需要重新选动作时，仍调用 Decision/Jev。错误或超时允许固定暂停/中性输入并记异常，不能启动脚本策略或 LLM 接管。

验收时逐步追溯观测、候选全集、模型响应、实际动作及版本；每个具有游戏意义的动作都必须关联有效的模型决策记录。仅检查调用次数不够，模型若只确认脚本事先选出的唯一答案，依然不满足约束。生成代码先隔离评审，初版不开放任意运行时代码注入。

**2. 每局必有优化任务，但新候选不必上线。**

正常游玩局结束后，以 `episode_id` 建唯一优化任务，完整记录分析、候选、评测或失败原因。先采用串行模式：本局收尾、GEPA 完成一次有限预算优化、选版，再开始下一局。允许无改进时保留旧版。任务明确限制候选数、环境调用量、时间和成本，重试也消耗同一预算。

评测局必须固定 Harness 才能比较，因此按 `purpose=search_eval/release_eval` 区分；它们记录结果但不递归触发新优化，反馈只汇入所属父任务。这里“每局优化”指正常游玩局；若连评测子局都要求立即改版，稳定比较与有限执行将无法同时成立，接口文档应明确此语义。

**3. 反复看的验证集不能叫独立测试集。**

划分搜索集、选版集、封存审计集。前两者参与 GEPA 或发布选择，均属于开发数据。候选与现版在相同种子、规则、预算上配对比较；随机模型还需重复试验。封存集只在冻结里程碑后使用，不回传轨迹给优化器；一旦根据其结果调整系统，这批数据便不再独立。长期运行需轮换未使用场景，并保存审计次数，不能以不断刷同一 holdout 宣称持续泛化提升。

**4. 一局一个版本，发布与回滚只改下一局。**

版本包含提示词、背景攻略、特征定义、记忆快照、动作模板、模型版本及配置的哈希。局内追加事件记忆与长期策略记忆分离；候选不能改运行中快照。以后并行时，以数据库事务和 `expected_active_version` 原子选版；旧父版本产生的候选须重新挑战现版，不能后完成就覆盖。优化事件持久化去重，任务恢复不能重复发布。回滚恢复完整已知版本，保留原轨迹，禁止把几版文件拼在一起。

**5. 固定裁判，按游戏分别解释进步。**

环境、原始得分、结束条件、超时、动作合法性与测试场景属于可信运行层，GEPA 无写权限；LLM 解释只能作为诊断。每局保存原分、达成目标、用时、非法动作、模型延迟和异常。蛇、马里奥、方块各设独立榜单与版本；跨游戏迁移的是观测、决策、记录和优化协议，不是把不同分数直接平均。王者阶段再增加多人观测、团队目标及同步预算，团队指挥与英雄决策仍必须由 Decision/Jev 承担。
