import { createSearchEngine } from "./search-engine.js";

let enginePromise;
let latestSearch = 0;
const send = (value) => self.postMessage(value);

async function initialize(id, catalogUrl) {
  if (!enginePromise) {
    enginePromise = (async () => {
      send({ type: "progress", id, stage: "load", progress: 0, message: "正在载入公开投稿目录…" });
      const response = await fetch(catalogUrl || new URL("./catalog.json", import.meta.url));
      if (!response.ok) throw new Error(`目录载入失败（HTTP ${response.status}），请刷新后重试。`);
      const catalog = await response.json();
      return createSearchEngine(catalog, {
        onProgress(event) { send({ type: "progress", id, ...event }); },
      });
    })().catch((error) => {
      enginePromise = undefined;
      throw error;
    });
  }
  return enginePromise;
}

self.addEventListener("message", async ({ data }) => {
  if (!data || typeof data !== "object") return;
  const { type, id } = data;
  const searchVersion = type === "search" ? ++latestSearch : latestSearch;
  try {
    const engine = await initialize(id, type === "init" ? data.catalogUrl : undefined);
    if (type === "init") {
      send({ type: "ready", id, stats: engine.stats, sources: engine.sources, areas: engine.areas, targets: engine.targets });
    } else if (type === "detail") {
      const venue = engine.getDetail(data.venueId);
      if (!venue) throw new Error("未找到这条目录记录。");
      send({ type: "detail", id, venue });
    } else if (type === "search" || type === "export") {
      const result = await engine.search(
        type === "export" ? { ...data, offset: 0, limit: Math.max(engine.stats.total, 1) } : data,
        {
          onProgress(event) { send({ type: "progress", id, ...event }); },
          isCancelled: type === "search" ? () => searchVersion !== latestSearch : undefined,
        },
      );
      if (type === "export" || searchVersion === latestSearch) send({ type: type === "export" ? "export" : "results", id, ...result });
    } else {
      throw new Error("无法识别的本地检索请求。");
    }
  } catch (error) {
    if (error?.name !== "AbortError") send({ type: "error", id, message: error?.message || "本地检索失败，请刷新后重试。" });
  }
});
