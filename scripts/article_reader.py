import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0 Safari/537.36"
)


@dataclass
class Article:
    url: str
    title: str
    description: str
    site_name: str
    text: str


def fetch_article(url: str, timeout: int = 20) -> Article:
    """Fetch a public article/blog URL and return readable metadata + text."""
    if not _looks_like_url(url):
        raise ValueError(f"Invalid URL: {url}")

    response = _get_url(url, timeout)
    response = _follow_known_bridge(response, timeout)
    response.raise_for_status()

    if not response.encoding:
        response.encoding = response.apparent_encoding

    soup = BeautifulSoup(response.text, "html.parser")
    _remove_noise(soup)

    title = _first_meta(soup, ["og:title", "twitter:title"]) or _text_or_empty(
        soup.find("title")
    )
    if not title:
        title = _text_or_empty(soup.find("h1"))

    description = _first_meta(
        soup, ["og:description", "twitter:description", "description"]
    )
    site_name = _first_meta(soup, ["og:site_name"]) or urlparse(response.url).netloc
    text = _extract_main_text(soup)

    if not text:
        raise ValueError("Could not extract readable article text.")

    return Article(
        url=response.url,
        title=_clean_space(title) or response.url,
        description=_clean_space(description),
        site_name=_clean_space(site_name),
        text=text,
    )


def discover_url_from_title(title: str, timeout: int = 20) -> str:
    """Best-effort URL discovery for Notion share rows that only contain a title."""
    title = _clean_space(title)
    if not title:
        return ""

    source_tokens = _tokens(title)
    best_url = ""
    best_score = 0.0

    search_pages = []
    for search_url in [
        "https://duckduckgo.com/html/",
        "https://html.duckduckgo.com/html/",
    ]:
        try:
            response = requests.get(
                search_url,
                params={"q": title},
                headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "ko,en;q=0.8"},
                timeout=timeout,
            )
            if response.status_code == 200:
                search_pages.append(response.text)
        except requests.RequestException:
            continue

    for html in search_pages:
        soup = BeautifulSoup(html, "html.parser")
        for link in soup.select("a.result__a"):
            href = _unwrap_search_result_url(link.get("href", ""))
            candidate_title = _clean_space(link.get_text(" ", strip=True))
            if not href or not href.startswith(("http://", "https://")):
                continue

            candidate_tokens = _tokens(candidate_title)
            if not candidate_tokens:
                continue

            overlap = len(source_tokens & candidate_tokens)
            score = overlap / max(1, min(len(source_tokens), 12))
            if score > best_score:
                best_score = score
                best_url = href

    if best_score >= 0.35:
        return best_url
    return ""


def _looks_like_url(value: str) -> bool:
    parsed = urlparse((value or "").strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _get_url(url: str, timeout: int) -> requests.Response:
    return requests.get(
        url,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "ko,en;q=0.8"},
        timeout=timeout,
    )


def _follow_known_bridge(response: requests.Response, timeout: int) -> requests.Response:
    parsed = urlparse(response.url)
    if parsed.netloc == "link.naver.com" and parsed.path.startswith("/bridge"):
        target = parse_qs(parsed.query).get("url", [""])[0]
        if target:
            return _get_url(target, timeout)
    return response


def _remove_noise(soup: BeautifulSoup) -> None:
    for tag in soup(
        [
            "script",
            "style",
            "noscript",
            "svg",
            "form",
            "iframe",
            "nav",
            "footer",
            "aside",
            "button",
        ]
    ):
        tag.decompose()


def _first_meta(soup: BeautifulSoup, names: list[str]) -> str:
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or soup.find(
            "meta", attrs={"name": name}
        )
        if tag and tag.get("content"):
            return str(tag["content"])
    return ""


def _text_or_empty(node: Optional[object]) -> str:
    if not node:
        return ""
    return _clean_space(node.get_text(" ", strip=True))


def _extract_main_text(soup: BeautifulSoup) -> str:
    candidates = []
    for selector in [
        "#newsct_article",
        "#dic_area",
        "#articeBody",
        "article",
        "main",
        "[role='main']",
        ".article",
        ".post",
        ".content",
    ]:
        candidates.extend(soup.select(selector))

    candidates.append(soup.body or soup)
    best_text = ""

    for candidate in candidates:
        paragraphs = []
        for node in candidate.find_all(["p", "li", "blockquote", "h2", "h3"]):
            text = _clean_space(node.get_text(" ", strip=True))
            if len(text) >= 35:
                paragraphs.append(text)

        candidate_text = "\n\n".join(_dedupe(paragraphs))
        if len(candidate_text) > len(best_text):
            best_text = candidate_text

    return best_text[:14000].strip()


def _clean_space(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _dedupe(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        key = value[:120]
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _unwrap_search_result_url(href: str) -> str:
    if not href:
        return ""
    if href.startswith("//"):
        href = f"https:{href}"
    parsed = urlparse(href)
    if parsed.netloc.endswith("duckduckgo.com"):
        return parse_qs(parsed.query).get("uddg", [""])[0]
    return href


def _tokens(value: str) -> set[str]:
    normalized = re.sub(r"[^0-9A-Za-z가-힣]+", " ", value or "").lower()
    return {token for token in normalized.split() if len(token) >= 2}
