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
├── scope_rank_runs.py     # 正式 SCOPE-Rank 与 11 个冻结消融
├── scope_rank_selective.py # 不重拟合的 coverage/risk/校准事后评测
├── sealed_preflight.py    # 标签无关的一次评测预检
├── sealed_namespace_crosswalk.py # exact-ISSN venue namespace 双射
├── sealed_namespace_repair.py # 授权的一次性 post-access 修复评测
├── sealed_evaluation.py   # 冻结预测后的 sealed 评测器
├── expert_review.py       # 三专家盲评材料、审计与导出
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

新对话开始前先阅读根目录 [`HANDOFF.md`](../HANDOFF.md)，并以 Section 0
作为当前权威状态。P0-A～P0-C、M3 强基线与 SCOPE-Rank 暴露开发集评测已冻结；
learned SCOPE-Rank 是显著负结果，不得声称方法有效。大型语料、
embedding 和逐查询 run 继续保存在忽略目录，不属于上述源码结构，也不得为
开始 M3 而重新生成。未来集的自动评测已以「标签访问后、确定性 namespace
修复」形式完成，必须明确标注它不是 pristine single-pass sealed test；真实
专家标注仍为 0，不得声称人评完成。

## 1. 运行现有 500 篇基准

P0-C 的正式开发集验收使用：

```bash
python -m research evaluate \
  --config research/configs/p0c_clean_pcl_acceptance.json
```

该 Search-free 运行完整覆盖 train/validation/June-test =
`1,086 / 1,544 / 2,161`，严格绑定全部 4,791 个 query 和按字典序冻结的
20,087 个候选。泄漏审计的 critical finding 为 0；June-test 上 BM25/TF-IDF
的 Hit@10 分别为 `0.0777417862 / 0.0920869968`，nDCG@10 分别为
`0.0443966293 / 0.0525464592`。这些是已暴露 development set 的可复现基线，
不是不可见测试结论。

旧 500 篇基准继续作为产品链路诊断与回归参考：

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

clean-PCL bge-m3 的多原型向量分数在评测前冻结；复用现有 v5 画像，不重建
clean corpus：

```bash
python -m research build-prototype-vector-run \
  --api-config llmapi.json \
  --dataset benchmark_artifacts/research_20260814/cached_crossref/papers.jsonl \
  --profiles benchmark_artifacts/historical_venues_20260331_clean_pcl_v5/venue_profiles.train.jsonl \
  --reference-manifest benchmark_artifacts/p0c_acceptance_20260824/clean_pcl_lexical_v2/manifest.json \
  --cache benchmark_artifacts/m3_strong_baselines_20260827/bge_m3_embeddings.json.gz \
  --output benchmark_artifacts/m3_strong_baselines_20260827/bge_m3_prototype_max_cache_only_v3.jsonl \
  --cache-only

PYTHONDONTWRITEBYTECODE=1 python -m research evaluate \
  --config research/configs/m3_all_strong_baselines_unified_v2.json
```

正式 `evaluate` 不访问网络，只导入上述冻结 run。当前 bge-m3
prototype-max 的 June-slice Hit@10 / nDCG@10 为 `0.1096714484 /
0.0593422852`；相对 clean-PCL BM25 的 nDCG@10 改善通过配对检验，
相对 TF-IDF 的全切片 95% 区间跨过零，不宣称显著。当前网页
scope 若采集时间晚于 cutoff，会保存在 production 画像中，但自动排除在
论文主榜画像之外。

## 5. 正式 graph、LightRAG 与科学模型强基线

property-graph run 只读取 P0-C 冻结画像、原型和 evidence，并验证原型到
真实 evidence 的边、时间边界和 manifest 哈希；LightRAG mix 只融合已冻结
的 local graph run 与 global bge run。两者都不调用 LLM、Search 或 embedding
API，也不会用生成文本替代真实边：

```bash
python -m research build-property-graph-run \
  --dataset benchmark_artifacts/research_20260814/cached_crossref/papers.jsonl \
  --profiles benchmark_artifacts/historical_venues_20260331_clean_pcl_v5/venue_profiles.train.jsonl \
  --prototypes benchmark_artifacts/historical_venues_20260331_clean_pcl_v5/prototypes.jsonl \
  --evidence benchmark_artifacts/historical_venues_20260331_clean_pcl_v5/research_evidence.jsonl \
  --corpus-manifest benchmark_artifacts/historical_venues_20260331_clean_pcl_v5/manifest.json \
  --reference-manifest benchmark_artifacts/p0c_acceptance_20260824/clean_pcl_lexical_v2/manifest.json \
  --output benchmark_artifacts/m3_strong_baselines_20260827/property_graph_edge_bm25_rrf_v1.jsonl \
  --cutoff 2026-03-31 --top-k 100 --candidate-pool 1000 --rrf-k 60 \
  --prototype-weight 1.0 --evidence-weight 1.0 --edge-support-weight 0.15

python -m research build-lightrag-mix-run \
  --dataset benchmark_artifacts/research_20260814/cached_crossref/papers.jsonl \
  --profiles benchmark_artifacts/historical_venues_20260331_clean_pcl_v5/venue_profiles.train.jsonl \
  --reference-manifest benchmark_artifacts/p0c_acceptance_20260824/clean_pcl_lexical_v2/manifest.json \
  --property-graph-run benchmark_artifacts/m3_strong_baselines_20260827/property_graph_edge_bm25_rrf_v1.jsonl \
  --vector-run benchmark_artifacts/m3_strong_baselines_20260827/bge_m3_prototype_max.jsonl \
  --output benchmark_artifacts/m3_strong_baselines_20260827/lightrag_mix_edge_rrf_v1.jsonl \
  --top-k 100 --rrf-k 60 --local-weight 1.0 --global-weight 1.0
```

完整强基线冻结配置是
`research/configs/m3_all_strong_baselines_unified_v2.json`。它统一导入 BM25、
TF-IDF、bge-m3、SPECTER2、SciNCL、property graph、LightRAG mix、cross-encoder
和三个 RRF 对照，并对 11 个方法的全部 55 个无序方法对作统一校正。完整
June-test 分母为 2,161；结果、哈希、延迟、失败统计和所有显著/非显著/负结果见
[M3 strong-baseline freeze](../docs/m3-strong-baselines.md)。这里只是已暴露
development set 结果，不能作为 sealed test 或论文最终有效性结论。旧配置和旧
artifact 继续保留，不作为最新全方法冻结。

SPECTER2、SciNCL 和 bge-reranker-v2-m3 从精确 commit 获取到忽略目录。
仓库配置 `research/configs/m3_official_model_assets.json` 固定 repo、40 位
revision、最小推理文件集合及约 3.188 GB 的规划估计。以下命令始终先对全部
缺失资产执行 HF CLI dry-run，记录缓存覆盖、磁盘、已知 API 成本和配额边界；
不带 `--execute` 时绝不下载：

```bash
python -m research materialize-model-assets \
  --config research/configs/m3_official_model_assets.json \
  --output-root benchmark_artifacts/m3_model_assets_20260828 \
  --hf-cli /home/wangrj/.cache/adodas_venv/bin/hf
```

只有收到明确下载授权后，才可在同一命令追加：

```bash
python -m research materialize-model-assets \
  --config research/configs/m3_official_model_assets.json \
  --output-root benchmark_artifacts/m3_model_assets_20260828 \
  --hf-cli /home/wangrj/.cache/adodas_venv/bin/hf \
  --execute \
  --authorization-reference '<non-secret authorization audit ID>'
```

执行模式不接受空授权引用，也不接受 branch/tag revision。每个资产先进入唯一
`.building-*` 目录；下载或结构校验失败时保留该目录和脱敏失败记录，成功后才
写入逐文件 SHA-256 manifest 并原子发布。既有目标只校验和复用，任何身份或
payload 哈希差异都会失败关闭，绝不覆盖。HF 下载属于准备步骤；下列正式 run
仍严格 local-files-only、Search-free，缓存指纹同时绑定模型树、revision、
精度、batch size、设备和确定性设置。metadata dry-run 默认 120 秒硬超时，
单资产下载默认 21,600 秒硬超时，二者都可由 CLI 显式调整并写入审计：

SPECTER2 的 proximity adapter 必须使用与 AdapterHub 兼容的独立
overlay。当前冻结组合是 Python 3.12、`torch==2.11.0+cu130`、
`adapters==1.3.0`、`transformers==4.57.6` 和
`huggingface-hub==0.36.2`。三个 PyPI wheel 的哈希锁定在
`research/configs/m3_adapter_overlay_requirements.txt`；在忽略的隔离运行时中先
执行 `pip install --dry-run --report ...`，确认解析后再执行：

```bash
python -m pip install --no-deps --require-hashes \
  -r research/configs/m3_adapter_overlay_requirements.txt
```

不得将该 overlay 安装到 Web/API 或既有模型环境。正式 SPECTER2 运行在
forward 前显式调用 `set_active_adapters` 并验证活动组合含
`specter2_proximity`；仅成功加载权重不视为通过。

```bash
benchmark_artifacts/m3_model_runtime_20260828/venv/bin/python \
  -m research build-scientific-encoder-run \
  --protocol scincl \
  --model-dir benchmark_artifacts/m3_model_assets_20260828/scincl__ebc5348d184b \
  --model-repo malteos/scincl \
  --model-revision ebc5348d184ba2fc9beee69b4e394263fce57b2e \
  --dataset benchmark_artifacts/research_20260814/cached_crossref/papers.jsonl \
  --profiles benchmark_artifacts/historical_venues_20260331_clean_pcl_v5/venue_profiles.train.jsonl \
  --reference-manifest benchmark_artifacts/p0c_acceptance_20260824/clean_pcl_lexical_v2/manifest.json \
  --cache benchmark_artifacts/m3_strong_baselines_20260827/scincl_embeddings_v2.sqlite3 \
  --output benchmark_artifacts/m3_strong_baselines_20260827/scincl_prototype_max_v2.jsonl

benchmark_artifacts/m3_model_runtime_20260828/venv/bin/python \
  -m research build-cross-encoder-run \
  --model-dir benchmark_artifacts/m3_model_assets_20260828/bge_reranker_v2_m3__953dc6f6f85a \
  --model-repo BAAI/bge-reranker-v2-m3 \
  --model-revision 953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e \
  --dataset benchmark_artifacts/research_20260814/cached_crossref/papers.jsonl \
  --profiles benchmark_artifacts/historical_venues_20260331_clean_pcl_v5/venue_profiles.train.jsonl \
  --reference-manifest benchmark_artifacts/p0c_acceptance_20260824/clean_pcl_lexical_v2/manifest.json \
  --first-stage-run benchmark_artifacts/m3_strong_baselines_20260827/lightrag_mix_edge_rrf_v1.jsonl \
  --cache benchmark_artifacts/m3_strong_baselines_20260827/bge_reranker_v2_m3_pairs_b128.sqlite3 \
  --output benchmark_artifacts/m3_strong_baselines_20260827/bge_reranker_v2_m3_lightrag_top100_b128.jsonl \
  --batch-size 128
```

SPECTER2 使用相同命令并增加精确 proximity adapter 目录、repo 和 revision；
其运行环境还必须固定并记录 `adapters` 包。2026-08-28 的四组授权资产已全部
原子发布并校验，SPECTER2、SciNCL 和 cross-encoder 正式 run 均完成且以零
Search/API 调用导入统一评测。不得把这项状态外推为 sealed-test 结论。

不下载模型也可验证真实 Transformers/safetensors 本地加载、CLS pooling、
归一化和 sequence-classification logits 路径。普通无 Torch 测试环境会明确
skip；模型运行环境必须通过：

```bash
benchmark_artifacts/m3_model_runtime_20260828/venv/bin/python -m unittest \
  tests.test_local_model_runtime -v
```

2026-08-28 正式隔离运行时使用 Python 3.12.3、Torch 2.11.0、
`adapters==1.3.0`、Transformers 4.57.6、huggingface-hub 0.36.2 和
safetensors 0.7.0，真实模型测试 6/6 通过；SPECTER2 活动 adapter 还通过
启用/禁用输出差异检查。该 overlay 通过 `.pth` 读取既有 Torch 环境，因此
`pip check` 会看到与本实验无关的父环境 vLLM/Transformers 版本冲突；正式
provider 不导入 vLLM，manifest 仍完整记录重复可见的 distributions，不能把
环境描述为全局依赖无冲突。

## 6. SCOPE-Rank 正式运行、消融与拒答评测

方法配置只读取已冻结 M3 run，使用确定性 train/calibration 分割，并在任何
validation/test 标签评分之前一次性产生 full + 11 消融。三个命令都必须使用
新输出路径；正式目录不得覆盖：

```bash
PYTHONDONTWRITEBYTECODE=1 \
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_DATASETS_OFFLINE=1 \
python -m research build-scope-rank-suite \
  --config research/configs/scope_rank_exposed_development_v1.json

PYTHONDONTWRITEBYTECODE=1 \
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_DATASETS_OFFLINE=1 \
python -m research evaluate \
  --config research/configs/scope_rank_unified_evaluation_v1.json

PYTHONDONTWRITEBYTECODE=1 \
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_DATASETS_OFFLINE=1 \
python -m research evaluate-scope-rank-selective \
  --config research/configs/scope_rank_selective_evaluation_v2.json
```

正式 learned full 在 2,161-query June test 的 nDCG@10 为 `0.0138134702`，
最强 M3 LightRAG 为 `0.0855317887`；LightRAG 减 full 的 95% CI 为
`[0.0610749606, 0.0823773039]`，Holm `p=0.0389805097`。线性和 RRF 替代的
nDCG@10 为 `0.0883333369 / 0.0868353087`，但与 LightRAG 比较均为
Holm `p=1.0`，不宣称增益。full 校准器在 221 个 train-only 校准 query 上
Top-1 正确数为 0，因而 fail-closed 并对全部 query 拒答。

完整 78 配对、所有消融、selective metrics、哈希、成本/延迟和根因见
[`docs/scope-rank-results.md`](../docs/scope-rank-results.md)。这是可复现平台交付与科学负结果，
不是 sealed-test 或论文方法有效性证据。

## 7. 未来 sealed test 与专家盲评

2026 年 7 月未来集的方法、候选、模型 revision、指标和统计协议已冻结；
Crossref 有界采集已按授权以完整 300/300 分母原子发布。全部 36 个 strata
填满，累计账本为 234/1,000；采集本身为 USD 0，未调用 Search、LLM 或
embedding。正式 future manifest、blind queries 和 mode-`0600` label vault 的
SHA-256 分别为 `b11de0a6...650`、`9cbf1948...96c4` 与
`1de2664e...bab`。三个采集失败目录与部分分母拒绝证据仍完整保留。

标签访问前，BM25、TF-IDF、property graph、SPECTER2 proximity、SciNCL、
bge-m3、LightRAG mix 和 cross-encoder 八个 source run 均完成 300 条
Top-100 ranking，0 empty、0 failed、全程 Search-free。已授权的 bge-m3 调用
只使用 ignored shadow cache，5 个逻辑 batch/实际 5 次请求、USD 0，
M3 正式缓存保持字节不变。随后得到 full、fixed-linear 和 RRF 三个
冻结 SCOPE 变体，并在首次 label access 前写入 prediction commitment
`8a2732e1626397d58f0be7bd9665aa98b79ddade13b1a294722640a5a39d875a`。

首次正式 evaluator 在 commitment 后按协议访问标签，但在生成 metrics 前
fail-closed：Crossref 采集器生成的 JCR venue ID namespace 与冻结 profile
namespace 不同。该首次访问审计 SHA-256 为
`85a0bab2daf23449026a016832de3daa1591f6fd03d2964e75f67b880e84e4a2`；没有隐藏失败或
产生不完整 metrics。

独立、label-free 的 catalog crosswalk 只使用 checksum-valid exact ISSN 唯一归属，
完成 20,087 source 到 20,087 target 的一对一全覆盖：20,039 identity、48
remap，unmapped/ambiguous/collision 全为 0。crosswalk manifest 与 mapping
SHA-256 为
`64456236a956ece0929bffc923b2f918a09c292fd3d35c1f2a9bd55eb2940d33` /
`c2001797828626141c8c6ae799a596853c016744690ef8fb320c9e883def1485`。

在显式授权的独立 one-shot guard 下，post-access namespace repair 只翻译 qrel
ID，不改变 method、prediction、query/order/text、candidate、gain、hyperparameter 或
statistics。它保留完整 300 query / 20,087 candidate 分母，评测 4 个
冻结方法与全部 6 个无序 pair；0 dropped、0 unmapped、0 failed、0 critical
leakage。正式 evaluation manifest、metrics、leakage audit 和 namespace audit 为：

- `b0eb5d5045df10a0e64f7dc0ffba264bdc479671cb669197b5f3580d79391a0b`;
- `e50da50af5a39266a8af9ef2fdde05bfc82abf2a5d11a047813567060cc7e52a`;
- `54cb5246cca70decb8b5383da650670dc0630c07e8b4f3b31fb9cc4b74e7e725`;
- `e42d787a4a595ed2e8effefe3e91c0fbb0be544f95bde66ee522f95842248c71`.

该运行必须表述为 **audited post-access namespace-repaired future
evaluation**；其修复是 deterministic，且 `pristine_single_pass_sealed_test=false`。不得称为 pristine sealed-test
success，也不得删除 null/负结果或将工程完成改写为 SCOPE-Rank 有效性。

三专家盲评 manifest
`75cdf406fbad493c751ca453c3e0d3fceb1b8923d2869793036d270d6e6e13a7`
已固定 250 queries、4 methods、6,129 个去重 review items 与 3 名匿名专家；
真实标注数为 0，agreement 不可用。当前唯一正确状态为
`tools_and_materials_complete_human_evaluation_pending`（工具与材料完成，人工评测待执行）。
不得重跑两个 evaluator、手工重新打开 label vault、覆盖正式输出或代替专家
生成标注。完整协议、授权/失败边界和产物 hash 见
[`docs/future-sealed-test.md`](../docs/future-sealed-test.md)。

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
