# model-inspect

用一套固定题库，在 10 分钟内给大模型做一次「智力体检」。每执行一次脚本就往本地 SQLite 累计一次结果，再生成一个离线 HTML 报告，用来长期观察某个模型（或某个中转站上的模型）有没有变笨。

- 纯 Python 标准库，无第三方依赖（Python 3.8+）
- 兼容 OpenAI `chat/completions` 和 Anthropic `messages` 两种接口
- 数据在 `data/results.db`，报告是单文件 `report.html`，全部离线

## 快速开始

接口地址、密钥、模型列表整份存在 camtools 的 `ct secret`（`~/qb/camtools/local_secrets`）里，项目目录里不放任何密钥。

```bash
ct secret init                                    # 本机第一次用 ct secret 时执行一次
ct secret import secret://model-inspect --stdin   # 粘贴配置（格式见 config.example.json），回车后按 Ctrl-D

python run.py --list                    # 看读到了哪些模型（不显示密钥）
python run.py --smoke                   # 每个模型只问 3 题，验证能连通（不入库）
python run.py --budget 3600 --timeout 600   # 临时放宽时限，做一次不丢题的完整测量
python run.py                           # 测全部模型，入库并更新 report.html
python run.py gpt-5.5 deepseek-chat     # 只测指定模型（按 name）
```

然后用浏览器直接打开 `report.html`。

要长期观察，就定时执行，比如每天 9 点：

```cron
0 9 * * * cd /path/to/model-inspect && python3 run.py >> data/cron.log 2>&1
```

注意：`ct secret` 依赖系统 keyring。Linux 上 cron 没有登录会话时 keyring 可能读不到，这一点还没有实际验证过；读取失败时脚本会以非零状态退出并说明原因。

## 配置

`run.py` 通过 ct secret 的 SDK（`SecretResolver` + `LocalSecretProvider`）在内存里解密配置，明文不落盘、不打印。当前 Python 缺 SDK 依赖时，脚本会自动换到 camtools 的 uv 环境重新执行，命令不用变。camtools 不在 `~/qb/camtools` 时用环境变量 `CAMTOOLS_HOME` 指定，非默认 profile 用 `MI_SECRET_PROFILE`。

配置内容是 JSON 或 YAML：

```json
{
  "defaults": {
    "base_url": "https://api.example.com/v1",
    "api_key": "sk-默认密钥",
    "concurrency": 8
  },
  "models": [
    {"name": "gpt-5.5", "model": "gpt-5.5"},
    {"name": "deepseek@官方", "model": "deepseek-chat", "base_url": "https://api.deepseek.com/v1", "api_key": "sk-deepseek-密钥"},
    {"name": "claude-opus", "model": "claude-opus-5-5", "base_url": "https://api.anthropic.com/v1", "api_key": "sk-ant-密钥", "protocol": "anthropic"}
  ]
}
```

`defaults` 里的字段对所有模型生效，单个模型里写了就覆盖。

`ct secret` 故意不提供读出明文的命令，所以改配置就是重新 `ct secret import` 一份完整的。如果模型列表经常变，可以改用第二种方式：模型列表放在本地 `config.json`（已在 `.gitignore` 里），密钥字段只写引用，密钥本身用隐藏输入单独存：

```bash
ct secret set secret://model-inspect/deepseek     # 隐藏输入，敲一个字符显示一个 *
```

```json
{"models": [{"name": "deepseek@官方", "model": "deepseek-chat", "base_url": "https://api.deepseek.com/v1", "api_key": "secret://model-inspect/deepseek"}]}
```

配置来源的优先级：`--config`（`secret://` 引用或文件路径）> 环境变量 `MI_CONFIG` > 本地 `config.json`（存在时）> `secret://model-inspect`。

临时测一个不在配置里的模型：

```bash
python run.py --base-url https://api.example.com/v1 --api-key secret://model-inspect/tmp --model gpt-5.5
```

| 字段 | 说明 | 默认 |
| --- | --- | --- |
| `name` | 报告里的名字，也是长期曲线的身份。同一个模型走不同渠道时起不同的名字（如 `gpt-5.5@中转A`） | 同 `model` |
| `model` | 发给接口的模型 ID | 必填 |
| `base_url` | 接口地址，写到 `/v1` 即可，会自动补 `/chat/completions` 或 `/messages` | 必填 |
| `api_key` | 密钥。整份配置存在 ct secret 里时直接写；放在本地文件时写 `secret://名字` 或 `secret://名字#字段` 引用 | 必填 |
| `protocol` | `openai` 或 `anthropic`。不写时按地址判断 | 自动 |
| `concurrency` | 同时在途的请求数上限。被限流就调低 | 8 |
| `rpm` | 每分钟最多发多少个请求，`0` 表示不限。接口有明确的 RPM 配额时填它 | 0 |
| `timeout` | 单题超时秒数 | 180 |
| `budget` | 单个模型总预算秒数，超过后剩余题目记为未作答 | 600 |
| `stream` | 用流式请求，避免慢的推理请求被网关 524 超时掐断。接口不支持流式或 `stream_options` 时设为 `false` | true |
| `max_tokens` | 只对 Anthropic 协议生效（该协议必填） | 8192 |
| `efforts` | 推理强度列表，如 `["low", "high"]`。每档展开成一个独立变体 `name@档位`，OpenAI 协议发 `reasoning_effort`，Anthropic 协议发 `output_config.effort` | 不展开 |
| `params` | 原样并入请求体，如 `{"temperature": 0, "reasoning_effort": "high"}` | `{}` |

### 请求频率控制

- 接口地址和密钥都相同的模型视为同一个账号，**依次执行**、共用一个限速器；不同账号之间并行。所以一个模型配了 5 档 `efforts`，总耗时大约是单档的 5 倍，但每档各自有完整的 `budget`，并发和单独跑时一样，耗时数据可比。
- 收到 429 时自动降速：按接口返回的 `Retry-After`（没有就从 10 秒起逐次加倍，最长 60 秒）暂停该账号的全部请求，并把并发减半，然后重试。限流不计入失败，降下来的并发会沿用到同账号后面的模型。终端会提示被限流了几次、当前并发是多少。
- 经常被限流就直接在配置里调低 `concurrency` 或设置 `rpm`，省掉试探的时间。
- 每题的耗时只统计成功那一次请求，不含排队和限流等待。

对比不同推理强度：

```json
{"name": "gpt-6.1-sol", "model": "gpt-6.1-sol", "efforts": ["low", "medium", "high", "xhigh", "max"]}
```

会得到 `gpt-6.1-sol@low` … `gpt-6.1-sol@max` 五条曲线，在报告里并排比较得分、耗时和输出 tokens。`python run.py gpt-6.1-sol` 选中全部档位，`python run.py gpt-6.1-sol@high` 只跑一档。各档依次执行；高档位单题更慢，单档可能超出 `budget`，届时可调大 `budget`。

默认不发送 `temperature` 和 OpenAI 协议的 `max_tokens`，因为不少推理模型会拒绝这些参数；需要时写进 `params`。注意：`params` 不同会影响得分，改了参数建议换一个 `name`，否则曲线前后不可比。

## 题库

`questions.json`，版本 `v3`，共 40 题，全部自动判分，不需要裁判模型。

| 类别 | 题数 | 来源 | 考什么 |
| --- | --- | --- | --- |
| 常识陷阱 `sense` | 10 | SimpleBench 公开题 | 带大量干扰信息的现实常识推理，区分度最高的一类 |
| 精确计算 `compute` | 10 | 固定种子程序生成 | 多位数乘法、模幂、交换追踪、计数、倒序、日期，答案由程序算出 |
| 学科 `knowledge` | 6 | MMLU-Pro（10 选 1） | 物理、化学、数学、法律等需要计算或推理的题 |
| 代码 `code` | 5 | 自编 Python 追踪 4 + modeltool JS 题 1 | 在脑中执行代码 |
| 陷阱题 `trap` | 5 | modeltool 糖果题 | 改过数字的经典熟题，背答案就会错。含兜底的区分题 `candy-bag`（糖袋题） |
| 逻辑 `logic` | 4 | modeltool 逻辑/资料卡题 | 演绎、约束求解、按资料卡回答不被谣言带偏 |

**总分 = 答对题数 / 总题数（0–100）。** 出错或超时的题按答错计。各类别分只作参考，题数少的类别一题就是 20–25 分。

#### 从 v1 到 v3

v1 有 85 题。2026-10-12 用 5 个模型、16 个推理强度变体各跑了一遍 v1，结果是 61 题所有变体全对，却占了 45% 的输出 tokens 和 52% 的耗时。据此精简：

- 留下至少有一个变体答错的题，再从全对的题里每类留几道便宜的当哨兵，用来发现明显的降智。
- 保留 `candy-bag`（糖袋题）作为兜底的区分题。它是全题库最贵的一道：单题平均 3300 输出 tokens，占 v1 全部输出的 13%，慢的模型一题要算 4–7 分钟，所以一次执行的总用时基本由它决定。
- 去掉 `code-join`：题干漏了「JavaScript」，按 Python 理解选 `[1, 2, 3, 4]` 也说得通，它的「区分度」其实是歧义造成的。
- v3 是 v1 的严格子集，题目和提示词没变，所以数据库里 v1 的 16 次执行已按 v3 的题目折算成 v3 记录（备注「由 v1 的 run #N 折算」，没有总用时）。v1 的原始记录保留，报告里切换题库版本可以看。
- v1 的总分是六个类别正确率的平均，v3 改成全部题目的正确率，两个版本的分数不能直接比。
- v2 是中间版本（39 题，没有 `candy-bag`），只存在了很短时间，不再使用。

同样这批变体，v1 的分数挤在 82–100，v3 拉开到 70–100；输出 tokens 是 v1 的 71% 左右。

选题时做过的处理：

- modeltool 原题库 161 题大多偏简单，v1 只取了中高难度的 38 题，并剔除了两道原解析自己都承认有歧义的题（`logic-seats`、`logic-order`）。选项用固定种子打乱过，避免正确答案集中在 A/B。
- MMLU-Pro 从随机抽样里人工核对了答案，去掉了选项重复或答案存疑的题。
- 每题允许模型先简要推理，最后一行写 `答案：X`。判分先找这一行，找不到再退回到全文最后一个选项字母；数值题取最后一个数字比较。

题库版本写在每次执行的记录里。**改题目必须同时改 `suite` 版本号**，报告里按题库版本分开看，不同版本的分数不放在一条曲线上。

### 这个分数能说明什么

- 适合做的事：同一个 `name` 的纵向对比（今天和上周比）、同一模型不同渠道的横向对比。
- 40 题的抽样误差不小：正确率 85% 左右时，单次得分的标准误约 6 分（题少了，单次更省，但噪声也更大；要更稳就多跑几次看均值）。所以报告只在本次得分偏离历史均值超过约 2 个标准误（最少 5 分）时才标「上升/下降」，一两分的抖动没有意义。
- 题目是公开的，模型可能见过。它不是排行榜，绝对分数不要和别处的榜单比。
- 耗时取决于模型和推理强度：按 2026-10-12 的实测折算，gpt 系列各档 1–2 分钟；glm 系列高强度被 `candy-bag` 一题拖到 3–7 分钟。超预算时各类别是穿插着问的，不会只丢掉某一类。

## 输出

每次执行在终端打印汇总（下面数字仅为格式示例）：

```
[deepseek-chat] 总分 77.5  对 31/40  出错 0  用时 96s  平均 11.2s/题  输出 14520 tokens
    陷阱题 87  逻辑 93  代码 75  学科 80  常识陷阱 50  精确计算 85
    已入库，run #12
```

数据写入 `data/results.db`：

- `runs`：每个模型每次执行一行。总分、各类别分、对/错/出错数、用时、平均耗时、输入输出 tokens、接口回显的模型名（可用来发现中转站偷换模型）、备注。
- `answers`：每题一行。是否正确、模型给的答案、标准答案、耗时、tokens、错误信息、模型原始回答（末尾 4000 字）。

```bash
sqlite3 data/results.db "select started_at,name,score from runs order by id desc limit 10"
python report.py --csv                  # 导出 data/runs.csv
```

全部请求都失败的执行（密钥错、地址不通）不入库，脚本以非零状态退出，方便定时任务报警。

调试或用假接口测试时，设环境变量 `MI_DB=/别处/test.db` 并加 `--no-report`，不要碰 `data/results.db`，它是长期累计的唯一副本。

## 报告

`python run.py` 跑完会自动更新 `report.html`，也可以单独执行 `python report.py`。页面不联网、不依赖任何外部资源：

- **最新成绩**：每个模型最近一次的总分、与历史均值的差、六个类别分、出错数、耗时、tokens，以及状态（首测 / 稳定 / 上升 / 下降 / 不完整）。
- **筛选栏**（固定在页面顶部，对下面所有内容生效）：时间范围、题库版本、题集（全部题目的总分或任一类别）、模型（可搜索、多选、只看某一个）。
- **趋势图**：横轴每一列是运行一次 `run.py`，这一次里测的所有模型画在同一列（库里的 `batch` 字段；加这个字段之前的旧记录按开始时间推断归并）。颜色区分模型，形状区分 `@` 后面的档位或渠道，空心点表示那次不完整。图例在图的上方，悬停高亮、点击隐藏。
- **逐题结果**：每题每个模型最近一次对错和历史通过次数，点击可看题目与模型原始回答。
- **执行记录**：所有历史执行。

`--note "换了中转站"` 可以给某次执行加备注，会显示在趋势图提示和执行记录里。

## 文件

```
run.py                执行测试并入库
report.py             从数据库生成 report.html
template.html         报告页面模板
questions.json        题库
config.example.json   配置格式示例（内容导入 ct secret，不要在这里填真密钥）
data/results.db       结果数据库（运行后生成，不入库）
report.html           报告（运行后生成，不入库）
```

## 题目来源与致谢

- [modeltool](https://github.com/fusuzhenxin/modeltool)（大模型降智检测）：陷阱题、逻辑、代码题。
- [MMLU-Pro](https://huggingface.co/datasets/TIGER-Lab/MMLU-Pro)，TIGER-Lab，MIT License。
- [SimpleBench](https://github.com/simple-bench/SimpleBench) 公开题集，MIT License。
