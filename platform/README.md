# StartLux Platform 0.3.0

独立的通用执行平台：**StartLux Decision 决策 + 场景插件 + Google Harness² 优化后端**。
Python 包与 Windows 客户端不依赖旧 `game-harness-lab` 模块、GEPA 或 RRSI。
仓库根目录保留旧小游戏工程，便于已有使用者继续使用；新开发入口是本目录。

## 当前可运行能力

- `core/`：通用观察、动作、决策回执、状态校验、停止、执行记录、策略版本及回滚。
- `scenarios/genshin/`：现有 BetterGI Windows 接管、每日委托、领奖证据校验；截图回放与记录导入。
- `scenarios/workspace/`：在独立目录读取账目、调用工具写出合计文件、独立检查结果，验证通用接口。
- `optimization/`：调用官方 Harness² 的 Propose–Probe–Compose、候选选择和跨任务经验库。
  固定上游提交 `1cfe254e8d25b15f37048c3730134686abe60335`，并行与串行模式均可运行。
- 云端改进模型使用可配置的 Chat Completions 接口；游戏动作仍由指定的 StartLux Decision 模型选择。

首版可优化 instructions、memory、tool guidance；固定执行器、动作参数、模型身份和完成校验。
上游只注册 Codex/OpenCode 提示模板，因此适配层在一个位置注册 StartLux 的文本编辑范围。
核心递归、试跑、合成、选择和经验库调用上游实现。

## 安装与离线体验

优化环境使用 Linux/WSL 的 Python 3.12 或 3.13；也已在 macOS 3.13 验证。
Windows 客户端仅运行执行层，无需安装优化器。

从仓库根目录运行：

```bash
python3.12 -m venv platform/.venv
source platform/.venv/bin/activate
pip install -r platform/requirements-optimizer.lock
pip install --no-deps -e ./platform
startlux scenarios
startlux optimize --scenario workspace.ledger --config platform/examples/workspace/config.json --output /tmp/startlux-workspace --fixture
startlux optimize --scenario genshin.daily --config platform/examples/genshin/config.json --output /tmp/startlux-genshin --fixture --mode parallel
```

`--fixture` 明确使用测试模型，演示流程不会调用云端，不代表真实模型效果。
文件场景故意通过测试策略演示修正漏算负数；即使测试改进通过，也不允许发布给客户端。
原神合成样例没有实机执行，只验证适配。未知结果不视为成功。

重复相同命令可继续已绑定的任务；修改数据、模型、模式或递归预算需要新的输出目录。
`--k 1..4` 控制递归预算。默认 `--max-improver-calls 32` 限制单次进程的改进模型请求次数。
输出包括 `provenance.json`、`candidate.json`、`library/`、`gate.json`、`report.json`。

## 接真实模型

配置文件只填写模型 ID、任务包和数据路径。示例里的 `fixture-*` 必须换成真实部署 ID。
原神模型在任务包 `manifest.json` 的 `decision_model` 中固定；配置增加
`"top_ranked_deployment_confirmed": true`，表示部署方已确认映射到 StartLux Decision v1.1 27B Instant。
平台不会推断公开别名等同于该权重。

```bash
export STARTLUX_DECISION_KEY_FILE=/private/decision-key.txt
export STARTLUX_IMPROVER_URL=https://your-provider.example/v1/chat/completions
export STARTLUX_IMPROVER_MODEL=your-improver-model
export STARTLUX_IMPROVER_KEY_FILE=/private/improver-key.txt
startlux optimize --scenario workspace.ledger --config /data/workspace-config.json --output /data/optimization-001
```

Decision 默认 `https://api.bamboonode.dev/v1/decisions`；可用 `STARTLUX_DECISION_URL` 配置开发端点。
改进接口需要支持文本/图片输入、`response_format=json_object` 和 `max_tokens`。
Key 文件由操作者保管，不写入任务包、提示上下文或运行记录。真实运行去掉 `--fixture`。
改进模型的美元成本目前记录为未知；发布检查使用 Decision 返回的实际 token 数，不推算价格。

## 原神记录 → 优化 → 候选试跑

Windows 仍使用仓库的安装包。新构建使用本项目运行时；已发布二进制版本以文件名及
`windows/installer-source.json` 为准。实际麦克风与原神每日任务闭环需要 Windows 登录账号验收。

运行记录位于 `%LOCALAPPDATA%\StartLux\Genshin\runs`。将某次完整目录复制到优化服务器：

```bash
startlux import-genshin --run /data/genshin-run --output /data/captured-suite.json
```

导入器校验截图 SHA-256、观察绑定、动作目录及模型身份，并生成未标注的 evolve/heldout 划分。
它不会把历史模型选择当作正确答案。要做独立回归，需要为 case 填写 `acceptable_actions`
或 `expect_abstention`，并设置 `label_available=true`；留出的 heldout 标签不会交给改进模型。
正式验收应跨账号、日期、任务类别准备独立数据；单次记录的自动划分仅用于开发。

配置文件示例：

```json
{"pack":"genshin-run/pack","suite":"captured-suite.json","top_ranked_deployment_confirmed":true}
```

```bash
startlux optimize --scenario genshin.daily --config /data/genshin-config.json --output /data/genshin-opt-001
startlux export-genshin --pack /data/genshin-run/pack --policy /data/genshin-opt-001/candidate.json --output /data/candidate-pack
```

导出包始终为 development，需要 Windows 实机验证。快照即使全对也不能自动发布。
开发者可以把 `candidate-pack` 的完整内容复制到
`%LOCALAPPDATA%\StartLux\Genshin\trial-pack`，随后在 0.3.0 客户端照常语音启动。
客户端只接受与内置模型、动作和执行资源完全一致的文本策略试验，记录为 `development_trial`。
删除该目录即可恢复内置/已发布策略；不要在执行中替换文件。
优化器不连接游戏输入、不重复领取奖励。已验证的策略库放在客户端
`%LOCALAPPDATA%\StartLux\Genshin\policies`，下一次启动才载入；正在运行的任务固定策略。

## 独立回归与发布

Harness² 完成候选选择后，平台才在 heldout 上运行基线与候选；不拿 heldout 挑选候选。
缺失记录、未知结果、任务退步、没有改进、未知 token 成本、token 超过基线两倍均拒绝发布。
快照和测试模型的结果始终没有客户端发布资格。

对已经通过真实执行验证的场景：

```bash
startlux promote --scenario workspace.ledger --config /data/workspace-config.json --optimization-run /data/optimization-001 --output /data/policies
startlux rollback --scenario workspace.ledger --config /data/workspace-config.json --output /data/policies --expected-active CANDIDATE_SHA256
```

更新已有策略时，promote 同样需要 `--expected-active`，避免覆盖另一个发布。
原神的 replay 插件只产生 snapshot 结果，因此其 promote 会被拒绝；当前不包含自动化的
跨日期实机对照试验、远程自动更新或模型权重训练服务。候选任务包供开发者进行实机验收。

## 新增场景插件

实现 `startlux.core.scenario.Scenario`：任务列表、用户目标、可见素材、基线策略、执行及独立验证。
`run()` 使用 `ExecutionEngine` 和场景会话提供的 `observe/candidates/start/reconcile/cancel/stop`。
`Trial` 区分 snapshot、sandbox_execution、live_execution，并记录证据来源。
可重置性由场景负责；真实不可逆操作不能直接用于多候选并行试跑。

外部 Python 包注册：

```toml
[project.entry-points."startlux.scenarios"]
"your.scenario" = "your_package:create_scenario"
```

工厂接收 JSON 配置，返回 Scenario。插件在安装时执行 Python 代码，仅安装可信插件。
`workspace.ledger` 是完整的最小参考。场景插件不依赖 Harness²，未来可增加其他优化后端。

## 验证与部署

```bash
python -m pytest platform/tests -q
docker build -t startlux-optimizer platform
```

Windows 构建入口仍为 `scripts/build_genshin_desktop.py`，原生 IPC、WPF 语音和安装卸载由
`.github/workflows/genshin.yml` 验证；Linux 的 Harness² 集成由 `platform.yml` 验证。
Docker 镜像提供 CLI 作业入口，生产队列、多租户控制台及远程更新服务尚未实现。

本平台沿用原神 Agent 的 GPL-3.0-or-later；Google Harness² 保留 Apache-2.0。
BetterGI、AutoCommission 及语音依赖的来源、许可证见仓库 `THIRD_PARTY_NOTICES.md`。
