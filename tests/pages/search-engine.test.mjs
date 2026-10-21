import assert from "node:assert/strict";
import { test } from "node:test";
import { createSearchEngine, tokenize } from "../../website/assets/search-engine.js";

const catalog = {
  sources: [{ id: "test", label: "Test source" }],
  stats: { build: "test" },
  venues: [
    { id: "nlp", name: "Transactions of Natural Language Processing", abbreviation: "TNLP", record_type: "journal", ranks: ["CCF-A", "JCR-Q1"], areas: ["人工智能"], scope: "Natural language processing, large language models and machine translation.", keywords: ["natural language", "language models", "translation"], reviewed: true, url: "https://example.org/nlp", scope_url: "https://example.org/nlp/scope", source_ids: ["test"] },
    { id: "vision", name: "Computer Vision Conference", abbreviation: "CVC", record_type: "conference", ranks: ["CCF-B"], areas: ["人工智能"], scope: "Image understanding, computer vision, visual recognition and deep learning.", keywords: ["computer vision", "image", "recognition"], reviewed: true, url: "https://example.org/vision" },
    { id: "graph", name: "Graph Learning Journal", abbreviation: "GLJ", record_type: "journal", ranks: ["CCF-A"], areas: ["人工智能"], scope: "Graph neural networks and graph representation learning.", keywords: ["graph", "neural", "network"], reviewed: true },
    { id: "chinese", name: "计算机视觉研究", abbreviation: "", record_type: "journal", ranks: ["CAS-1"], areas: ["计算机科学"], scope: "图像识别、计算机视觉、模式识别与深度学习。", keywords: ["图像识别", "计算机视觉"], reviewed: false },
    { id: "database", name: "Database Systems Conference", abbreviation: "DBSC", record_type: "conference", ranks: ["CCF-A"], areas: ["数据库"], scope: "Database management, query optimization and transactions.", keywords: ["database", "transactions"], reviewed: true },
    { id: "graph-related", name: "Network Representation Letters", abbreviation: "NRL", record_type: "journal", ranks: ["JCR-Q2"], areas: ["数学"], scope: "Neural network representations.", keywords: ["neural", "network"], reviewed: true },
    { id: "unsafe", name: "<script>alert(1)</script> Journal", abbreviation: "XSS", record_type: "journal", ranks: ["JCR-Q4"], areas: [], scope: "Security and malicious scripts.", keywords: ["security"], reviewed: false, url: "javascript:alert(1)", scope_url: "https://user:secret@example.org/", source_ids: ["test"], unexpected_secret: "omit" },
  ],
};
const engine = await createSearchEngine(catalog, { yieldControl: async () => {} });

const lookupCatalog = {
  venues: [
    { id: "z", name: "ACM Computing Surveys", abbreviation: "CSUR", publishers: ["Association for Computing Machinery", "ACM"], issns: ["0360-0300", "1557-7341"], record_type: "journal", ranks: ["CCF-A", "JCR-Q1"], areas: ["计算机科学"], reviewed: true, source_ids: ["ccf", "jcr"] },
    { id: "vision", name: "Computer Vision Conference", abbreviation: "CV", publishers: ["IEEE", "Computer Society"], issns: ["1234-567X"], record_type: "conference", ranks: ["CCF-B", "THCPL-A"], areas: ["人工智能"], reviewed: true, source_ids: ["ccf", "th_cpl"] },
    { id: "llm-b", name: "LLM Systems", abbreviation: "LLMS", publishers: ["Zeta Press"], issns: ["2049-3630"], record_type: "journal", ranks: ["CAS-2"], areas: ["人工智能"], reviewed: false, source_ids: ["cas"] },
    { id: "llm-a", name: "LLM Systems", abbreviation: "LLM", publishers: ["Alpha Press"], issns: [], record_type: "journal", ranks: ["CCF-A"], areas: ["计算机科学"], reviewed: true, source_ids: ["ccf"] },
    { id: "hidden", name: "Network Mechanics", abbreviation: "NM", publishers: ["Elsevier"], issns: ["0169-7552"], record_type: "journal", ranks: ["JCR-Q2"], areas: ["通信"], reviewed: false, source_ids: ["jcr"], scope: "Natural language processing and LLM", keywords: ["computer vision", "ACM"], exclusions: "LLM" },
    { id: "identity", name: "ACM", abbreviation: "ACM", publishers: ["ACM", "Academic Press"], issns: ["0000-000X"], record_type: "journal", ranks: ["CCF-A"], areas: ["计算机科学"], reviewed: false, source_ids: ["ccf"] },
  ],
};
const lookupEngine = await createSearchEngine(lookupCatalog, { yieldControl: async () => {} });
const lookup = (options = {}) => lookupEngine.search({ mode: "lookup", ...options });
const resultIds = (found) => found.results.map((result) => result.id);

test("catalog metadata, strict public fields and unsafe URL handling", () => {
  assert.equal(engine.stats.total, 7);
  assert.equal(engine.stats.reviewedCount, 5);
  assert.equal(engine.stats.build, "test");
  assert.ok(engine.targets.includes("CCF-A"));
  assert.ok(engine.areas.includes("人工智能"));
  assert.deepEqual(engine.sources, catalog.sources);
  const unsafe = engine.getDetail("unsafe");
  assert.equal(unsafe.url, "");
  assert.equal(unsafe.scope_url, "");
  assert.equal(unsafe.name, "<script>alert(1)</script> Journal");
  assert.equal(unsafe.unexpected_secret, undefined);
  assert.equal(engine.getDetail("unknown"), null);
});

test("English retrieval has explainable non-probability signals", async () => {
  const found = await engine.search({ query: "graph neural networks" });
  assert.equal(found.results[0].id, "graph");
  assert.ok(found.results[0].matchedTerms.includes("graph"));
  assert.match(found.results[0].explanation, /词项命中/);
  for (const result of found.results) {
    assert.ok(result.score >= 0 && result.score <= 100);
    for (const signal of Object.values(result.signals)) assert.ok(signal >= 0 && signal <= 1);
  }
});

test("Chinese phrases and domain abbreviations bridge English catalogue terms", async () => {
  assert.equal((await engine.search({ query: "自然语言处理" })).results[0].id, "nlp");
  assert.equal((await engine.search({ query: "NLP" })).results[0].id, "nlp");
  assert.equal((await engine.search({ query: "LLM" })).results[0].id, "nlp");
  assert.equal((await engine.search({ query: "GNN" })).results[0].id, "graph");
  const vision = await engine.search({ query: "计算机视觉" });
  assert.ok(vision.results.some((result) => result.id === "vision"));
  assert.ok(vision.results.some((result) => result.id === "chinese"));
  assert.ok(tokenize("图神经网络推荐系统").includes("神经"));
});

test("NFKC-normalized domain aliases produce identical retrieval for fullwidth input", async () => {
  for (const [ascii, fullwidth] of [
    ["NLP", "ＮＬＰ"], ["LLM", "ＬＬＭ"], ["GNN", "ＧＮＮ"], ["CV", "ＣＶ"],
    ["natural language processing", "ｎａｔｕｒａｌ ｌａｎｇｕａｇｅ ｐｒｏｃｅｓｓｉｎｇ"],
  ]) {
    assert.deepEqual(
      await engine.search({ query: ascii }),
      await engine.search({ query: fullwidth }),
    );
  }
});

test("Chinese topic descriptions retain domain matches within a longer sentence", async () => {
  const nlp = await engine.search({
    query: "我们研究自然语言处理，基于大语言模型构建机器翻译方法，希望寻找适合投稿的期刊。",
  });
  assert.equal(nlp.results[0].id, "nlp");
  assert.match(nlp.results[0].explanation, /NLP/);
  const graph = await engine.search({
    query: "基于图神经网络开展节点表示学习，应用于推荐系统，请推荐相关期刊。",
  });
  assert.equal(graph.results[0].id, "graph");
  const vision = await engine.search({
    query: "研究计算机视觉与图像识别，使用深度学习进行视觉表示建模。",
  });
  assert.ok(vision.results.slice(0, 2).some((result) => result.id === "vision"));
});

test("exact venue abbreviations and names have precedence", async () => {
  const exact = await engine.search({ query: "cVc" });
  assert.equal(exact.results[0].id, "vision");
  assert.equal(exact.results[0].score, 100);
  assert.match(exact.results[0].explanation, /精确匹配/);
  assert.equal((await engine.search({ query: "Graph Learning Journal" })).results[0].id, "graph");
  const short = await createSearchEngine({ venues: [{ id: "short", name: "Example Venue", abbreviation: "A" }] });
  assert.equal((await short.search({ query: "A" })).results[0].score, 100);
});

test("target OR, type, area and reviewed filters combine correctly", async () => {
  const targets = await engine.search({ targets: ["CCF-B", "CAS-1"] });
  assert.deepEqual(targets.results.map((result) => result.id), ["vision", "chinese"]);
  const filtered = await engine.search({
    targets: ["CCF-A", "CCF-B"], recordType: "conference", area: "人工智能", reviewedOnly: true,
  });
  assert.deepEqual(filtered.results.map((result) => result.id), ["vision"]);
  const unreviewed = await engine.search({ query: "计算机视觉", reviewedOnly: true });
  assert.ok(unreviewed.results.every((result) => result.reviewed));
  assert.ok(!unreviewed.results.some((result) => result.id === "chinese"));
});

test("empty queries browse with no invented matching score and stable pagination", async () => {
  const first = await engine.search({ query: "", limit: 2, offset: 0 });
  const second = await engine.search({ query: " ", limit: 2, offset: 2 });
  assert.equal(first.total, 7);
  assert.equal(first.results.length, 2);
  assert.equal(first.results[0].score, null);
  assert.notEqual(first.results[0].id, second.results[0].id);
  assert.equal((await engine.search({ offset: 100 })).results.length, 0);
});

test("unknown terms, stop words and hostile inputs never become fake results", async () => {
  assert.equal((await engine.search({ query: "zzzzunfindablexyz" })).total, 0);
  assert.equal((await engine.search({ query: "the and of" })).total, 0);
  const result = await engine.search({ query: "<img src=x onerror=alert(1)>" });
  assert.ok(Array.isArray(result.results));
  assert.equal(globalThis.alert, undefined);
});

test("co-occurrence contributes actual reviewed keyword relationships", async () => {
  const found = await engine.search({ query: "graph" });
  const related = found.results.find((result) => result.id === "graph-related");
  assert.ok(related);
  assert.ok(related.signals.graph > 0);
  assert.equal(related.signals.lexical, 0);
  assert.match(related.explanation, /共现扩展/);
});

test("unreviewed-only keyword pairs do not create graph links", async () => {
  const isolated = await createSearchEngine({ venues: [
    { id: "one", name: "Alpha", scope: "quux foo", keywords: ["quux", "foo"], reviewed: false },
    { id: "two", name: "Beta", scope: "foo" },
  ] });
  assert.equal(isolated.stats.graphTerms, 0);
  assert.deepEqual((await isolated.search({ query: "quux" })).results.map((result) => result.id), ["one"]);
});

test("explicit out-of-scope text is never indexed as positive evidence", async () => {
  const isolated = await createSearchEngine({ venues: [
    { id: "one", name: "Graph Journal", scope: "graph research", exclusions: "quantumplasmaexcluded" },
  ] });
  assert.equal((await isolated.search({ query: "quantumplasmaexcluded" })).total, 0);
});

test("newer search requests can cancel obsolete computation", async () => {
  await assert.rejects(engine.search({ query: "graph" }, { isCancelled: () => true }), { name: "AbortError" });
});

test("malformed catalogues fail clearly and empty valid catalogues work", async () => {
  await assert.rejects(createSearchEngine({}), /venues/);
  await assert.rejects(createSearchEngine({ venues: [{}] }), /名称/);
  await assert.rejects(createSearchEngine({ venues: [{ id: "a", name: "a" }, { id: "a", name: "b" }] }), /重复/);
  const empty = await createSearchEngine({ venues: [] });
  assert.equal((await empty.search({ query: "NLP" })).total, 0);
});

test("lookup uses literal metadata only, without aliases, word stemming or scope matching", async () => {
  assert.deepEqual(resultIds(await lookup({ query: "LLM" })), ["llm-a", "llm-b"]);
  assert.equal((await lookup({ query: "NLP" })).total, 0);
  assert.equal((await lookup({ query: "自然语言处理" })).total, 0);
  assert.equal((await lookup({ query: "Natural language", searchField: "name" })).total, 0);
  assert.equal((await lookup({ query: "Computing ACM", searchField: "name" })).total, 0);
  assert.equal((await lookup({ query: "Computer.*Vision", searchField: "name" })).total, 0);
  assert.equal((await lookup({ query: "computers", searchField: "name" })).total, 0);
  assert.equal((await lookup({ query: "人工智能", searchField: "all" })).total, 0);
  assert.equal((await lookup({ query: "CCF-A", searchField: "all" })).total, 0);
});

test("lookup exact matching checks each selected field value and exposes actual matched fields", async () => {
  const all = await lookup({ query: "ACM", searchField: "all", matchMode: "exact" });
  assert.deepEqual(resultIds(all), ["identity", "z"]);
  assert.deepEqual(all.results[0].matchedFields, ["name", "abbreviation", "publisher"]);
  assert.deepEqual(all.results[1].matchedFields, ["publisher"]);
  assert.equal(all.results[0].matchKind, "exact");
  assert.deepEqual(resultIds(await lookup({ query: "ACM", searchField: "name", matchMode: "exact" })), ["identity"]);
  assert.deepEqual(resultIds(await lookup({ query: "ACM", searchField: "publisher", matchMode: "exact" })), ["identity", "z"]);
  assert.deepEqual(resultIds(await lookup({ query: "CSUR", searchField: "abbreviation", matchMode: "exact" })), ["z"]);
  assert.equal((await lookup({ query: "ACM Computing", matchMode: "exact" })).total, 0);
  assert.equal((await lookup({ query: "ACM Academic Press", searchField: "publisher" })).total, 0);
});

test("literal lookup normalizes case, fullwidth characters and whitespace but keeps punctuation literal", async () => {
  const options = { query: "  ａｃｍ\tＣＯＭＰＵＴＩＮＧ　 Ｓｕｒｖｅｙｓ \n", searchField: "name", matchMode: "exact" };
  assert.deepEqual(resultIds(await lookup(options)), ["z"]);
  assert.deepEqual(resultIds(await lookup({ query: "ｃｓｕｒ", searchField: "abbreviation", matchMode: "exact" })), ["z"]);
  assert.deepEqual(resultIds(await lookup({ query: "association\n for   COMPUTING", searchField: "publisher" })), ["z"]);
  assert.equal((await lookup({ query: "Computer-Vision", searchField: "name" })).total, 0);
});

test("ISSN lookup handles print/electronic values, hyphens, spaces, X and exact-versus-contains", async () => {
  for (const query of ["0360-0300", "03600300", "０３６０ ０３００", "0360–0300"]) {
    assert.deepEqual(resultIds(await lookup({ query, searchField: "issn", matchMode: "exact" })), ["z"]);
    assert.deepEqual(resultIds(await lookup({ query, searchField: "all", matchMode: "exact" })), ["z"]);
  }
  assert.deepEqual(resultIds(await lookup({ query: "1557 7341", searchField: "issn", matchMode: "exact" })), ["z"]);
  assert.deepEqual(resultIds(await lookup({ query: "１２３４ ５６７ｘ", searchField: "issn", matchMode: "exact" })), ["vision"]);
  assert.equal((await lookup({ query: "0360", searchField: "issn", matchMode: "exact" })).total, 0);
  assert.deepEqual(resultIds(await lookup({ query: "0360", searchField: "issn", matchMode: "contains" })), ["z"]);
  assert.equal((await lookup({ query: "0360030015577341", searchField: "issn" })).total, 0);
  assert.equal((await lookup({ query: "---", searchField: "issn" })).total, 0);
  assert.equal((await lookup({ query: "03600300", searchField: "name" })).total, 0);
});

test("lookup combines source, target OR, type, area and reviewed filters", async () => {
  const options = { source: "ccf", targets: ["CCF-A", "CCF-B"], recordType: "journal", area: "计算机科学", reviewedOnly: true };
  assert.deepEqual(resultIds(await lookup(options)), ["z", "llm-a"]);
  assert.deepEqual(resultIds(await lookup({ ...options, source: "jcr" })), ["z"]);
  assert.deepEqual(resultIds(await lookup({ source: "th_cpl" })), ["vision"]);
  assert.deepEqual(resultIds(await lookup({ source: "cas", targets: ["CAS-2", "CCF-B"] })), ["llm-b"]);
  assert.equal((await lookup({ source: "cas", targets: ["CCF-A"] })).total, 0);
  assert.equal((await lookup({ source: "cas", reviewedOnly: true })).total, 0);
  assert.deepEqual(resultIds(await lookupEngine.search({ source: "jcr" })), ["z", "hidden"]);
});

test("lookup has no recommendation scores and summary counts all matches before pagination", async () => {
  const found = await lookup({ query: " ", limit: 2, offset: 2 });
  assert.equal(found.total, 6);
  assert.equal(found.results.length, 2);
  assert.deepEqual(found.summary, { conference: 1, journal: 5, reviewed: 3 });
  for (const result of found.results) {
    assert.equal(result.score, null);
    assert.equal(result.signals, null);
    assert.deepEqual(result.matchedTerms, []);
    assert.deepEqual(result.matchedFields, []);
    assert.equal(result.matchKind, "browse");
  }
  const partial = await lookup({ query: "Computing", searchField: "name" });
  assert.equal(partial.results[0].matchKind, "contains");
  assert.deepEqual(partial.summary, { conference: 0, journal: 1, reviewed: 1 });
  const none = await lookup({ query: "notfound" });
  assert.deepEqual(none.summary, { conference: 0, journal: 0, reviewed: 0 });
});

test("lookup sorting follows the selected column and direction, with stable id ties and no exact-match boost", async () => {
  assert.deepEqual(resultIds(await lookup({ sortBy: "name" })), ["identity", "z", "vision", "llm-a", "llm-b", "hidden"]);
  assert.deepEqual(resultIds(await lookup({ sortBy: "name", sortDirection: "desc" })), ["hidden", "llm-a", "llm-b", "vision", "z", "identity"]);
  assert.deepEqual(resultIds(await lookup({ sortBy: "abbreviation" })), ["identity", "z", "vision", "llm-a", "llm-b", "hidden"]);
  assert.deepEqual(resultIds(await lookup({ sortBy: "publisher" })), ["identity", "llm-a", "z", "hidden", "vision", "llm-b"]);
  assert.deepEqual(resultIds(await lookup({ sortBy: "type" })), ["vision", "hidden", "identity", "llm-a", "llm-b", "z"]);
  assert.deepEqual(resultIds(await lookup({ sortBy: "area" })), ["identity", "llm-a", "z", "llm-b", "vision", "hidden"]);
  assert.deepEqual(resultIds(await lookup({ query: "ACM", sortDirection: "desc" })), ["z", "identity"]);
  const full = await lookup({ limit: 6 });
  const first = await lookup({ limit: 2 });
  const second = await lookup({ limit: 2, offset: 2 });
  const third = await lookup({ limit: 2, offset: 4 });
  assert.deepEqual([...resultIds(first), ...resultIds(second), ...resultIds(third)], resultIds(full));
  assert.equal((await lookup({ offset: 99 })).results.length, 0);
});

test("lookup preserves safe publisher/ISSN arrays and rejects unsupported search controls", async () => {
  const detail = lookupEngine.getDetail("z");
  assert.deepEqual(detail.publishers, ["Association for Computing Machinery", "ACM"]);
  assert.deepEqual(detail.issns, ["0360-0300", "1557-7341"]);
  const malformedFields = await createSearchEngine({ venues: [
    { name: "Unsafe", publishers: ["<script>bad()</script>", 123, null, ""], issns: "0360-0300", secret: "no" },
  ] });
  const venue = malformedFields.getDetail("venue:0");
  assert.deepEqual(venue.publishers, ["<script>bad()</script>"]);
  assert.deepEqual(venue.issns, []);
  assert.equal(venue.secret, undefined);
  assert.equal((await malformedFields.search({ mode: "lookup", query: "<script>bad()</script>", searchField: "publisher", matchMode: "exact" })).total, 1);
  for (const options of [{ searchField: "scope" }, { matchMode: "regex" }, { sortBy: "score" }, { sortDirection: "random" }]) {
    await assert.rejects(lookup(options));
  }
});

test("lookup yields during scanning and cancels an obsolete request", async () => {
  let shouldCancel = false;
  let cancellationEnabled = false;
  const many = await createSearchEngine({ venues: Array.from({ length: 2100 }, (_, index) => ({ id: `v${index}`, name: `Venue ${index}` })) }, {
    yieldControl: async () => { if (cancellationEnabled) shouldCancel = true; },
  });
  cancellationEnabled = true;
  await assert.rejects(many.search({ mode: "lookup" }, { isCancelled: () => shouldCancel }), { name: "AbortError" });
});

test("worker protocol loads one same-directory catalog and supports search, detail, export and stale cancellation", async (t) => {
  const originalSelf = Object.getOwnPropertyDescriptor(globalThis, "self");
  const messages = [];
  let receive;
  Object.defineProperty(globalThis, "self", {
    configurable: true,
    value: {
      postMessage: (message) => messages.push(message),
      addEventListener: (type, callback) => { if (type === "message") receive = callback; },
    },
  });
  t.after(() => {
    if (originalSelf) Object.defineProperty(globalThis, "self", originalSelf);
    else delete globalThis.self;
  });
  const fetched = [];
  t.mock.method(globalThis, "fetch", async (url) => {
    fetched.push(String(url));
    return Response.json(catalog);
  });
  await import("../../website/assets/search-worker.js?worker-protocol-test");
  await receive({ data: { type: "init", id: "init" } });
  assert.match(fetched[0], /\/assets\/catalog\.json$/);
  assert.equal(messages.find((message) => message.type === "ready").stats.total, 7);
  await receive({ data: { type: "search", id: "search", query: "GNN" } });
  assert.equal(messages.find((message) => message.id === "search" && message.type === "results").results[0].id, "graph");
  await receive({ data: { type: "detail", id: "detail", venueId: "nlp" } });
  assert.equal(messages.find((message) => message.type === "detail").venue.id, "nlp");
  await receive({ data: { type: "export", id: "export", targets: ["CCF-B", "CAS-1"], limit: 1, offset: 1 } });
  assert.equal(messages.find((message) => message.type === "export").results.length, 2);
  assert.equal(fetched.length, 1);
  await Promise.all([
    receive({ data: { type: "search", id: "stale", query: "GNN" } }),
    receive({ data: { type: "search", id: "current", query: "NLP" } }),
  ]);
  assert.equal(messages.some((message) => message.id === "stale" && message.type === "results"), false);
  assert.equal(messages.find((message) => message.id === "current" && message.type === "results").results[0].id, "nlp");
  await receive({ data: { type: "detail", id: "missing", venueId: "missing" } });
  assert.match(messages.find((message) => message.id === "missing").message, /未找到/);
});

test("worker lookup preserves literal filters and sorting through pagination, detail and full export", async (t) => {
  const originalSelf = Object.getOwnPropertyDescriptor(globalThis, "self");
  const messages = [];
  let receive;
  Object.defineProperty(globalThis, "self", {
    configurable: true,
    value: {
      postMessage: (message) => messages.push(message),
      addEventListener: (type, callback) => { if (type === "message") receive = callback; },
    },
  });
  t.after(() => {
    if (originalSelf) Object.defineProperty(globalThis, "self", originalSelf);
    else delete globalThis.self;
  });
  t.mock.method(globalThis, "fetch", async () => Response.json(lookupCatalog));
  await import("../../website/assets/search-worker.js?lookup-protocol-test");
  await receive({ data: { type: "init", id: "init" } });
  const options = { mode: "lookup", query: "ACM", searchField: "all", matchMode: "exact", source: "ccf", sortBy: "name", sortDirection: "desc", limit: 1, offset: 1 };
  await receive({ data: { ...options, type: "search", id: "lookup-page" } });
  const page = messages.find((message) => message.id === "lookup-page" && message.type === "results");
  assert.equal(page.total, 2);
  assert.deepEqual(resultIds(page), ["identity"]);
  assert.deepEqual(page.summary, { conference: 0, journal: 2, reviewed: 1 });
  assert.equal(page.results[0].signals, null);
  await receive({ data: { ...options, type: "export", id: "lookup-export" } });
  const exported = messages.find((message) => message.id === "lookup-export" && message.type === "export");
  assert.deepEqual(resultIds(exported), ["z", "identity"]);
  assert.deepEqual(exported.summary, page.summary);
  assert.equal(exported.offset, 0);
  await receive({ data: { type: "detail", id: "lookup-detail", venueId: "z" } });
  const detail = messages.find((message) => message.id === "lookup-detail").venue;
  assert.deepEqual(detail.publishers, lookupCatalog.venues[0].publishers);
  assert.deepEqual(detail.issns, lookupCatalog.venues[0].issns);
  await Promise.all([
    receive({ data: { type: "search", id: "old-lookup", mode: "lookup", query: "ACM" } }),
    receive({ data: { type: "search", id: "new-lookup", mode: "lookup", query: "LLM" } }),
  ]);
  assert.equal(messages.some((message) => message.id === "old-lookup" && message.type === "results"), false);
  assert.deepEqual(resultIds(messages.find((message) => message.id === "new-lookup" && message.type === "results")), ["llm-a", "llm-b"]);
});
