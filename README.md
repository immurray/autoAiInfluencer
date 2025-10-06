# AI 虚拟人账号自动运营系统（MVP）

本项目提供一个可在本地运行的 AI 虚拟人账号自动运营最小可行产品，具备以下能力：

- 轮询本地图片目录，选择尚未使用的素材。
- 基于 OpenAI 文案模型自动生成推文文案，若未配置 API 则使用本地模板。
- 通过 Tweepy 将图文发布到 X 平台，支持 dry-run 模式模拟发布。
- APScheduler 定时调度，持续输出内容。
- 使用 SQLite 记录发布记录、错误信息与互动数据，方便后续分析。

## 快速开始

1. **安装依赖**

   ```bash
   pip install -r requirements.txt
   ```

2. **复制环境变量模板并填写凭证**

   ```bash
   cp .env.example .env
   ```

   - 若仅需 dry-run，可暂时不填写推特与 OpenAI 凭证。
   - 填写后即可开启真实发布能力。

3. **配置参数**

   编辑根目录下的 `config.json`，设定图片目录、调度频率、文案模板等信息。默认会从 `./images` 读取图片，日志与数据库位于 `./data`。

4. **准备素材**

   将待发布的图片放入 `images/` 目录。系统会按照文件名排序依次发布，并跳过已经记录过的图片。

5. **运行系统**

   ```bash
   python -m auto_ai_influencer.main --once   # 仅执行一次
   python -m auto_ai_influencer.main          # 持续运行并按计划任务循环
   ```

   dry-run 模式下会在日志中输出模拟发布结果，可用于本地验收。

## 结构说明

- `auto_ai_influencer/config.py`：配置解析及数据结构。
- `auto_ai_influencer/caption.py`：文案生成逻辑，支持 OpenAI 或模板。
- `auto_ai_influencer/image_source.py`：本地图片轮询。
- `auto_ai_influencer/poster.py`：调用 Tweepy 发布或模拟发布推文。
- `auto_ai_influencer/storage.py`：SQLite 记账，保存发布、错误、互动信息。
- `auto_ai_influencer/runner.py`：主流程编排。
- `auto_ai_influencer/main.py`：命令行入口与 APScheduler 集成。

## 数据库结构

系统会自动初始化 SQLite 数据库，包括：

- `posts`：记录每次发布的图片路径、文案、时间、Tweet ID 以及是否 dry-run。
- `errors`：记录错误上下文、消息、堆栈。
- `engagements`：用于保存后续采集的互动数据快照（需自行调用接口填充）。

## 日志

日志默认输出到终端与 `data/bot.log`。可根据需要在 `config.json` 中调整路径。

## 注意事项

- 首次运行前请确认 `images/` 与 `data/` 目录存在（仓库已包含占位文件）。
- 若 dry-run 为 `true` 或未填写推特凭证，系统不会真实发文，仅模拟流程。
- OpenAI API 调用失败时系统会自动回退至模板文案，保证流程可用。

祝使用顺利！
