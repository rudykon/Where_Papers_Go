# Aims & Scope Enrichment

`where_paper_go.enrichment` enriches the normalized CSV files in `data/` with official/publisher/conference-page scope information.

It does not overwrite the existing derived `收稿方向` by default. It writes these columns:

- `收稿方向_官网摘取`
- `收稿方向_来源URL`
- `收稿方向_证据`
- `收稿方向_置信度`
- `收稿方向_状态`
- `收稿方向_更新时间`

`收稿方向_状态=ok` only means that the automatic extraction pipeline accepted the candidate. It is not a human-review status. Candidates marked `superseded` are retained for audit history but are excluded from the official-scope candidate view (for example, a workshop, industry track, short-paper track, or stale historical CFP). Approved fine-grained scopes live separately in `data/curated_venue_scopes.tsv`; the recommender uses that reviewed overlay by default and keeps these automatic columns opt-in.

## API Config

Copy `api.example.json` to `api.json` and fill in your model endpoint:

```json
{
  "llm": {
    "provider": "openai_compatible",
    "base_url": "https://api.example.com/v1",
    "api_key": "YOUR_LLM_API_KEY",
    "model": "your-model-name",
    "temperature": 0,
    "timeout": 60,
    "json_mode": false
  },
  "embedding": {
    "provider": "openai_compatible",
    "base_url": "https://api.example.com/v1",
    "model": "your-embedding-model",
    "dimensions": 1024,
    "send_dimensions": false,
    "batch_size": 64,
    "timeout": 60,
    "max_chars": 8000,
    "max_retries": 2
  },
  "search": {
    "provider": "tavily",
    "endpoint": "https://api.tavily.com/search",
    "api_key": "YOUR_TAVILY_API_KEY",
    "search_depth": "advanced",
    "topic": "general",
    "include_answer": false,
    "include_raw_content": false,
    "max_results": 8
  }
}
```

主题检索要求 `llm`、`embedding` 和 `search` 三节同时存在。支持的搜索提供方为
`duckduckgo`、`brave`、`bing`、`serpapi`、`tavily`。Tavily 使用
`Authorization: Bearer <api_key>` 请求头；`search_depth=advanced` 更偏向相关性，
但会增加请求延迟和额度消耗。`max_results` 会被单次查询的 `--api-search-results`
限制在 1–20 范围内。若 `embedding` 与 `llm` 使用同一兼容网关，embedding 可省略
`api_key`，程序会继承 `llm.api_key`。`direct_fallback=true` 会在本地代理连接失败
时自动用直连重试；如必须使用指定代理，可在 `search.proxy` 中填写代理地址。

## Typical Runs

Prepare empty output columns without network/API calls:

```bash
python3 -m where_paper_go.enrichment --prepare-columns
```

Dry-run query construction:

```bash
python3 -m where_paper_go.enrichment --dataset ccf --limit 5 --dry-run
```

Run a small batch:

```bash
python3 -m where_paper_go.enrichment --api-config api.json --dataset ccf --limit 20 --sleep 1
```

Run journals only:

```bash
python3 -m where_paper_go.enrichment --api-config api.json --record-type journal --limit 100 --sleep 1
```

For journals, the script defaults to a strict source policy: extracted results
are accepted only when the selected source URL is on a known publisher/platform
domain allow-list such as Nature, Wiley, Springer, Elsevier, IEEE, ACM, OUP, and
similar domains. Third-party metric/index pages are rejected even if they contain
scope-like text. To allow these domains during exploration:

```bash
python3 -m where_paper_go.enrichment --api-config api.json --dataset jcr --limit 20 --allow-untrusted-domains
```

Reprocess existing enriched rows:

```bash
python3 -m where_paper_go.enrichment --api-config api.json --dataset jcr --overwrite --limit 100
```

`--replace-scope` is intentionally disabled. Automatic extraction must not overwrite the full-coverage classification scope or write directly into the reviewed overlay:

```bash
python3 -m where_paper_go.enrichment --api-config api.json --dataset ccf --limit 20 --replace-scope
```

The command above now exits with an error. The review flow is:

```text
automatic candidate (`ok`)
  → verify venue identity, main track / journal scope, year and source evidence
  → add an exact approved row to data/curated_venue_scopes.tsv (including target_status and optional secondary_source_urls)
  → run the recommender tests
```

## Caching

Search results, fetched page text, and LLM outputs are cached under:

```text
data/.aims_scope_cache/
```

The script creates a timestamped `.bak-YYYYmmddHHMMSS` backup before writing a CSV.

## Smoke Test Results

The configured LLM endpoint was tested successfully in the existing smoke tests.
The Tavily request format is covered by unit tests (Bearer authentication and
`advanced` search payload); a live Tavily call still requires the deployment
host to have outbound HTTPS access.

- CCF conference sample `PPoPP`: found `https://ppopp26.sigplan.org/`, extracted high-confidence scope.
- JCR journal sample `NATURE REVIEWS MOLECULAR CELL BIOLOGY`: found `https://www.nature.com/nrm/aims`, extracted high-confidence scope.
- JCR journal sample `CA-A CANCER JOURNAL FOR CLINICIANS`: search without a dedicated search API found only third-party pages, so strict mode rejected the result instead of writing non-official scope.
