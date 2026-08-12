# 旧 SQLite 检索索引（兼容模式）

> 默认持久层已替换为属性图谱，请优先阅读
> [检索架构](retrieval-architecture.md)。本文仅保留用于旧部署、对照实验和回归测试。

`venue_index.sqlite3` 是从榜单 CSV 和人工审核 TSV 生成的可丢弃查询缓存。
它解决两个问题：避免每次 CLI 调用重新解析全部源文件、避免每次查询重复执行
跨榜单实体聚合。

## 数据流

```text
四份榜单 CSV + curated_venue_scopes.tsv
  → 原有严格校验
  → 保守实体聚合
  → 临时 SQLite 文件
  → quick_check
  → 原子替换 venue_index.sqlite3
```

可选的向量构建链路：

```text
venue.semantic_text
  → embedding API（显式启用）
  → 文本哈希缓存
  → L2 归一化 float32 向量 + 符号位
  → vector_embedding
```

可选的 API 辅助查询链路：

```text
原始查询
  → LLM 结构化理解（扩展词 + 白名单 topic_tag + 搜索语句）
  → 本地 FTS5 + topic_tag + 可选向量召回
  → Search API 官网/CFP 证据
  → LLM 只对已知候选 ID 打分
  → 与本地排序做加权倒数排名融合
```

构建过程中不会修改源 CSV/TSV。构建失败时保留原索引；查询也不会使用已经
判定为过期的索引。

## 表结构

- `venue`：聚合后的投稿实体及其类型、规范名称、审核状态和语义文本。
- `venue_record`：构成实体的全部原始榜单记录，用于恢复原有输出语义。
- `ranking`：按榜单、等级和实体建立的结构化索引。
- `venue_alias`：名称、简称和 ISSN 等实体别名及其规范形式。
- `scope`：人工审核范围及投稿语义。
- `topic_posting`：受控 L2 方向标签倒排表。
- `venue_fts`：字段化 FTS5 索引。
- `vector_embedding`：可选的归一化 float32 向量、模型指纹及符号位。
- `index_meta`：schema 版本、源数据 SHA-256、构建时间和记录数。

FTS 字段保持独立，以便 BM25 对简称、审核关键词、审核范围、榜单分类和自动
摘取候选使用不同权重。中文沿用推荐器的二元切分，`C++`、`C#`、`.NET`
等技术词保留特殊字符。

有人工审核范围的实体使用名称、分类、审核范围和审核主题生成
`semantic_text`；尚未覆盖细粒度范围的实体只使用基础分类。未经人工复核的
官网自动摘取文本不会进入向量。基础分类相同的文本会共享 embedding 缓存，
但每个实体仍保留独立向量记录。

## 查询过程

1. 解析目标等级和查询意图。
2. 通过 `ranking` 定位包含目标等级的实体，恢复其完整跨榜单记录。
3. 继续执行类型、分类、审核范围和停用状态硬过滤。
4. 在剩余实体中执行 FTS5/BM25 召回，并合并受控 `topic_tag` 召回。
5. 若显式启用语义检索，在同一个硬过滤集合中读取全部 float32 向量并执行
   精确余弦相似度排序；只有显式使用 `--approximate-vector-search` 时才先
   扫描紧凑符号位，以汉明距离缩小候选集合。
6. 合并词法、受控标签和语义候选，使用整个硬过滤集合的文档频率执行重排。
7. 应用原创/综述类型约束、停用目标惩罚和 `out_of_scope` 边界。

显式使用 `--api-assisted-search` 时，在第 1 步后由 LLM 生成有上限的中英文
扩展词，并且只允许选择索引已知的 `topic_tag`。本地召回后，最多将
`--api-candidate-limit` 个候选交给 LLM；输出 ID、证据 URL 和分数都经过白名单
校验。Search API 只能让结果映射回等级硬过滤后的已知会刊，不能添加任意
字符串作为推荐。重排使用加权倒数排名融合，保留本地排序信号。

向量相似度只增加召回和排序信号，不能绕过榜单等级、类型、分类、审核范围、
稿件类型和明确排除条件。实现只依赖 Python 标准库，不要求 NumPy、FAISS 或
SQLite 向量扩展。

质量优先默认值为：词法候选不设上限、语义候选 500、向量精确扫描。对于明确
愿意牺牲少量召回率换取延迟的部署，可设置 `--candidate-pool` 并启用
`--approximate-vector-search`。

官网自动摘取字段仍然默认不参与匹配，只有显式使用
`--match-official-scope` 时才进入 FTS 列过滤范围。
API 辅助模式会自动以低权重使用该字段，但输出仍标记为“自动摘取，
待核验”，不会冒充人工审核范围。

## 新鲜度和故障处理

每次启动会计算以下文件的组合 SHA-256：

- `ccf_conferences_2026.csv`
- `th_cpl_partition_2019.csv`
- `cas_partition_2025.csv`
- `jcr_partition_2025.csv`
- `curated_venue_scopes.tsv`

任一文件变化或索引 schema 版本变化都会触发原子重建。索引不可写、损坏或
FTS 查询异常时，默认打印警告并回退到 CSV 内存流程；显式使用
`--rebuild-index` 时，重建失败会直接报错。

语义检索是显式模式：未指定 `--vector-search` 时不会读取 embedding 配置，
也不会发送外部请求。显式启用后，如果向量未构建、模型指纹不同、维度错误或
API 请求失败，命令会直接报错，不会悄悄退回词法结果。API 密钥不写入索引或
缓存；索引只保存不含密钥的配置指纹。

API 辅助检索也是显式模式。配置缺失时直接报错；运行中的 LLM 或证据
重排请求失败时，会输出警告并回退到已完成的本地排序。查询、候选范围
和搜索语句会发送给用户配置的外部服务，缓存默认位于
`data/.query_api_cache/`，不保存 API 密钥。

## 命令

```bash
# 构建（已有新鲜索引时不重复工作）
python3 -m scripts.build_legacy_index

# 强制重建
python3 -m scripts.build_legacy_index --force

# CI/运维检查；过期时退出码为 1
python3 -m scripts.build_legacy_index --check

# 使用 api.json 中独立的 embedding 配置构建向量；此命令可能调用外部 API
python3 -m scripts.build_legacy_index --with-vectors --embedding-config api.json

# 混合使用 FTS5、受控标签和向量语义召回
python3 -m where_paper_go.recommender \
  --target CCF-A \
  --query '面向弱连接终端的链路自适应方法' \
  --vector-search \
  --embedding-config api.json

# LLM 查询理解 + Search API 证据 + 受约束重排
python3 -m where_paper_go.recommender \
  --target CCF-A \
  --query '手机在信号时好时坏时自动调整传输策略' \
  --api-assisted-search \
  --api-config api.json

# 对照旧流程
python3 -m where_paper_go.recommender --no-index --target CCF-A --query '计算机网络'
```

生成的 `data/venue_index.sqlite3` 和 `data/.embedding_cache.sqlite3` 已加入
`.gitignore`，不应作为事实数据提交。
数据发布或部署时可以在测试通过后预构建该文件，以消除首次查询的构建成本。

配置采用 OpenAI-compatible `/embeddings` 请求格式。必须在配置文件中提供独立
的 `embedding.model`，程序不会把 `llm.model` 当成嵌入模型。可复制
`api.example.json` 的 `embedding` 节；固定维度模型可省略 `dimensions`。
