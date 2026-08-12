#!/usr/bin/env python3
"""Enrich normalized data CSV files with official aims-and-scope information.

This script is intentionally dependency-free. It can use search provider keys
from api.json when available, fetch candidate official/publisher pages, and call
an OpenAI-compatible chat-completions API to extract concise submission-scope
information.

The existing derived 收稿方向 column is not overwritten by default. Extracted
official information is written to separate columns:

  收稿方向_官网摘取, 收稿方向_来源URL, 收稿方向_证据, 收稿方向_置信度, 收稿方向_状态, 收稿方向_更新时间
"""

from __future__ import annotations

import argparse
import contextlib
import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import html
import http.client
import json
import re
import signal
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any


from .paths import DATA_DIR, PROJECT_ROOT


ROOT = PROJECT_ROOT
_USE_ENV_PROXY = object()
DEFAULT_FILES = [
    DATA_DIR / "ccf_conferences_2026.csv",
    DATA_DIR / "th_cpl_partition_2019.csv",
    DATA_DIR / "cas_partition_2025.csv",
    DATA_DIR / "jcr_partition_2025.csv",
]

OUTPUT_COLUMNS = [
    "收稿方向_官网摘取",
    "收稿方向_来源URL",
    "收稿方向_证据",
    "收稿方向_置信度",
    "收稿方向_状态",
    "收稿方向_更新时间",
]

SCOPE_LINK_KEYWORDS = [
    "aims",
    "scope",
    "aims-and-scope",
    "about",
    "overview",
    "topics",
    "call-for-papers",
    "call for papers",
    "cfp",
    "tracks",
    "submission",
    "authors",
    "home page",
    "homepage",
    "website",
]

BAD_DOMAINS = {
    "dblp.org",
    "dblp.uni-trier.de",
    "scholar.google.com",
    "researchgate.net",
    "semanticscholar.org",
    "wikidata.org",
    "wikipedia.org",
    "askbisht.com",
    "academic-accelerator.com",
    "bioxbio.com",
    "callforpaper.org",
    "cfplist.com",
    "ccfcycle.com",
    "conferencealerts.com",
    "conferenceindex.org",
    "conferencelists.org",
    "countryofpapers.com",
    "doi.org",
    "exaly.com",
    "guide2research.com",
    "impactfactorforjournal.com",
    "impactfactor.org",
    "ivysci.com",
    "journal-database.com",
    "journalguide.com",
    "journalsinsights.com",
    "leibniz-gemeinschaft.de",
    "letpub.com",
    "linkedin.com",
    "myhuiban.com",
    "ooir.org",
    "ores.su",
    "pubscope.org",
    "resurchify.com",
    "scijournal.org",
    "scimagojr.com",
    "speechtechjobs.com",
    "sciencedirectelsevier.com",
    "jrank.net",
    "sesar.di.unimi.it",
    "clausiuspress.com",
    "typeset.io",
    "wikicfp.com",
    "x.com",
}

BAD_URL_PATTERNS = [
    "ieeexplore.ieee.org/xpl/conhome",
    "computer.org/csdl/proceedings/",
    "dl.acm.org/doi/proceedings",
    "linkedin.com/sharearticle",
]

PREFERRED_DOMAINS = {
    "acm.org",
    "aclweb.org",
    "aclanthology.org",
    "aaai.org",
    "computer.org",
    "conf.researchr.org",
    "ieee.org",
    "ieee-ras.org",
    "sigarch.org",
    "sigbed.org",
    "sigchi.org",
    "sigcomm.org",
    "sigda.org",
    "sigmetrics.org",
    "sigmod.org",
    "sigops.org",
    "sigplan.org",
    "siggraph.org",
    "usenix.org",
    "vldb.org",
    "kdd.org",
    "neurips.cc",
    "icml.cc",
    "iclr.cc",
    "thecvf.com",
    "cv-foundation.org",
    "springer.com",
    "link.springer.com",
    "elsevier.com",
    "sciencedirect.com",
    "wiley.com",
    "onlinelibrary.wiley.com",
    "nature.com",
    "science.org",
    "oup.com",
    "academic.oup.com",
    "cambridge.org",
    "tandfonline.com",
    "sagepub.com",
    "mdpi.com",
    "frontiersin.org",
    "plos.org",
    "iopscience.iop.org",
}


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str = ""


@dataclass
class PageText:
    url: str
    title: str
    text: str
    links: list[tuple[str, str]]


class TextAndLinkExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skip_depth = 0
        self.in_title = False
        self.in_anchor = False
        self.current_href = ""
        self.current_anchor_text: list[str] = []
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self.links: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg"}:
            self.skip_depth += 1
            return
        if tag == "title":
            self.in_title = True
        if tag == "a":
            attrs_dict = {key.lower(): value or "" for key, value in attrs}
            self.in_anchor = True
            self.current_href = attrs_dict.get("href", "")
            self.current_anchor_text = []
        if tag in {"p", "div", "section", "article", "br", "li", "tr", "h1", "h2", "h3", "h4"}:
            self.text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg"} and self.skip_depth:
            self.skip_depth -= 1
            return
        if tag == "title":
            self.in_title = False
        if tag == "a" and self.in_anchor:
            text = normalize_space(" ".join(self.current_anchor_text))
            if self.current_href:
                self.links.append((self.current_href, text))
            self.in_anchor = False
            self.current_href = ""
            self.current_anchor_text = []

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        value = data.strip()
        if not value:
            return
        if self.in_title:
            self.title_parts.append(value)
        if self.in_anchor:
            self.current_anchor_text.append(value)
        self.text_parts.append(value)

    @property
    def title(self) -> str:
        return normalize_space(" ".join(self.title_parts))

    @property
    def text(self) -> str:
        return normalize_space("\n".join(self.text_parts))


def normalize_space(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@contextlib.contextmanager
def hard_timeout(seconds: int):
    if (
        seconds <= 0
        or not hasattr(signal, "SIGALRM")
        or threading.current_thread() is not threading.main_thread()
    ):
        yield
        return

    def raise_timeout(_signum: int, _frame: object) -> None:
        raise TimeoutError(f"network request exceeded {seconds}s hard timeout")

    old_handler = signal.getsignal(signal.SIGALRM)
    old_timer = signal.setitimer(signal.ITIMER_REAL, 0)
    signal.signal(signal.SIGALRM, raise_timeout)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)
        if old_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, old_timer[0], old_timer[1])


def cache_path(cache_dir: Path, kind: str, key: str) -> Path:
    return cache_dir / kind / f"{sha256_text(key)}.json"


def read_json(path: Path) -> Any | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_api_config(path: Path | None) -> dict[str, Any]:
    if path is None:
        for candidate in [ROOT / "api.json", ROOT / "llmapi.json"]:
            if candidate.exists() and candidate.stat().st_size:
                path = candidate
                break
    if path is None or not path.exists() or not path.stat().st_size:
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid API config JSON: {path}: {exc}") from exc


def llm_config(config: dict[str, Any]) -> dict[str, Any]:
    return config.get("llm", config)


def search_config(config: dict[str, Any]) -> dict[str, Any]:
    return config.get("search", {})


def api_headers(config: dict[str, Any]) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "scope-enricher/1.0",
    }
    for key, value in config.get("headers", {}).items():
        headers[str(key)] = str(value)
    api_key = config.get("api_key") or config.get("key")
    if api_key and "Authorization" not in headers:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def http_request(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: int = 30,
    max_bytes: int = 1_000_000,
    proxy: str | None | object = _USE_ENV_PROXY,
) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers=headers
        or {
            "User-Agent": "Mozilla/5.0 scope-enricher/1.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Encoding": "identity",
        },
    )
    opener = None
    if proxy is not _USE_ENV_PROXY:
        if proxy is None or str(proxy).strip().lower() in {"", "direct", "none", "off"}:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        else:
            proxy_url = str(proxy).strip()
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
            )
    open_url = opener.open if opener is not None else urllib.request.urlopen
    with hard_timeout(max(1, int(timeout) + 2)):
        with open_url(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 200))
            response_headers = {key.lower(): value for key, value in response.headers.items()}
            content = response.read(max_bytes + 1)
    return status, response_headers, content[:max_bytes]


def decode_bytes(content: bytes, headers: dict[str, str]) -> str:
    content_type = headers.get("content-type", "")
    match = re.search(r"charset=([\w.-]+)", content_type, flags=re.I)
    encodings = [match.group(1)] if match else []
    encodings.extend(["utf-8", "latin-1"])
    for encoding in encodings:
        try:
            return content.decode(encoding, errors="replace")
        except LookupError:
            continue
    return content.decode("utf-8", errors="replace")


def parse_html_page(url: str, html_text: str) -> PageText:
    parser = TextAndLinkExtractor()
    parser.feed(html_text)
    links = []
    for href, text in parser.links:
        resolved = urllib.parse.urljoin(url, href)
        if resolved.startswith(("http://", "https://")):
            links.append((resolved, text))
    return PageText(url=url, title=parser.title, text=parser.text, links=links)


def fetch_page(url: str, cache_dir: Path, timeout: int, max_bytes: int, use_cache: bool = True) -> PageText | None:
    key = f"page:{url}"
    path = cache_path(cache_dir, "page", key)
    if use_cache:
        cached = read_json(path)
        if cached:
            return PageText(
                url=cached["url"],
                title=cached.get("title", ""),
                text=cached.get("text", ""),
                links=[tuple(link) for link in cached.get("links", [])],
            )

    try:
        _status, headers, content = http_request(url, timeout=timeout, max_bytes=max_bytes)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError, http.client.HTTPException):
        return None
    content_type = headers.get("content-type", "").lower()
    if "pdf" in content_type or url.lower().endswith(".pdf"):
        return None
    html_text = decode_bytes(content, headers)
    page = parse_html_page(url, html_text)
    write_json(
        path,
        {
            "url": page.url,
            "title": page.title,
            "text": page.text,
            "links": page.links[:200],
            "cached_at": now_iso(),
        },
    )
    return page


def domain_of(url: str) -> str:
    host = urllib.parse.urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def is_bad_search_result(url: str) -> bool:
    target = url.lower()
    if any(pattern in target for pattern in BAD_URL_PATTERNS):
        return True
    domain = domain_of(url)
    return any(domain == bad or domain.endswith("." + bad) for bad in BAD_DOMAINS)


def is_preferred_domain(url: str) -> bool:
    domain = domain_of(url)
    return any(domain == preferred or domain.endswith("." + preferred) for preferred in PREFERRED_DOMAINS)


def is_dblp_domain(url: str) -> bool:
    domain = domain_of(url)
    return domain in {"dblp.org", "dblp.uni-trier.de", "dblp2.uni-trier.de"} or domain.endswith(".dblp.org")


def compact_issn(value: str | None) -> str:
    return re.sub(r"[^0-9x]", "", normalize_space(value).lower())


def page_for_source_url(source_url: str, pages: list[PageText]) -> PageText | None:
    if not source_url:
        return None
    source_clean = source_url.rstrip("/")
    for page in pages:
        if page.url.rstrip("/") == source_clean:
            return page
    source_domain = domain_of(source_url)
    for page in pages:
        if domain_of(page.url) == source_domain:
            return page
    return None


def journal_issn_matches_page(row: dict[str, str], page: PageText | None) -> bool:
    if page is None:
        return False
    haystack = compact_issn(f"{page.title} {page.url} {page.text}")
    if not haystack:
        return False
    for raw_issn in [row.get("issn", ""), row.get("eissn", "")]:
        issn = compact_issn(raw_issn)
        if issn and issn in haystack:
            return True
    return False


def journal_homepage_urls(row: dict[str, str], cache_dir: Path, timeout: int) -> list[str]:
    if row.get("record_type") != "journal":
        return []
    urls: list[str] = []
    seen_issns = set()
    for raw_issn in [row.get("issn", ""), row.get("eissn", "")]:
        issn = normalize_space(raw_issn)
        if not issn or issn in seen_issns:
            continue
        seen_issns.add(issn)
        key = f"openalex-source-issn:{issn}"
        path = cache_path(cache_dir, "journal_homepage", key)
        cached = read_json(path)
        if cached and not cached.get("not_found") and not normalize_space(cached.get("homepage_url", "")):
            cached = None
        if cached is None:
            endpoint = "https://api.openalex.org/sources/issn:" + urllib.parse.quote(issn) + "?" + urllib.parse.urlencode(
                {"mailto": "scope-enricher@example.com"}
            )
            data: dict[str, Any] = {}
            should_cache = False
            try:
                _status, _headers, content = http_request(
                    endpoint,
                    headers={
                        "Accept": "application/json",
                        "User-Agent": "scope-enricher/1.0 (mailto:scope-enricher@example.com)",
                    },
                    timeout=timeout,
                    max_bytes=500_000,
                )
                data = json.loads(content.decode("utf-8"))
                should_cache = True
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    data = {"not_found": True}
                    should_cache = True
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError, OSError, http.client.HTTPException):
                data = {}
            cached = {
                "issn": issn,
                "homepage_url": normalize_space(data.get("homepage_url", "")) if isinstance(data, dict) else "",
                "display_name": normalize_space(data.get("display_name", "")) if isinstance(data, dict) else "",
                "host_organization_name": normalize_space(data.get("host_organization_name", "")) if isinstance(data, dict) else "",
                "not_found": bool(data.get("not_found")) if isinstance(data, dict) else False,
                "cached_at": now_iso(),
            }
            if should_cache:
                write_json(path, cached)
        homepage = normalize_space(cached.get("homepage_url", ""))
        if homepage and not is_bad_search_result(homepage) and homepage not in urls:
            urls.append(homepage)
    return urls


def conference_abbreviation_for_search(value: str) -> str:
    original = normalize_space(value)
    cleaned = re.sub(r"[（(].*?[）)]", "", original).strip()
    return cleaned or original


def search_queries_for(row: dict[str, str]) -> list[str]:
    name = row.get("name", "")
    abbreviation = conference_abbreviation_for_search(row.get("abbreviation", ""))
    record_type = row.get("record_type", "")
    if record_type == "conference":
        years = []
        for year in [row.get("version_year", ""), str(datetime.now().year), str(datetime.now().year + 1)]:
            if year and year not in years:
                years.append(year)
        year_queries = []
        for year in years:
            year_queries.extend(
                [
                    normalize_space(f'"{abbreviation}" {year} call for papers topics'),
                    normalize_space(f'"{name}" {year} call for papers'),
                ]
            )
        return [
            *year_queries,
            normalize_space(f'"{name}" {abbreviation} official website scope topics call for papers'),
            normalize_space(f'"{name}" {abbreviation} call for papers'),
            normalize_space(f'"{abbreviation}" conference call for papers topics'),
        ]
    queries = []
    if row.get("issn"):
        queries.append(normalize_space(f'"{row["issn"]}" journal aims and scope'))
    if row.get("eissn"):
        queries.append(normalize_space(f'"{row["eissn"]}" journal aims and scope'))
    queries.extend(
        [
            normalize_space(f'"{name}" journal aims and scope official'),
            normalize_space(f'"{name}" aims and scope'),
            normalize_space(f'"{name}" publisher journal scope'),
        ]
    )
    return queries


def search_query_for(row: dict[str, str]) -> str:
    return search_queries_for(row)[0]


def parse_duckduckgo_results(html_text: str) -> list[SearchResult]:
    results: list[SearchResult] = []
    for match in re.finditer(
        r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
        html_text,
        flags=re.I | re.S,
    ):
        href = html.unescape(match.group(1))
        title = normalize_space(re.sub(r"<[^>]+>", " ", html.unescape(match.group(2))))
        parsed = urllib.parse.urlparse(href)
        query = urllib.parse.parse_qs(parsed.query)
        if "uddg" in query:
            href = query["uddg"][0]
        results.append(SearchResult(title=title, url=href))
    for match in re.finditer(r'<a[^>]+href="([^"]*duckduckgo\.com/l/\?[^"]+)"[^>]*>(.*?)</a>', html_text, flags=re.I | re.S):
        href = html.unescape(match.group(1))
        title = normalize_space(re.sub(r"<[^>]+>", " ", html.unescape(match.group(2))))
        parsed = urllib.parse.urlparse(href)
        query = urllib.parse.parse_qs(parsed.query)
        if "uddg" in query and title:
            results.append(SearchResult(title=title, url=query["uddg"][0]))
    deduped = []
    seen = set()
    for result in results:
        if result.url in seen:
            continue
        seen.add(result.url)
        deduped.append(result)
    return deduped


def _llm_native_search(
    query: str,
    config: dict[str, Any],
    limit: int,
    timeout: int,
) -> list[SearchResult]:
    """Use an OpenAI-compatible gateway's native web-search switch.

    This is intentionally a search adapter, not a generic LLM fallback.  The
    request explicitly enables the provider's search capability and accepts
    only structured URLs returned by the gateway (or its URL annotations).
    Plain prose without URLs is treated as an empty search response.
    """

    llm = dict(config.get("_llm_config") or config)
    base_url = str(
        llm.get("base_url") or llm.get("api_base") or llm.get("endpoint") or ""
    ).strip()
    model = str(config.get("model") or llm.get("model") or "").strip()
    if not base_url or not model:
        raise ValueError("llm_native search requires LLM endpoint and model")
    endpoint = str(llm.get("chat_completions_url") or "").strip()
    if not endpoint:
        endpoint = base_url.rstrip("/") + "/chat/completions"
    payload: dict[str, Any] = {
        **dict(llm.get("extra_body") or {}),
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a strict web-search adapter. Use the provider's native web "
                    "search capability. Never invent, infer, or autocomplete URLs. Return "
                    "JSON only with results: an array of objects containing title, url, "
                    "and snippet. Return an empty array when no real search result exists."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Search query: {query}\n"
                    f"Return at most {max(1, int(limit))} results, prioritizing official "
                    "CFP, aims-and-scope, publisher, or scholarly pages."
                ),
            },
        ],
        "temperature": 0,
        "max_tokens": int(config.get("max_tokens") or llm.get("max_tokens") or 900),
        "enable_search": True,
    }
    _status, _headers, content = http_request(
        endpoint,
        method="POST",
        headers=api_headers(llm),
        body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        timeout=timeout,
        max_bytes=4_000_000,
    )
    response = json.loads(content.decode("utf-8"))
    message = response.get("choices", [{}])[0].get("message", {})
    annotations = message.get("annotations") or []
    raw_content = message.get("content", "")
    if isinstance(raw_content, list):
        raw_content = "".join(
            str(block.get("text", "")) if isinstance(block, dict) else str(block)
            for block in raw_content
        )
    parsed: dict[str, Any] = {}
    if isinstance(raw_content, str) and raw_content.strip():
        try:
            parsed = extract_json_object(raw_content)
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = {}
    # A chat-completions response without provider URL citations is ordinary
    # model text, even when it contains a JSON-looking list of links.  Do not
    # promote those model-generated links to Search API evidence.
    raw_results = parsed.get("results") if isinstance(parsed, dict) else None
    if raw_results is None and isinstance(parsed, dict):
        raw_results = parsed.get("search_results")
    if not annotations:
        return []
    if not isinstance(raw_results, list):
        raw_results = []
    results: list[SearchResult] = []
    seen_urls: set[str] = set()

    def add_result(title: Any, url: Any, snippet: Any) -> None:
        normalized_url = str(url or "").strip()
        parsed_url = urllib.parse.urlparse(normalized_url)
        if (
            parsed_url.scheme not in {"http", "https"}
            or not parsed_url.netloc
            or normalized_url in seen_urls
            or is_bad_search_result(normalized_url)
        ):
            return
        seen_urls.add(normalized_url)
        results.append(
            SearchResult(
                title=normalize_space(str(title or query))[:300],
                url=normalized_url,
                snippet=normalize_space(str(snippet or ""))[:700],
            )
        )

    for item in raw_results:
        if isinstance(item, str):
            add_result(query, item, "")
        elif isinstance(item, dict):
            add_result(item.get("title"), item.get("url") or item.get("link"), item.get("snippet") or item.get("description"))
    for annotation in annotations:
        if not isinstance(annotation, dict):
            continue
        citation = annotation.get("url_citation") or annotation.get("urlCitation") or annotation
        if isinstance(citation, dict):
            add_result(citation.get("title"), citation.get("url"), citation.get("snippet") or citation.get("text"))
    return results[:limit]


def search_web(
    query: str,
    config: dict[str, Any],
    cache_dir: Path,
    timeout: int,
    limit: int,
    *,
    raise_on_error: bool = False,
) -> list[SearchResult]:
    provider = (config.get("provider") or "duckduckgo").lower()
    cache_options = {
        name: config.get(name)
        for name in (
            "endpoint",
            "search_depth",
            "topic",
            "max_results",
            "include_answer",
            "include_raw_content",
        )
        if name in config
    }
    key = f"search:{provider}:{query}:{limit}:{json.dumps(cache_options, sort_keys=True, ensure_ascii=False)}"
    path = cache_path(cache_dir, "search", key)
    cached = read_json(path)
    if cached:
        cached_results = [
            SearchResult(**item)
            for item in cached.get("results", [])
            if item.get("url") and not is_bad_search_result(item.get("url", ""))
        ]
        if cached_results:
            return cached_results

    results: list[SearchResult] = []
    try:
        if provider == "llm_native":
            results = _llm_native_search(query, config, limit, timeout)
        elif provider == "brave":
            token = config["api_key"]
            url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode(
                {"q": query, "count": limit}
            )
            _status, _headers, content = http_request(
                url,
                headers={
                    "Accept": "application/json",
                    "X-Subscription-Token": token,
                    "User-Agent": "scope-enricher/1.0",
                },
                timeout=timeout,
            )
            data = json.loads(content.decode("utf-8"))
            for item in data.get("web", {}).get("results", [])[:limit]:
                results.append(
                    SearchResult(
                        title=normalize_space(item.get("title", "")),
                        url=item.get("url", ""),
                        snippet=normalize_space(item.get("description", "")),
                    )
                )
        elif provider == "bing":
            token = config["api_key"]
            url = "https://api.bing.microsoft.com/v7.0/search?" + urllib.parse.urlencode(
                {"q": query, "count": limit}
            )
            _status, _headers, content = http_request(
                url,
                headers={
                    "Accept": "application/json",
                    "Ocp-Apim-Subscription-Key": token,
                    "User-Agent": "scope-enricher/1.0",
                },
                timeout=timeout,
            )
            data = json.loads(content.decode("utf-8"))
            for item in data.get("webPages", {}).get("value", [])[:limit]:
                results.append(
                    SearchResult(
                        title=normalize_space(item.get("name", "")),
                        url=item.get("url", ""),
                        snippet=normalize_space(item.get("snippet", "")),
                    )
                )
        elif provider == "serpapi":
            url = "https://serpapi.com/search.json?" + urllib.parse.urlencode(
                {"engine": "google", "q": query, "api_key": config["api_key"], "num": limit}
            )
            _status, _headers, content = http_request(url, timeout=timeout)
            data = json.loads(content.decode("utf-8"))
            for item in data.get("organic_results", [])[:limit]:
                results.append(
                    SearchResult(
                        title=normalize_space(item.get("title", "")),
                        url=item.get("link", ""),
                        snippet=normalize_space(item.get("snippet", "")),
                    )
                )
        elif provider == "tavily":
            endpoint = config.get("endpoint", "https://api.tavily.com/search")
            api_key = str(config.get("api_key") or config.get("key") or "").strip()
            if not api_key:
                raise ValueError("tavily search requires api_key")
            try:
                configured_max_results = int(config.get("max_results", limit))
                max_results = max(1, min(20, limit, configured_max_results))
            except (TypeError, ValueError) as exc:
                raise ValueError("tavily max_results must be an integer in [1, 20]") from exc
            search_depth = str(config.get("search_depth") or "advanced").strip().lower()
            if search_depth not in {"advanced", "basic", "fast", "ultra-fast"}:
                search_depth = "advanced"
            topic = str(config.get("topic") or "general").strip().lower()
            if topic not in {"general", "news", "finance"}:
                topic = "general"
            payload = json.dumps(
                {
                    "query": query,
                    "search_depth": search_depth,
                    "max_results": max_results,
                    "topic": topic,
                    "include_answer": config.get("include_answer", False),
                    "include_raw_content": config.get("include_raw_content", False),
                }
            ).encode("utf-8")
            request_kwargs: dict[str, Any] = {
                "method": "POST",
                "headers": {
                    "Accept": "application/json",
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "User-Agent": "scope-enricher/1.0",
                },
                "body": payload,
                "timeout": timeout,
            }
            proxy_setting: str | None | object = _USE_ENV_PROXY
            if "proxy" in config:
                configured_proxy = config.get("proxy")
                if configured_proxy is None or str(configured_proxy).strip().lower() in {
                    "",
                    "direct",
                    "none",
                    "off",
                }:
                    proxy_setting = None
                elif str(configured_proxy).strip().lower() not in {"auto", "env"}:
                    proxy_setting = str(configured_proxy).strip()
            if proxy_setting is not _USE_ENV_PROXY:
                request_kwargs["proxy"] = proxy_setting
            try:
                _status, _headers, content = http_request(endpoint, **request_kwargs)
            except urllib.error.HTTPError:
                raise
            except (
                urllib.error.URLError,
                TimeoutError,
                OSError,
                http.client.HTTPException,
            ):
                direct_fallback = config.get("direct_fallback", True)
                if (
                    isinstance(direct_fallback, str)
                    and direct_fallback.strip().lower() in {"0", "false", "no", "off"}
                ) or direct_fallback is False or proxy_setting is None:
                    raise
                request_kwargs["proxy"] = None
                _status, _headers, content = http_request(endpoint, **request_kwargs)
            data = json.loads(content.decode("utf-8"))
            for item in data.get("results", [])[:limit]:
                results.append(
                    SearchResult(
                        title=normalize_space(item.get("title", "")),
                        url=item.get("url", ""),
                        snippet=normalize_space(item.get("content", "")),
                    )
                )
        else:
            url = "https://lite.duckduckgo.com/lite/?" + urllib.parse.urlencode({"q": query})
            _status, headers, content = http_request(url, timeout=timeout)
            results = parse_duckduckgo_results(decode_bytes(content, headers))[:limit]
    except urllib.error.HTTPError as exc:
        if raise_on_error:
            raise RuntimeError(
                f"search provider {provider} request failed: HTTP {exc.code}"
            ) from exc
        results = []
    except (
        KeyError,
        urllib.error.URLError,
        TimeoutError,
        json.JSONDecodeError,
        ValueError,
        OSError,
        http.client.HTTPException,
    ) as exc:
        if raise_on_error:
            reason = getattr(exc, "reason", None)
            reason_text = str(reason or exc).strip().replace("\n", " ")[:240]
            raise RuntimeError(
                f"search provider {provider} request failed: {type(exc).__name__}"
                + (f" ({reason_text})" if reason_text else "")
            ) from exc
        results = []

    results = [result for result in results if result.url and not is_bad_search_result(result.url)]
    write_json(
        path,
        {"provider": provider, "query": query, "results": [result.__dict__ for result in results], "cached_at": now_iso()},
    )
    return results


def score_link(url: str, text: str) -> int:
    target = f"{url} {text}".lower()
    score = 0
    for keyword in SCOPE_LINK_KEYWORDS:
        if keyword in target:
            score += 4
    if "aims" in target and "scope" in target:
        score += 8
    if any(part in target for part in ["/journal/", "/journals/", "/conference/", "/conf/"]):
        score += 1
    if is_bad_search_result(url):
        score -= 10
    domain = domain_of(url)
    if is_preferred_domain(url):
        score += 5
    return score


def candidate_pages(
    row: dict[str, str],
    search_results: list[SearchResult],
    cache_dir: Path,
    timeout: int,
    max_bytes: int,
    max_pages: int,
    use_journal_homepage_lookup: bool,
) -> list[PageText]:
    urls: list[tuple[str, bool]] = []
    row_url = normalize_space(row.get("url", ""))
    if row_url and (not is_bad_search_result(row_url) or is_dblp_domain(row_url)):
        urls.append((row_url, not is_bad_search_result(row_url)))
    if use_journal_homepage_lookup:
        urls.extend((url, True) for url in journal_homepage_urls(row, cache_dir, timeout))
    urls.extend((result.url, True) for result in search_results)

    seen = set()
    queued_links = set()
    pages: list[PageText] = []
    link_candidates: list[tuple[int, str, str]] = []

    def add_link_candidates(page: PageText) -> None:
        for link_url, link_text in page.links:
            if link_url in seen or link_url in queued_links:
                continue
            score = score_link(link_url, link_text)
            if score > 0:
                queued_links.add(link_url)
                link_candidates.append((score, link_url, link_text))

    for url, use_as_candidate in urls:
        if url in seen:
            continue
        seen.add(url)
        page = fetch_page(url, cache_dir, timeout=timeout, max_bytes=max_bytes)
        if not page:
            continue
        if use_as_candidate:
            pages.append(page)
        add_link_candidates(page)

    while len(pages) < max_pages and link_candidates:
        _score, url, _text = sorted(link_candidates, reverse=True).pop(0)
        link_candidates = [candidate for candidate in link_candidates if candidate[1] != url]
        if len(pages) >= max_pages:
            break
        if url in seen:
            continue
        seen.add(url)
        page = fetch_page(url, cache_dir, timeout=timeout, max_bytes=max_bytes)
        if page:
            pages.append(page)
            add_link_candidates(page)

    pages.sort(key=lambda page: score_link(page.url, page.title), reverse=True)
    return pages[:max_pages]


def trim_text(text: str, max_chars: int) -> str:
    text = normalize_space(text)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + " ..."


def build_llm_prompt(row: dict[str, str], pages: list[PageText], max_chars_per_page: int) -> list[dict[str, str]]:
    page_blocks = []
    for index, page in enumerate(pages, start=1):
        page_blocks.append(
            "\n".join(
                [
                    f"[Page {index}]",
                    f"URL: {page.url}",
                    f"Title: {page.title}",
                    f"Text: {trim_text(page.text, max_chars_per_page)}",
                ]
            )
        )
    system = (
        "You extract publication/conference submission scope from official or publisher pages. "
        "Return strict JSON only. Do not invent information. Prefer Chinese output for summaries."
    )
    user = f"""
Record:
- dataset: {row.get('dataset', '')}
- record_type: {row.get('record_type', '')}
- name: {row.get('name', '')}
- abbreviation: {row.get('abbreviation', '')}
- issn: {row.get('issn', '')}
- eissn: {row.get('eissn', '')}
- existing_classification: {row.get('收稿方向', '')}

Candidate page text:
{chr(10).join(page_blocks)}

Task:
Identify whether the pages contain official aims/scope/topics/call-for-papers information for this record.
Return JSON with these keys:
- is_relevant: boolean
- scope_summary: concise Chinese summary of accepted topics/scope, preferably 30-120 Chinese characters
- scope_keywords: array of 3-12 concise Chinese or English topic keywords
- source_url: the best URL used
- evidence: one short evidence phrase from the page, max 80 characters
- confidence: one of high, medium, low
- reason: short reason if not relevant or low confidence
"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user.strip()}]


def extract_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return json.loads(text[start : end + 1])
    raise ValueError("LLM response did not contain a JSON object")


def call_llm(
    row: dict[str, str],
    pages: list[PageText],
    config: dict[str, Any],
    cache_dir: Path,
    timeout: int,
    max_chars_per_page: int,
) -> dict[str, Any]:
    llm = llm_config(config)
    base_url = llm.get("base_url") or llm.get("api_base") or llm.get("endpoint")
    model = llm.get("model")
    if not base_url or not model:
        return {
            "is_relevant": False,
            "scope_summary": "",
            "scope_keywords": [],
            "source_url": pages[0].url if pages else "",
            "evidence": "",
            "confidence": "low",
            "reason": "missing_api_config",
        }
    endpoint = llm.get("chat_completions_url")
    if not endpoint:
        endpoint = base_url.rstrip("/") + "/chat/completions"
    messages = build_llm_prompt(row, pages, max_chars_per_page=max_chars_per_page)
    payload = {
        "model": model,
        "messages": messages,
        "temperature": llm.get("temperature", 0),
    }
    if llm.get("json_mode"):
        payload["response_format"] = {"type": "json_object"}
    cache_key = json.dumps({"endpoint": endpoint, "model": model, "messages": messages}, ensure_ascii=False)
    path = cache_path(cache_dir, "llm", cache_key)
    cached = read_json(path)
    if cached:
        return cached["result"]
    _status, _headers, content = http_request(
        endpoint,
        method="POST",
        headers=api_headers(llm),
        body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        timeout=int(llm.get("timeout", timeout)),
        max_bytes=2_000_000,
    )
    data = json.loads(content.decode("utf-8"))
    message = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    result = extract_json_object(message)
    write_json(path, {"result": result, "cached_at": now_iso()})
    return result


def ensure_output_columns(fieldnames: list[str]) -> list[str]:
    fields = list(fieldnames)
    for column in OUTPUT_COLUMNS:
        if column not in fields:
            fields.append(column)
    return fields


def row_identity(row: dict[str, str]) -> str:
    parts = [
        row.get("dataset", ""),
        row.get("version_year", ""),
        row.get("record_type", ""),
        row.get("name", ""),
        row.get("abbreviation", ""),
        row.get("issn", ""),
        row.get("eissn", ""),
    ]
    return "|".join(normalize_space(part).lower() for part in parts)


def backup_path(path: Path) -> Path:
    backup_dir = path.parent / "_backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    return backup_dir / f"{path.name}.bak-{datetime.now().strftime('%Y%m%d%H%M%S')}"


def write_csv_file(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    tmp_path = path.with_name(f".{path.name}.tmp")
    with tmp_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    tmp_path.replace(path)


def select_rows(
    rows: list[dict[str, str]],
    *,
    dataset: str | None,
    record_type: str | None,
    overwrite: bool,
    retry_failed: bool,
    row_numbers: set[int] | None,
) -> list[int]:
    selected = []
    for index, row in enumerate(rows):
        if row_numbers and index + 1 not in row_numbers:
            continue
        if dataset and row.get("dataset") != dataset:
            continue
        if record_type and row.get("record_type") != record_type:
            continue
        if not overwrite:
            status = normalize_space(row.get("收稿方向_状态"))
            extracted_scope = normalize_space(row.get("收稿方向_官网摘取"))
            source_url = normalize_space(row.get("收稿方向_来源URL"))
            has_bad_source = bool(source_url and is_bad_search_result(source_url))
            if retry_failed:
                if not has_bad_source and (status == "ok" or extracted_scope):
                    continue
            elif extracted_scope or status:
                continue
        selected.append(index)
    return selected


def update_row_from_result(row: dict[str, str], result: dict[str, Any], status: str, replace_scope: bool) -> None:
    keywords = result.get("scope_keywords", [])
    if isinstance(keywords, list):
        keyword_text = "；".join(normalize_space(str(item)) for item in keywords if normalize_space(str(item)))
    else:
        keyword_text = normalize_space(str(keywords))
    summary = normalize_space(str(result.get("scope_summary", "")))
    if keyword_text and keyword_text not in summary:
        summary = f"{summary}；关键词：{keyword_text}" if summary else f"关键词：{keyword_text}"
    row["收稿方向_官网摘取"] = summary
    row["收稿方向_来源URL"] = normalize_space(str(result.get("source_url", "")))
    row["收稿方向_证据"] = normalize_space(str(result.get("evidence", "")))[:120]
    row["收稿方向_置信度"] = normalize_space(str(result.get("confidence", "")))
    row["收稿方向_状态"] = status
    row["收稿方向_更新时间"] = now_iso()
    if replace_scope and summary and status == "ok":
        row["收稿方向"] = summary


def enrich_row(
    index: int,
    row: dict[str, str],
    args: argparse.Namespace,
    config: dict[str, Any],
    search_conf: dict[str, Any],
) -> tuple[int, str, dict[str, Any] | None, str]:
    working_row = dict(row)
    try:
        results = []
        seen_urls = set()
        queries = search_queries_for(working_row)
        if args.max_search_queries is not None:
            queries = queries[: args.max_search_queries]
        for query in queries:
            for result in search_web(query, search_conf, args.cache_dir, args.timeout, args.search_results):
                if result.url in seen_urls:
                    continue
                seen_urls.add(result.url)
                results.append(result)
            if len(results) >= args.search_results:
                break

        pages = candidate_pages(
            working_row,
            results,
            args.cache_dir,
            timeout=args.timeout,
            max_bytes=args.max_html_bytes,
            max_pages=args.max_pages,
            use_journal_homepage_lookup=not args.skip_journal_homepage_lookup,
        )
        if not pages:
            return index, "no_candidate_pages", {"reason": "no_candidate_pages"}, ""

        result = call_llm(
            working_row,
            pages,
            config,
            args.cache_dir,
            timeout=args.timeout,
            max_chars_per_page=args.max_chars_per_page,
        )
        status = "ok"
        if not result.get("is_relevant", False):
            reason = normalize_space(str(result.get("reason", "")))
            if reason and not normalize_space(str(result.get("evidence", ""))):
                result["evidence"] = reason[:120]
            status = "not_relevant"
        source_url = normalize_space(str(result.get("source_url", "")))
        if source_url and is_bad_search_result(source_url):
            result["scope_summary"] = ""
            result["scope_keywords"] = []
            result["confidence"] = "low"
            result["reason"] = f"bad_source_domain:{domain_of(source_url)}"
            result["evidence"] = result["reason"]
            status = result["reason"]
        if (
            working_row.get("record_type") == "journal"
            and source_url
            and not is_preferred_domain(source_url)
        ):
            if args.allow_untrusted_domains:
                source_page = page_for_source_url(source_url, pages)
                if not journal_issn_matches_page(working_row, source_page):
                    result["scope_summary"] = ""
                    result["scope_keywords"] = []
                    result["confidence"] = "low"
                    result["reason"] = f"unverified_journal_source:{domain_of(source_url)}"
                    result["evidence"] = "source page did not contain matching ISSN/eISSN"
                    status = result["reason"]
            else:
                result["scope_summary"] = ""
                result["scope_keywords"] = []
                result["confidence"] = "low"
                result["reason"] = f"untrusted_source_domain:{domain_of(source_url)}"
                result["evidence"] = result["reason"]
                status = result["reason"]
        return index, status, result, ""
    except Exception as exc:  # noqa: BLE001 - batch jobs should continue.
        return index, f"error:{type(exc).__name__}", None, normalize_space(str(exc))[:120]


def process_file(path: Path, args: argparse.Namespace, config: dict[str, Any]) -> tuple[int, int]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        original_fieldnames = reader.fieldnames or []
        fieldnames = ensure_output_columns(original_fieldnames)

    if args.prepare_columns:
        if fieldnames != original_fieldnames:
            backup = backup_path(path)
            path.replace(backup)
            with path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)
            print(f"prepared columns in {path}; backup {backup.name}")
        else:
            print(f"{path}: output columns already present")
        return len(rows), 0

    selected = select_rows(
        rows,
        dataset=args.dataset,
        record_type=args.record_type,
        overwrite=args.overwrite,
        retry_failed=args.retry_failed,
        row_numbers=set(args.row_numbers) if args.row_numbers else None,
    )
    selected = selected[args.offset :]
    if args.limit is not None:
        selected = selected[: args.limit]

    print(f"{path}: selected {len(selected)} rows")
    if args.dry_run:
        for index in selected[: min(len(selected), 10)]:
            row = rows[index]
            print(
                json.dumps(
                    {
                        "row": index + 1,
                        "dataset": row.get("dataset"),
                        "record_type": row.get("record_type"),
                        "name": row.get("name"),
                        "abbreviation": row.get("abbreviation"),
                        "queries": search_queries_for(row),
                    },
                    ensure_ascii=False,
                )
            )
        return len(selected), 0
    if not selected:
        return 0, 0

    changed = 0
    backup = backup_path(path)
    shutil.copy2(path, backup)
    search_conf = search_config(config)
    workers = max(1, args.workers)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(enrich_row, index, rows[index], args, config, search_conf): index
            for index in selected
        }
        for count, future in enumerate(as_completed(futures), start=1):
            index, status, result, error = future.result()
            row = rows[index]
            if result is None:
                row["收稿方向_状态"] = status
                row["收稿方向_证据"] = error
                row["收稿方向_更新时间"] = now_iso()
            else:
                update_row_from_result(row, result, status, replace_scope=args.replace_scope)
            changed += 1
            if args.sleep > 0 and workers == 1:
                time.sleep(args.sleep)
            if args.checkpoint_every and count % args.checkpoint_every == 0:
                write_csv_file(path, fieldnames, rows)
                print(f"  checkpoint {count}/{len(selected)} rows in {path.name}", flush=True)
            if args.progress_every and count % args.progress_every == 0:
                print(f"  processed {count}/{len(selected)} rows in {path.name}", flush=True)

    write_csv_file(path, fieldnames, rows)
    print(f"wrote {path}; backup {backup.name}")
    return len(selected), changed


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-config", type=Path, default=None, help="Path to api.json. Defaults to api.json then llmapi.json.")
    parser.add_argument("--files", nargs="*", type=Path, default=DEFAULT_FILES, help="CSV files to enrich.")
    parser.add_argument("--dataset", choices=["ccf", "th_cpl", "cas", "jcr"], default=None)
    parser.add_argument("--record-type", choices=["conference", "journal"], default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--row-numbers", nargs="*", type=int, default=None, help="1-based CSV data row numbers to process.")
    parser.add_argument("--overwrite", action="store_true", help="Reprocess rows that already have 收稿方向_官网摘取.")
    parser.add_argument("--retry-failed", action="store_true", help="Reprocess rows with failed/empty status while keeping ok rows.")
    parser.add_argument(
        "--replace-scope",
        action="store_true",
        help="Deprecated: automatic extraction may not overwrite the classification scope.",
    )
    parser.add_argument("--allow-untrusted-domains", action="store_true", help="Allow journal extraction from domains outside the publisher/platform allow-list.")
    parser.add_argument(
        "--skip-journal-homepage-lookup",
        action="store_true",
        help="Skip OpenAlex ISSN homepage lookup for journals; useful for faster large batches.",
    )
    parser.add_argument("--prepare-columns", action="store_true", help="Only add output columns to CSV files; no network/API calls.")
    parser.add_argument("--dry-run", action="store_true", help="Print selected rows and queries without network/API calls.")
    parser.add_argument("--cache-dir", type=Path, default=DATA_DIR / ".aims_scope_cache")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--llm-timeout", type=int, default=None)
    parser.add_argument("--search-results", type=int, default=5)
    parser.add_argument("--max-search-queries", type=int, default=None)
    parser.add_argument("--max-pages", type=int, default=4)
    parser.add_argument("--max-html-bytes", type=int, default=1_000_000)
    parser.add_argument("--max-chars-per-page", type=int, default=12_000)
    parser.add_argument("--sleep", type=float, default=0.0)
    parser.add_argument("--workers", type=int, default=1, help="Concurrent row workers; use a small value for network/API enrichment.")
    parser.add_argument("--checkpoint-every", type=int, default=0)
    parser.add_argument("--progress-every", type=int, default=10)
    args = parser.parse_args(argv)
    if args.replace_scope:
        parser.error(
            "--replace-scope 已停用：自动摘取只能写入待审核列；"
            "审核通过的范围请维护在 data/curated_venue_scopes.tsv"
        )
    return args


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    config = load_api_config(args.api_config)
    if args.llm_timeout is not None:
        if "llm" in config:
            config["llm"]["timeout"] = args.llm_timeout
        else:
            config["timeout"] = args.llm_timeout
    args.cache_dir.mkdir(parents=True, exist_ok=True)
    total_selected = 0
    total_changed = 0
    for file_path in args.files:
        selected, changed = process_file(file_path, args, config)
        total_selected += selected
        total_changed += changed
    print(f"done: selected={total_selected}, changed={total_changed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
