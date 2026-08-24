# 近期论文期刊恢复评测

本评测把近期已发表论文的真实期刊视为一个自然正标签：系统只能看到标题与摘要，随后检查真实期刊是否出现在推荐结果前 K 位。它衡量的是“已发表期刊恢复率”，不是所有合理投稿目标的完整准确率；未命中的其他推荐期刊仍可能同样适合。

## 数据集构建

主数据源使用 Crossref REST API 的 `journal-article` 元数据。Crossref 无需注册即可访问，并支持发布日期、是否含摘要和文章类型过滤。构建器不会抓取 PDF 或出版社全文，只读取 DOI、标题、摘要、发布日期、期刊名与 ISSN。

默认协议（本次先导实验显式固定为以下日期）：

- 固定发布日期窗口为 `2026-01-01` 至 `2026-06-30`，避免使用尚未闭合的当前月份；
- 只保留有 DOI、标题、可用摘要、有效发布日期和期刊 ISSN 的 journal article；
- 安全移除 JATS/XML 标记，并排除摘要过短、勘误、撤稿、社论等明显非研究论文；
- 将外部 ISSN/eISSN 经校验后映射到当前 JCR Q1–Q4 期刊实体；多个 ISSN 指向不同实体时拒绝该样本；
- 按 JCR 主分类的宽领域与分区分层，以稳定哈希抽样；默认每种期刊最多一篇，避免高产大刊支配指标；
- 记录构建参数、数据源 URL、图谱摘要与数据集 SHA-256，保证实验可复现。

```bash
python3 -m scripts.build_recent_journal_benchmark \
  --from-date 2026-01-01 \
  --until-date 2026-06-30 \
  --sample-size 500 \
  --mailto your-email@example.com
```

默认输出位于 `benchmark_artifacts/recent_journals/`。该目录可能包含摘要，因此被 Git 忽略；仓库只提交构建代码、测试和汇总报告。Crossref 说明部分成员提供的摘要可能仍受出版者或作者版权约束，不应未经核验批量再发布。

新建数据集的默认规模现为 500 篇。本仓库中已有的 20 篇先导实验历史结果仍保留在[结果报告](recent-journal-benchmark-results.md)中，用于与 500 篇新基准对照。

## 20,087 个期刊的 aims & scope 补全

先生成全目录状态和可恢复队列：

```bash
python3 -m scripts.enrich_journal_scope_catalog --status-only
```

正式运行时会先处理 500 篇基准数据集的金标准期刊，然后按学科与 JCR 分区交错扫描其余唯一期刊实体。同一 ISSN 实体只调用一次 Search API + LLM，成功结果同步回 JCR/CAS/TH-CPL 所有重复行。默认每 10 个实体原子落盘，再次执行会跳过已成功实体：

```bash
python3 -m scripts.enrich_journal_scope_catalog \
  --api-config llmapi.json \
  --workers 4 \
  --checkpoint-every 10
```

可先用 `--limit 100` 小批量验证。队列与覆盖统计默认写入 `benchmark_artifacts/scope_enrichment/`；缓存继续复用 `data/.aims_scope_cache/`。

失败实体会记入 `attempts.jsonl` 并移到同优先级队列后端，避免单个受阻出版社页面反复占据队首。官网范围发生变化后，需要将其同步进属性图、bge-m3 向量和 LightRAG：

```bash
python3 -m scripts.prepare_retrieval \
  --api-config llmapi.json \
  --force \
  --force-graph
```

需要持续推进时，使用可恢复的批次监督器。它将单批限制在 50–100 个期刊，默认不重试本轮已尝试实体，并在每批结束后归档报告、刷新检索资产。`--priority-only` 会在 500 篇金标准数据集对应期刊全部尝试后自动停止：

```bash
python3 -m scripts.run_scope_enrichment_batches \
  --api-config llmapi.json \
  --batch-size 50 \
  --workers 2 \
  --priority-only
```

运行状态位于 `benchmark_artifacts/scope_enrichment/runner_state.json`，日志位于同目录的 `runner.log`。监督器会在每批前检测 Search API；服务受限时进入 `waiting_search` 并定时复检，不会消费期刊队列。单批若出现 90% 以上 `no_candidate_pages`，熔断器会停止后续批次并将该批恢复为待处理。若需在当前批完成后停止，创建 `benchmark_artifacts/scope_enrichment/STOP` 文件即可；进程不会在半批数据上强制退出。

## 盲测轨道

完整系统强制运行 LLM 意图理解、bge-m3 精确向量召回、LightRAG mix、Search API 与 LLM 重排。评测器复用一个常驻 worker，避免每篇论文重复加载图谱与向量。

```bash
python3 -m scripts.evaluate_recent_journals \
  --dataset benchmark_artifacts/recent_journals/dataset.jsonl \
  --output-dir benchmark_artifacts/recent_journals/evaluation
```

默认同时运行：

1. `title_abstract`：标题与摘要，代表用户粘贴完整论文信息的生产用法；
2. `abstract_only`：只提供摘要，降低通过标题直接发现原论文的风险。

Search API 可能检索到论文原页并暴露真实期刊。评测会审计重排器实际收到的全部证据 URL、标题与摘要片段，若出现 DOI、真实期刊名称，或证据标题与论文标题高度相似，就把该样本标记为 `search_leakage`。报告同时给出全部样本和无明显泄漏子集的指标。LLM 自身可能记忆公开论文，仍属于无法完全排除的残余风险。

## 指标

- `Catalog Mapping Check`：目录内抽样的真实期刊能否在当前版本再次通过 ISSN 唯一映射；因为数据集从当前目录反向抽样，这不是独立的外部目录覆盖率；
- `Preliminary Hit@40`：本地综合排序的 Top-40 中是否有真实期刊；
- `Recall-pool Hit@40`：送入 LLM 重排的多通道配额并集中是否有真实期刊。默认保留综合排序 12、向量 8、LightRAG 6、属性图 6、LLM 学科路由 4、Search 期刊提示 4 个席位，去重后循环回填；
- `Hit@1/3/5/10`：真实期刊出现在前 K 位的比例；
- `MRR@10`：真实期刊越靠前得分越高；
- 错误率与端到端延迟的中位数、P90；
- 按轨道、JCR 分区和宽领域分层的结果；
- 去除明显 Search 泄漏后的相同指标。

评测时始终同时选择 JCR-Q1、Q2、Q3、Q4，它们是“或”的候选并集。不能只传真实分区，否则相当于提前泄露标签并人为缩小搜索空间。也不设置 `area`、`scope` 等硬过滤。

## 结果解释边界

该基准初始冻结时，20,087 个 JCR Q1–Q4 期刊实体中只有 151 个具有审核细粒度范围或自动官网范围（0.752%）；后续同一产品口径已增至 420 个（2.091%）。另外，截止 2026-03-31 的候选侧历史论文采集已覆盖 19,593 个实体（97.54%），但它与官方 scope 覆盖是不同指标，不能混合。因此应分开判断：

- 真实期刊不在目录：目录覆盖问题；
- 真实期刊未进 Preliminary Top-40：本地召回或范围数据问题；
- 已召回但未进最终 Top-10：Search/LLM 融合排序问题；
- 严格期刊未命中但推荐主题合理：单一自然标签低估了可接受替代期刊。

若需要评估“推荐是否合理”而不仅是“是否恢复历史期刊”，应另抽样进行盲评，让领域评审对 Top-5 的主题适配与稿件类型适配分级，再报告 nDCG 和评审一致性。
