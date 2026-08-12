(() => {
  "use strict";

  const $ = (selector) => document.querySelector(selector);
  const LOCALE_KEY = "where-paper-go-locale";
  const stepOrder = ["llm", "vector", "graph", "search"];

  const messages = {
    zh: {
      document_title: "where paper go · 投稿目标推荐",
      skip_to_search: "跳到主题检索",
      main_navigation: "主导航",
      brand_caption: "智能选刊",
      nav_search: "主题检索",
      nav_results: "推荐结果",
      nav_system: "系统状态",
      nav_search_aria: "前往主题检索",
      nav_results_aria: "前往推荐结果",
      nav_system_aria: "前往系统状态",
      checking_pipeline: "正在检查检索链路",
      four_way_retrieval: "4 路联合检索",
      search: "检索",
      refresh_status: "刷新系统状态",
      switch_language: "切换为英文",
      hero_title: "你的论文，<br /><em>应该投哪里？</em>",
      query_label: "论文主题",
      query_label_hint: "题目、摘要或想法",
      example_topics: "示例主题",
      suggestion_distributed: "分布式训练",
      suggestion_medical: "可解释医疗 AI",
      suggestion_satellite: "卫星网络",
      start_search: "开始检索",
      searching_button: "检索中…",
      filters_aria: "筛选条件",
      scope_compact_label: "投稿范围",
      scope_edit: "调整",
      scope_constraints: "{count} 个方向",
      scope_heading: "限定投稿范围",
      reset_filters: "重置筛选",
      target_rank: "目标等级",
      multi_select: "可多选",
      record_type: "投稿类型",
      all_types: "会议 + 期刊",
      conference: "会议",
      journal: "期刊",
      research_area: "研究分类",
      optional: "可选",
      all_areas: "全部分类",
      result_limit: "返回数量",
      items: "条",
      reviewed_only: "仅已审核范围",
      official_scope: "补充官网范围",
      scope_input_aria: "添加研究方向约束",
      scope_placeholder: "方向约束，如：无线网络",
      pipeline_heading: "检索进度",
      preparing: "准备中",
      pipeline_llm_title: "LLM 意图理解",
      pipeline_llm_desc: "理解主题",
      pipeline_vector_title: "向量语义召回",
      pipeline_vector_desc: "语义召回",
      pipeline_graph_desc: "图谱关联",
      pipeline_search_title: "Search API + 重排",
      pipeline_search_desc: "证据重排",
      waiting: "等待",
      running: "进行中",
      done: "完成",
      blocked: "受阻",
      not_run: "未运行",
      unconfirmed: "未确认",
      step_llm_short: "理解主题",
      step_vector_short: "语义召回",
      step_graph_short: "图谱关联",
      step_search_short: "证据重排",
      stage_progress: "{stage} · {current}/4",
      search_complete: "检索完成",
      search_incomplete: "检索未完成",
      stage_blocked: "{stage}受阻",
      results_heading: "推荐结果",
      not_searched: "尚未检索",
      copy_summary: "复制摘要",
      empty_title: "输入主题，开始推荐",
      system_heading: "系统状态",
      index_label: "索引",
      refresh: "刷新",
      checking: "检查中",
      health_graph_title: "数据图谱",
      health_graph_initial: "投稿目标与等级",
      health_vector_title: "语义向量",
      health_vector_initial: "向量索引",
      health_graph_index: "图谱索引",
      health_api_config: "接口配置",
      footer: "where paper go · 投稿前请以官网最新信息为准",
      close_details: "关闭详情",
      request_failed: "请求失败（{status}）",
      system_status_aria: "系统状态：{label}",
      target_count_title: "{count} 个投稿实体",
      options_failed: "筛选项读取失败。",
      area_unavailable: "分类不可用",
      graph_ready: "属性图谱和等级索引可用",
      graph_missing: "缺少 venue_graph.json.gz",
      vector_fallback: "向量模型",
      vector_missing: "尚未生成向量侧车",
      lightrag_entities: "mix · {count} 个实体",
      lightrag_missing: "工作目录不存在",
      search_network: "DuckDuckGo（需外网可达）",
      not_configured: "未配置",
      config_missing: "缺少 llmapi.json",
      ready: "已就绪",
      verify: "待验证",
      needs_action: "需处理",
      system_ready: "系统就绪",
      system_partial: "部分待验证",
      system_check: "系统需检查",
      read_failed: "读取失败",
      status_retry: "无法读取当前状态，请稍后刷新。",
      status_read_failed: "系统状态读取失败",
      status_unavailable: "状态读取失败",
      options_status_unavailable: "部分状态不可用",
      status_failure_prefix: "无法读取部分系统状态：{details}",
      unknown_error: "未知错误",
      validation_query: "请先输入论文题目、摘要或研究主题。",
      validation_target: "请至少选择一个目标等级。",
      search_started: "检索已开始。",
      searching: "检索中",
      searching_title: "正在检索",
      searching_copy: "四路检索协同运行。",
      preliminary_badge: "初步结果",
      preliminary_count: "初步显示 {shown} / {total}",
      preliminary_announcement: "已返回 {shown} 条初步结果，Search API 与 LLM 正在最终重排。",
      finalizing_results: "已显示初步结果 · 最终重排中",
      preliminary_failed: "最终重排失败，当前保留初步结果。",
      preliminary_match: "初步匹配",
      elapsed_done: "{elapsed} ms 完成",
      candidate_count: "{count} 个候选",
      graph_count: "图谱 {count}",
      evidence_count: "网页证据 {count}",
      showing_count: "显示 {shown} / {total}",
      results_announcement: "检索完成，共找到 {total} 个候选，当前显示 {shown} 个。",
      no_results: "没有匹配结果",
      no_results_copy: "尝试放宽筛选或补充关键词。",
      venue_fallback: "投稿目标",
      semantic_match: "综合语义匹配",
      scope_match: "主题与收稿范围匹配。",
      best_match: "最佳匹配",
      overall_score: "综合得分",
      reason: "理由：",
      view_scope: "查看收稿范围 →",
      view_scope_aria: "查看 {venue} 的收稿范围",
      search_failed: "检索失败",
      retry_tavily: "Tavily 已配置；请检查服务器到 api.tavily.com 的 HTTPS、代理、密钥额度，程序已在代理失败时尝试直连。",
      retry_search_config: "请在 llmapi.json 的 search 节配置可达的 Tavily、Brave、Bing 或 SerpAPI，并重试。",
      retry_general: "请检查服务日志和 API 配置后重试。",
      no_reviewed_scope: "暂无已审核范围。",
      no_exclusion: "暂无明确排除边界。",
      semantic_similarity: "语义相似度",
      llm_relevance: "LLM 相关性",
      rank_and_area: "等级与分类",
      reviewed_scope: "已审核收稿范围",
      covered_topics: "覆盖主题",
      excluded_topics: "明确不匹配的方向",
      retrieval_signals: "检索信号",
      external_evidence: "外部/官网证据",
      evidence_aria: "在新窗口打开 {host} 的证据链接 {index}",
      disclaimer: "投稿前请以官网最新信息为准。",
      copy_topic: "主题：{query}",
      copied: "已复制推荐摘要。",
      clipboard_denied: "浏览器不允许访问剪贴板，请手动复制结果。",
      field_semantic_vector: "语义向量",
      field_lightrag_mix_recall: "LightRAG mix",
      field_llm_api_rerank: "LLM 重排",
      field_search_api_evidence: "Search 证据",
      field_property_graph_lexical_recall: "图谱词法",
      field_knowledge_graph_path: "图谱路径",
      field_curated_topics: "审核主题",
      field_curated_topic_tags: "受控标签",
      field_curated_scope: "收稿范围",
      field_name: "名称匹配",
    },
    en: {
      document_title: "where paper go · Venue Finder",
      skip_to_search: "Skip to topic search",
      main_navigation: "Main navigation",
      brand_caption: "Venue finder",
      nav_search: "Search",
      nav_results: "Results",
      nav_system: "System",
      nav_search_aria: "Go to topic search",
      nav_results_aria: "Go to recommendations",
      nav_system_aria: "Go to system status",
      checking_pipeline: "Checking retrieval pipeline",
      four_way_retrieval: "4-way retrieval",
      search: "Search",
      refresh_status: "Refresh system status",
      switch_language: "切换为中文",
      hero_title: "Where should<br /><em>your paper go?</em>",
      query_label: "Paper topic",
      query_label_hint: "Title, abstract, or idea",
      example_topics: "Example topics",
      suggestion_distributed: "Distributed training",
      suggestion_medical: "Explainable medical AI",
      suggestion_satellite: "Satellite networks",
      start_search: "Search",
      searching_button: "Searching…",
      filters_aria: "Search filters",
      scope_compact_label: "Venue scope",
      scope_edit: "Adjust",
      scope_constraints: "{count} constraints",
      scope_heading: "Narrow the venue scope",
      reset_filters: "Reset",
      target_rank: "Target ranks",
      multi_select: "Multiple",
      record_type: "Venue type",
      all_types: "Conferences + Journals",
      conference: "Conference",
      journal: "Journal",
      research_area: "Research area",
      optional: "Optional",
      all_areas: "All areas",
      result_limit: "Results",
      items: "items",
      reviewed_only: "Reviewed scopes only",
      official_scope: "Include official scopes",
      scope_input_aria: "Add a research-area constraint",
      scope_placeholder: "Constraint, e.g. wireless networks",
      pipeline_heading: "Retrieval progress",
      preparing: "Preparing",
      pipeline_llm_title: "LLM Intent",
      pipeline_llm_desc: "Understand topic",
      pipeline_vector_title: "Vector Recall",
      pipeline_vector_desc: "Semantic recall",
      pipeline_graph_desc: "Graph recall",
      pipeline_search_title: "Search API + Rerank",
      pipeline_search_desc: "Evidence rerank",
      waiting: "Waiting",
      running: "Running",
      done: "Done",
      blocked: "Blocked",
      not_run: "Not run",
      unconfirmed: "Unknown",
      step_llm_short: "Intent",
      step_vector_short: "Vector",
      step_graph_short: "Graph",
      step_search_short: "Rerank",
      stage_progress: "{stage} · {current}/4",
      search_complete: "Done",
      search_incomplete: "Incomplete",
      stage_blocked: "{stage} blocked",
      results_heading: "Recommendations",
      not_searched: "Not searched",
      copy_summary: "Copy summary",
      empty_title: "Enter a topic to begin",
      system_heading: "System status",
      index_label: "Index",
      refresh: "Refresh",
      checking: "Checking",
      health_graph_title: "Data graph",
      health_graph_initial: "Venues and rankings",
      health_vector_title: "Semantic vectors",
      health_vector_initial: "Vector index",
      health_graph_index: "Graph index",
      health_api_config: "API configuration",
      footer: "where paper go · Verify the latest details on the official venue site before submitting",
      close_details: "Close details",
      request_failed: "Request failed ({status})",
      system_status_aria: "System status: {label}",
      target_count_title: "{count} venues",
      options_failed: "Could not load filters.",
      area_unavailable: "Areas unavailable",
      graph_ready: "Property graph and ranking index available",
      graph_missing: "venue_graph.json.gz is missing",
      vector_fallback: "Vector model",
      vector_missing: "Vector sidecar has not been built",
      lightrag_entities: "mix · {count} entities",
      lightrag_missing: "Working directory is missing",
      search_network: "DuckDuckGo (network required)",
      not_configured: "Not configured",
      config_missing: "llmapi.json is missing",
      ready: "Ready",
      verify: "Check",
      needs_action: "Fix",
      system_ready: "Ready",
      system_partial: "Check",
      system_check: "Failed",
      read_failed: "Failed",
      status_retry: "Could not read status. Refresh and try again.",
      status_read_failed: "Status unavailable",
      status_unavailable: "Status failed",
      options_status_unavailable: "Partly unavailable",
      status_failure_prefix: "Some system status could not be loaded: {details}",
      unknown_error: "Unknown error",
      validation_query: "Enter a paper title, abstract, or research topic first.",
      validation_target: "Select at least one target rank.",
      search_started: "Search started.",
      searching: "Searching",
      searching_title: "Searching",
      searching_copy: "Four retrieval paths are running together.",
      preliminary_badge: "Preliminary",
      preliminary_count: "Preliminary {shown} / {total}",
      preliminary_announcement: "Showing {shown} preliminary results while Search API and LLM finish reranking.",
      finalizing_results: "Preliminary results shown · final rerank running",
      preliminary_failed: "Final reranking failed; preliminary results are retained.",
      preliminary_match: "Preliminary match",
      elapsed_done: "Done in {elapsed} ms",
      candidate_count: "{count} candidates",
      graph_count: "Graph {count}",
      evidence_count: "Web evidence {count}",
      showing_count: "Showing {shown} / {total}",
      results_announcement: "Search complete. Found {total} candidates and showing {shown}.",
      no_results: "No matching venues",
      no_results_copy: "Try broader filters or add keywords.",
      venue_fallback: "Venue",
      semantic_match: "Combined semantic match",
      scope_match: "The topic matches the venue scope.",
      best_match: "Best match",
      overall_score: "Overall score",
      reason: "Why:",
      view_scope: "View scope →",
      view_scope_aria: "View the scope for {venue}",
      search_failed: "Search failed",
      retry_tavily: "Tavily is configured. Check HTTPS access to api.tavily.com, proxy settings, and API quota. A direct connection is attempted if the proxy fails.",
      retry_search_config: "Configure a reachable Tavily, Brave, Bing, or SerpAPI provider in the search section of llmapi.json, then retry.",
      retry_general: "Check the service logs and API configuration, then retry.",
      no_reviewed_scope: "No reviewed scope is available.",
      no_exclusion: "No explicit exclusion is available.",
      semantic_similarity: "Semantic similarity",
      llm_relevance: "LLM relevance",
      rank_and_area: "Ranks and areas",
      reviewed_scope: "Reviewed scope",
      covered_topics: "Covered topics",
      excluded_topics: "Explicit exclusions",
      retrieval_signals: "Retrieval signals",
      external_evidence: "External / official evidence",
      evidence_aria: "Open evidence link {index} from {host} in a new window",
      disclaimer: "Verify the latest details on the official venue site before submitting.",
      copy_topic: "Topic: {query}",
      copied: "Recommendation summary copied.",
      clipboard_denied: "Clipboard access was denied. Please copy the results manually.",
      field_semantic_vector: "Semantic vector",
      field_lightrag_mix_recall: "LightRAG mix",
      field_llm_api_rerank: "LLM rerank",
      field_search_api_evidence: "Search evidence",
      field_property_graph_lexical_recall: "Graph lexical",
      field_knowledge_graph_path: "Graph path",
      field_curated_topics: "Reviewed topics",
      field_curated_topic_tags: "Curated tags",
      field_curated_scope: "Venue scope",
      field_name: "Name match",
    },
  };

  function initialLocale() {
    try {
      const saved = window.localStorage.getItem(LOCALE_KEY);
      if (saved === "zh" || saved === "en") return saved;
    } catch (_) {
    }
    return String(navigator.language || "").toLowerCase().startsWith("zh") ? "zh" : "en";
  }

  const state = {
    locale: initialLocale(),
    options: null,
    optionsFailed: false,
    health: null,
    healthFailed: false,
    systemLoading: true,
    payload: null,
    resultStatus: "idle",
    loading: false,
    timer: null,
    pipelineStatus: "hidden",
    pipelineIndex: 0,
    failedStep: null,
    elapsedMs: null,
    lastError: null,
    drawerResult: null,
    drawerTimer: null,
    lastDrawerTrigger: null,
  };

  const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  }[char]));
  const list = (value) => Array.isArray(value) ? value.filter(Boolean) : [];
  const localeTag = () => state.locale === "zh" ? "zh-CN" : "en-US";
  const t = (key, variables = {}) => {
    const template = messages[state.locale][key] ?? messages.zh[key] ?? key;
    return String(template).replace(/\{(\w+)\}/g, (_, name) => String(variables[name] ?? ""));
  };
  const formatNumber = (value, options = {}) => {
    const number = Number(value);
    return Number.isFinite(number) ? new Intl.NumberFormat(localeTag(), options).format(number) : "—";
  };
  const asNumber = (value, digits = 2) => formatNumber(value, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
  const percent = (value) => {
    const number = Number(value);
    return Number.isFinite(number)
      ? new Intl.NumberFormat(localeTag(), { style: "percent", maximumFractionDigits: 0 }).format(Math.max(0, Math.min(1, number)))
      : "—";
  };
  const recordTypeLabel = (result) => {
    if (result?.record_type === "conference") return t("conference");
    if (result?.record_type === "journal") return t("journal");
    return result?.record_type_name || result?.record_type || t("venue_fallback");
  };
  const fieldLabel = (field) => {
    const key = `field_${field}`;
    const translated = t(key);
    return translated === key ? field : translated;
  };

  const safeHttpUrl = (value) => {
    try {
      const url = new URL(String(value || ""));
      if (url.username || url.password) return null;
      return ["http:", "https:"].includes(url.protocol) ? url.href : null;
    } catch (_) {
      return null;
    }
  };

  async function request(url, options = {}) {
    const response = await fetch(url, {
      ...options,
      headers: { "Accept": "application/json", ...(options.headers || {}) },
    });
    let payload = null;
    try { payload = await response.json(); } catch (_) { payload = {}; }
    if (!response.ok) {
      const error = new Error(payload.detail || payload.error || t("request_failed", { status: response.status }));
      error.payload = payload;
      error.status = response.status;
      throw error;
    }
    return payload;
  }

  async function streamSearch(body, onEvent) {
    const response = await fetch("/api/search/stream", {
      method: "POST",
      headers: {
        "Accept": "application/x-ndjson",
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      let payload = {};
      try { payload = await response.json(); } catch (_) {
      }
      const error = new Error(payload.detail || payload.error || t("request_failed", { status: response.status }));
      error.payload = payload;
      error.status = response.status;
      throw error;
    }
    if (!response.body?.getReader) {
      let finalPayload = null;
      const text = await response.text();
      for (const line of text.split("\n").filter((item) => item.trim())) {
        const event = JSON.parse(line);
        if (event.type === "error") {
          const error = new Error(event.detail || event.error || t("unknown_error"));
          error.payload = event;
          error.status = event.status || 500;
          throw error;
        }
        onEvent(event);
        if (event.type === "complete") finalPayload = event.payload;
      }
      if (!finalPayload) throw new Error(t("search_incomplete"));
      return finalPayload;
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffer = "";
    let finalPayload = null;

    function consume(line) {
      if (!line.trim()) return;
      let event;
      try { event = JSON.parse(line); } catch (_) {
        throw new Error("Invalid streaming response");
      }
      if (event.type === "error") {
        const error = new Error(event.detail || event.error || t("unknown_error"));
        error.payload = event;
        error.status = event.status || 500;
        throw error;
      }
      onEvent(event);
      if (event.type === "complete") finalPayload = event.payload;
    }

    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      const lines = buffer.split("\n");
      buffer = lines.pop() || "";
      lines.forEach(consume);
      if (done) break;
    }
    if (buffer.trim()) consume(buffer);
    if (!finalPayload) throw new Error(t("search_incomplete"));
    return finalPayload;
  }

  function showToast(message) {
    const toast = $("#toast");
    toast.textContent = message;
    toast.classList.add("show");
    window.clearTimeout(showToast.timer);
    showToast.timer = window.setTimeout(() => toast.classList.remove("show"), 5500);
  }

  function updateLiveBadge(tone, label, steps = []) {
    const badge = $("#live-badge");
    const pulse = badge.querySelector(".status-pulse");
    badge.className = `live-badge ${tone}`;
    badge.setAttribute("aria-label", t("system_status_aria", { label }));
    pulse.className = `status-pulse ${tone}`;
    $("#live-badge-label").textContent = label;
    badge.querySelectorAll("[data-live-step]").forEach((item, index) => {
      item.classList.remove("active", "done", "warn", "error");
      if (steps[index]) item.classList.add(steps[index]);
    });
  }

  function healthItems(payload) {
    const config = payload?.config || {};
    const searchNeedsNetwork = config.search_provider === "duckduckgo" && !config.search_key_configured;
    const apiConfigured = Boolean(config.exists && config.llm_model && config.search_provider);
    return [
      { status: payload?.graph?.exists ? "ready" : "error", title: t("health_graph_title"), detail: payload?.graph?.exists ? t("graph_ready") : t("graph_missing") },
      {
        status: payload?.vectors?.exists ? "ready" : "error",
        title: t("health_vector_title"),
        detail: payload?.vectors?.exists
          ? `${config.embedding_model || payload?.lightrag?.embedding_model || t("vector_fallback")} · ${formatNumber(payload?.lightrag?.dimensions, { maximumFractionDigits: 0 })}D`
          : t("vector_missing"),
      },
      {
        status: payload?.lightrag?.exists ? "ready" : "error",
        title: "LightRAG",
        detail: payload?.lightrag?.exists ? t("lightrag_entities", { count: formatNumber(payload?.lightrag?.counts?.entities || 0) }) : t("lightrag_missing"),
      },
      {
        status: !apiConfigured ? "error" : (searchNeedsNetwork ? "warn" : "ready"),
        title: "LLM / Search",
        detail: config.exists
          ? `${config.llm_model || "LLM"} · ${searchNeedsNetwork ? t("search_network") : (config.search_provider || t("not_configured"))}`
          : t("config_missing"),
      },
    ];
  }

  function overallHealth(items) {
    if (items.some((item) => item.status === "error")) return "error";
    if (items.some((item) => item.status === "warn") || state.optionsFailed) return "warn";
    return "ready";
  }

  function overallHealthText(status) {
    if (state.optionsFailed && status !== "error") return t("options_status_unavailable");
    return status === "ready" ? t("system_ready") : (status === "warn" ? t("system_partial") : t("system_check"));
  }

  function renderSystemLive() {
    if (state.pipelineStatus !== "hidden") return;
    if (state.systemLoading) {
      updateLiveBadge("loading", t("checking"));
      return;
    }
    if (state.healthFailed || !state.health) {
      updateLiveBadge("error", t("status_unavailable"), stepOrder.map(() => "error"));
      return;
    }
    const items = healthItems(state.health);
    const overall = overallHealth(items);
    const graphStatus = [items[0].status, items[2].status].includes("error")
      ? "error"
      : ([items[0].status, items[2].status].includes("warn") ? "warn" : "ready");
    const asDot = (status) => status === "ready" ? "done" : status;
    updateLiveBadge(overall, overallHealthText(overall), [
      asDot(items[3].status),
      asDot(items[1].status),
      asDot(graphStatus),
      asDot(items[3].status),
    ]);
  }

  function renderPipeline() {
    const section = $("#signal-section");
    const status = state.pipelineStatus;
    const failedIndex = stepOrder.indexOf(state.failedStep);
    document.body.classList.toggle("pipeline-visible", status !== "hidden");
    if (status === "hidden") {
      section.hidden = true;
      section.setAttribute("aria-busy", "false");
      renderSystemLive();
      return;
    }
    section.hidden = false;
    section.setAttribute("aria-busy", String(status === "loading"));
    stepOrder.forEach((name, index) => {
      const item = document.querySelector(`[data-step="${name}"]`);
      if (!item) return;
      const label = item.querySelector(".step-state");
      item.classList.remove("active", "done", "error");
      if (status === "loading" && index < state.pipelineIndex) {
        item.classList.add("done");
        label.textContent = t("done");
      } else if (status === "loading" && index === state.pipelineIndex) {
        item.classList.add("active");
        label.textContent = t("running");
      } else if (status === "loading") {
        label.textContent = t("waiting");
      } else if (status === "done") {
        item.classList.add("done");
        label.textContent = t("done");
      } else if (status === "error" && failedIndex < 0) {
        label.textContent = t("unconfirmed");
      } else if (status === "error" && index < failedIndex) {
        item.classList.add("done");
        label.textContent = t("done");
      } else if (status === "error" && index === failedIndex) {
        item.classList.add("error");
        label.textContent = t("blocked");
      } else {
        label.textContent = t("not_run");
      }
    });
    if (status === "loading") {
      const current = Math.min(state.pipelineIndex, stepOrder.length - 1);
      updateLiveBadge(
        "loading",
        t("stage_progress", { stage: t(`step_${stepOrder[current]}_short`), current: current + 1 }),
        stepOrder.map((_, index) => index < current ? "done" : (index === current ? "active" : ""))
      );
    } else if (status === "done") {
      updateLiveBadge("ready", t("search_complete"), stepOrder.map(() => "done"));
    } else if (status === "error") {
      const steps = stepOrder.map((_, index) => {
        if (failedIndex < 0) return "";
        if (index < failedIndex) return "done";
        if (index === failedIndex) return "error";
        return "";
      });
      const label = failedIndex < 0 ? t("search_incomplete") : t("stage_blocked", { stage: t(`step_${state.failedStep}_short`) });
      updateLiveBadge("error", label, steps);
    }
  }

  function setPipeline(status, failedStep = null) {
    state.pipelineStatus = status;
    state.failedStep = failedStep;
    if (status === "loading") state.pipelineIndex = 0;
    renderPipeline();
  }

  function inferFailureStep(error) {
    const detail = String(error?.payload?.detail || error?.message || "").toLowerCase();
    if (/timeout|timed out|超时/.test(detail)) return null;
    const matches = new Set();
    if (/search api|tavily|brave|bing|serpapi|duckduckgo|网页证据|搜索接口/.test(detail)) matches.add("search");
    if (/lightrag|knowledge graph|知识图谱|属性图谱/.test(detail)) matches.add("graph");
    if (/embedding|vector|bge-m3|向量/.test(detail)) matches.add("vector");
    if (/\bllm\b|大模型|意图理解|模型接口/.test(detail)) matches.add("llm");
    return matches.size === 1 ? [...matches][0] : null;
  }

  function animatePipeline() {
    window.clearInterval(state.timer);
    setPipeline("loading");
    state.timer = window.setInterval(() => {
      state.pipelineIndex = Math.min(state.pipelineIndex + 1, stepOrder.length - 1);
      renderPipeline();
      if (state.pipelineIndex >= stepOrder.length - 1) window.clearInterval(state.timer);
    }, 2300);
  }

  function renderOptions(payload) {
    const priorInputs = [...document.querySelectorAll('input[name="target"]')];
    const selected = new Set(priorInputs.filter((input) => input.checked).map((input) => input.value));
    const currentArea = $("#area-select").value;
    const hasPriorSelectionState = priorInputs.length > 0;
    state.options = payload;
    state.optionsFailed = false;
    $("#target-list").innerHTML = list(payload.targets).map((target, index) => {
      const checked = hasPriorSelectionState ? selected.has(String(target.value)) : index === 0;
      return `<div class="target-choice">
        <input type="checkbox" id="target-${index}" name="target" value="${escapeHtml(target.value)}" ${checked ? "checked" : ""} />
        <label for="target-${index}" title="${escapeHtml(t("target_count_title", { count: formatNumber(target.count) }))}">${escapeHtml(target.value)}</label>
      </div>`;
    }).join("");
    const areaSelect = $("#area-select");
    areaSelect.disabled = false;
    areaSelect.innerHTML = `<option value="">${escapeHtml(t("all_areas"))}</option>` + list(payload.areas)
      .map((area) => `<option value="${escapeHtml(area.value)}">${escapeHtml(area.value)} (${formatNumber(area.count)})</option>`).join("");
    if ([...areaSelect.options].some((option) => option.value === currentArea)) areaSelect.value = currentArea;
    $("#venue-count").textContent = formatNumber(payload.counts?.venues || 0);
    renderScopeSummary();
  }

  function renderOptionsFailure() {
    state.options = null;
    state.optionsFailed = true;
    $("#target-list").innerHTML = `<p class="filter-error" role="status">${escapeHtml(t("options_failed"))}</p>`;
    const areaSelect = $("#area-select");
    areaSelect.innerHTML = `<option value="">${escapeHtml(t("area_unavailable"))}</option>`;
    areaSelect.disabled = true;
    $("#venue-count").textContent = "—";
    renderScopeSummary();
  }

  function renderHealth(payload) {
    state.health = payload;
    state.healthFailed = false;
    const items = healthItems(payload);
    const labels = {
      ready: { icon: "✓", text: t("ready") },
      warn: { icon: "!", text: t("verify") },
      error: { icon: "×", text: t("needs_action") },
    };
    $("#status-grid").innerHTML = items.map((item) => `
      <div class="status-card ${item.status}">
        <div class="status-card-top"><span class="status-symbol" aria-hidden="true">${labels[item.status].icon}</span><span class="status-card-state">${escapeHtml(labels[item.status].text)}</span></div>
        <b>${escapeHtml(item.title)}</b><small>${escapeHtml(item.detail)}</small>
      </div>`).join("");
    $("#status-grid").setAttribute("aria-busy", "false");
    const overall = overallHealth(items);
    $(".mini-status").innerHTML = `<span class="status-pulse ${overall}"></span><span>${escapeHtml(overallHealthText(overall))}</span>`;
    renderSystemLive();
  }

  function renderHealthFailure() {
    state.health = null;
    state.healthFailed = true;
    const titles = [t("health_graph_title"), t("health_vector_title"), "LightRAG", "LLM / Search"];
    $("#status-grid").innerHTML = titles.map((title) => `
      <div class="status-card error">
        <div class="status-card-top"><span class="status-symbol" aria-hidden="true">×</span><span class="status-card-state">${escapeHtml(t("read_failed"))}</span></div>
        <b>${escapeHtml(title)}</b><small>${escapeHtml(t("status_retry"))}</small>
      </div>`).join("");
    $(".mini-status").innerHTML = `<span class="status-pulse error"></span><span>${escapeHtml(t("status_read_failed"))}</span>`;
    renderSystemLive();
  }

  function renderHealthLoading() {
    const titles = [
      [t("health_graph_title"), t("health_graph_initial")],
      [t("health_vector_title"), t("health_vector_initial")],
      ["LightRAG", t("health_graph_index")],
      ["LLM / Search", t("health_api_config")],
    ];
    $("#status-grid").innerHTML = titles.map(([title, detail]) => `
      <div class="status-card loading">
        <div class="status-card-top"><span class="status-symbol" aria-hidden="true">◌</span><span class="status-card-state">${escapeHtml(t("checking"))}</span></div>
        <b>${escapeHtml(title)}</b><small>${escapeHtml(detail)}</small>
      </div>`).join("");
    $("#status-grid").setAttribute("aria-busy", "true");
    $(".mini-status").innerHTML = `<span class="status-pulse loading"></span><span>${escapeHtml(t("checking_pipeline"))}</span>`;
    renderSystemLive();
  }

  const selectedTargets = () => [...document.querySelectorAll('input[name="target"]:checked')].map((input) => input.value);
  const scopeValues = () => $("#scope-input").value.split(/[,，;；、]+/).map((item) => item.trim()).filter(Boolean);

  function renderScopeSummary() {
    const summary = $("#scope-summary");
    if (!summary) return;
    const targets = selectedTargets();
    const targetText = targets.length > 2
      ? `${targets.slice(0, 2).join(", ")} +${formatNumber(targets.length - 2)}`
      : (targets.join(", ") || "—");
    const type = $("#record-type")?.value || "all";
    const typeLabel = type === "conference" ? t("conference") : (type === "journal" ? t("journal") : t("all_types"));
    const parts = [targetText, typeLabel];
    const area = $("#area-select")?.value || "";
    if (area) parts.push(area);
    const constraints = scopeValues().length;
    if (constraints) parts.push(t("scope_constraints", { count: formatNumber(constraints) }));
    if ($("#reviewed-only")?.checked) parts.push(t("reviewed_only"));
    parts.push(`${formatNumber(Number($("#limit-input")?.value || 10))} ${t("items")}`);
    summary.textContent = parts.join(" · ");
  }

  function setLoading(loading) {
    state.loading = loading;
    const button = $("#search-button");
    $("#search-form").setAttribute("aria-busy", String(loading));
    button.setAttribute("aria-disabled", String(loading));
    button.querySelector(".button-label").textContent = loading ? t("searching_button") : t("start_search");
    $("#query-input").readOnly = loading;
  }

  function renderElapsed() {
    if (state.resultStatus === "loading") $("#elapsed-label").textContent = t("searching");
    else if (state.resultStatus === "partial") $("#elapsed-label").textContent = t("finalizing_results");
    else if (state.resultStatus === "done") $("#elapsed-label").textContent = t("elapsed_done", { elapsed: asNumber(state.elapsedMs, 0) });
    else if (["error", "partial_error"].includes(state.resultStatus)) $("#elapsed-label").textContent = t("search_incomplete");
    else $("#elapsed-label").textContent = t("preparing");
  }

  function renderSearchLoading() {
    $("#result-count").textContent = t("searching");
    $("#result-announcement").textContent = t("search_started");
    $("#result-list").innerHTML = `<div class="empty-state"><div class="empty-orbit"><span></span><span></span><span></span></div><h3>${escapeHtml(t("searching_title"))}</h3><p>${escapeHtml(t("searching_copy"))}</p></div>`;
    $("#result-summary").hidden = true;
    $("#copy-summary").hidden = true;
  }

  function renderPayload(payload, preliminary = false) {
    const api = payload.api_assisted_search || {};
    const light = payload.lightrag || {};
    const candidateLabel = state.locale === "zh" ? "个候选" : "candidates";
    const graphLabel = state.locale === "zh" ? "图谱" : "Graph";
    const evidenceLabel = state.locale === "zh" ? "网页证据" : "Web evidence";
    $("#result-summary").innerHTML = [
      preliminary ? `<span class="summary-pill preliminary-pill"><strong>◌</strong> ${escapeHtml(t("preliminary_badge"))}</span>` : "",
      `<span class="summary-pill"><strong>${formatNumber(payload.total ?? 0)}</strong> ${escapeHtml(candidateLabel)}</span>`,
      `<span class="summary-pill">${escapeHtml(graphLabel)} <strong>${formatNumber(light.recalled_venue_count ?? 0)}</strong></span>`,
      `<span class="summary-pill">${escapeHtml(evidenceLabel)} <strong>${formatNumber(api.search_result_count ?? 0)}</strong></span>`,
    ].filter(Boolean).join("");
    $("#result-summary").hidden = false;
    $("#result-count").textContent = t(preliminary ? "preliminary_count" : "showing_count", { shown: formatNumber(payload.displayed ?? 0), total: formatNumber(payload.total ?? 0) });
    $("#result-announcement").textContent = state.resultStatus === "partial_error"
      ? t("preliminary_failed")
      : t(preliminary ? "preliminary_announcement" : "results_announcement", { total: formatNumber(payload.total ?? 0), shown: formatNumber(payload.displayed ?? 0) });
    $("#copy-summary").hidden = preliminary;
    const results = list(payload.results);
    $("#result-list").innerHTML = results.length
      ? results.map((result, index) => renderResult(result, index, preliminary)).join("")
      : `<div class="empty-state"><h3>${escapeHtml(t("no_results"))}</h3><p>${escapeHtml(t("no_results_copy"))}</p></div>`;
  }

  function renderResult(result, index, preliminary = false) {
    const venueName = result.name || result.abbreviation || t("venue_fallback");
    const rankingTags = list(result.matched_rankings).slice(0, 3).map((item) => `<span class="meta-tag rank">${escapeHtml(item)}</span>`).join("");
    const areas = list(result.areas).slice(0, 2).map((item) => `<span class="meta-tag">${escapeHtml(item)}</span>`).join("");
    const fields = list(result.matched_fields).slice(0, 5).map((item) => `<span class="signal-tag ${String(item).includes("api") ? "api" : ""}">${escapeHtml(fieldLabel(item))}</span>`).join("");
    const reason = result.api_reason || list(result.reviewed_scopes)[0] || t("scope_match");
    const termText = list(result.matched_concepts).slice(0, 3).join(" · ") || list(result.matched_terms).slice(0, 4).join(" · ") || t("semantic_match");
    return `<article class="result-card ${index === 0 ? "top-result" : ""} ${preliminary ? "preliminary-result" : ""}" aria-labelledby="result-title-${index}">
      ${index === 0 ? `<span class="best-match"><span aria-hidden="true">${preliminary ? "◌" : "✦"}</span> ${escapeHtml(t(preliminary ? "preliminary_match" : "best_match"))}</span>` : ""}
      <div class="result-card-main"><span class="rank-number">${String(index + 1).padStart(2, "0")}</span><div class="result-content">
        <div class="result-title-row"><h3 class="result-name" id="result-title-${index}">${escapeHtml(venueName)} <span class="result-abbr">${escapeHtml(result.abbreviation || "")}</span></h3><div class="score-box" aria-label="${escapeHtml(t("overall_score"))} ${asNumber(result.score, 2)}"><span class="score-value">${asNumber(result.score, 2)}</span><span class="score-caption">${escapeHtml(t("overall_score"))}</span></div></div>
        <div class="result-meta"><span class="meta-tag">${escapeHtml(recordTypeLabel(result))}</span>${rankingTags}${areas}</div>
        <p class="result-reason"><strong>${escapeHtml(t("reason"))}</strong>${escapeHtml(reason)}</p>
        <div class="signal-row">${fields}</div>
        <div class="result-footer"><span class="match-text" title="${escapeHtml(termText)}">${escapeHtml(termText)}</span><button class="detail-button" type="button" data-result-index="${index}" aria-label="${escapeHtml(t("view_scope_aria", { venue: venueName }))}">${escapeHtml(t("view_scope"))}</button></div>
      </div></div>
    </article>`;
  }

  function errorAdvice(detail) {
    const normalized = String(detail).toLowerCase();
    if (normalized.includes("search api")) return normalized.includes("tavily") ? t("retry_tavily") : t("retry_search_config");
    return t("retry_general");
  }

  function renderError(error, announce = false) {
    const detail = error?.payload?.detail || error?.message || t("unknown_error");
    $("#result-count").textContent = t("search_failed");
    $("#result-announcement").textContent = `${t("search_failed")}: ${detail}`;
    $("#result-summary").hidden = true;
    $("#copy-summary").hidden = true;
    $("#result-list").innerHTML = `<div class="empty-state"><div class="empty-orbit"><span></span><span></span><span></span></div><h3>${escapeHtml(t("search_incomplete"))}</h3><p>${escapeHtml(detail)}</p><p class="error-advice">${escapeHtml(errorAdvice(detail))}</p></div>`;
    if (announce) showToast(detail);
  }

  function renderCurrentResults() {
    renderElapsed();
    if (state.resultStatus === "loading") renderSearchLoading();
    else if (["partial", "partial_error"].includes(state.resultStatus) && state.payload) renderPayload(state.payload, true);
    else if (state.resultStatus === "done" && state.payload) renderPayload(state.payload);
    else if (state.resultStatus === "error" && state.lastError) renderError(state.lastError, false);
  }

  function handleSearchStreamEvent(event) {
    if (event.type === "progress") {
      const index = stepOrder.indexOf(event.stage);
      if (index >= 0) {
        state.pipelineStatus = "loading";
        state.pipelineIndex = Math.min(
          index + (event.status === "done" && index < stepOrder.length - 1 ? 1 : 0),
          stepOrder.length - 1,
        );
        renderPipeline();
      }
      return;
    }
    if (event.type === "results" && event.phase === "preliminary" && event.payload) {
      state.payload = event.payload;
      state.resultStatus = "partial";
      state.elapsedMs = event.elapsed_ms;
      renderCurrentResults();
    }
  }

  async function runSearch(event) {
    event.preventDefault();
    if (state.loading) return;
    const query = $("#query-input").value.trim();
    if (!query) {
      showToast(t("validation_query"));
      $("#query-input").focus();
      return;
    }
    const targets = selectedTargets();
    if (!targets.length) {
      showToast(t("validation_target"));
      return;
    }
    const body = {
      query,
      targets,
      record_type: $("#record-type").value,
      areas: $("#area-select").value ? [$("#area-select").value] : [],
      scopes: scopeValues(),
      reviewed_scope_only: $("#reviewed-only").checked,
      match_official_scope: $("#official-scope").checked,
      limit: Number($("#limit-input").value || 10),
      locale: state.locale === "zh" ? "zh-CN" : "en",
    };
    $("#scope-panel").open = false;
    state.payload = null;
    state.lastError = null;
    state.resultStatus = "loading";
    state.elapsedMs = null;
    setLoading(true);
    window.clearInterval(state.timer);
    setPipeline("loading");
    $("#result-list").setAttribute("aria-busy", "true");
    renderCurrentResults();
    try {
      const payload = await streamSearch(body, handleSearchStreamEvent);
      state.payload = payload;
      state.resultStatus = "done";
      state.elapsedMs = payload.elapsed_ms;
      window.clearInterval(state.timer);
      setPipeline("done");
      renderCurrentResults();
    } catch (error) {
      state.lastError = error;
      state.resultStatus = state.resultStatus === "partial" ? "partial_error" : "error";
      window.clearInterval(state.timer);
      setPipeline("error", inferFailureStep(error));
      renderCurrentResults();
      showToast(error?.payload?.detail || error?.message || t("unknown_error"));
    } finally {
      setLoading(false);
      $("#result-list").setAttribute("aria-busy", "false");
    }
  }

  function renderDrawerContent(result) {
    if (!result) return;
    const venueName = result.name || result.abbreviation || t("venue_fallback");
    const scopes = list(result.reviewed_scope_entries);
    const fallbackScope = list(result.reviewed_scopes)[0] || result.official_scope || t("no_reviewed_scope");
    const scopeTopics = list(result.reviewed_scope_topics).slice(0, 20);
    const outOfScope = list(result.reviewed_scope_out_of_scope)[0] || scopes[0]?.out_of_scope || t("no_exclusion");
    const evidence = [...new Set(list(result.api_evidence_urls).concat(list(result.official_scope_candidates)).map(safeHttpUrl).filter(Boolean))];
    $("#drawer-content").innerHTML = `
      <h2 class="drawer-title" id="drawer-title">${escapeHtml(venueName)}</h2><div class="drawer-abbr">${escapeHtml(result.abbreviation || "")} · ${escapeHtml(recordTypeLabel(result))}</div>
      <div class="drawer-section"><div class="drawer-stats"><div class="drawer-stat"><small>${escapeHtml(t("overall_score"))}</small><b>${asNumber(result.score, 2)}</b></div><div class="drawer-stat"><small>${escapeHtml(t("semantic_similarity"))}</small><b>${percent(result.semantic_similarity)}</b></div><div class="drawer-stat"><small>${escapeHtml(t("llm_relevance"))}</small><b>${asNumber(result.api_relevance, 0)}</b></div><div class="drawer-stat"><small>LightRAG</small><b>${asNumber(result.lightrag_relevance, 2)}</b></div></div></div>
      <div class="drawer-section"><h4>${escapeHtml(t("rank_and_area"))}</h4><div class="drawer-list">${list(result.all_rankings || result.matched_rankings).map((item) => `<span>${escapeHtml(item)}</span>`).join("")}${list(result.areas).map((item) => `<span>${escapeHtml(item)}</span>`).join("")}</div></div>
      <div class="drawer-section"><h4>${escapeHtml(t("reviewed_scope"))}</h4><p>${escapeHtml(scopes[0]?.summary || fallbackScope)}</p></div>
      ${scopeTopics.length ? `<div class="drawer-section"><h4>${escapeHtml(t("covered_topics"))}</h4><div class="drawer-list">${scopeTopics.map((item) => `<span>${escapeHtml(item)}</span>`).join("")}</div></div>` : ""}
      <div class="drawer-section"><h4>${escapeHtml(t("excluded_topics"))}</h4><div class="drawer-list"><span class="warn">${escapeHtml(outOfScope)}</span></div></div>
      <div class="drawer-section"><h4>${escapeHtml(t("retrieval_signals"))}</h4><div class="drawer-list">${list(result.matched_fields).map((item) => `<span>${escapeHtml(fieldLabel(item))}</span>`).join("")}</div></div>
      ${evidence.length ? `<div class="drawer-section"><h4>${escapeHtml(t("external_evidence"))}</h4>${evidence.slice(0, 8).map((url, index) => `<a class="evidence-link" href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer" aria-label="${escapeHtml(t("evidence_aria", { host: new URL(url).hostname, index: index + 1 }))}">↗ ${escapeHtml(url)}</a>`).join("")}</div>` : ""}
      <div class="drawer-section"><p class="drawer-disclaimer">${escapeHtml(t("disclaimer"))}</p></div>`;
  }

  function openDrawer(result, trigger) {
    const drawer = $("#detail-drawer");
    state.drawerResult = result;
    renderDrawerContent(result);
    window.clearTimeout(state.drawerTimer);
    state.lastDrawerTrigger = trigger || document.activeElement;
    $(".app-shell").inert = true;
    $("#drawer-backdrop").hidden = false;
    drawer.hidden = false;
    drawer.inert = false;
    drawer.setAttribute("aria-hidden", "false");
    document.body.classList.add("drawer-open");
    window.requestAnimationFrame(() => {
      drawer.classList.add("open");
      window.requestAnimationFrame(() => $("#close-drawer").focus());
    });
  }

  function closeDrawer() {
    const drawer = $("#detail-drawer");
    if (!drawer.classList.contains("open")) return;
    drawer.classList.remove("open");
    document.body.classList.remove("drawer-open");
    $(".app-shell").inert = false;
    if (state.lastDrawerTrigger && typeof state.lastDrawerTrigger.focus === "function") state.lastDrawerTrigger.focus();
    state.lastDrawerTrigger = null;
    drawer.inert = true;
    drawer.setAttribute("aria-hidden", "true");
    window.clearTimeout(state.drawerTimer);
    const finishClose = () => {
      if (drawer.classList.contains("open")) return;
      $("#drawer-backdrop").hidden = true;
      drawer.hidden = true;
      state.drawerResult = null;
      window.clearTimeout(state.drawerTimer);
    };
    drawer.addEventListener("transitionend", (event) => {
      if (event.propertyName === "transform") finishClose();
    }, { once: true });
    const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    state.drawerTimer = window.setTimeout(finishClose, reduceMotion ? 0 : 360);
  }

  function trapDrawerFocus(event) {
    const drawer = $("#detail-drawer");
    if (event.key !== "Tab" || !drawer.classList.contains("open")) return;
    const focusable = [...drawer.querySelectorAll('a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])')].filter((element) => !element.hidden && element.getClientRects().length);
    if (!focusable.length) {
      event.preventDefault();
      drawer.focus();
      return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  function setActiveNav(sectionId) {
    document.querySelectorAll(".nav-item").forEach((link) => {
      const active = link.getAttribute("href") === `#${sectionId}`;
      link.classList.toggle("active", active);
      if (active) link.setAttribute("aria-current", "location");
      else link.removeAttribute("aria-current");
    });
  }

  function bindSectionNavigation() {
    const links = [...document.querySelectorAll('.nav-item[href^="#"]')];
    links.forEach((link) => link.addEventListener("click", () => setActiveNav(link.getAttribute("href").slice(1))));
    if (!("IntersectionObserver" in window)) return;
    const observer = new IntersectionObserver((entries) => {
      const visible = entries.filter((entry) => entry.isIntersecting).sort((a, b) => b.intersectionRatio - a.intersectionRatio);
      if (visible.length) setActiveNav(visible[0].target.id);
    }, { rootMargin: "-18% 0px -62% 0px", threshold: [0, .1, .25] });
    links.forEach((link) => {
      const section = document.querySelector(link.getAttribute("href"));
      if (section) observer.observe(section);
    });
  }

  function resetFilters() {
    document.querySelectorAll('input[name="target"]').forEach((input, index) => { input.checked = index === 0; });
    $("#record-type").value = "all";
    $("#area-select").value = "";
    $("#limit-input").value = "10";
    $("#reviewed-only").checked = false;
    $("#official-scope").checked = true;
    $("#scope-input").value = "";
    renderScopeSummary();
  }

  async function copySummary() {
    if (!state.payload) return;
    const lines = list(state.payload.results).map((result, index) => {
      const details = state.locale === "zh" ? `（${result.abbreviation || ""}）— ${asNumber(result.score, 2)}` : ` (${result.abbreviation || ""}) — ${asNumber(result.score, 2)}`;
      return `${index + 1}. ${result.name || t("venue_fallback")}${details}`;
    });
    try {
      await navigator.clipboard.writeText(`${t("copy_topic", { query: state.payload.query })}\n${lines.join("\n")}`);
      showToast(t("copied"));
    } catch (_) {
      showToast(t("clipboard_denied"));
    }
  }

  function applyStaticTranslations() {
    document.documentElement.lang = state.locale === "zh" ? "zh-CN" : "en";
    document.title = t("document_title");
    document.querySelectorAll("[data-i18n]").forEach((element) => { element.textContent = t(element.dataset.i18n); });
    document.querySelectorAll("[data-i18n-html]").forEach((element) => { element.innerHTML = t(element.dataset.i18nHtml); });
    [
      ["data-i18n-placeholder", "placeholder", "i18nPlaceholder"],
      ["data-i18n-aria-label", "aria-label", "i18nAriaLabel"],
      ["data-i18n-title", "title", "i18nTitle"],
    ].forEach(([attribute, target, datasetKey]) => {
      document.querySelectorAll(`[${attribute}]`).forEach((element) => { element.setAttribute(target, t(element.dataset[datasetKey])); });
    });
    document.querySelectorAll(".suggestion").forEach((button) => {
      button.dataset.query = state.locale === "zh" ? button.dataset.queryZh : button.dataset.queryEn;
    });
    const toggle = $("#language-toggle");
    $("#language-toggle-label").textContent = state.locale === "zh" ? "EN" : "中";
    toggle.title = t("switch_language");
    toggle.setAttribute("aria-label", t("switch_language"));
    $("#char-count").textContent = formatNumber($("#query-input").value.length);
    renderScopeSummary();
  }

  function setLocale(locale, persist = true) {
    state.locale = locale === "en" ? "en" : "zh";
    if (persist) {
      try { window.localStorage.setItem(LOCALE_KEY, state.locale); } catch (_) {
      }
    }
    applyStaticTranslations();
    if (state.options) renderOptions(state.options);
    else if (state.optionsFailed) renderOptionsFailure();
    if (state.systemLoading) renderHealthLoading();
    else if (state.health) renderHealth(state.health);
    else if (state.healthFailed) renderHealthFailure();
    setLoading(state.loading);
    renderPipeline();
    renderCurrentResults();
    if (state.drawerResult && $("#detail-drawer").classList.contains("open")) renderDrawerContent(state.drawerResult);
  }

  async function loadSystem() {
    state.systemLoading = true;
    renderHealthLoading();
    const [optionsResult, healthResult] = await Promise.allSettled([request("/api/options"), request("/api/health")]);
    state.systemLoading = false;
    if (optionsResult.status === "fulfilled") renderOptions(optionsResult.value);
    else renderOptionsFailure();
    if (healthResult.status === "fulfilled") renderHealth(healthResult.value);
    else renderHealthFailure();
    if (optionsResult.status === "rejected" && healthResult.status === "fulfilled") {
      $(".mini-status").innerHTML = `<span class="status-pulse warn"></span><span>${escapeHtml(t("options_failed"))}</span>`;
      renderSystemLive();
    }
    const failures = [optionsResult, healthResult].filter((result) => result.status === "rejected").map((result) => result.reason?.message || t("unknown_error"));
    if (failures.length) showToast(t("status_failure_prefix", { details: failures.join(state.locale === "zh" ? "；" : "; ") }));
    $("#status-grid").setAttribute("aria-busy", "false");
  }

  function bindEvents() {
    bindSectionNavigation();
    $("#search-form").addEventListener("submit", runSearch);
    $("#query-input").addEventListener("input", (event) => { $("#char-count").textContent = formatNumber(event.target.value.length); });
    document.querySelectorAll(".suggestion").forEach((button) => button.addEventListener("click", () => {
      $("#query-input").value = button.dataset.query || "";
      $("#query-input").dispatchEvent(new Event("input"));
      $("#query-input").focus();
    }));
    $("#language-toggle").addEventListener("click", () => setLocale(state.locale === "zh" ? "en" : "zh"));
    $("#reset-filters").addEventListener("click", resetFilters);
    $("#scope-panel").addEventListener("change", renderScopeSummary);
    $("#scope-input").addEventListener("input", renderScopeSummary);
    $("#refresh-health").addEventListener("click", loadSystem);
    $("#refresh-health-bottom").addEventListener("click", loadSystem);
    $("#copy-summary").addEventListener("click", copySummary);
    $("#close-drawer").addEventListener("click", closeDrawer);
    $("#drawer-backdrop").addEventListener("click", closeDrawer);
    $("#result-list").addEventListener("click", (event) => {
      const button = event.target.closest("[data-result-index]");
      if (button && state.payload) openDrawer(state.payload.results[Number(button.dataset.resultIndex)], button);
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") closeDrawer();
      else trapDrawerFocus(event);
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    setLocale(state.locale, false);
    bindEvents();
    loadSystem();
  });
})();
