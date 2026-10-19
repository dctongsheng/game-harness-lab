# Windows 原神体验包

## 下载和安装

- [安装程序](StartLux-Genshin-0.3.0-preview-win-x64.exe)：在 GitHub 文件页点击 **Download raw file**。Git 克隆的用户运行 `git lfs pull` 拉取完整二进制。
- [安装包 SHA-256](StartLux-Genshin-0.3.0-preview-win-x64.exe.sha256)
- [Windows 验证结果](windows-verification.json)及[实际窗口截图](desktop-preview.png)
- [独立 BetterGI 完整改造源码](BetterGI-StartLux-source.zip)；可阅读的改造补丁和固定上游版本位于 `integrations/bettergi/`。

运行根目录的 `Try-Genshin.cmd`，它会校验 SHA-256 并打开安装程序。也可直接运行安装程序。安装包已包含运行时和离线中文语音模型，目标系统为 Windows 11 22H2 及以上 x64。

## 首次试跑

1. 游戏使用中文界面，设置蒙德每日委托，进入可操作的主界面。
2. 确保 BetterGI 战斗策略可用。随包 BetterGI 默认位置为 `%LOCALAPPDATA%\Programs\StartLuxGenshin\bettergi\BetterGI.exe`。配置后从系统托盘完全退出 BetterGI，再通过助手启动接管。
3. 启动 StartLux 原神语音助手，填写游戏程序路径、UID、服务器、Bamboo Key 和榜首模型的真实部署 ID。
4. 确认该部署对应 **StartLux Decision v1.1 27B Instant**。客户端不会自动把其他别名当成同一权重。Key 在当前 Windows 用户下用 DPAPI 加密保存。
5. 开启语音，说“开始每日任务”；需要中断时说“停止”。也可使用界面上的启动、停止按钮。

每项委托执行后重新读取冒险之证并保存截图。路线失败、无法识别或未确认完成会停止；四项真实委托全部确认完成后才开放凯瑟琳每日奖励领取。原石数量没有独立证据时保持未知。

运行记录保存在：

```text
%LOCALAPPDATA%\StartLux\Genshin\runs
```

试跑后保留最新一次运行目录，其中包含 `report.json`、`trace.jsonl` 和截图。异常发生过早时可能只有部分记录。

本安装包来自已通过验证的源码版本 `be629fb624739f7198273e41c44411f79abb5106`。[Windows 构建与安装验证](https://github.com/dctongsheng/game-harness-lab/actions/runs/39214636384)通过；真实麦克风、榜首模型 API 联调及原神实机每日委托/领奖闭环尚未验收。完整对应源码和许可证同时保留在安装包的 `sources/` 与 `licenses/` 中。
