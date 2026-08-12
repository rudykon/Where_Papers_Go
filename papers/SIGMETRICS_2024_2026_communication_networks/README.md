# SIGMETRICS 2024–2026：通信网络论文集

本目录为筹备 SIGMETRICS 2027 而整理。截至 2026-07-31，`2027` 届尚未举行，因此“最近三年”按已公开的 **SIGMETRICS 2024、2025、2026** 三届处理。

共下载 **20 篇**可公开获取的 PDF：

- **17 篇**直接涉及通信网络：网络测量、卫星/LEO、O-RAN、端到端时限调度、流量生成与分类、传输协议、DNS、网络切片、网络优化等；
- **3 篇**为紧密相关的性能建模与资源调度参考（`09`、`10`、`17`），对 deadline-constrained allocation 的建模和基线设计尤其有帮助。

## 目录与命名

20 篇 PDF 均集中放在 `论文PDF/` 下，命名规则为：

`序号_年份_SIGMETRICS年份_中文题名_第一作者等.pdf`

例如 `04_2024_SIGMETRICS2024_端到端截止期约束下多跳网络中的近最优分组调度_Tsanikidis等.pdf`。

`sources.tsv` 是完整清单，含会议年份、序号、中文题名、第一作者、主题类别、论文原题和公开 PDF 来源。`CHECKSUMS.sha256` 可用于校验文件完整性。

## 与 FPTR 最相关的优先阅读

1. `论文PDF/04_2024_SIGMETRICS2024_端到端截止期约束下多跳网络中的近最优分组调度_Tsanikidis等.pdf`：端到端 deadline 约束下的多跳分组调度。
2. `论文PDF/06_2024_SIGMETRICS2024_虚拟化O-RAN平台中的公平资源分配_Aslan等.pdf`：虚拟化 O-RAN 的公平资源分配。
3. `论文PDF/12_2025_SIGMETRICS2025_Bandit反馈下的对抗性网络优化：最大化非平稳多跳网络效用_Dai等.pdf`：非平稳多跳网络的在线效用优化。
4. `论文PDF/15_2025_SIGMETRICS2025_网络中的学习增强型去中心化在线凸优化_Li等.pdf`：网络中的学习增强分布式在线凸优化。
5. `论文PDF/20_2026_SIGMETRICS2026_动态SLA感知网络切片监测_Saha等.pdf`：SLA 感知的网络切片监测。

## 选取与版权说明

论文以 SIGMETRICS 官方/DBLP 会议目录为准，并从 arXiv 或 TU Delft Repository 下载作者公开版本或公开最终版本；未绕过 ACM 等受限访问页面。预印本的标题或版本可能较会议版本略有更新，`sources.tsv` 保留了 SIGMETRICS 会议条目标题。

会议目录：

- https://dblp.org/db/conf/sigmetrics/sigmetrics2024.html
- https://dblp.org/db/conf/sigmetrics/sigmetrics2025.html
- https://dblp.org/db/conf/sigmetrics/sigmetrics2026.html
