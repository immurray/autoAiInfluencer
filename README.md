# AI 虚拟人账号自动运营系统（MVP）

本项目提供一个可在本地运行的 AI 虚拟人账号自动运营最小可行产品，具备以下能力：

- 轮询本地图片目录，选择尚未使用的素材。
- 基于 OpenAI 文案模型自动生成推文文案，若未配置 API 则使用本地模板。
- 通过 Tweepy 将图文发布到 **X（原 Twitter）** 平台，支持 dry-run 模式模拟发布。
- APScheduler 定时调度，持续输出内容。
- 使用 SQLite 记录发布记录、错误信息与互动数据，方便后续分析。

## 环境准备

1. **克隆仓库并进入目录**（如已在本地忽略此步）：

   ```bash
   git clone <repo-url>
   cd autoAiInfluencer
   ```

2. **确保 Python 版本在 3.10 及以上**。建议为项目单独创建虚拟环境：

   ```bash
   python -m venv .venv
   source .venv/bin/activate  # Windows 使用 .venv\Scripts\activate
   ```

3. **安装依赖**：

   ```bash
   pip install -r requirements.txt
   ```

## 准备凭证与配置

1. **复制环境变量模板并填写凭证**：

   ```bash
   cp .env.example .env
   ```

   | 环境变量 | 说明 |
   | --- | --- |
   | `TWITTER_API_KEY`、`TWITTER_API_SECRET`、`TWITTER_ACCESS_TOKEN`、`TWITTER_ACCESS_TOKEN_SECRET`、`TWITTER_BEARER_TOKEN` | 在 [X Developer Portal](https://developer.twitter.com/) 申请的 API 凭证，缺失时仅能 dry-run。 |
   | `OPENAI_API_KEY` | 用于文案生成的 OpenAI API 密钥，留空则使用本地模板。 |

   > 提示：若暂时不打算真实发文，可留空推特凭证并保持 `config.json` 中的 `"dry_run": true`。

2. **配置 `config.json`**：

   `config.json` 控制运行参数，可根据需要调整。主要字段说明如下：

   | 字段 | 默认值 | 作用 |
   | --- | --- | --- |
   | `image_directory` | `./images` | 待发布图片所在目录，系统会按文件名排序并跳过已发布文件。 |
   | `database_path` | `./data/auto_ai.db` | SQLite 数据库存储路径。 |
   | `log_path` | `./data/bot.log` | 日志输出文件。 |
   | `dry_run` | `true` | 是否仅模拟发布（不调用 X API）。 |
   | `max_posts_per_cycle` | `1` | 每轮调度最多发出的帖子数量。 |
   | `caption.model` | `gpt-4o-mini` | 使用的 OpenAI 模型名称，可改为已开通的模型。 |
   | `caption.prompt` / `caption.templates` | 见文件 | 文案生成提示词与后备模板，模板中可使用 `{filename}` 占位符。 |
   | `tweet.prefix` / `tweet.suffix` | `""` / `"#AI #虚拟人"` | 文案前缀/后缀，会在最终发布时拼接。 |
   | `tweet.max_length` | `280` | X 帖子最大长度。 |
   | `scheduler.interval_minutes` | `60` | 调度间隔（分钟）。 |
   | `scheduler.timezone` | `Asia/Shanghai` | 调度器使用的时区。 |
   | `scheduler.initial_run` | `true` | 启动后是否立即执行一次。 |

3. **准备素材与数据目录**：

   - 确认 `images/` 中放置待发布的图片（支持常见格式 JPG/PNG 等）。
   - `data/` 目录用于存放 SQLite 数据库与日志，可根据需求在配置中调整路径。
   - 系统会自动创建缺失的数据库文件，但目录需提前存在。

## 运行方式

项目通过 `auto_ai_influencer.main` 提供命令行入口，可传入自定义配置文件：

```bash
python -m auto_ai_influencer.main --once           # 仅执行一轮任务
python -m auto_ai_influencer.main                  # 长期运行，按调度循环
python -m auto_ai_influencer.main --config other.json  # 指定自定义配置
```

- dry-run 模式下，流程会在日志中输出模拟发布结果，不会调用 X API。
- 真实发文前请确认 `.env` 中的 X 凭证已填写且 `dry_run` 为 `false`。
- 运行中断后再次启动，系统会读取数据库，避免重复发布同一素材。

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
