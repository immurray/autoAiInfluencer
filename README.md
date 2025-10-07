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

   > 提示：项目已适配新版 `openai` 官方 SDK，支持 `1.30.1` 至 `2.x` 的主要版本。若你已全局安装了 `openai`，请确认版本满足 `pip show openai` 输出的要求，避免旧版本接口缺失。 

## 准备凭证与配置

1. **复制环境变量模板并填写凭证**：

   ```bash
   cp .env.example .env
   ```

   程序启动时会自动按以下顺序加载 `.env` 文件：

   1. 当前工作目录（通常是仓库根目录）中的 `.env`。
   2. 若你在配置文件目录（如 `auto_ai_influencer/` 或 `src/`）也维护了 `.env`，系统会将其中未定义的变量补充加载。

   因此通常只需在仓库根目录配置一次 `.env` 即可，确保 `OPENAI_API_KEY` 等敏感信息能够正确注入。

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

### FastAPI 服务（推荐）

新版系统提供 FastAPI 服务，支持可视化接口、异步任务调度与 AI 流水线整合：

```bash
python -m src.main
```

- 如需热重载，可执行 `uvicorn src.main:app --reload --port 5500`。
- 启动后访问 `http://127.0.0.1:5500/docs` 可查看 API 文档。
- `/health` 用于健康检查，`/pipeline/run` 可手动触发一次 AI 流水线。
- `/posts/history` 与 `/captions/logs` 返回最新的数据库记录。
- `/images/upload` 支持上传新素材到 `data/ready_to_post/`。
- `/images/generate` 会调用配置的云端服务生成一张新图片并写入 `data/ready_to_post/`，便于提前备稿。
- 默认监听地址为 `0.0.0.0:5500`，可通过 `APP_HOST`、`APP_PORT`、`APP_RELOAD` 环境变量覆盖。

若仍需使用原有命令行入口，可继续执行：

```bash
python -m auto_ai_influencer.main --once
python -m auto_ai_influencer.main
```

### 启用 AI 虚拟人流水线

`config.json` 中新增 `ai_pipeline` 节点，用于配置每日定时自动发帖：

```json
"ai_pipeline": {
  "enable": true,
  "post_slots": ["11:00", "19:00"],
  "image_source": "replicate",
  "replicate_model": "stability-ai/sdxl",
  "replicate_token": "",
  "prompt_template": "portrait of a young woman, soft light, film tone",
  "caption_style": "soft_romance",
  "openai_api_key": ""
}
```

- `enable` 为 `true` 时，调度器将在 `post_slots` 指定的时间自动执行以下流程：
  1. 检查 `data/ready_to_post/` 目录，优先选取未发布的素材。
  2. 若目录为空且配置了云端服务（如 Replicate 或 Leonardo.ai），则在线生成图片并保存。
  3. 调用 OpenAI（可选）或本地模板生成 X 平台文案。
  4. 通过 Tweepy/X API 发布，dry-run 模式下仅记录日志。
  5. 将图片名称、文案、执行时间与结果写入 `post_history`，文案写入 `caption_log`。
- 缺少任何 API Key 时，系统会自动回退到本地模板与默认测试图片，仍可 dry-run 验证流程。
- `openai_api_key`、`replicate_token` 等敏感信息请通过环境变量或 `.env` 提供，示例中留空表示需自行填写。

### Dry-Run 说明

- `config.json` 顶层的 `dry_run` 仍然有效，会同时影响 FastAPI 流水线与命令行模式。
- dry-run 为 `true` 或缺少推特凭证时，发布环节不会真实调用 X API，仅在日志与数据库中记录模拟结果。

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
