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
