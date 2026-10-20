# GitHub Pages 浏览器版

[打开 Where Papers Go](https://rudykon.github.io/Where_Papers_Go/)。
此版本将公开刊会目录和检索代码作为静态文件部署到 GitHub Pages。无需账号、
API 密钥或 Python 服务；页面加载数据后，由浏览器 Web Worker 执行筛选与检索。

输入的题目、摘要和关键词不上传到服务器。浏览器请求页面资源与公开目录；
打开结果中的来源链接时，会访问对应外部网站。静态版没有在线搜索、实时 CFP
核验或 LLM 推理；结果中的来源链接表示目录保存的证据，不表示本次查询已访问
或重新验证这些网页。

## 数据与检索

构建器读取仓库的四份 CSV，并复用 Python 项目的实体合并与人工审核范围加载逻辑。
当前 45,207 条榜单记录合并为 23,451 个活动投稿实体，其中 157 个实体包含审核范围。
目录覆盖 CCF 2026、TH-CPL 2019、中科院 2025 与 JCR 2025；榜单年份与审核范围年份
分别保留。历史合并实体和不能独立投稿的刊系占位项被排除。

| 数据 | 公开内容 |
| --- | --- |
| 榜单记录 | 名称、简称、会议/期刊类型、等级、学科分类及目录链接 |
| 审核范围 | 收稿范围、中英文关键词、排除方向、文章类型、投稿模式、来源链接与年份 |
| 未审核实体 | 保留榜单及学科事实，范围与审核关键词为空 |

浏览器下载 `assets/catalog.json`，Worker 根据目录建立索引，并在后台完成三个信号的
计算：BM25 词法匹配、基于词项 TF-IDF 向量的余弦相似度、审核关键词关联扩展。
等级、类型、方向和审核范围等约束限制候选集合；多个等级按“或”组合。
前端展示可追溯的目录信息和匹配依据，计算过程不占用页面主线程。

融合权重为 BM25 0.6、TF-IDF 0.3、关键词关联 0.1。BM25 按当前候选的最高值
归一化，名称和简称的精确匹配优先。空查询用于浏览目录，不生成相关度得分。

TF-IDF 向量来自目录中的词项统计，无法提供预训练语言模型的语义理解；
大多数条目仅根据名称和分类检索，关键词扩展来自 157 个审核实体的关键词共现。
中文分词及有限的中英领域别名支持常见研究表达，不提供通用跨语言语义翻译。
相关度仅用于比较同一次查询的候选，不能跨查询比较，也不代表录用概率。
最新征稿范围、期限与投稿要求仍应通过所列官方来源核对。

| 实现 | 检索与证据 |
| --- | --- |
| GitHub Pages 浏览器版 | Worker 本地 BM25、词项 TF-IDF、审核关键词扩展；仓库数据快照 |
| Python 严格在线模式 | LightRAG、模型语义向量、LLM 推理与 Search API；依赖服务和密钥 |

两种实现采用不同算法，排序结果可能不同。浏览器版不会把某个本地计算信号标记为
LightRAG、LLM 或实时搜索，也不修改 Python 严格在线模式的检索约束。

## 本地构建与预览

在仓库根目录运行，Python 3.10 及以上即可，构建不需要安装模型依赖：

```bash
python3 scripts/build_github_pages.py
python3 -m http.server 8080 --bind 127.0.0.1 --directory dist/github-pages
```

浏览器打开 `http://127.0.0.1:8080/`。应通过 HTTP 预览，直接双击 HTML
会受到浏览器对 Worker 和数据请求的限制。

静态源代码位于 `website/`，发布目录位于 `dist/github-pages/`。
构建仅复制 HTML、样式、页面脚本、Worker、检索模块和图标六个显式文件，
并生成安全字段白名单目录及 `.nojekyll`。不会发布 API 配置、缓存、
图谱与模型索引、原始本地路径或未经审核的自动范围文本。
生成目录已由 `.gitignore` 排除。

独立验证入口：

```bash
python3 -m unittest discover -s tests/pages -p 'test_*.py' -v
node tests/pages/search-engine.test.mjs
```

Pages 测试与后端固定验收测试集合分开；修改 Python 主程序仍应运行项目原有验收。

## 发布

仓库的 **Settings → Pages → Build and deployment → Source** 使用
**GitHub Actions**。[Pages workflow](../.github/workflows/pages.yml)
在 `main` 的网页、构建器、目录数据或相关加载逻辑变更时执行测试与构建，
并通过官方 Pages Actions 上传 `dist/github-pages/` 和部署。
也可在 **Actions → GitHub Pages → Run workflow** 手动触发。

只有构建成功的静态产物进入部署；部署结果和网址显示在 Actions 的
`github-pages` 环境中。资料更新后需要重新构建和发布，目录不会在用户查询时
自动更新。
