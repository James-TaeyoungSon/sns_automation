# -*- coding: utf-8 -*-
"""
AI 콘텐츠 생성 파이프라인.
Step 1: SEO 키워드 + 리서치 쿼리 추출
Step 2: OpenAI web_search_preview로 추가 조사
Step 3a: Blogspot 장문 HTML 생성 (2000-3000자)
Step 3b: Threads 단문 생성 (480자 이내)
"""
from __future__ import annotations

import json
import re

from openai import OpenAI

from config import cfg
from services.image_service import generate_blog_image, fetch_stock_image_urls

_client: OpenAI | None = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=cfg.OPENAI_API_KEY)
    return _client


# ── Step 1: 분석 ──────────────────────────────────────────────────────────────

_ANALYZE_PROMPT = """아래 AI/테크 기사를 분석해서 JSON만 출력해줘. 다른 설명 없이 JSON만.

[기사 제목]
{title}

[기사 본문 발췌]
{text}

[출력 형식]
{{
  "seo_keyword": "구글 검색량이 높을 핵심 키워드 1개 (한국어, 2-5음절)",
  "seo_related": ["연관 롱테일 키워드1", "연관 롱테일 키워드2"],
  "research_queries": [
    "이 기사 주제와 관련된 최신 데이터·사례를 찾기 위한 영어 검색 쿼리",
    "관련 기업·모델·기술의 경쟁 현황을 찾는 검색 쿼리",
    "한국 AI 시장·산업계 영향 관련 검색 쿼리"
  ],
  "image_query": "Pexels 이미지 검색에 사용할 영어 키워드 (2-4단어)"
}}"""


def _analyze(title: str, text: str) -> dict:
    client = _get_client()
    prompt = _ANALYZE_PROMPT.format(title=title, text=text[:3000])
    try:
        resp = client.responses.create(
            model="gpt-4o-mini",
            input=[{"role": "user", "content": prompt}],
            temperature=0.2,
        )
        raw = getattr(resp, "output_text", "") or ""
        raw = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw.strip())
        return json.loads(raw)
    except Exception:
        return {
            "seo_keyword": "",
            "seo_related": [],
            "research_queries": [],
            "image_query": "artificial intelligence technology",
        }


# ── Step 2: 웹 리서치 ─────────────────────────────────────────────────────────

def _research_web(queries: list[str]) -> str:
    client = _get_client()
    results = []
    for query in queries[:3]:
        try:
            resp = client.responses.create(
                model="gpt-4o-mini",
                tools=[{"type": "web_search_preview"}],
                input=[{
                    "role": "user",
                    "content": (
                        f"다음 AI/테크 주제에 대해 최신 구체적 사례, 수치, 전문가 의견을 조사해줘. "
                        f"출처와 함께 핵심 내용을 한국어로 요약해줘: {query}"
                    ),
                }],
            )
            content = getattr(resp, "output_text", "") or ""
            if content:
                results.append(f"[조사: {query}]\n{content}")
        except Exception as e:
            results.append(f"[조사: {query}] 실패: {e}")
    return "\n\n".join(results)


# ── Step 3a: Blogspot 장문 생성 ───────────────────────────────────────────────

_BLOGSPOT_SYSTEM = """너는 AI 테크 전문 블로거이자 분석가다. 머신러닝, 대형 언어 모델, AI 비즈니스 생태계에 대한 깊은 이해를 바탕으로 복잡한 기술 발전을 일반인도 이해할 수 있는 언어로 분석한다.

[문체 원칙]
- 도입부는 가장 반직관적이거나 놀라운 사실부터 시작한다. 헤드라인을 그대로 반복하지 않는다.
- 구체적 수치를 사용한다: 모델 파라미터 수, 벤치마크 점수, 투자 금액, 채택률.
- 역사적 맥락을 제공한다: 이 발전이 AI 발전 흐름에서 어디에 위치하는가?
- 비즈니스 각도를 설명한다: 누가 이익을 얻고, 누가 위협받으며, 경쟁 구도는 어떤가?
- "비기술 독자에게 이게 왜 중요한가"를 별도 섹션에서 명확히 설명한다.
- 마지막에는 아직 업계가 답하지 못한 열린 질문으로 마무리한다.
- 금지어: "혁명적", "게임체인저", "전례 없는" (단독으로 데이터 없이 사용 시).
- 한국어 2000-3000자 (HTML 태그 제외), HTML 형식으로 출력.

[HTML 구조]
<p>[반전 시작 도입부]</p>
<h2>[이게 왜 중요한가]</h2>
<p>...</p>
<h2>[더 깊은 맥락: 역사·경쟁 구도]</h2>
<p>...</p>
<h2>[실무 적용 포인트]</h2>
<p>...</p>
<h2>[남은 질문]</h2>
<p>...</p>
<hr>
<p><small>참고자료: [출처 목록]</small></p>

반드시 JSON으로 반환:
{{"title": "SEO 최적화된 포스트 제목 (30-60자)", "body_html": "HTML 본문"}}"""


def _generate_blogspot(title: str, article_text: str, research: str, seo_keyword: str, image_urls: list[str]) -> dict:
    client = _get_client()

    img_html = ""
    if image_urls:
        img_html = (
            f'<div style="text-align:center;margin:24px 0;">'
            f'<img src="{image_urls[0]}" alt="{seo_keyword}" '
            f'style="width:100%;max-width:860px;border-radius:12px;box-shadow:0 4px 16px rgba(0,0,0,0.12);">'
            f'</div>\n'
        )

    user_prompt = f"""SEO 키워드: {seo_keyword}

[원문 기사 제목]
{title}

[원문 기사 발췌]
{article_text[:5000]}

[추가 리서치 결과]
{research[:3000]}

[이미지 HTML (본문 적절한 위치에 삽입)]
{img_html}

위 내용을 바탕으로 Blogspot 포스트를 작성해줘. JSON만 반환."""

    try:
        resp = client.chat.completions.create(
            model=cfg.OPENAI_MODEL,
            messages=[
                {"role": "system", "content": _BLOGSPOT_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.5,
        )
        result = json.loads(resp.choices[0].message.content)
        return {
            "title": str(result.get("title", title)).strip(),
            "body_html": str(result.get("body_html", "")).strip(),
        }
    except Exception as e:
        return {"title": title, "body_html": f"<p>생성 실패: {e}</p>"}


# ── Step 3b: Threads 단문 생성 ────────────────────────────────────────────────

_THREADS_SYSTEM = """너는 일과 연구를 모두 치열하게 해내는 'AI 전문 직장인 연구자'다. 최신 AI 트렌드를 단순히 전달하는 것을 넘어, 실무와 일상 속에서 건져 올린 통찰을 공유한다.

threads_post 작성법:
- **글 본문 400자 이내** + 마지막 줄에 블로그 링크 1줄. 총 480자 미만.
- 첫 줄: 이 뉴스를 보고 느낀 가장 날카로운 한 마디. 독자가 멈추게 만드는 한 방.
- 중간: 핵심 사실 1-2개 + 직장/연구 맥락에서 나만의 관점. ("요즘 업무에서 느끼는 게..." 식의 인간적 시각)
- 마지막 줄: 반드시 "자세한 분석 → BLOGSPOT_URL" 형식으로 끝낼 것. BLOGSPOT_URL은 발행 후 실제 URL로 교체되는 플레이스홀더.
- 이모지 1-2개만 사용. 과하지 않게.
- 반드시 JSON만 반환: {{"threads_text": "..."}}"""


def _generate_threads(title: str, article_text: str, seo_keyword: str) -> dict:
    client = _get_client()

    user_prompt = f"""기사 제목: {title}
SEO 키워드: {seo_keyword}
기사 발췌: {article_text[:2000]}

Threads 포스트를 작성해줘. BLOGSPOT_URL은 나중에 실제 URL로 교체될 플레이스홀더야. JSON만 반환."""

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": _THREADS_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.7,
        )
        result = json.loads(resp.choices[0].message.content)
        threads_text = str(result.get("threads_text", "")).strip()
        return {"threads_text": _fit_limit(threads_text)}
    except Exception as e:
        return {"threads_text": f"[생성 실패: {e}]"}


def _fit_limit(text: str, limit: int = 480) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    last_newline = cut.rfind("\n")
    if last_newline > limit * 0.6:
        return cut[:last_newline].rstrip()
    return cut.rstrip()


# ── 공개 인터페이스 ───────────────────────────────────────────────────────────

def generate_pair(
    article_url: str,
    article_title: str,
    article_text: str,
    log_fn=None,
) -> dict:
    """
    Blogspot + Threads 콘텐츠 쌍 생성.
    log_fn(msg): 진행 상황 로그 콜백 (선택).
    반환: {"blogspot": {"title", "body_html"}, "threads": {"threads_text"}, "seo_keyword", "image_urls"}
    """
    def log(msg: str):
        print(f"[llm_generator] {msg}")
        if log_fn:
            log_fn(msg)

    log("Step 1: SEO 키워드 및 리서치 쿼리 추출 중...")
    analysis = _analyze(article_title, article_text)
    seo_keyword = analysis.get("seo_keyword") or article_title[:20]
    queries = analysis.get("research_queries", [])
    image_query = analysis.get("image_query", "artificial intelligence")

    log(f"SEO 키워드: {seo_keyword}")

    log("Step 2: 웹 리서치 중...")
    research = _research_web(queries) if queries else ""

    log("DALL-E 3 이미지 생성 중... (약 20-30초)")
    dalle_url = generate_blog_image(article_title, seo_keyword)
    if dalle_url:
        image_urls = [dalle_url]
        log(f"이미지 생성 완료: {dalle_url[:60]}...")
    else:
        log("DALL-E 실패 → Pexels 스톡 이미지로 대체")
        image_urls = fetch_stock_image_urls(image_query, count=1)

    log("Step 3a: Blogspot 장문 생성 중...")
    blogspot = _generate_blogspot(article_title, article_text, research, seo_keyword, image_urls)

    log("Step 3b: Threads 단문 생성 중...")
    threads = _generate_threads(article_title, article_text, seo_keyword)

    log("생성 완료!")
    return {
        "blogspot": blogspot,
        "threads": threads,
        "seo_keyword": seo_keyword,
        "image_urls": image_urls,
    }
