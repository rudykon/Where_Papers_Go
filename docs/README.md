# 项目文档

新对话先从根目录的 [当前交接状态](../HANDOFF.md) 开始；它记录数据快照、
未提交基线、关键风险和 P0-A～P0-C 的验收条件。

## 架构与产品

- [检索架构](retrieval-architecture.md)：属性图、LightRAG、精确向量、LLM 与 Search API。
- [Web 前端](web-frontend.md)：服务启动、接口和部署建议。
- [收稿范围补全](enrichment.md)：官网范围采集和审核流程。

## 研究协议与结果

- [CCF-A 研究化路线图](ccf-a-research-roadmap.md)：SCOPE-Rank、无泄漏协议、强基线、消融与投稿准入条件。
- [全期刊历史画像语料](historical-profile-corpus.md)：PCL 辅助的 20,087 刊多来源采集、标题-only、多原型画像和分层报告。
- [近期论文期刊恢复评测](recent-journal-benchmark.md)：Crossref 自然标签、泄漏审计与完整流水线指标。
- [近期论文评测结果](recent-journal-benchmark-results.md)：500 篇分层数据集、多通道冒烟评测与历史 20 篇先导对照。
- [性能与离线效果评测](performance-evaluation-2026-08-14.md)：向量召回提速、4,791 篇时间评测和原 500 篇对照。
- [M3 强基线冻结](m3-strong-baselines.md)：11 方法、55 配对统计、延迟/成本与完整负结果。
- [SCOPE-Rank 开发集冻结](scope-rank-results.md)：正式方法、11 消融、78 配对统计、拒答评测与负结果根因。

## 遗留兼容

- [旧 SQLite 索引](legacy-sqlite-index.md)：仅用于回归和迁移对照。
