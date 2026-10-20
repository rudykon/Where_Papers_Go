const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const escape = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
const preferences = {
  get(key, fallback) { try { return localStorage.getItem(`wpg-${key}`) || fallback; } catch { return fallback; } },
  set(key, value) { try { localStorage.setItem(`wpg-${key}`, value); } catch { /* Private browsing can disable persistence. */ } },
};
let locale = preferences.get("language", "zh");
const text = (zh, en) => locale === "en" ? en : zh;
const number = (value) => Number(value).toLocaleString(locale === "en" ? "en-US" : "zh-CN");
const zhText = new WeakMap();
const views = {
  overview: { title: ["项目概览", "Overview"], links: [["intro", "项目介绍", "Introduction"], ["journey", "使用流程", "Getting started"], ["examples", "研究示例", "Example topics"]] },
  recommend: { title: ["投稿推荐", "Find a venue"], links: [["search-form", "描述研究", "Your research"], ["results", "推荐清单", "Your shortlist"]] },
  catalog: { title: ["刊会目录", "Venue directory"], links: [["search-form", "检索与筛选", "Search & filters"], ["results", "目录结果", "Venues"]] },
  method: { title: ["检索方法", "How it works"], links: [["pipeline", "检索流程", "Retrieval pipeline"], ["boundaries", "能力边界", "Scope & limits"]] },
  data: { title: ["数据说明", "Data & sources"], links: [["sources", "榜单来源", "Ranking sources"], ["scope-data", "审核范围", "Reviewed scopes"], ["privacy", "本地计算", "Local processing"]] },
};
let currentView = "overview";
let sequence = 0;
let worker;
let workerReady = false;
let workerError = "";
let initId = "";
let activeSearchId = "";
let exportId = "";
let total = 0;
let offset = 0;
let resultRows = [];
let lastSearch = null;
let detailVenue = null;
let datasetInfo = null;
let elapsed = 0;
let searchStart = 0;
let queuedSearch = null;
let toastTimer;
const storedForms = {};
const storedResults = {};
const newId = () => `wpg-${++sequence}`;
const safeUrl = (value) => {
  try { const url = new URL(value); return ["https:", "http:"].includes(url.protocol) && !url.username && !url.password ? url.href : ""; } catch { return ""; }
};

function translateStatic() {
  $$("[data-en]").forEach((element) => {
    if (!zhText.has(element)) zhText.set(element, element.textContent);
    element.textContent = locale === "en" ? element.dataset.en : zhText.get(element);
  });
  document.documentElement.lang = locale === "en" ? "en" : "zh-CN";
  document.title = text("Where Papers Go · 为研究找到合适的去处", "Where Papers Go · Find the right venue");
  $("#language-toggle").textContent = locale === "en" ? "中文" : "English";
  $("#language-toggle").setAttribute("aria-label", locale === "en" ? "切换至中文" : "Switch to English");
  $("#theme-toggle").setAttribute("aria-label", text("切换深浅主题", "Toggle light and dark theme"));
  $("#close-dialog").setAttribute("aria-label", text("关闭详情", "Close details"));
  $("#theme-toggle").title = text("切换深浅主题", "Toggle light and dark theme");
  renderNavigation();
  renderExploreHeading();
  renderDataset();
  renderEngineStatus();
  if (lastSearch && !activeSearchId && !queuedSearch) renderResults();
  else if (!lastSearch && !activeSearchId && !queuedSearch) clearResults();
  if (detailVenue) renderDetail(detailVenue);
}

function renderNavigation() {
  const config = views[currentView];
  $("#sidebar-title").textContent = text(...config.title);
  $("#breadcrumb-page").textContent = text(...config.title);
  $("#toc").innerHTML = config.links.map(([id, zh, en]) => `<a href="#${id}">${escape(text(zh, en))}</a>`).join("");
  $$(".nav-link").forEach((link) => {
    const active = link.hash === `#${currentView}`;
    link.classList.toggle("active", active);
    if (active) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current");
  });
}

function renderExploreHeading() {
  const catalog = currentView === "catalog";
  $("#explore-eyebrow").textContent = catalog ? "EXPLORE THE VENUE LANDSCAPE" : "FIND YOUR NEXT VENUE";
  $("#explore-title").textContent = catalog ? text("你的刊会地图。", "Your map of research venues.") : text("从你的研究，出发。", "Start with your research.");
  $("#explore-lead").textContent = catalog ? text("查看真实刊会目录，按名称、等级、研究分类与审核状态筛选。", "Browse real venues by name, ranking, research area, and reviewed-scope status.") : text("描述论文主题，找到相关刊会，并查看每一条推荐的匹配依据。", "Describe your research and see the evidence behind each venue match.");
  $("#query-label").textContent = catalog ? text("名称、简称或关键词", "Name, abbreviation, or keyword") : text("论文主题", "Your research topic");
  $("#query").placeholder = catalog ? text("例如 NeurIPS、Nature、人工智能；留空浏览全部目录", "For example NeurIPS, Nature, artificial intelligence; leave empty to browse all") : text("输入论文题目、摘要或关键词，中英文均可…", "Enter your title, abstract, or keywords in Chinese or English…");
  $("#query").rows = catalog ? 2 : 4;
  $("#query").required = !catalog;
}

function formValues() {
  return {
    query: $("#query").value.trim(),
    targets: $("#rank-filters").children.length
      ? $$("#rank-filters input:checked").map((input) => input.value)
      : currentView === "catalog" ? [] : ["CCF-A"],
    recordType: $("#record-type").value,
    area: $("#area").value,
    reviewedOnly: $("#reviewed-only").checked,
    limit: Number($("#page-size").value),
  };
}

function restoreForm(values) {
  $("#query").value = values.query || "";
  $$("#rank-filters input").forEach((input) => { input.checked = (values.targets || []).includes(input.value); });
  $("#record-type").value = values.recordType || "all";
  $("#area").value = values.area || "";
  $("#reviewed-only").checked = Boolean(values.reviewedOnly);
  $("#page-size").value = String(values.limit || 20);
  $("#query-count").textContent = `${number($("#query").value.length)} / 8,000`;
}

function clearResults() {
  activeSearchId = "";
  queuedSearch = null;
  lastSearch = null;
  resultRows = [];
  total = 0;
  $("#search-progress").hidden = true;
  $("#export-results").hidden = true;
  $("#pagination").hidden = true;
  $("#results-note").hidden = true;
  $("#result-list").innerHTML = `<div class="empty-state"><span class="empty-symbol">⌕</span><h3>${escape(text("下一站，从这里开始", "Your next destination starts here"))}</h3><p>${escape(text("输入研究主题，或前往刊会目录查看全部目标。", "Enter your topic or browse the venue directory."))}</p></div>`;
  $("#results-meta").textContent = text("输入研究主题，开始探索。", "Enter a topic to begin.");
  $("#search-button").disabled = false;
}

function navigate(view, scroll = true) {
  if (!views[view]) return;
  if (currentView === "catalog" || currentView === "recommend") {
    storedForms[currentView] = formValues();
    storedResults[currentView] = lastSearch && !activeSearchId && !queuedSearch
      ? { params: lastSearch, rows: resultRows, total, offset, elapsed } : null;
  }
  const changed = currentView !== view;
  if (changed) {
    activeSearchId = "";
    queuedSearch = null;
    $("#search-progress").hidden = true;
    $("#search-button").disabled = false;
  }
  currentView = view;
  $$("[data-view]").forEach((element) => { element.hidden = element.dataset.view !== (["recommend", "catalog"].includes(view) ? "explore" : view); });
  if (changed && ["recommend", "catalog"].includes(view)) {
    restoreForm(storedForms[view] || { targets: view === "recommend" ? ["CCF-A"] : [] });
    clearResults();
    if (storedResults[view]) {
      const saved = storedResults[view];
      lastSearch = saved.params;
      resultRows = saved.rows;
      total = saved.total;
      offset = saved.offset;
      elapsed = saved.elapsed;
      renderResults();
    } else if (view === "catalog") submitSearch(0);
  }
  renderNavigation();
  renderExploreHeading();
  if (scroll) window.scrollTo({ top: 0, behavior: "instant" });
}

function toast(message) {
  clearTimeout(toastTimer);
  $("#toast").textContent = message;
  $("#toast").hidden = false;
  toastTimer = setTimeout(() => { $("#toast").hidden = true; }, 4500);
}

function renderEngineStatus() {
  $("#engine-status").textContent = workerError
    ? text("本地目录加载失败", "Catalog unavailable")
    : workerReady ? text("本地检索已就绪", "Local search ready") : text("正在准备本地检索", "Preparing local search");
  $("#engine-dot").classList.toggle("error", Boolean(workerError));
}

function renderDataset() {
  if (!datasetInfo) return;
  $$("[data-stat]").forEach((element) => { element.textContent = number(datasetInfo.stats[element.dataset.stat] || 0); });
  const tiers = { ccf: "A / B / C", th_cpl: "A / B", cas: "1 / 2 / 3 / 4", jcr: "Q1 / Q2 / Q3 / Q4 / N/A" };
  $("#source-table").innerHTML = datasetInfo.sources.map((source) => `<tr><td>${escape(source.id === "cas" ? text("中科院分区", "CAS partitions") : source.label)}</td><td>${escape(source.year)}</td><td>${number(source.records)}</td><td>${escape(tiers[source.id] || "")}</td></tr>`).join("");
}

function renderFilters(data) {
  const currentValues = formValues();
  const ordered = ["CCF-A", "CCF-B", "CCF-C", "THCPL-A", "THCPL-B", "CAS-1", "CAS-2", "CAS-3", "CAS-4", "JCR-Q1", "JCR-Q2", "JCR-Q3", "JCR-Q4", "JCR-N/A"];
  const targets = [...data.targets].sort((a, b) => {
    const aRank = ordered.indexOf(a), bRank = ordered.indexOf(b);
    return (aRank < 0 ? 100 : aRank) - (bRank < 0 ? 100 : bRank) || a.localeCompare(b);
  });
  const checked = currentView === "catalog" ? [] : ["CCF-A"];
  $("#rank-filters").innerHTML = targets.map((target) => `<label class="rank-chip"><input type="checkbox" value="${escape(target)}"${checked.includes(target) ? " checked" : ""}><span>${escape(target.replace("THCPL", "TH-CPL"))}</span></label>`).join("");
  $("#area").innerHTML = `<option value="" data-en="All areas">全部分类</option>` + data.areas.map((area) => `<option value="${escape(area)}">${escape(area)}</option>`).join("");
  zhText.set($("#area").options[0], "全部分类");
  if (locale === "en") $("#area").options[0].textContent = "All areas";
  restoreForm(currentValues);
}

function showError(message) {
  resultRows = [];
  lastSearch = null;
  $("#result-list").innerHTML = `<div class="empty-state"><span class="empty-symbol">!</span><h3>${escape(text("这次检索未完成", "Search did not complete"))}</h3><p>${escape(message)}</p><button class="button" type="button" id="retry-search">${escape(text("重试", "Retry"))} ↻</button></div>`;
  $("#results-meta").textContent = text("检索未完成，没有生成推荐结果。", "No recommendations were generated.");
  $("#pagination").hidden = true;
  $("#export-results").hidden = true;
  $("#results-note").hidden = true;
  $("#search-progress").hidden = true;
  $("#search-button").disabled = false;
  activeSearchId = "";
}

function initializeWorker() {
  worker?.terminate();
  exportId = "";
  $("#export-results").disabled = false;
  workerReady = false;
  workerError = "";
  initId = newId();
  renderEngineStatus();
  try {
    worker = new Worker(new URL("./search-worker.js", import.meta.url), { type: "module" });
    worker.addEventListener("message", ({ data }) => {
      if (data.type === "ready") {
        workerReady = true;
        datasetInfo = data;
        renderFilters(data);
        renderDataset();
        renderEngineStatus();
        if (queuedSearch) {
          const pending = queuedSearch;
          queuedSearch = null;
          if (pending.view === currentView) submitSearch(pending.offset, false, pending.params);
        }
      } else if (data.type === "progress") {
        if (data.id === initId && !workerReady) {
          $("#engine-status").textContent = locale === "zh" ? data.message : (data.stage === "load" ? "Loading catalog…" : `Building index… ${Math.round(data.progress * 100)}%`);
        }
        if (data.id === activeSearchId || (queuedSearch && data.id === initId)) {
          $("#progress-message").textContent = locale === "zh" ? data.message : (workerReady ? "Matching terms and reviewed topics locally…" : "Preparing the local catalog and index…");
        }
      } else if (data.type === "results" && data.id === activeSearchId) {
        total = data.total;
        resultRows = data.results;
        offset = data.offset;
        elapsed = performance.now() - searchStart;
        activeSearchId = "";
        $("#search-progress").hidden = true;
        $("#search-button").disabled = false;
        renderResults();
      } else if (data.type === "export" && data.id === exportId) {
        exportId = "";
        $("#export-results").disabled = false;
        downloadCsv(data.results);
      } else if (data.type === "error") {
        if (data.id === initId) {
          workerError = data.message;
          renderEngineStatus();
          if (queuedSearch || ["recommend", "catalog"].includes(currentView)) showError(data.message);
          else toast(text("目录暂时未能加载，进入检索页可重试。", "Catalog loading failed. Open search to retry."));
        } else if (data.id === activeSearchId) showError(data.message);
        else if (data.id === exportId) { exportId = ""; $("#export-results").disabled = false; toast(data.message); }
      }
    });
    worker.addEventListener("error", () => {
      workerError = text("浏览器无法启动本地检索，请使用支持 Web Worker 的现代浏览器并刷新重试。", "The local worker could not start. Use a modern browser and reload.");
      workerReady = false;
      renderEngineStatus();
      if (queuedSearch || activeSearchId) showError(workerError);
    });
    worker.postMessage({ type: "init", id: initId });
  } catch {
    workerError = text("当前浏览器不支持模块化 Web Worker。请使用新版 Chrome、Edge、Firefox 或 Safari。", "This browser does not support module workers. Please use a current browser.");
    renderEngineStatus();
  }
}

function submitSearch(startOffset = 0, reuse = false, snapshot = null) {
  const params = snapshot || (reuse && lastSearch ? { ...lastSearch } : formValues());
  if (currentView === "recommend" && !params.query) {
    $("#query").focus();
    toast(text("请先输入论文主题、摘要或关键词。", "Enter a title, abstract, or keywords first."));
    return;
  }
  offset = startOffset;
  if (workerError) initializeWorker();
  if (!workerReady) {
    queuedSearch = { view: currentView, params, offset: startOffset };
    $("#search-progress").hidden = false;
    $("#progress-message").textContent = text("首次使用正在建立本地索引，请稍候…", "Building the local index for first use…");
    return;
  }
  lastSearch = { ...params };
  activeSearchId = newId();
  searchStart = performance.now();
  $("#search-progress").hidden = false;
  $("#progress-message").textContent = text("正在本地匹配词项、向量与审核主题…", "Matching local terms, vectors, and reviewed topics…");
  $("#search-button").disabled = true;
  $("#export-results").hidden = true;
  $("#pagination").hidden = true;
  $("#results-meta").textContent = text("正在检索…", "Searching…");
  $("#results-note").hidden = true;
  $("#result-list").innerHTML = `<div class="empty-state"><span class="spinner"></span><p>${escape(text("正在检索，请稍候…", "Searching locally…"))}</p></div>`;
  resultRows = [];
  total = 0;
  worker.postMessage({ type: "search", id: activeSearchId, ...params, offset });
}

function renderResults() {
  if (!lastSearch) return;
  const query = Boolean(lastSearch.query);
  const from = total ? offset + 1 : 0;
  const to = offset + resultRows.length;
  $("#results-meta").textContent = text(
    `${number(total)} 个${query ? "匹配" : "符合条件的"}刊会 · 显示 ${number(from)}–${number(to)} · 本地检索 ${Math.max(1, Math.round(elapsed))} ms`,
    `${number(total)} ${query ? "matching" : "eligible"} venues · ${number(from)}–${number(to)} shown · ${Math.max(1, Math.round(elapsed))} ms locally`,
  );
  $("#results-note").hidden = !query;
  $("#results-note").textContent = text("匹配度来自本地词项、TF-IDF 向量与关键词图，不代表录用概率。未审核的刊会仅按名称和研究分类匹配。", "Scores combine local terms, TF-IDF vectors, and a keyword graph; they are not acceptance probabilities. Unreviewed venues are matched by names and classifications.");
  $("#result-list").innerHTML = resultRows.length ? resultRows.map((venue, index) => {
    const reason = venue.scope || text("尚无已审核收稿范围。可依据名称与研究分类初步筛选，并通过来源网站核实。", "No reviewed scope is available. Use the venue name and research classifications as initial evidence, then verify with the source.");
    return `<article class="venue-card"><span class="venue-index">${String(offset + index + 1).padStart(2, "0")}</span><div><div class="venue-card-top"><h3>${venue.abbreviation ? `<span class="venue-abbr">${escape(venue.abbreviation)}</span>` : ""}${escape(venue.name)}</h3>${query && venue.score != null ? `<div class="match-score">${Number(venue.score).toFixed(1)}<small>${escape(text("相对匹配度", "relative fit"))}</small></div>` : ""}</div>
    <div class="tags"><span class="tag">${escape(venue.record_type === "conference" ? text("会议", "Conference") : text("期刊", "Journal"))}</span>${venue.ranks.map((rank) => `<span class="tag rank">${escape(rank.replace("THCPL", "TH-CPL"))}</span>`).join("")}${venue.reviewed ? `<span class="tag reviewed">✓ ${escape(text("范围已审核", "Reviewed scope"))}</span>` : ""}${venue.areas.slice(0, 2).map((area) => `<span class="tag">${escape(area)}</span>`).join("")}</div><p class="venue-reason">${escape(reason.length > 230 ? `${reason.slice(0, 230)}…` : reason)}</p><div class="venue-footer"><span class="venue-terms">${escape(query ? `${text("匹配词项", "Matched terms")} · ${(venue.matchedTerms || []).slice(0, 7).join(" · ") || text("审核主题扩展", "Reviewed-topic expansion")}` : venue.areas.join(" · "))}</span><button type="button" class="detail-button" data-detail="${index}">${escape(text("查看范围与依据", "View scope & evidence"))} ↗</button></div></div></article>`;
  }).join("") : `<div class="empty-state"><span class="empty-symbol">⌕</span><h3>${escape(text("暂未找到匹配的刊会", "No matching venues"))}</h3><p>${escape(text("试试更具体的主题词或英文术语，或放宽等级、研究分类及审核范围筛选。", "Try specific keywords or English terms, or broaden the ranking, research area, and scope filters."))}</p><button class="button" type="button" id="broaden-filters">${escape(text("清除筛选后重试", "Clear filters and retry"))} →</button></div>`;
  $("#export-results").hidden = total === 0;
  $("#export-results").disabled = Boolean(exportId);
  $("#pagination").hidden = total <= lastSearch.limit;
  $("#previous-page").disabled = offset === 0;
  $("#next-page").disabled = offset + resultRows.length >= total;
  $("#page-label").textContent = `${Math.floor(offset / lastSearch.limit) + 1} / ${Math.max(1, Math.ceil(total / lastSearch.limit))}`;
}

function renderDetail(venue) {
  const scopeUrl = safeUrl(venue.scope_url);
  const venueUrl = safeUrl(venue.url);
  const explain = locale === "zh" ? venue.explanation : [
    venue.matchedTerms?.length ? `Matched catalog terms: ${venue.matchedTerms.join(", ")}.` : "",
    venue.signals?.graph > 0 ? "Reviewed keyword co-occurrences contributed to recall." : "",
    venue.reviewed ? "Scope evidence comes from the reviewed catalog." : "The venue has no reviewed scope; verify its research coverage at the source.",
  ].filter(Boolean).join(" ");
  $("#venue-content").innerHTML = `<h2 id="venue-title">${escape(venue.name)}</h2><p class="venue-abbr">${escape(venue.abbreviation)}</p><div class="tags">${venue.ranks.map((rank) => `<span class="tag rank">${escape(rank.replace("THCPL", "TH-CPL"))}</span>`).join("")}<span class="tag">${escape(venue.record_type === "conference" ? text("会议", "Conference") : text("期刊", "Journal"))}</span></div>
  <section class="detail-section"><h3>${escape(text("研究分类", "Research areas"))}</h3><p>${escape(venue.areas.join(" · ") || "—")}</p></section>
  <section class="detail-section"><h3>${escape(text("已审核收稿范围", "Reviewed submission scope"))}${venue.scope_year ? ` · ${escape(venue.scope_year)}` : ""}</h3><p>${escape(venue.scope || text("暂无已审核范围。该记录仅使用榜单名称与分类进行匹配，不能确认具体投稿适配性。", "No reviewed scope is available. Matching uses the listed name and classification, which cannot confirm detailed submission fit."))}</p></section>
  ${venue.keywords.length ? `<section class="detail-section"><h3>${escape(text("覆盖主题", "Covered topics"))}</h3><div class="tags">${venue.keywords.map((keyword) => `<span class="tag">${escape(keyword)}</span>`).join("")}</div></section>` : ""}
  <section class="detail-section"><h3>${escape(text("明确排除的方向", "Scope exclusions"))}</h3><p class="exclusions">${escape(venue.exclusions || text("目录没有记录明确的排除方向；这不表示所有研究都适合投稿。", "No explicit exclusions are recorded. This does not mean all research topics are suitable."))}</p></section>
  ${venue.score != null ? `<section class="detail-section"><h3>${escape(text("本次匹配依据", "Evidence for this match"))}</h3><p>${escape(explain)}</p><div class="signal-bars">${[["lexical", "词项召回", "Lexical"], ["vector", "词项向量", "Term vectors"], ["graph", "主题扩展", "Topic graph"]].map(([key, zh, en]) => `<div class="signal-bar"><label>${escape(text(zh, en))} · ${Math.round((venue.signals?.[key] || 0) * 100)} / 100</label><progress max="1" value="${Number(venue.signals?.[key] || 0)}" aria-label="${escape(text(zh, en))}"></progress></div>`).join("")}</div></section>` : ""}
  <section class="detail-section"><h3>${escape(text("原始来源", "Source links"))}</h3>${scopeUrl ? `<a href="${escape(scopeUrl)}" target="_blank" rel="noopener noreferrer">${escape(text("审核范围来源", "Reviewed scope source"))} ↗<br>${escape(scopeUrl)}</a>` : ""}${venueUrl ? `<a href="${escape(venueUrl)}" target="_blank" rel="noopener noreferrer">${escape(text("刊会主页或榜单目录链接", "Venue homepage or directory entry"))} ↗<br>${escape(venueUrl)}</a>` : ""}${!scopeUrl && !venueUrl ? `<p>${escape(text("此条目没有可用的公开来源链接。", "No public source link is available for this entry."))}</p>` : ""}</section>
  <section class="detail-section"><p>${escape(text("榜单等级与范围反映所收录版本。投稿前请核实最新征稿、截止日期和投稿要求；匹配度不是录用概率。", "Ranks and scopes reflect the included editions. Verify current calls, deadlines, and submission rules. Match scores are not acceptance probabilities."))}</p></section>`;
}

function downloadCsv(rows) {
  const fields = ["name", "abbreviation", "record_type", "ranks", "areas", "score", "scope", "exclusions", "scope_year", "scope_url", "url", "matchedTerms"];
  const cell = (value) => {
    let content = Array.isArray(value) ? value.join("; ") : String(value ?? "");
    // Prevent CSV formulas from executing in spreadsheet applications.
    if (/^[\s]*[=+\-@\t\r]/.test(content)) content = `'${content}`;
    return `"${content.replace(/"/g, '""')}"`;
  };
  const csv = "\uFEFF" + [fields.map(cell).join(","), ...rows.map((row) => fields.map((key) => cell(row[key])).join(","))].join("\r\n");
  const url = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = "where-papers-go-venues.csv";
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  toast(text(`已导出 ${number(rows.length)} 个刊会。`, `Exported ${number(rows.length)} venues.`));
}

$("#theme-toggle").addEventListener("click", () => {
  const theme = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = theme;
  preferences.set("theme", theme);
});
document.documentElement.dataset.theme = preferences.get("theme", matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
$("#language-toggle").addEventListener("click", () => {
  locale = locale === "en" ? "zh" : "en";
  preferences.set("language", locale);
  translateStatic();
});
$("#query").addEventListener("input", () => { $("#query-count").textContent = `${number($("#query").value.length)} / 8,000`; });
$("#query").addEventListener("keydown", (event) => { if ((event.ctrlKey || event.metaKey) && event.key === "Enter") { event.preventDefault(); $("#search-form").requestSubmit(); } });
$("#clear-query").addEventListener("click", () => { $("#query").value = ""; $("#query").dispatchEvent(new Event("input")); $("#query").focus(); });
$("#search-form").addEventListener("submit", (event) => { event.preventDefault(); submitSearch(0); });
$("#reset-filters").addEventListener("click", () => { restoreForm({ query: $("#query").value, targets: [] }); if (currentView === "catalog") submitSearch(0); });
$("#cancel-search").addEventListener("click", () => { clearResults(); toast(text("已取消显示本次检索。", "Search cancelled.")); });
$("#previous-page").addEventListener("click", () => { submitSearch(Math.max(0, offset - lastSearch.limit), true); $("#results").scrollIntoView({ behavior: "instant" }); });
$("#next-page").addEventListener("click", () => { submitSearch(offset + lastSearch.limit, true); $("#results").scrollIntoView({ behavior: "instant" }); });
$("#export-results").addEventListener("click", () => {
  if (!lastSearch || !workerReady || exportId) return;
  exportId = newId();
  $("#export-results").disabled = true;
  worker.postMessage({ type: "export", id: exportId, ...lastSearch });
});
$("#result-list").addEventListener("click", (event) => {
  const detailButton = event.target.closest("[data-detail]");
  if (detailButton) {
    detailVenue = resultRows[Number(detailButton.dataset.detail)];
    renderDetail(detailVenue);
    $("#venue-dialog").showModal();
    return;
  }
  if (event.target.closest("#retry-search")) { initializeWorker(); submitSearch(0); }
  if (event.target.closest("#broaden-filters")) { restoreForm({ query: $("#query").value, targets: [] }); submitSearch(0); }
});
$("#close-dialog").addEventListener("click", () => $("#venue-dialog").close());
$("#close-dialog-bottom").addEventListener("click", () => $("#venue-dialog").close());
$("#venue-dialog").addEventListener("close", () => { detailVenue = null; });
$("#venue-dialog").addEventListener("click", (event) => { if (event.target === $("#venue-dialog")) { const box = event.target.getBoundingClientRect(); if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) event.target.close(); } });
document.addEventListener("click", (event) => {
  const example = event.target.closest("[data-example]");
  if (example) {
    navigate("recommend");
    history.replaceState({ view: "recommend" }, "", "#recommend");
    $("#query").value = example.dataset.example;
    $("#query").dispatchEvent(new Event("input"));
    submitSearch(0);
    return;
  }
  const link = event.target.closest('a[href^="#"]');
  if (link && views[link.hash.slice(1)]) {
    event.preventDefault();
    const view = link.hash.slice(1);
    if (location.hash !== link.hash) history.pushState({ view }, "", link.hash);
    navigate(view);
  } else if (link && $(link.hash)) {
    event.preventDefault();
    history.pushState({ view: currentView }, "", link.hash);
    $(link.hash).scrollIntoView();
  }
});
const focusSearch = () => { navigate("catalog"); history.pushState({ view: "catalog" }, "", "#catalog"); $("#query").focus(); };
$("#quick-search").addEventListener("click", focusSearch);
document.addEventListener("keydown", (event) => {
  if (event.key === "/" && !event.ctrlKey && !event.metaKey && !["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName) && !$("#venue-dialog").open) {
    event.preventDefault();
    focusSearch();
  }
});
function followLocation() {
  const hash = location.hash.slice(1);
  const targetView = views[hash] ? hash : history.state?.view
    || Object.entries(views).find(([, config]) => config.links.some(([id]) => id === hash))?.[0] || "overview";
  if (currentView !== targetView) navigate(targetView, false);
  if (hash && !views[hash]) document.getElementById(hash)?.scrollIntoView({ behavior: "instant" });
}
window.addEventListener("popstate", followLocation);
window.addEventListener("hashchange", followLocation);
translateStatic();
initializeWorker();
followLocation();
