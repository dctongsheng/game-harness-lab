# 游戏环境选择与边界

调研日期：2026-10-16。已阅读官方文档、固定提交的源码及许可证；未安装依赖、运行游戏或验证本机兼容性。首版按用户选择采用可暂停环境，先验证决策与赛后进化闭环。

## 1. 统一契约

采用 Gymnasium 的 `reset(seed) -> (observation, info)`、`step(action) -> (observation, reward, terminated, truncated, info)`。死亡、胜利属于终止；外部预算耗尽属于截断，不能混成“输了”。`info` 可能含隐藏信息，不能整包交给模型。适配器必须复制观测，避免后续环境更新篡改历史轨迹。[官方接口](https://gymnasium.farama.org/api/env/)

统一记录 `observation_mode`、环境提交、规则版本、关卡/种子、动作持续帧数、模拟帧、墙钟时间、原始分数、奖励分项及结束原因。渲染频率不等于游戏时间：步进环境等待模型时停止推进；未来实时环境仍会前进，必须另测延迟、丢帧及过期动作，二者成绩分榜。

局内策略和动作选择全部由 StartLux Decision/Jev 完成。解析器只提取事实，执行器只执行选中的按键；不得自动寻路、挑安全方向或看见敌人就自动跳跃。候选只排除协议上不支持的动作，不能把撞墙、自咬等“会输的动作”当成非法动作掩掉。模型无响应时暂停重试或结束为技术失败，不让启发式玩家接管。

## 2. 首批环境

| 游戏 | 选型与固定提交 | 采用理由 |
|---|---|---|
| 贪吃蛇 | [helpingstar/gym-snakegame](https://github.com/helpingstar/gym-snakegame/tree/a809354855d274cf63c92b6be1d9aa8d050f1636)，MIT，`a809354855d274cf63c92b6be1d9aa8d050f1636` | 代码小，四方向，结构化棋盘，支持种子与图像渲染；作为第一适配对象，但先验收下面的规则细节。 |
| 俄罗斯方块 | [Max-We/Tetris-Gymnasium](https://github.com/Max-We/Tetris-Gymnasium/tree/5ef067026180db7d46e6f8a9c7c9f1b96a642301)，MIT，`5ef067026180db7d46e6f8a9c7c9f1b96a642301` | 棋盘、当前块、暂存块和预览队列已经结构化；规则可配置。 |
| 超级马里奥 | [Kautenja/gym-super-mario-bros](https://github.com/Kautenja/gym-super-mario-bros/tree/7ba17c778e1df8240fd1cf99dbe5238d0e9b93aa)，`7ba17c778e1df8240fd1cf99dbe5238d0e9b93aa` | NES 模拟器可步进，提供关卡元数据及细分指标；代码和 ROM 权利必须分开。 |

### 贪吃蛇

首版一轮决策走一格，固定棋盘大小、一个食物及初始长度，以吃到的食物数量作为原始分数，另记死亡与无进展截断。四方向都提交模型选择。[环境源码](https://github.com/helpingstar/gym-snakegame/blob/a809354855d274cf63c92b6be1d9aa8d050f1636/gym_snakegame/envs/snake_game.py)

源码有三个接入细节：单通道观测返回内部棋盘视图；反向按键会被环境转为继续前进；碰撞判断发生在移除尾巴之前。因此必须复制观测，记录请求动作与实际动作，并固定“能否进入即将腾出的尾格”的规则。不能静默修正规则后与旧成绩比较。源码 README 的四通道顺序也与实现有差异，先使用单通道降低歧义。[源码中的观测与步进](https://github.com/helpingstar/gym-snakegame/blob/a809354855d274cf63c92b6be1d9aa8d050f1636/gym_snakegame/envs/snake_game.py#L82) · [许可证](https://github.com/helpingstar/gym-snakegame/blob/a809354855d274cf63c92b6be1d9aa8d050f1636/LICENSE)

### 俄罗斯方块

首版保持八个原生动作及一步一次重力，包括左右、下移、两种旋转、硬降、暂存和空操作；不让代码预选最佳落点。硬降是游戏原生按键，不等同于替玩家决定落点。后续“选择最终落点”模式可以单设任务，但不能与原生按键成绩混用。[动作定义](https://github.com/Max-We/Tetris-Gymnasium/blob/5ef067026180db7d46e6f8a9c7c9f1b96a642301/tetris_gymnasium/mappings/actions.py) · [重力与步进](https://github.com/Max-We/Tetris-Gymnasium/blob/5ef067026180db7d46e6f8a9c7c9f1b96a642301/tetris_gymnasium/envs/tetris.py#L204)

该实现的消行分为 `消行数² × 棋盘宽度`，默认奖励还含锁块后的存活加分。因此原始消行分应按环境事件单独累计，不能把累计 reward 叫“游戏得分”。固定重力、旋转规则、预览长度、暂存规则和方块生成器；不给模型内部随机数状态或未展示的未来方块。[计分实现](https://github.com/Max-We/Tetris-Gymnasium/blob/5ef067026180db7d46e6f8a9c7c9f1b96a642301/tetris_gymnasium/envs/tetris.py#L408) · [奖励配置](https://github.com/Max-We/Tetris-Gymnasium/blob/5ef067026180db7d46e6f8a9c7c9f1b96a642301/tetris_gymnasium/mappings/rewards.py) · [许可证及上游声明](https://github.com/Max-We/Tetris-Gymnasium/blob/5ef067026180db7d46e6f8a9c7c9f1b96a642301/LICENSE)

### 超级马里奥

当前提交标为 9.1.0，已用 Gymnasium，要求 Python ≥3.13、`gymnasium>=1.0.0`、`nes-py>=9.0.1`；不能套用旧版 Gym 教程。建议独立环境进程，先做 SMB1 单关，固定按钮组合与短按帧数，例如同一按钮执行四帧；参数写入任务版本，由模型决定按钮，不用“自动过坑”宏动作。[依赖](https://github.com/Kautenja/gym-super-mario-bros/blob/7ba17c778e1df8240fd1cf99dbe5238d0e9b93aa/pyproject.toml) · [按键组合](https://github.com/Kautenja/gym-super-mario-bros/blob/7ba17c778e1df8240fd1cf99dbe5238d0e9b93aa/gym_super_mario_bros/actions.py)

原始分数读取游戏计分，通关、最大前进距离、死亡和用时另记；其默认 reward 已混入进度、时间、分数、金币、能力变化、通关及死亡，不是计分板分数。RAM 中敌人类型等字段不能冒充屏幕识别结果。[指标源码](https://github.com/Kautenja/gym-super-mario-bros/blob/7ba17c778e1df8240fd1cf99dbe5238d0e9b93aa/gym_super_mario_bros/smb_env.py#L564)

项目原创代码为 MIT，捆绑 Nintendo ROM 明确不在授权内；仓库可访问并不赋予复制、修改或分发 ROM 的权利。我们的项目只纳入适配代码，不打包 ROM，运行时使用用户具备使用权限的资源。[官方授权范围](https://github.com/Kautenja/gym-super-mario-bros/blob/7ba17c778e1df8240fd1cf99dbe5238d0e9b93aa/LICENSING.md)

## 3. 观测与验收

首版采用结构化状态验证决策闭环，并标为 `state`；这不证明屏幕操作能力。后续 `pixels` 模式由固定识别器提取屏幕可见事实，再交决策模型；不在局内调用教练 LLM。蛇身体顺序、马里奥屏外实体、方块隐藏序列等额外信息，必须明确是否属于该任务允许的观测。

得分由只读裁判计算，GEPA 无权修改。每局触发优化，但升级需在相同场景的新旧版本配对评测中通过；另留不参与反思的测试场景。蛇限制总步数及无进展步数，方块固定最大步数/落块预算，马里奥同时看通关与原始分数，避免绕圈或重复刷分被当成进步。报告均值、分位数、失败率、调用成本及技术失败；不给成功局单独挑样。马里奥的种子不必然改变关卡，泛化测试应按关卡、起点和存档哈希划分。

## 4. 王者荣耀的扩展入口

[Tencent hok_env](https://github.com/tencent-ailab/hok_env/tree/c6e0029a5e4b6e037a049804fdbe23ac74484311) 固定提交 `c6e0029a5e4b6e037a049804fdbe23ac74484311`，代码 Apache-2.0；公开 SDK 文档覆盖 1v1、3v3，Gamecore 和许可需单独申请，运行要求仍涉及 Windows/Wine、Linux 容器及旧 Python。它不是公开 5v5 手机客户端自动化包。[部署要求](https://github.com/tencent-ailab/hok_env/blob/c6e0029a5e4b6e037a049804fdbe23ac74484311/README.md#running-requirement) · [Gamecore 独立许可](https://github.com/tencent-ailab/hok_env/blob/c6e0029a5e4b6e037a049804fdbe23ac74484311/GAMECORE.LICENSE)

现在预留每个玩家的观测、阵营、动作掩码、联合动作及逐帧时间戳即可。未来先验收官方研究环境接入，再处理五人并发、通信和真实时间；团队目标、角色分工及英雄动作仍由决策模型选择，不能在比赛中重新引入 LLM 指挥。
