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

### 今后的 formal acquisition evidence

普通构建仍只能作为诊断数据。今后若要让一个**新采集**的 500 篇数据集进入 `formal_500_full_denominator`，必须从第一次 HTTP 尝试前启用 `--require-complete-acquisition-evidence`，并使用全新的私有 cache、request ledger、high-water/global usage anchor、稳定 budget ID 和固定全局 registry。先运行零网络计划并取得针对该次采集的明确授权；当前没有针对新 500 篇 Crossref 采集或其后 Search/LLM live evaluation 的授权。

```bash
FORMAL_BUILD=benchmark_artifacts/recent_journals_FORMAL_NEW
ACQUISITION_STATE=benchmark_artifacts/.recent_journals_FORMAL_NEW.acquisition-state
ACQUISITION_PLAN=benchmark_artifacts/recent_journals_FORMAL_NEW.acquisition-plan.json
CROSSREF_BUDGET_ID='<new stable non-secret budget ID>'
CROSSREF_HTTP_BUDGET='<explicitly authorized positive integer>'

FORMAL_BUILD_ARGS=(
  --from-date 2026-01-01
  --until-date 2026-06-30
  --sample-size 500
  --mailto your-email@example.com
  --output-dir "$FORMAL_BUILD"
  --cache-dir "$ACQUISITION_STATE/crossref_cache"
  --request-ledger "$ACQUISITION_STATE/request_ledger.jsonl"
  --request-budget-id "$CROSSREF_BUDGET_ID"
  --request-budget-registry-dir benchmark_artifacts/.crossref_request_budget_registry
  --max-network-requests "$CROSSREF_HTTP_BUDGET"
  --require-complete-acquisition-evidence
)

test ! -e "$FORMAL_BUILD"
test ! -e "$ACQUISITION_PLAN"
(umask 077; set -o noclobber; \
  python3 -m scripts.build_recent_journal_benchmark \
    "${FORMAL_BUILD_ARGS[@]}" --plan-only > "$ACQUISITION_PLAN")
```

计划审核必须覆盖完整分母、缓存命中、最坏 HTTP 尝试数、USD 成本和失败保留策略。只有新授权与计划完全一致时，才可从同一参数中移除 `--plan-only`；不得把这里的占位符当作授权，也不得复用旧 ledger/cache 来补造 acquisition-time evidence。

该模式会拒绝 redirect，并在 socket 前消费持久 attempt reservation；固定官方 Crossref request descriptor（base URL、path、query）会重新生成 URL SHA-256。每个入选样本必须从已绑定 reservation 的原始 response leaf 按 item 位置和 canonical hash 重放。最终新目录以相对路径保存 `provenance.jsonl`、cache evidence tree、仅实际使用的 `raw_cache/` leaves，以及 request-ledger/high-water/global-usage 的只读前缀快照和预算 binding。发布前任何源文件漂移都会 fail closed，已有输出不会被覆盖。

这些材料只支持“在操作者纪律下，可本地重放入选行的 provenance 及其实际使用的 Crossref 成功响应”；它们不保证未入选响应或失败响应的完整性，也不是 Crossref 的密码学签名、远端真实性证明或人工身份认证。本地 mode、哈希、追加世代、high-water/global anchor 和回滚检测的威胁模型，假定运行用户的证据、账本、anchor、验证代码与时钟不会被联合重写。它们可以 fail-closed 地发现普通断尾、回退或事后漂移，但**无法防御拥有同等或更高文件权限的管理员**同时改写这些文件、代码或时钟。要提升这一保证，必须把签名或 WORM anchor 放在独立权限域；当前仓库没有这种外部信任根。尤其不能追认历史产物：现有 500 篇数据和 2026-07 Crossref 300 篇数据都在这套 pre-attempt evidence 协议启用前采集，不得事后创建 binding/high-water 来把它们改称新协议 `complete`。2026-07 的结果只能称为“经审计的 post-access namespace-repaired future evaluation”，不能称为 pristine single-pass sealed test，也不能代替 500 篇 formal live evaluation。

仓库收尾的 aggregate-only validator 只验证上述冻结 aggregate 文件的固定
路径、已知 SHA-256、字节数和 `0444` 模式；它不会执行 live formal-500，
不会启动人工标注，也不会请求 live
Crossref/Search/LLM/embedding provider workflow。离线 guard 只能证明被守护的
Python 进程中未观察到 non-loopback attempt；loopback、AF_UNIX 和原生子进程
不在该计数内。其 full-suite 中涉及 500 条记录的回归使用临时合成数据、
cache 和 dry-run，不能改称一次新的正式评测。

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

以下命令只是今后合格新数据的协议模板，不能直接指向现有 legacy 500 数据。截至本次收口，用户**尚未授权** `formal_500_full_denominator` 的实时 Search/LLM/API 评测；既有 Crossref、embedding 或 sealed-label 授权均不扩展为这项授权，因此当前不得创建 formal grant 或执行 live 命令。

下面流程使用 Bash 数组保证 dry-run 与 live 的数据、方法、缓存种子和输出路径完全一致。正式输出目录必须从未存在；计划 JSON 必须写入另一个新的私有 sibling 文件，不得写入输出目录或覆盖旧计划。

```bash
FORMAL_DATASET_ROOT=benchmark_artifacts/recent_journals_FORMAL_NEW
FORMAL_OUTPUT=benchmark_artifacts/recent_journals/evaluation_FORMAL_NEW
FORMAL_BASE_ARGS=(
  --dataset "$FORMAL_DATASET_ROOT/dataset.jsonl"
  --builder-manifest "$FORMAL_DATASET_ROOT/manifest.json"
  --output-dir "$FORMAL_OUTPUT"
  --api-config llmapi.json
  --evaluation-mode formal_500_full_denominator
  --skip-explanations
  --api-cache-seed-dir data/.query_api_cache
  --query-embedding-cache-seed data/.query_embedding_cache.json.gz
  --lightrag-embedding-cache-seed data/.embedding_cache.json.gz
)
```

首先执行授权前 discovery dry-run。它只用于审核完整 500 篇、1,000 个 case-track、缓存覆盖、Search 配额、调用估算和成本缺口；这一次输出的 digest **不能**用于 live，因为它尚未绑定授权控制。

```bash
DISCOVERY_PLAN=benchmark_artifacts/recent_journals/formal500.discovery-plan.NEW.json
test ! -e "$DISCOVERY_PLAN"
(umask 077; set -o noclobber; \
  python3 -m scripts.evaluate_recent_journals \
    "${FORMAL_BASE_ARGS[@]}" --dry-run > "$DISCOVERY_PLAN")
```

`--dry-run` 严格为只读预检：不创建评测输出目录、worker、客户端或授权账本，`network_calls_made` 为 0，`live_clients_instantiated` 为 false。它报告精确的数据/选择/binding/seed hashes，但不会在 LLM 计划尚未生成时伪造逐条缓存命中。调用估算明确不是完成上界；只有持久外部调用账本是硬上限。

只有在人工审阅 discovery 计划后，才可请求一次单独的 live 授权。授权必须明确给出：非秘密审计编号、全 provider HTTP 尝试硬预算、单次尝试保守 USD 上限和授权总 USD 上限。下列变量必须替换为该次明确授权的值；不得自行推断、复用其他授权或把凭据放入 `AUTH_REF`。

```bash
AUTH_REF='<approved non-secret audit ID>'
CALL_BUDGET='<approved positive integer>'
ATTEMPT_USD='<approved finite non-negative decimal>'
MAX_USD='<approved finite non-negative decimal>'
AUTHORIZED_ARGS=(
  "${FORMAL_BASE_ARGS[@]}"
  --authorization-reference "$AUTH_REF"
  --external-call-budget "$CALL_BUDGET"
  --external-attempt-cost-ceiling-usd "$ATTEMPT_USD"
  --authorized-max-cost-usd "$MAX_USD"
)

REVIEWED_PLAN=benchmark_artifacts/recent_journals/formal500.reviewed-plan.NEW.json
test ! -e "$REVIEWED_PLAN"
(umask 077; set -o noclobber; \
  python3 -m scripts.evaluate_recent_journals \
    "${AUTHORIZED_ARGS[@]}" --dry-run > "$REVIEWED_PLAN")
```

人工审阅该 JSON 时至少核对：`evaluation_mode=formal_500_full_denominator`、`case_count=500`、`case_track_count=1000`、两个默认 track 顺序、`output.exists=false`、数据/构建器/acquisition evidence/代码/API/图/向量/LightRAG/三类缓存 hashes、共享配额、预算警告和 `maximum_estimated_cost_usd`。此时尚未提供 digest 和不可变授权文件，`live_control_ready=false`，缺失项包含 `--reviewed-plan-digest` 与 `--authorization-grant` 是预期状态。

审阅者确认整份计划并给出与其完全一致的新授权后，提取 digest，并在 ignored 私有目录创建一次性 grant。grant 必须是当前用户拥有的真实 regular file、mode `0444`、无 symlink、无重复或额外 JSON 字段；它精确绑定授权编号 hash、reviewed digest、输出目录 identity、评测模式、HTTP 硬预算和两项 USD 上限。它是在上述本地权限假设下的不可变审计哨兵，不是数字签名、人工身份的密码学证明，也不能阻止同权限管理员联合重写 grant 与其他本地 anchor。

```bash
REVIEWED_PLAN_DIGEST="$(python3 -c \
  'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["reviewed_plan_digest"])' \
  "$REVIEWED_PLAN")"

AUTHORIZATION_GRANT=benchmark_artifacts/recent_journals/formal500.authorization-grant.NEW.json
test ! -e "$AUTHORIZATION_GRANT"
(umask 077; set -o noclobber; \
  python3 -c '
import hashlib, json, sys
from decimal import Decimal
from pathlib import Path

def decimal_text(raw):
    value = Decimal(raw).normalize()
    return "0" if not value else format(value, "f")

reference, digest, output, budget_raw, attempt_raw, maximum_raw = sys.argv[1:]
budget = int(budget_raw)
attempt = Decimal(attempt_raw)
payload = {
    "schema_version": "1",
    "status": "explicit_external_api_authorization",
    "authorization_reference_sha256": hashlib.sha256(reference.encode()).hexdigest(),
    "reviewed_plan_digest": digest,
    "output_identity_sha256": hashlib.sha256(str(Path(output).resolve()).encode()).hexdigest(),
    "evaluation_mode": "formal_500_full_denominator",
    "external_call_budget": budget,
    "external_attempt_cost_ceiling_usd": decimal_text(attempt_raw),
    "authorized_max_cost_usd": decimal_text(maximum_raw),
    "maximum_estimated_cost_usd": decimal_text(str(attempt * budget)),
}
print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
' "$AUTH_REF" "$REVIEWED_PLAN_DIGEST" "$FORMAL_OUTPUT" \
    "$CALL_BUDGET" "$ATTEMPT_USD" "$MAX_USD" > "$AUTHORIZATION_GRANT")
chmod 0444 "$AUTHORIZATION_GRANT"

GRANTED_ARGS=(
  "${AUTHORIZED_ARGS[@]}"
  --authorization-grant "$AUTHORIZATION_GRANT"
)

VERIFIED_PLAN=benchmark_artifacts/recent_journals/formal500.verified-plan.NEW.json
test ! -e "$VERIFIED_PLAN"
(umask 077; set -o noclobber; \
  python3 -m scripts.evaluate_recent_journals \
    "${GRANTED_ARGS[@]}" \
    --reviewed-plan-digest "$REVIEWED_PLAN_DIGEST" \
    --dry-run > "$VERIFIED_PLAN")
```

verified dry-run 必须输出同一 digest、`live_control_ready=true` 且缺失项为空；否则不得执行 live。grant 的原始授权编号不会写入 run 产物；run 只保存其 hash 和 grant binding 元数据，grant 自身仍属于私有审计材料。

获得 live 授权不代表可以修改方法或计划。执行时仅从上一条 verified 命令中移除 `--dry-run` 和输出重定向：

```bash
python3 -m scripts.evaluate_recent_journals \
  "${GRANTED_ARGS[@]}" \
  --reviewed-plan-digest "$REVIEWED_PLAN_DIGEST"
```

每次外部传输都在打开 socket 前向全局私有账本 `fsync` 一条 reservation；重试、并发和 worker 重启不能恢复已消耗的预算。新账本同时要求同 inode 的 `.highwater.jsonl` 镜像和 mode-`0400` 的 `.binding.json` 身份绑定：单边截断、回滚、替换或写入中断都会在下次传输前 fail closed。没有这两个 sidecar 的旧 schema-v1 单文件账本不会被追溯绑定或自动恢复；继续 live 需要新授权和新账本。本地三文件方案不是密码学见证：拥有同等写权限的主体可在保留 inode 的同时把 ledger 与 high-water 协调回滚到同一旧前缀，且无需修改 binding；防御该威胁必须使用独立管理的 WORM/追加式存储或签名远程审计。

Tavily 还有独立的跨运行共享配额状态：primary/backup 两份状态必须同时有效且 revision 一致，configured keyset、容量、`used` 和 revision 都被绑定；`used`/revision 在 resume 与 closeout 链上只能单调增加。单副本 degraded、不可读、keyset 漂移或 usage 回退都会令 live fail closed。run manifest 记录净化后的 `shared_external_quota_initial`，每个 closeout 记录 `shared_external_quota_final`，绝不记录 key 明文。

除 Tavily 的共享配额外，所有运行时写入都隔离在本次输出中。`source_evidence/` 和 `runtime_cache/` 都是 mode `0700` 的敏感私有目录，但语义不同：前者保存 mode `0400` 的不可变审计文件，后者保存 mode `0600` 的可变运行文件。正式输出目录必须位于 Git ignored 路径或仓库外；`.building-*`、中断 segment 和失败 closeout 也必须按同一敏感级别保留，不得提交或公开打包。输出目录采用追加世代，不存在可覆盖的 formal `raw.jsonl` 或 `summary.json`：

- `run_manifest.json`：只读运行契约；
- `source_evidence/`：数据集、builder manifest、acquisition evidence 文件与 authorization grant 的 content-addressed 只读克隆；可能含摘要、标签和审计元数据，不得公开；
- `runtime_cache/`：从三类冻结 seed、图/向量与 LightRAG workspace 创建的私有 run-local 克隆；`api_config.snapshot.json` 含凭据，provider cache 可能含查询、响应和论文文本，绝不能提交或公开；
- `raw_segments/generation-NNNNNN.jsonl`：逐条 `fsync` 的私有原始结果；
- `summary.generation-NNNNNN.json` 与 `.md`：版本化汇总；
- `closeout.generation-NNNNNN.json`：该世代的退出码、outcomes、segment/report hashes 和账本状态。

退出码是评测状态的一部分，不能只看汇总中的 formal 布尔值：

| 退出码 | 含义 |
| ---: | --- |
| `0` | 无 fatal/ledger closeout 错误，且全部 1,000 个 case-track 均为 `ok` |
| `3` | 不存在 missing，但至少一条 `error`、fatal error 或账本 closeout 错误 |
| `4` | 至少一个 case-track 仍为 missing；该状态优先于 error |
| `130` | 收到 `KeyboardInterrupt`，已保留不可变部分 segment |

CLI/预检 `SystemExit` 通常返回 `1`，`argparse` 用法错误返回 `2`；强制杀死进程可能没有 closeout，必须保留 segment 并使用同一计划做只读 resume 预检。首次 registry claim 前会再次比对 live source files；claim 后通过 shadow clone 写入 `source_evidence/`，克隆期间漂移同样 fail closed。此后每次 resume/closeout 都校验 source evidence 和 runtime cache identity，不能用改过的数据、grant、manifest 或缓存继续。`evaluation_mode=formal_500_full_denominator`、`formal_full_denominator=true` 和 formal `claim_status` 在失败 closeout 中也会出现，只表示选择了协议，**不表示已完成**。只有最新 closeout 与 summary 同时满足 `exit_code=0`、`interrupted=false`、`fatal_error=null`、`ok=1000`、`error=0`、`missing=0`，且 manifest/dataset/builder/acquisition evidence/digest/ledger/quota hashes 一致时，才能声称正式运行完成。

当前 formal `--resume` 只能追加 missing 键；formal 模式禁止 `--retry-errors`，因此不得把仍含 error 的 resume 输出改写成成功。恢复前先对同一参数添加 `--resume --dry-run --reviewed-plan-digest "$REVIEWED_PLAN_DIGEST"` 审核 pending/outcomes，再只移除 `--dry-run`。

`scripts/merge_recent_journal_evaluation.py` 仅保留给历史 `shard-*/raw.jsonl` 产物生成 **legacy diagnostic** 汇总。它会拒绝 `run_manifest.json`、`raw_segments/` 和版本化 closeout，输出始终标记 `formal_full_denominator=false`，不得用于正式运行或修改正式结论。

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
