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
