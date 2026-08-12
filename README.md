# 顶会顶刊辅助选刊系统

本项目用于在已经给定投稿等级的前提下，快速浏览候选会刊及其研究范围，或根据论文题目、摘要、关键词和项目描述对候选进行方向相关性排序。

它的定位是“辅助形成投稿清单”，不是录用率预测，也不替代作者对最新官网、Call for Papers、投稿截止日期和格式要求的最终核验。

## 核心使用逻辑

```text
选择目标等级
  → 得到满足等级要求的会刊清单
  → 可按学科分类继续过滤
  → 可用论文/项目描述做方向相关性排序
  → 作者自行访问官网确认并做最终选择
```

用户侧最重要的数据是：

1. 榜单体系和等级，例如 CCF-A、TH-CPL-A、中科院 1 区、JCR-Q1。
2. 已审核的会刊级细粒度收稿范围、主题词、文章类型与明确边界。
3. 榜单分类形成的全覆盖基础范围，用于尚未完成细化的候选。
4. 尚未经人工复核的官网自动摘取范围只能作为补充候选。

ISSN、eISSN、publisher 和 URL 不出现在默认结果中，也不是候选进入系统的前提。程序只在内部用经过校验的 ISSN 做保守去重。

## 快速开始

环境要求：Python 3.10 或更高版本。只列会刊/分类时仅使用本地属性图；
只要提供 `--query` 或 `--query-file`，系统就强制使用 LightRAG、
向量语义召回、LLM 和 Search API。

查询默认使用 `data/venue_graph.json.gz` 文件化属性图谱，不启动传统数据库，
也不使用 Neo4j。首次运行或源 CSV/审核 TSV 变化后，程序校验数据、
保守聚合投稿实体，再原子替换图谱快照。等级、主题、收稿范围和证据均以
节点/边表示，后续查询不再重复聚合全部 45,207 条源记录。

首次执行主题检索前，一次性安装并准备全部检索层：

```bash
python3 -m pip install -e .
cp api.example.json llmapi.json
python3 -m scripts.prepare_retrieval --api-config llmapi.json
```

`llmapi.json` 必须同时包含 `llm`、`embedding` 和 `search` 三节。
准备过程会建立精确图节点向量和 LightRAG `mix` 知识库，并将
embedding 缓存到 `data/.embedding_cache.json.gz`。

列出全部 CCF-A 投稿目标：

```bash
python3 -m where_paper_go.recommender --target CCF-A --all
```

同时查询 CCF-A、TH-CPL-A 和中科院 1 区：

```bash
python3 -m where_paper_go.recommender \
  --target 'CCFA或者THCPL-A或者中科院1区' \
  --limit 50
```

只看中科院 1 区中涉及人工智能的期刊：

```bash
python3 -m where_paper_go.recommender \
  --target 中科院1区 \
  --record-type journal \
  --area 人工智能 \
  --all
```

查看 CCF-A 覆盖的分类范围及每类数量：

```bash
python3 -m where_paper_go.recommender --target CCF-A --areas
```

根据论文或项目描述排序：

```bash
python3 -m where_paper_go.recommender \
  --target CCF-A \
  --target THCPL-A \
  --query '面向无线网络流量预测的资源调度方法' \
  --limit 20
```

只在已审核细粒度范围内查找无线网络投稿目标：

```bash
python3 -m where_paper_go.recommender \
  --target 'CCF-A或者THCPL-A或者中科院1区' \
  --scope 无线网络 \
  --reviewed-scope-only \
  --all
```

摘要较长时可以从 UTF-8 文本读取：

```bash
python3 -m where_paper_go.recommender \
  --target 中科院1区 \
  --query-file abstract.txt \
  --record-type journal
```

机器可读输出：

```bash
python3 -m where_paper_go.recommender --target CCF-A --area 计算机网络 --format json
python3 -m where_paper_go.recommender --target 中科院1区 --area 人工智能 --format csv
```

### 持久化属性图谱

手动构建、强制重建或检查图谱：

```bash
python3 -m scripts.build_graph
python3 -m scripts.build_graph --force
python3 -m scripts.build_graph --check
python3 -m scripts.build_graph --summary-json
```

主题查询先沿图谱的等级关系做硬过滤，再合并词法倒排、受控主题节点和
`RELATED_TOPIC` 图路径召回，默认
保留全部词法候选，再使用字段权重、IDF、稿件类型和明确排除边界做可解释
重排。本项目默认优先检索全面性；只有显式设置非零 `--candidate-pool` 才会
截断词法候选。

图谱是可重建的查询表示，CSV 和 `curated_venue_scopes.tsv` 仍是可审计事实源。
`--no-graph` 和旧 `--index PATH` 只可用于无主题的诊断/清单对照；
主题检索会直接拒绝这两种降级路径。详细设计见
[检索架构说明](docs/retrieval-architecture.md)。

### 强制增强检索链路

主题查询固定执行以下步骤，不提供静默回退：

1. LLM 将口语化/模糊描述转换为受控主题、中英文扩展词和搜索语句。
2. Search API、bge-m3 查询向量和 LightRAG `mode="mix"` 并行执行。
3. 项目向量侧车在硬过滤集合上执行全量精确余弦扫描，与词法、受控主题和图路径合并。
4. LLM 以最多两个并发批次为全部 40 个已知候选 ID 评分，不能创建会刊或修改排名事实。
5. 融合排序后，只为最终前十生成中文理由与证据链接；解释阶段不会修改已分配的分数。

```bash
python3 -m where_paper_go.recommender \
  --target CCF-A \
  --query '手机在信号时好时坏时自动调整传输策略' \
  --api-config llmapi.json \
  --format json
```

LightRAG 存储固定为 `NetworkXStorage + NanoVectorDBStorage +
JsonKVStorage + JsonDocStatusStorage`，不存在 Neo4j 配置或回退路径。
向量模型、维度、源图谱摘要或 LightRAG 导入清单不一致时会直接终止。
[LightRAG 官方项目](https://github.com/HKUDS/LightRAG) 已合并
RAG-Anything 的新多模态管线，因此本项目直接强制 LightRAG。

Search API 文本被当作不可信证据，URL 只能来自实际搜索结果或本地来源。
搜索请求全部失败或所有查询都没有可用网页证据时，主题检索会直接报错，不会把
缺失 Search API 的部分结果标为成功。生产环境建议配置 `tavily`、`brave`、
`bing` 或 `serpapi` 及其 `api_key`；免密钥的 `duckduckgo` 仅适合网络可达时的
开发验证，部分网络会阻断其 HTML 搜索入口。
LLM、搜索和六小时内的完整 Web 检索结果缓存到 `data/.query_api_cache/`。
完整结果缓存同时绑定请求参数、源数据、属性图、向量侧车、LightRAG 清单和 API
配置版本，任一依赖变化都会自动失效。在线查询向量使用独立的小型
`data/.query_embedding_cache.json.gz`，避免每次查询重写建库向量缓存。主题查询会将查询、
候选名称和部分收稿范围发送给配置的服务，未公开摘要应先核对服务商数据政策。

## 等级表达式

`--target` 可以重复，也可以使用逗号、分号、“或”“或者”分隔。以下写法均可识别：

| 体系 | 示例 | 当前数据含义 |
| --- | --- | --- |
| CCF | `CCFA`、`CCF-A`、`ccf:A` | CCF 2026 A 类 |
| TH-CPL | `THCPL-A`、`TH-CPL A` | TH-CPL 2019 A 类 |
| 中科院 | `中科院1区`、`中科院一区`、`CAS-1` | 2025 中科院大类 1 区 |
| JCR | `JCR-Q1`、`jcr:q1` | 2025 JCR 第一/主类别 Q1 |

多个目标之间是“或”的关系。结果会合并能够安全识别为同一会刊的记录，并同时展示它在其他榜单中的已知等级；等级不会被合成为一个所谓“全局等级”。

常见的“及以上”也可直接表达，例如 `CCF-B及以上` 会展开为 CCF-A、CCF-B，`中科院2区及以上` 会展开为中科院 1 区、2 区，`JCR-Q2及以上` 会展开为 JCR-Q1、Q2。

## 查询模式

不提供 `--query` 时，程序执行确定性的榜单清单查询：

1. 按 `--target` 硬过滤等级。
2. 按 `--record-type`、`--area` 或 `--scope` 继续过滤。
3. 按目标表达式顺序和名称输出。

提供 `--query` 或 `--query-file` 时，程序在上述候选集内做相关性排序：

1. 对中英文主题词做 Unicode 归一化、英文词切分和中文二元词切分。
2. 优先使用已审核的细粒度范围和主题词，并将中英文查询对齐到受控 L2 方向标签，再回退到榜单分类与基础 `收稿方向`。
3. 对未直接命中的模糊主题，最多扩展一跳经审核范围共现得到的 `RELATED_TOPIC`，输出完整图路径且只给低权重加分。
4. 同一个词命中多个字段时只取最高权重，避免分类、摘要和关键词重复加分。
5. 用逆文档频率降低过于常见词的影响。
6. 若查询使用“原创论文”、“综述文章”等明确稿件类型表达，会排除类型明确不兼容的已审核候选，并识别“不是综述”等否定表达；摘要中普通的 `review prior work` 不会被当成综述稿。
7. `out_of_scope` 既作为显式的不收/受限边界展示，也会在查询明确表达负向条件且与边界高度重合时硬过滤，避免把明确不收的目标排在前面；低置信度边界只做降权。
8. 输出匹配分、命中词和图路径，便于理解排序原因。

匹配分只表示当前数据中的词语重合程度，不是投稿成功率、录用概率或权威适配度。

### 已审核细粒度范围

独立数据文件 `data/curated_venue_scopes.tsv` 当前包含 160 个投稿目标：

- CCF-A 主会 58/58，全部覆盖；会议范围只采用主会研究轨，不混用 workshop、industry track 或 short-paper track。
- TH-CPL A 117/117，全部覆盖；跨榜单重合会刊复用同一条审核范围。历史条目 IPSN 已标记为并入 SenSys、不可再独立投稿。
- 中科院 1 区计算机科学大类期刊 53/53，全部覆盖。

每条审核记录严格锚定榜单、榜单年份、记录类型和完整名称，并保存细分主题、受控 L2 标签、文章类型、投稿方式、范围年份、明确不适用范围、主/辅来源和审核信息。加载时会校验受控值、日期、URL、锚点和实体唯一性；同一投稿实体最多启用一条审核范围。来源 URL 属于内部审核元数据，不在默认推荐界面中展示。

投稿语义也单独建模：普通主会、期刊优先/会议展示、会议审稿后进入期刊论文集、仅限邀请、先提交选题提案、需要继续选择具体分刊的期刊家族，以及已经合并停用的历史目标不会混为一种“可投稿期刊”。刊系占位项和历史合并项默认不进入推荐结果；需要审计历史榜单时可使用 `--include-inactive` 显式查看。

`--area` 只过滤榜单基础分类；`--areas` 汇总时默认同样排除历史合并实体和刊系占位项，需要审计这些记录时可加 `--include-inactive`。`--scope` 只过滤已审核细粒度范围。二者不会混为同一种数据质量。过滤保留 `C++`、`C#`、`.NET` 等技术符号，英文缩写按完整词匹配。显式使用 `--scope` 或 `--reviewed-scope-only` 时，若审核数据文件缺失则直接报错，不会伪装成“零匹配”。

### 官网范围候选

当前官网自动摘取数据尚未完成人工复核，因此：

- 默认不使用官网摘取文本参与排序。
- 若结果存在官网范围，文本输出会明确标为“自动摘取，待核验”。
- 可显式传入 `--match-official-scope`，以较低权重让其参与匹配。
- 官网补全失败不会导致该会刊被排除。

## 当前数据

| 数据集 | 记录数 | 类型 | 等级 |
| --- | ---: | --- | --- |
| CCF 2026 | 386 | 会议 | A/B/C |
| TH-CPL 2019 | 406 | 225 会议、181 期刊 | A/B |
| 中科院 2025 | 21,772 | 期刊 | 大类 1/2/3/4 区 |
| JCR 2025 | 22,643 | 期刊 | 第一类别 Q1/Q2/Q3/Q4/N/A |

需要特别注意：当前 `ccf_conferences_2026.csv` 只包含 CCF 会议。因此严格查询“CCF-A 期刊”会返回 0 条；界面统一使用“投稿目标”或“会刊”，避免把会议错误称为期刊。

目前 CCF-A、TH-CPL A 和中科院 1 区计算机科学大类期刊均已完成细粒度范围覆盖。其他等级或学科中尚未覆盖的候选仍使用基础分类参与清单和主题召回，不会因为缺少 scope 而从等级结果中消失。

数据字段与 enrichment 说明见 [data/README.md](data/README.md) 和 [收稿范围补全文档](docs/enrichment.md)。

## 常用参数

```text
--target / -t             目标等级，可重复
--record-type             all、journal 或 conference
--area                    只按榜单分类/基础范围过滤，可重复
--scope                   只按已审核细粒度范围过滤，可重复
--reviewed-scope-only     只看已有审核范围的投稿目标
--include-inactive        包含历史合并实体和不可直接投稿的刊系占位项
--areas                   汇总分类范围
--query / -q              论文题目、摘要、关键词或项目描述
--query-file              UTF-8 文本文件
--limit / -n              最多显示数量，默认 30
--all                     显示全部
--names-only              只显示名称、类型和等级
--match-official-scope    让未复核官网范围以低权重参与匹配
--api-assisted-search     兼容选项（主题检索已强制启用）
--api-config              同时含 llm/embedding/search 配置节的 JSON
--api-cache-dir           指定 API 查询和重排缓存目录
--api-candidate-limit     LLM 重排候选数，默认 40
--api-search-query-limit  Search API 查询数，默认 3
--api-search-results      每条搜索保留结果数，默认 8
--api-rerank-weight       LLM 与本地排序的融合权重，默认 1.0
--format                  text、json 或 csv
--graph                   指定属性图谱快照
--rebuild-graph           查询前强制重建图谱
--no-graph                仅无主题诊断可用；主题检索拒绝
--index                   仅无主题旧 SQLite 对照可用
--candidate-pool          词法候选池大小，默认 0（不限）
--vector-search           兼容选项（主题检索已强制启用）
--embedding-config        兼容别名；必须与 --api-config 指向同一文件
--embedding-cache         指定 embedding 缓存路径
--vector-limit            语义候选池大小，默认 500
--approximate-vector-search 使用符号位近似召回；默认精确扫描
--vector-min-similarity   最低余弦相似度，默认 0.35
--vector-weight           混合排序语义权重，默认 6.0
--lightrag-working-dir    LightRAG 本地存储目录
--lightrag-top-k          LightRAG 实体/关系召回数，默认 200
--lightrag-chunk-top-k    LightRAG 向量文本块召回数，默认 200
--lightrag-weight         LightRAG mix 融合权重，默认 5.0
```

完整帮助：

```bash
python3 -m where_paper_go.recommender --help
```

## Web 检索工作台

无需 Node.js 即可启动同源前端：

```bash
python3 -m where_paper_go.web_app --host 127.0.0.1 --port 8000
```

打开 `http://127.0.0.1:8000/`，即可使用主题输入、等级/类型/范围筛选、四阶段检索状态、结果详情抽屉和证据链接。前端与命令行共用同一条强制检索链路，详细说明见 [Web 前端文档](docs/web-frontend.md)。

## 项目结构

```text
where-paper-go/
├── where_paper_go/        # 核心检索、图谱、API、Web 服务和静态前端
│   └── static/            # HTML、CSS、JavaScript
├── scripts/               # 建库、迁移、数据补全和性能基准
├── tests/                 # 单元、集成和检索约束测试
├── docs/                  # 架构、前端、数据补全与旧索引说明
├── data/                  # 可审计源数据；生成索引和缓存由 .gitignore 排除
├── papers/                # 研究资料元数据；论文 PDF 不进入 Git
├── api.example.json       # 无密钥配置模板
├── pyproject.toml         # Python 包、依赖与命令入口
└── README.md
```

本地密钥文件 `llmapi.json`、LightRAG 存储、图向量、查询缓存、备份和论文
PDF 均不会被 Git 跟踪。

## 许可证

项目代码采用 [Apache License 2.0](LICENSE) 发布。数据文件、论文、榜单名称及
第三方内容仍受各自来源的版权、许可和使用条款约束。

## 测试

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
python3 -m scripts.benchmark_retrieval
# 可选：与旧索引做 Top-K 对照
python3 -m scripts.benchmark_retrieval --legacy-index data/venue_index.sqlite3
```

当前 85 项测试覆盖等级表达式、真实记录数、审核范围、跨榜单聚合、图节点/边完整性、
图路径模糊召回、图谱新鲜度、文件向量精确召回、LightRAG 导出引用完整性、旧 SQLite 兼容对照、LLM/API 约束和代表性主题排序。
