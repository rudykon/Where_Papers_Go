# Where Papers Go · Offline Research Benchmark

这个目录是论文实验层，与线上产品检索分离。它只读取冻结文件，不导入 Search/LLM 客户端，不发起网络请求，不会因 API 配额而删除失败样本。

## 目录

```text
research/
├── configs/              # 冻结基线、多原型和 500 篇诊断配置
├── outputs/              # 本地 run、审计和指标，不入 Git
├── __main__.py           # `python -m research` 入口
├── cli.py                # 声明式实验、语料构建和采集命令
├── data.py               # JSONL/JCR 读取、时间切分、清单和 run I/O
├── cache_builder.py      # Crossref 本地缓存 -> 时间冻结语料
├── historical_builder.py # 20,087 刊多来源、多原型历史画像
├── pcl_retry.py          # 独立持久化 PCL 重试队列
├── prototype_vectors.py  # bge-m3 多原型 max-pooling 冻结 run
├── leakage.py            # DOI/标题/内容/时间/跨分割泄漏审计
├── baselines.py          # BM25、TF-IDF 和冻结 run 统一接口
├── fusion.py             # RRF 和 train-only 学习融合
├── metrics.py            # Recall/Hit/MRR/nDCG/MAP@K
├── statistics.py         # paired bootstrap CI 和 permutation test
├── reporting.py          # 完整分母的四维分层报告
├── types.py              # 查询、候选、run 和 qrels 数据类型
├── config.example.json   # 最小静态 JCR 示例
└── README.md
```

新对话开始前先阅读根目录 [`HANDOFF.md`](../HANDOFF.md)。它记录当前 dirty
基线、历史语料快照、P0-A～P0-C 的阻塞项与验收门槛。大型语料、embedding
和逐查询 run 继续保存在忽略目录，不属于上述源码结构。

## 1. 运行现有 500 篇基准

在项目根目录执行：

```bash
python -m research evaluate --config research/config.example.json
```

若已经执行下文的缓存语料构建，还可用同一个 20,087 候选画像评测原 500 篇数据集中的 June test 子集：

```bash
python -m research evaluate --config research/configs/recent_500_baselines.json
```

示例主实验仅使用 2025 JCR 中的期刊名和类别字段。它明确不使用在 2026 年测试论文发表后补全的「官网摘取/证据/更新时间」字段。默认时间分割是：

- train: 2026-01-01 至 2026-03-31；
- validation: 2026-04-01 至 2026-05-31；
- test: 2026-06-01 至 2026-06-30。

所有 test 样本都进入分母。候选库没有金标签期刊时会记为 miss，不会被静默过滤。

输出位于 `research/outputs/static-jcr-2025/`：

- `manifest.json`：输入 SHA-256、有序 query/candidate 指纹、Git/环境/硬件、完整配置和单命令复现信息；
- `leakage_audit.json`：时间、DOI、标题、内容指纹、摘要近重复、publication version 和跨分割重复审计；审计与每个方法的实际 `document`/`prototypes` 索引视图一致，未进入索引的保留来源目录若与评测身份重合则单独记为 warning；
- `runs/*.jsonl`：每个方法的可重用排名（包括显式空排名记录）；
- `runs/*.jsonl.manifest.json`：逐 run 的强制 sidecar，绑定 dataset/profile/config/method/runtime 与完整 query 覆盖；
- `metrics.json`：总体及学科/分区指标、配对置信区间和显著性检验。

`metrics.json` 始终把完整 test 作为主结果。同时它会额外报告 `identity_safe`敏感性指标：仅排除输入文本直接包含金标准期刊名的 query，并写明完整、安全和排除数量及全部排除 ID。这套子集只是保守对照，不会静默替换主分母。

## 2. 从现有 Crossref 缓存建时间冻结语料

```bash
python -m research build-cached-corpus \
  --cache-dir benchmark_artifacts/recent_journals/crossref_cache \
  --jcr-csv data/jcr_partition_2025.csv \
  --output-dir benchmark_artifacts/research_20260814/cached_crossref \
  --start 2026-01-01 \
  --train-end 2026-03-31 \
  --dev-end 2026-05-31 \
  --test-end 2026-06-30 \
  --min-abstract-chars 100
```

该命令只遍历本地 `*.json`，并执行：

1. ISSN 校验位验证和唯一 JCR 归属；
2. JATS/HTML 摘要清洗；
3. 以归一化 DOI 去重；
4. 严格 train/validation/test 时间切分；
5. 保留全部 JCR Q1--Q4 候选，只把 train 论文追加到期刊画像。没有历史论文的候选仍使用冻结的名称/学科元数据，不会从 20,087 候选空间消失。

`papers.jsonl` 可直接作为 `dataset.path`。若使用历史论文画像，把 corpus 配置改为：

```json
{
  "type": "jsonl",
  "path": "../benchmark_artifacts/research_20260814/cached_crossref/venue_profiles.train.jsonl",
  "id_field": "venue_id",
  "text_fields": ["name", "profile_text"],
  "snapshot_field": "snapshot_date"
}
```

## 3. 导入冻结的向量/图谱分数

论文实验不在运行时请求 embedding API。先离线生成 JSONL，再加入 `imported_runs`：

```json
{
  "name": "bge_m3",
  "type": "vector_scores",
  "path": "frozen_runs/bge_m3.jsonl",
  "manifest_path": "frozen_runs/bge_m3.jsonl.manifest.json",
  "manifest_sha256": "<sha256-of-sidecar>",
  "generation_config_sha256": "<sidecar.binding.configuration.canonical_sha256>",
  "provider_fingerprint": "<sidecar.method.provider_fingerprint>",
  "corpus_snapshot": "2025-12-31",
  "training_cutoff_disclosure": "see model card"
}
```

每行支持两种格式：

```json
{"query_id":"doi:10.x/example","venue_id":"jcr-abc","score":0.812}
{"query_id":"doi:10.x/example","scores":{"jcr-abc":0.812,"jcr-def":0.701}}
```

导入不接受裸分数文件。sidecar 本身必须由配置中的 SHA-256 固定，并与当前 dataset SHA、有序 query 指纹、profile SHA、20,087 候选指纹、生成配置及 exact model revision/provider fingerprint 全部一致。缺/多 query、未知或重复候选、空缺 sidecar、错误指纹、NaN/Inf、run 文件被改写都会立即失败。BM25、TF-IDF、向量、图谱均转换为同一 `Run` 格式，因此 RRF 和学习融合不需要了解各通道的实现。

## 4. 独立构建 20,087 刊历史画像

近期测试集缓存不能兼任全库训练语料。新的 `collect-historical-corpus` 命令以完整 JCR 目录建立与金标无关的稳定队列，按 50--100 本一批采集 Crossref/OpenAlex/官网证据，并用 PCL API 生成必须引用来源 ID 的多个主题原型。历史论文只要求标题，摘要作为增强字段而不是准入门槛。

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
  --pcl-max-tokens 8192 \
  --max-batches 1
```

先加 `--dry-run` 可无网络验证 20,087 队列。来源采集和 PCL 合成使用独立线程池；生产默认采用 6 路来源采集、3 路 PCL 原型合成和 1 路独立 Scope LLM。独立 Scope 槽可防止 PCL 积压占满共享信号量而饿死来源流水线。PCL 原型生成优先从 `llm.pcl_models` 读取实测活跃池，再回退到 `llm.models`；当前活跃池为 Qwen3.6-35B 与 DeepSeek-V4-Pro，其余模型仍保留为可配置候选。客户端线程安全地轮换请求起点；无效 JSON、无依据结果、超时或模型路由错误会使该模型短暂冷却，并最多切换一个不同模型。证据先原子落盘，PCL 超时按指数退避重试，第一轮仍失败的记录自动进入 PCL-only 二次补采。重复执行会从 `venues/*.json` 和持久化 PCL 队列断点继续；二次补采只读取已有证据，不会再次消耗 Tavily。详见[全期刊历史画像语料](../docs/historical-profile-corpus.md)。多原型词法消融在 baseline 中设置 `"use_prototypes": true`，可直接使用 `configs/historical_multi_prototype.example.json`。

PCL 与 Scope LLM 默认使用 OpenAI-compatible SSE 流式传输。后端会完整聚合所有 delta，收到 `[DONE]` 后才解析、验证并写入缓存；断流、错误事件、非法 UTF-8、超出 4 MB 或缺少结束标志均进入现有 fallback/重试链。`finish_reason=length/content_filter` 即使形成可解析 JSON 也不会缓存。生产配置将流空闲超时设为 60 秒、通用总生成超时设为 180 秒；真实 canary 后进一步将 Qwen3.6-35B/DeepSeek-V4-Pro 的总时限分别设为 90/120 秒，超时后仍由另一模型接续。传输模式不进入缓存键，因此既有有效非流式缓存仍可复用。

完成 acquisition 后不直接将旧 PCL 画像用于因果评测。先运行 `rebuild-clean-corpus --mode deterministic` 从已存证据生成无网络、论文+冻结 catalog 身份的 lower bound，再运行 `--mode pcl` 仅用截止日前证据重新合成。派生目录分开写入 production/research evidence 与 prototypes，并对证据 ID、paper-backed fallback、逐期刊 PCL provenance 和全部哈希失败关闭。具体命令见[全期刊历史画像语料](../docs/historical-profile-corpus.md)。

PCL bge-m3 的多原型向量分数在评测前冻结：

```bash
python -m research build-prototype-vector-run \
  --api-config llmapi.json \
  --dataset benchmark_artifacts/research_20260814/cached_crossref/papers.jsonl \
  --profiles benchmark_artifacts/historical_venues_20260331/venue_profiles.train.jsonl \
  --cache benchmark_artifacts/historical_venues_20260331/prototype_embeddings.json.gz \
  --output benchmark_artifacts/historical_venues_20260331/runs/pcl_bge_m3_prototype.jsonl
```

正式 `evaluate` 不访问网络，只导入上述冻结 run。当前网页 scope 若采集时间晚于 cutoff，会保存在 production 画像中，但自动排除在论文主榜画像之外。

## 泄漏规则

以下任一项默认中止实验：

- validation/test DOI、具有区分度的标题、查询内容指纹或 query ID 出现在候选画像中；
- 同一 DOI、内容或具有区分度的标题跨越 train 和 validation/test；
- corpus 无快照日期，或快照不早于最早评测样本。

“Introduction”等非区分性通用标题只记 warning。期刊名称和冻结的 aims & scope 是合法的候选特征；如果查询文本本身显式包含金标期刊名，完整分母仍保留该样本，另外报告 `identity_safe` 敏感性指标。预训练向量模型是否见过某篇论文无法仅由文件审计证明，所以每个导入 run 必须在配置和论文中披露模型精确 revision 与训练时间信息。

## 指标与统计

框架对每个 K 报告 Recall、Hit、MRR、nDCG 和 MAP。当前天然标签每个 query 只有一个相关期刊，因此部分指标数值会重合；未来加入专家多相关分级标注后，nDCG/MAP 会表达更多信息。

`metrics.json` 的主结果始终以完整 test 为分母，同时在每个方法下输出四类诊断分层：

- `by_history_status`：`warm` 为金标期刊有至少 5 篇训练历史，`few-shot` 为 1--4 篇，`cold` 为 0 篇；
- `by_profile_level`：优先读取期刊画像的 `metadata.evidence_grade`，并兼容 `profile_level/profile_grade`；
- `by_subject`：按金标论文学科报告，缺失时回退到金标期刊学科；
- `by_jcr_quartile`：按金标期刊 JCR 分区报告。

如果金标期刊不在冻结候选库中，样本会进入 `out-of-catalog` 而不是被删除；元数据缺失则进入 `unknown`。顶层 `primary_evaluation.query_count` 与 `stratification.summary` 记录每个分层的样本数，可用来校验所有分层仍保留完整主分母。旧的 `by_field` 和 `by_quartile` 键作为兼容别名保留。

方法比较使用同一批 query 的 paired bootstrap 差值置信区间和双侧 paired permutation test；随机种子和迭代次数写入配置及结果。

## 快速测试

```bash
python -m unittest discover -s tests -p 'test_research_*.py'
```
