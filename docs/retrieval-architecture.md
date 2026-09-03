# 强制 LightRAG 图谱与向量检索

本项目已将默认查询持久层从 SQLite/FTS5 切换为文件化属性图谱。
“替代”的含义是正常构建、硬过滤、召回、排序和向量查询都不需要打开传统
数据库；不意味着用 LLM 推测的关系覆盖确定性排名事实。CSV/TSV 仍保留为人可审计、
可版本化的事实源。

## 存储结构

`data/venue_graph.json.gz` 是原子替换的 gzip JSON 属性图快照：

| 节点 | 含义 |
| --- | --- |
| `venue` | 保守聚合后的会议/期刊投稿实体 |
| `ranking_record` | 一条原始榜单记录 |
| `ranking` | 榜单体系与等级 |
| `submission_scope` | 已审核的收稿范围、文章类型与明确边界 |
| `topic` | 受控 L2 主题 |
| `area` | 榜单基础分类 |
| `evidence_source` | 收稿范围的辅助证据页 |

主要关系为 `HAS_RANKING_RECORD`、`RANKED_IN`、`CLASSIFIED_AS`、
`HAS_SUBMISSION_SCOPE`、`ACCEPTS_TOPIC`、`SUPPORTED_BY` 和 `RELATED_TOPIC`。
`RELATED_TOPIC` 只由已审核范围中的主题共现生成，保存共现次数和归一化强度。

主题检索必需的 `data/venue_graph_vectors.json.gz` 是与图谱 `source_digest` 和
`semantic_digest` 双重绑定的图节点
float32 向量快照。`data/.embedding_cache.json.gz` 按不含密钥的模型指纹和文本
SHA-256 复用 embedding。三个文件均不是关系数据库或数据库服务。
长驻 worker 在启动时将向量预加载为只读 NumPy 矩阵；大候选子集先做一次全矩阵乘法再筛选分数，避免每次查询复制约 80 MiB 矩阵。标准库标量实现保留为质量回归对照。

## 构建与一致性

```text
榜单 CSV + curated_venue_scopes.tsv
  → 严格字段/受控词/审核状态校验
  → 保守实体聚合
  → 节点和边生成
  → 临时 gzip 快照完整性校验
  → 原子替换 venue_graph.json.gz
```

源文件组合 SHA-256、图 schema 版本、节点/边/记录/实体数被写入元数据。
边不得指向未知节点，榜单记录必须恰好归属一个投稿实体。源文件、schema 或归属
变化会使快照过期，查询前自动重建。

## 检索顺序

1. 沿 `venue → ranking_record → ranking` 关系定位目标等级，再执行类型、分类、审核范围和停用状态硬过滤。
2. LLM 将模糊表达生成受约束的受控主题、中英文扩展词和 Search API 查询，同时输出有界的模糊度与跨学科信号。
3. Search API 获取外部收稿证据；外部文本按不可信输入处理。
4. 属性图合并字段词法、`venue → topic` 直接召回和一跳 `RELATED_TOPIC`。
5. 项目向量侧车在硬过滤集上执行全量精确余弦扫描。
6. LightRAG 强制以 `QueryParam(mode="mix")` 同时检索知识图谱实体/关系和 NanoVectorDB 文本块。
7. 正式离线 SCOPE-Rank 研究路径根据查询画像、通道覆盖和可用性自适应分配候选席位，并执行 train-only 学习融合与校准拒答。暴露开发集上的 learned 方法显著弱于最强 M3 基线，因此不是产品有效性证据；固定预算、RRF 和线性融合都保留为正式消融对照。
8. 用 IDF、字段权重、受控主题、图路径、两路向量、LightRAG 信号和稿件边界融合，最后由 LLM 在已知候选 ID 内受约束重排。

图扩展与向量只能增加召回/相关性信号，不能绕过榜单等级、记录类型、稿件类型或明确排除边界。
Search API 是强制阶段：全部请求失败或全部返回空结果时，查询会失败关闭，禁止
静默退回到仅 LLM/本地召回。生产配置应优先使用带密钥的 Tavily、Brave、Bing
或 SerpAPI；DuckDuckGo 免密钥入口受部署网络可达性影响。

## LightRAG 与 RAG-Anything

主题检索强制安装 LightRAG。`scripts.prepare_retrieval` 生成官方
`insert_custom_kg` 所需的 `chunks/entities/relationships` 结构，再调用异步
`ainsert_custom_kg` 导入。清单将源图谱和 embedding 指纹绑定，不一致就拒绝查询。

存储后端在代码中显式固定为：

```text
KV_STORAGE         = JsonKVStorage
VECTOR_STORAGE     = NanoVectorDBStorage
GRAPH_STORAGE      = NetworkXStorage
DOC_STATUS_STORAGE = JsonDocStatusStorage
```

不存在 Neo4j URI、账号、驱动或退回路径。LightRAG 官方文档确认上述四种默认
文件持久化后端，并提供 `insert_custom_kg`/`ainsert_custom_kg`：

- [LightRAG Core 编程文档](https://github.com/HKUDS/LightRAG/blob/main/docs/ProgramingWithCore.md)
- [LightRAG 官方仓库](https://github.com/HKUDS/LightRAG)
- [RAG-Anything 官方仓库](https://github.com/HKUDS/RAG-Anything)

官方已将 RAG-Anything 的多模态处理并入新版 LightRAG。因此新建环境优先使用
LightRAG；只有需要维护旧文档处理链路时才直接引入 RAG-Anything。

LightRAG 官方也明确说明，默认文件持久化后端更适合开发、调试和中小数据集。
当投稿实体增长到需要多进程并发写、分布式高可用时，单文件图谱不是诚实的
“完美”方案；需要转向可事务化的分布式图/文档存储。当前 23,454 个投稿实体主要是
单机读多写少场景，文件图谱的一致性和简单性更合适。

## 命令

```bash
# 安装并准备强制检索链路
python3 -m pip install -e .
python3 -m scripts.prepare_retrieval --api-config llmapi.json

# 源图谱或模型变化后强制重建（旧 LightRAG 目录会备份）
python3 -m scripts.prepare_retrieval --api-config llmapi.json --force

# 仅校验 LightRAG 导入规模，不调 API
python3 -m scripts.build_lightrag --dry-run

# 强制 LightRAG + 向量 + LLM + Search API 主题查询
python3 -m where_paper_go.recommender --target CCF-A --query '模糊项目描述'

# 真实审核主题 Top-K 效果与延迟基准
python3 -m scripts.benchmark_retrieval

# 无主题的诊断对照仍可用（主题查询会拒绝降级）
python3 -m where_paper_go.recommender --no-graph --target CCF-A --limit 10
```

## 已验证规模

2026-08-24 工程基线（图规模仍对应同一份 45,207 条榜单数据）：

- 45,207 个榜单记录，23,454 个投稿实体。
- 69,240 个节点，137,656 条边。
- 属性图谱 gzip 快照约 6.7 MiB。
- LightRAG custom-KG 映射为 23,714 个实体、2,007 条关系。
- 208/208 自动测试通过，其中包含真实 LightRAG 本地存储导入、`mix` 查询、Web API 配置校验、Search API 引用安全校验以及离线泄漏/重试/流式回归。

单次新进程查询需解压并构建内存邻接/倒排索引，因此目前实测启动约 5 秒、
峰值内存约 520 MiB。本项目明确优先全面性与准确性；若后续转为长驻服务，图谱只需
加载一次，不应用 CLI 冷启动时间作为在线查询延迟。
