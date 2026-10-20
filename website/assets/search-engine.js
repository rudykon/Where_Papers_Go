/**
 * Local, inspectable retrieval. All three signals use catalogue text:
 * BM25, sparse TF-IDF cosine, and reviewed keyword co-occurrence.
 * These are not neural embeddings, an LLM, LightRAG or live web evidence.
 */
const STOPWORDS = new Set((
  "a an and are as at be been being by can could did do does each for from had has have how i if in into is it its may more most of on or our paper papers research results should show such than that the their them these they this those through to towards using use used uses via was we were what when where which while who will with within would you your " +
  "的 了 和 与 及 或 在 对 是 为 为了 通过 基于 使用 提出 研究 方法 论文 我们 一种 一个 以及 进行 相关 面向 关于 领域"
).split(/\s+/));
const STEMS = new Map(Object.entries({
  computing: "comput", computation: "comput", computational: "comput", computer: "comput", computers: "comput",
  learning: "learn", learned: "learn", learns: "learn",
  retrieval: "retriev", retrieving: "retriev", retrieved: "retriev",
  recommendation: "recommend", recommendations: "recommend", recommender: "recommend", recommenders: "recommend",
  processing: "process", processed: "process", processes: "process",
  mining: "mine", classification: "classify", optimization: "optim",
  modelling: "model", modeling: "model", models: "model",
}));
const ALIASES = [
  { pattern: /\bnlp\b|\bnatural language processing\b|自然语言处理/iu, label: "NLP / 自然语言处理", terms: "nlp natural language processing 自然语言处理" },
  { pattern: /\bllms?\b|\blarge language models?\b|大语言模型|大型语言模型|大模型/iu, label: "LLM / 大语言模型", terms: "llm large language model 大语言模型 大模型" },
  { pattern: /\bgnns?\b|\bgraph neural networks?\b|图神经网络/iu, label: "GNN / 图神经网络", terms: "gnn graph neural network 图神经网络" },
  { pattern: /\bcv\b|\bcomputer vision\b|计算机视觉/iu, label: "CV / 计算机视觉", terms: "cv computer vision visual image 计算机视觉" },
  { pattern: /\bml\b|\bmachine learning\b|机器学习/iu, label: "ML / 机器学习", terms: "machine learning 机器学习" },
  { pattern: /\bdeep learning\b|深度学习/iu, label: "深度学习 / Deep learning", terms: "deep learning 深度学习" },
  { pattern: /\breinforcement learning\b|强化学习/iu, label: "强化学习 / Reinforcement learning", terms: "reinforcement learning 强化学习" },
  { pattern: /\binformation retrieval\b|信息检索/iu, label: "信息检索 / Information retrieval", terms: "information retrieval 信息检索" },
  { pattern: /\brecommend(?:ation|er)? systems?\b|推荐系统/iu, label: "推荐系统 / Recommender systems", terms: "recommendation recommender personalization 推荐系统" },
  { pattern: /\bdatabases?\b|\bdbms\b|数据库/iu, label: "数据库 / Database", terms: "database dbms 数据库" },
  { pattern: /\bcybersecurity\b|\bcomputer security\b|网络安全|信息安全/iu, label: "信息安全 / Security", terms: "cybersecurity security privacy 网络安全 信息安全" },
  { pattern: /\bdistributed systems?\b|分布式系统/iu, label: "分布式系统 / Distributed systems", terms: "distributed systems 分布式系统" },
];
const segmenter = typeof Intl.Segmenter === "function"
  ? new Intl.Segmenter("zh-CN", { granularity: "word" }) : null;
const pause = () => new Promise((resolve) => setTimeout(resolve, 0));
const normalize = (text) => String(text ?? "").normalize("NFKC").toLowerCase().trim();
const clamp = (value) => Math.max(0, Math.min(1, value));
const str = (value) => typeof value === "string" ? value : "";
const strings = (value) => Array.isArray(value) ? [...new Set(value.filter((item) => typeof item === "string" && item.trim()))] : [];

function stem(token) {
  if (STEMS.has(token)) return STEMS.get(token);
  if (/^[a-z]{5,}ies$/.test(token)) return `${token.slice(0, -3)}y`;
  if (/^[a-z]{4,}s$/.test(token) && !/(?:ss|us|is)$/.test(token)) return token.slice(0, -1);
  return token;
}

/** Limited bigrams supplement Chinese word segmentation; no n-gram explosion. */
export function tokenize(text) {
  const normalized = normalize(text);
  const tokens = [];
  const pieces = segmenter ? segmenter.segment(normalized) : normalized.matchAll(/[\p{L}\p{N}]+/gu);
  for (const part of pieces) {
    if (segmenter && !part.isWordLike) continue;
    const raw = segmenter ? part.segment : part[0];
    const token = stem(raw);
    if (token.length > 1 && !STOPWORDS.has(raw) && !STOPWORDS.has(token)) tokens.push(token);
  }
  let bigrams = 0;
  for (const match of normalized.matchAll(/[\u3400-\u9fff]{3,}/gu)) {
    for (let index = 0; index < match[0].length - 1 && bigrams < 96; index += 1) {
      const token = match[0].slice(index, index + 2);
      if (!STOPWORDS.has(token)) tokens.push(token);
      bigrams += 1;
    }
    if (bigrams >= 96) break;
  }
  return tokens;
}

function safeUrl(value) {
  if (!value) return "";
  try {
    const parsed = new URL(value);
    return /^https?:$/.test(parsed.protocol) && !parsed.username && !parsed.password ? parsed.href : "";
  } catch {
    return "";
  }
}

function cleanVenue(value, index) {
  if (!value || typeof value !== "object" || !str(value.name).trim()) {
    throw new Error(`目录第 ${index + 1} 条记录缺少场所名称。`);
  }
  return {
    id: str(value.id) || `venue:${index}`,
    name: str(value.name), abbreviation: str(value.abbreviation),
    record_type: ["journal", "conference"].includes(value.record_type) ? value.record_type : str(value.record_type),
    ranks: strings(value.ranks), areas: strings(value.areas), scope: str(value.scope),
    keywords: strings(value.keywords), exclusions: str(value.exclusions),
    url: safeUrl(value.url), scope_url: safeUrl(value.scope_url),
    reviewed: value.reviewed === true, source_ids: strings(value.source_ids),
    scope_year: typeof value.scope_year === "number" ? value.scope_year : str(value.scope_year),
    article_types: strings(value.article_types), submission_mode: str(value.submission_mode),
  };
}

function cancelled(check) {
  if (check?.()) {
    const error = new Error("本次检索已被新的请求替代。");
    error.name = "AbortError";
    throw error;
  }
}

function queryTerms(query) {
  const normalized = normalize(query);
  const direct = new Set(tokenize(query));
  const weighted = new Map([...direct].map((term) => [term, 1]));
  const aliases = [];
  for (const alias of ALIASES) {
    if (!alias.pattern.test(normalized)) continue;
    aliases.push(alias.label);
    for (const term of tokenize(alias.terms)) {
      if (!weighted.has(term)) weighted.set(term, 0.45);
    }
  }
  return { direct, weighted, aliases };
}

function containsFilter(venue, options) {
  if (options.recordType && options.recordType !== "all" && venue.record_type !== options.recordType) return false;
  if (options.reviewedOnly && !venue.reviewed) return false;
  const targets = Array.isArray(options.targets) ? options.targets : [];
  if (targets.length && !venue.ranks.some((rank) => targets.includes(rank))) return false;
  if (options.area && options.area !== "all" && !venue.areas.includes(options.area)) return false;
  return true;
}

/**
 * Build once in a worker. Postings use typed arrays, and indexing yields so
 * progress / newer messages remain responsive even on mobile devices.
 */
export async function createSearchEngine(catalog, { onProgress = () => {}, yieldControl = pause } = {}) {
  if (!catalog || !Array.isArray(catalog.venues)) throw new Error("目录数据格式无效，缺少 venues 列表。");
  const started = performance.now();
  const venues = [];
  const ids = new Map();
  const rawPostings = new Map();
  const graphCounts = new Map();
  const graphFrequencies = new Map();
  const lengths = new Float32Array(catalog.venues.length);
  const norms = new Float64Array(catalog.venues.length);
  const areaSet = new Set();
  const targetSet = new Set();
  let totalLength = 0;
  let reviewedCount = 0;

  for (let index = 0; index < catalog.venues.length; index += 1) {
    const venue = cleanVenue(catalog.venues[index], index);
    if (ids.has(venue.id)) throw new Error(`目录含重复标识：${venue.id}`);
    ids.set(venue.id, index);
    venues.push(venue);
    venue.areas.forEach((value) => areaSet.add(value));
    venue.ranks.forEach((value) => targetSet.add(value));
    const counts = new Map();
    for (const [text, weight] of [
      [venue.name, 4], [venue.abbreviation, 6], [venue.keywords.join(" "), 2.5],
      [venue.areas.join(" "), 1.5], [venue.scope, 1],
    ]) {
      for (const term of tokenize(text)) counts.set(term, (counts.get(term) || 0) + weight);
    }
    let length = 0;
    for (const [term, count] of counts) {
      if (!rawPostings.has(term)) rawPostings.set(term, []);
      rawPostings.get(term).push(index, count);
      length += count;
    }
    lengths[index] = length;
    totalLength += length;

    if (venue.reviewed) {
      reviewedCount += 1;
      // Only reviewed metadata supplies graph relationships. A bounded number
      // of terms and neighbours keeps construction and expansion predictable.
      const terms = [...new Set(tokenize(venue.keywords.join(" ")))].slice(0, 16);
      for (const term of terms) {
        graphFrequencies.set(term, (graphFrequencies.get(term) || 0) + 1);
        if (!graphCounts.has(term)) graphCounts.set(term, new Map());
        const neighbours = graphCounts.get(term);
        for (const other of terms) {
          if (other !== term) neighbours.set(other, (neighbours.get(other) || 0) + 1);
        }
      }
    }
    if ((index + 1) % 96 === 0) {
      onProgress({ stage: "index", progress: (index + 1) / Math.max(catalog.venues.length, 1) * 0.8, message: `正在建立本地索引：${index + 1} / ${catalog.venues.length}` });
      await yieldControl();
    }
  }

  const postings = new Map();
  let termCount = 0;
  for (const [term, entries] of rawPostings) {
    const count = entries.length / 2;
    const posting = {
      ids: new Uint32Array(count), tf: new Float32Array(count),
      idf: Math.log(1 + (venues.length - count + 0.5) / (count + 0.5)),
      vectorIdf: Math.log(1 + venues.length / (1 + count)),
    };
    for (let index = 0; index < count; index += 1) {
      const id = entries[index * 2];
      posting.ids[index] = id;
      posting.tf[index] = entries[index * 2 + 1];
      norms[id] += ((1 + Math.log(posting.tf[index])) * posting.vectorIdf) ** 2;
    }
    postings.set(term, posting);
    rawPostings.delete(term);
    termCount += 1;
    if (termCount % 2048 === 0) await yieldControl();
  }
  for (let index = 0; index < norms.length; index += 1) norms[index] = Math.sqrt(norms[index]);

  const graph = new Map();
  for (const [term, neighbours] of graphCounts) {
    const sorted = [...neighbours].map(([other, count]) => ({
      term: other, weight: count / Math.sqrt(graphFrequencies.get(term) * graphFrequencies.get(other)),
    })).sort((a, b) => b.weight - a.weight).slice(0, 10);
    graph.set(term, sorted);
  }
  graphCounts.clear();
  const averageLength = totalLength / Math.max(venues.length, 1) || 1;
  const stats = {
    ...catalog.stats, total: venues.length, totalVenues: venues.length,
    reviewedCount, indexedTerms: postings.size, graphTerms: graph.size,
    journalCount: venues.filter((venue) => venue.record_type === "journal").length,
    conferenceCount: venues.filter((venue) => venue.record_type === "conference").length,
    indexMs: Math.round(performance.now() - started),
  };
  onProgress({ stage: "index", progress: 1, message: `本地索引已就绪，共 ${venues.length} 个投稿场所。` });

  return {
    stats, sources: catalog.sources || [],
    areas: [...areaSet].sort((a, b) => a.localeCompare(b, "zh-CN")),
    targets: [...targetSet].sort(),
    getDetail(id) { return ids.has(id) ? { ...venues[ids.get(id)] } : null; },
    async search(options = {}, { onProgress: progress = () => {}, isCancelled } = {}) {
      cancelled(isCancelled);
      const query = str(options.query).trim().slice(0, 8000);
      const queryLower = normalize(query);
      const offset = Math.max(0, Math.trunc(Number(options.offset) || 0));
      const limit = Math.max(1, Math.min(Math.max(venues.length, 1), Math.trunc(Number(options.limit) || 20)));
      progress({ stage: "search", progress: 0, message: "正在本地检索词项与审核主题关联…" });
      const allowed = new Uint8Array(venues.length);
      const eligible = [];
      for (let index = 0; index < venues.length; index += 1) {
        if (containsFilter(venues[index], options)) {
          allowed[index] = 1;
          eligible.push(index);
        }
      }
      if (!query) {
        const results = eligible.slice(offset, offset + limit).map((index) => ({
          ...venues[index], score: null, matchedTerms: [], signals: { lexical: 0, vector: 0, graph: 0 },
          explanation: "按当前筛选条件浏览目录；尚未输入主题，不计算匹配度。",
        }));
        progress({ stage: "search", progress: 1, message: `找到 ${eligible.length} 个符合筛选条件的场所。` });
        return { total: eligible.length, results, offset, limit };
      }

      const { direct, weighted, aliases } = queryTerms(query);
      const expansions = new Map();
      // Expand only a few high-information query terms. Common words cannot
      // seed an unbounded co-occurrence traversal.
      const seeds = [...weighted].filter(([term]) => graph.has(term) && postings.has(term))
        .sort((a, b) => postings.get(b[0]).idf * b[1] - postings.get(a[0]).idf * a[1]).slice(0, 8);
      for (const [term, weight] of seeds) {
        for (const neighbour of graph.get(term).slice(0, 4)) {
          if (!weighted.has(neighbour.term) && neighbour.weight >= 0.15) {
            expansions.set(neighbour.term, Math.max(expansions.get(neighbour.term) || 0, neighbour.weight * weight));
          }
        }
      }
      const graphTerms = [...expansions].sort((a, b) => b[1] - a[1]).slice(0, 16);
      const candidates = new Map();
      const candidate = (index) => {
        if (!candidates.has(index)) candidates.set(index, { index, bm25: 0, dot: 0, graph: 0, terms: [], expanded: [] });
        return candidates.get(index);
      };
      // A one-letter or stop-word abbreviation still works as an exact lookup.
      for (const index of eligible) {
        if (normalize(venues[index].name) === queryLower
            || (venues[index].abbreviation && normalize(venues[index].abbreviation) === queryLower)) {
          candidate(index);
        }
      }
      let queryNorm = 0;
      let traversed = 0;
      for (const [term, weight] of weighted) {
        const posting = postings.get(term);
        if (!posting) continue;
        const queryVector = weight * posting.vectorIdf;
        queryNorm += queryVector ** 2;
        for (let index = 0; index < posting.ids.length; index += 1) {
          const document = posting.ids[index];
          if (allowed[document]) {
            const match = candidate(document);
            const tf = posting.tf[index];
            match.bm25 += weight * posting.idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * lengths[document] / averageLength));
            match.dot += queryVector * (1 + Math.log(tf)) * posting.vectorIdf;
            if (match.terms.length < 24) match.terms.push(term);
          }
          traversed += 1;
          if (traversed % 4096 === 0) {
            cancelled(isCancelled);
            await yieldControl();
          }
        }
      }
      queryNorm = Math.sqrt(queryNorm);
      const graphWeight = graphTerms.reduce((sum, [, weight]) => sum + weight, 0) || 1;
      for (const [term, weight] of graphTerms) {
        const posting = postings.get(term);
        if (!posting) continue;
        for (let index = 0; index < posting.ids.length; index += 1) {
          const document = posting.ids[index];
          if (allowed[document]) {
            const match = candidate(document);
            match.graph += weight / graphWeight;
            if (match.expanded.length < 8) match.expanded.push(term);
          }
          traversed += 1;
          if (traversed % 4096 === 0) {
            cancelled(isCancelled);
            await yieldControl();
          }
        }
      }
      cancelled(isCancelled);
      progress({ stage: "search", progress: 0.8, message: "正在融合词项信号并生成匹配解释…" });
      let maxBm25 = 0;
      for (const match of candidates.values()) maxBm25 = Math.max(maxBm25, match.bm25);
      const ranked = [];
      for (const match of candidates.values()) {
        const venue = venues[match.index];
        const exact = normalize(venue.name) === queryLower || (venue.abbreviation && normalize(venue.abbreviation) === queryLower);
        const phrase = queryLower.length >= 3 && (normalize(venue.name).includes(queryLower) || normalize(venue.abbreviation).includes(queryLower));
        const lexical = maxBm25 ? match.bm25 / maxBm25 : 0;
        const vector = queryNorm && norms[match.index] ? clamp(match.dot / (queryNorm * norms[match.index])) : 0;
        const graphSignal = clamp(match.graph);
        // Relative matching signal, not acceptance likelihood or a calibrated
        // cross-query probability. Exact venue-name lookups have precedence.
        const fusion = 0.6 * lexical + 0.3 * vector + 0.1 * graphSignal;
        ranked.push({ ...match, exact, lexical, vector, graphSignal, score: exact ? 100 : Math.min(99, 100 * fusion + (phrase ? 8 : 0)) });
      }
      ranked.sort((a, b) => Number(b.exact) - Number(a.exact) || b.score - a.score
        || venues[a.index].name.localeCompare(venues[b.index].name, "en"));
      cancelled(isCancelled);
      const results = ranked.slice(offset, offset + limit).map((match) => {
        const venue = venues[match.index];
        const matchedTerms = [...match.terms].sort((a, b) => Number(direct.has(b)) - Number(direct.has(a))).slice(0, 12);
        const parts = [];
        if (match.exact) parts.push("名称或简称精确匹配。");
        if (matchedTerms.length) parts.push(`目录词项命中：${matchedTerms.slice(0, 6).join("、")}。`);
        if (aliases.length) parts.push(`查询使用领域别名：${aliases.join("；")}。`);
        if (match.expanded.length) parts.push(`审核关键词共现扩展：${match.expanded.slice(0, 4).join("、")}。`);
        parts.push(venue.reviewed ? "收稿范围来自已审核目录记录。" : "该记录的收稿范围尚未审核，请以官网为准。");
        return {
          ...venue, score: Math.round(match.score * 10) / 10, matchedTerms,
          signals: {
            lexical: Math.round(match.lexical * 1000) / 1000,
            vector: Math.round(match.vector * 1000) / 1000,
            graph: Math.round(match.graphSignal * 1000) / 1000,
          },
          explanation: parts.join(" "),
        };
      });
      progress({ stage: "search", progress: 1, message: `本地检索完成，共 ${ranked.length} 个匹配场所。` });
      return { total: ranked.length, results, offset, limit };
    },
  };
}
