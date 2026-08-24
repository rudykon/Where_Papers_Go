# 全期刊历史画像语料

这条管线解决“训练期历史画像只覆盖 181/20,087 本期刊”的数据瓶颈。它与近期论文测试集完全分离：测试集只提供查询和天然金标，历史管线以冻结的 JCR Q1--Q4 目录为唯一队列，对所有期刊使用相同采集策略。

## 数据边界

- 历史证据窗口：默认 `2021-01-01` 至 `2026-03-31`；
- 测试查询窗口仍由研究配置独立控制；
- 采集顺序只依赖固定 seed、学科和分区，不读取测试金标；
- 当前网页 scope 若抓取日晚于 cutoff，只进入 `production_profile_text`，不会进入论文主榜的 `profile_text`；
- 标题是历史证据的最低要求，摘要不再是硬门槛。

## 多来源证据链

```text
JCR 冻结目录
    ├── Crossref：DOI、标题、日期、可用摘要
    ├── OpenAlex：Crossref 不足时补标题、概念和可用摘要
    ├── Search API：补官方 aims & scope
    └── 独立 PCL 队列
          ├── DeepSeek：证据约束的主题原型抽取
          ├── 指数退避：瞬时超时/5xx 自动重试
          ├── 二次补采：仅复用落盘 evidence，不重拉来源
          └── bge-m3：原型向量与冻结分数 run
```

PCL 不是论文事实数据库。PCL 生成的每个主题原型必须引用输入 `evidence_id`；不存在或越界的引用会被拒绝。论文与发表期刊的事实绑定只依赖 DOI、经过校验的 ISSN 和来源记录。

## 运行

先验证配置、候选总数和无金标优先队列，不发网络请求：

```bash
python -m research collect-historical-corpus \
  --api-config llmapi.json \
  --jcr-csv data/jcr_partition_2025.csv \
  --data-dir data \
  --output-dir benchmark_artifacts/historical_venues_20260331 \
  --history-start 2021-01-01 \
  --cutoff 2026-03-31 \
  --batch-size 100 \
  --dry-run
```

运行一个 100 本批次：

```bash
python -m research collect-historical-corpus \
  --api-config llmapi.json \
  --jcr-csv data/jcr_partition_2025.csv \
  --data-dir data \
  --output-dir benchmark_artifacts/historical_venues_20260331 \
  --history-start 2021-01-01 \
  --cutoff 2026-03-31 \
  --batch-size 100 \
  --workers 6 \
  --pcl-workers 3 \
  --scope-workers 1 \
  --pcl-model-fallbacks 1 \
  --pcl-retries 2 \
  --pcl-second-pass-attempts 2 \
  --pcl-backoff-base 2 \
  --pcl-backoff-max 30 \
  --pcl-max-tokens 8192 \
  --max-batches 1
```

重复同一命令会跳过已有期刊分片，并扫描 `error:*`、`invalid_response`、`ungrounded_response` 的旧分片进入 PCL-only 二次补采。该路径直接读取 `venues/*.json` 中的 `evidence`，不会调用 Crossref、OpenAlex、Search API 或 Tavily。省略 `--max-batches 1` 会继续完整的 201 批队列；在输出目录创建 `STOP` 文件可在当前批完成后停止，尚未完成的 PCL 作业由持久化队列在下次启动时恢复。

`--retry-partial` 会重新执行完整来源采集，只应用于确实需要重拉来源的记录，不能用它代替 PCL 二次补采。生产默认使用 6 路来源采集、3 路 PCL 原型合成和 1 路独立 Scope LLM；这是对当前网关做稳定性测试后的渐进扩容值，且可避免 PCL 积压占满共享信号量后阻塞 Scope。原型客户端优先读取 `llm.pcl_models` 活跃池，再回退到完整的 `llm.models` 候选表；当前活跃池只启用 Qwen3.6-35B 与 DeepSeek-V4-Pro，避免 GLM 截断以及 Flash、MiniMax 超时拖慢队列。客户端线程安全地轮换请求起点；每个队列 attempt 最多切换 `--pcl-model-fallbacks 1` 个不同模型，失败模型会短暂冷却；401 作为共享凭证错误立即失败，404、超时、无效或无依据输出先切换模型。PCL 队列首次调用后最多指数退避重试 2 次，再执行 2 次第二轮尝试。`--pcl-max-tokens 8192` 是所有模型的硬上限，配置中的普通模型基础值仍为 2048；真实 canary 证明 Qwen3.6-35B 在 2048/4096 均因推理过程耗尽输出，而 8192 能在约 28 秒返回 5 个有效、可溯源原型，因此仅通过 `llm.model_max_output_tokens` 为 Qwen 设置 8192。若其他模型检测到 `finish_reason=length`，下一模型至少使用 3072，但不会突破硬上限。若要明确重新尝试已经耗尽两轮的作业，添加 `--retry-pcl-exhausted`。

PCL 与 Scope 请求默认设置 `stream: true`。SSE delta 只在内存中聚合，后端必须收到 `[DONE]`，并在完整 JSON、证据 ID 和 `finish_reason` 校验通过后才原子写盘。断流、畸形事件、响应超限以及 `length/content_filter` 不产生缓存，直接交给模型 fallback 和持久化重试队列。当前参数为 `stream_idle_timeout: 60`、通用 `stream_total_timeout: 180`、`max_stream_response_bytes: 4000000`；模型级总时限覆盖为 Qwen3.6-35B 90 秒、DeepSeek-V4-Pro 120 秒。若兼容网关忽略流式参数并返回普通 JSON，客户端会安全回退解析。

模型上下文通过 `llm.model_context_windows` 配置，输入还会统一限制在 49,152 个保守 tokenizer 上界单位。当前已登记 GLM-5.2 1,024K、DeepSeek-V4-Pro 400K、DeepSeek-V4-Flash-0731 512K、Qwen3.6-35B 256K。MiniMax-M3 未提供真实上下文值，因此不伪造窗口，仅应用 32,768 的保守输入上限；获得官方值后可直接在配置中补充。

## 输出

- `venue_profiles.train.jsonl`：始终保留完整 20,087 候选；
- `evidence.jsonl`：规范化来源、DOI、ISSN、日期、URL、许可状态和内容哈希；
- `prototypes.jsonl`：一刊多个主题/范围/静态原型；
- `venue_identity_crosswalk.jsonl`：JCR 哈希 ID 到线上图实体 ID 的严格 ISSN 映射；
- `lightrag_custom_kg.json`：仅导出截止日前原型以及 `HAS_PROTOTYPE/DERIVED_FROM` 关系，可直接作为 LightRAG custom KG；
- `venues/*.json`：期刊级断点分片；
- `attempts.jsonl`、`runner_state.json`：可恢复状态；
- `pcl_retry_queue.jsonl`：PCL 入队、退避、二次补采和终态事件；
- `pcl_retry_attempts.jsonl`：每次 PCL 调用的耗时与脱敏错误；
- `raw/pcl_model_attempts.jsonl`：逐模型状态、解析阶段、finish reason、长度与耗时，不保存响应原文或凭证；
- `pcl_retry_state.json`：独立 PCL 队列的实时 pending/inflight/成功/失败计数；
- `manifest.json`：边界、来源、PCL 模型、覆盖率与全部输出 SHA-256。

画像等级为：A（至少 10 篇带摘要历史论文）、B（至少 10 篇历史标题）、C（少量历史或截止日前官方 scope）、D（静态冷启动）。历史状态独立定义为 warm（至少 5 篇）、few-shot（1--4 篇）和 cold（0 篇）。

## 多原型向量 run

采集完成后，使用同一 PCL 网关的 bge-m3 对所有截止日前原型和查询编码，并在期刊层执行 prototype max pooling：

```bash
python -m research build-prototype-vector-run \
  --api-config llmapi.json \
  --dataset benchmark_artifacts/research_20260814/cached_crossref/papers.jsonl \
  --profiles benchmark_artifacts/historical_venues_20260331/venue_profiles.train.jsonl \
  --cache benchmark_artifacts/historical_venues_20260331/prototype_embeddings.json.gz \
  --output benchmark_artifacts/historical_venues_20260331/runs/pcl_bge_m3_prototype.jsonl \
  --top-k 100
```

该命令把模型指纹、输入 SHA-256、向量维度和聚合方式写入相邻 manifest。之后将 run 加入研究配置的 `imported_runs`，正式 `evaluate` 仍保持完全离线。

## 报告

`research evaluate` 的完整 test 仍是论文主分母。每个方法额外输出：

- `by_history_status`：warm / few-shot / cold；
- `by_profile_level`：A / B / C / D；
- `by_subject`：学科；
- `by_jcr_quartile`：JCR Q1--Q4。

`unknown` 和 `out-of-catalog` 都保留在分层中，不能通过缩小分母美化结果。
