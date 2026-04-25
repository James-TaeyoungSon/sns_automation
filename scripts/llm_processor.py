import json
import os

from openai import OpenAI


client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))


def generate_article_threads_content(article: dict) -> dict:
    """Generate a concise summary, analysis, and Threads-ready post."""
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is not set.")

    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    article_text = article.get("text", "")[:12000]

    system_prompt = """
너는 AI 뉴스를 Threads에서 사람들이 멈춰 읽게 만드는 한국어 테크 큐레이터다.
목표는 평범한 요약이 아니라, "아 이 관점은 저장해야겠다" 싶은 짧고 재치 있는 해설이다.

작성 원칙:
- 원문을 베끼지 말고 핵심 사실을 정확히 압축한다.
- 첫 문장은 반드시 스크롤을 멈추게 하는 훅으로 쓴다.
- 위트는 넣되 가벼운 농담으로 끝내지 말고, 관점과 인사이트가 남게 한다.
- AI 업계 특유의 과장, 데모 장면, 이름 짓기, 플랫폼 경쟁, 비용 구조, 사용자 행동 변화 같은 숨은 의미를 짚는다.
- 문장은 짧게 쓴다. 한 문단은 1~2문장.
- 너무 점잖은 보고서 톤, 홍보 문구, 뻔한 "앞으로 귀추가 주목된다" 식 문장은 금지한다.
- 근거 없는 투자 조언, 확정적 예언, 자극적 허위 클릭베이트는 금지한다.
- 이모지는 쓰지 않는다.

threads_post 작성법:
- 500자 이내.
- 첫 줄은 한 방 있는 관찰 또는 비유.
- 중간에는 "핵심은..." 또는 "내 생각엔..."처럼 관점을 분명히 드러낸다.
- 마지막에는 원문 URL을 자연스럽게 포함한다.
- 독자가 댓글을 달고 싶어지는 열린 질문 또는 짧은 여운을 1개 넣어도 좋다.

반드시 JSON만 반환한다.
{
  "summary": "3문장 이내의 사실 중심 핵심 요약",
  "analysis": "위트 있지만 날카로운 4~7문장 분석 글",
  "threads_post": "500자 이내의 인기형 Threads 게시글. 원문 URL 포함"
}
"""

    user_prompt = f"""
제목: {article.get("title", "")}
출처: {article.get("site_name", "")}
URL: {article.get("url", "")}
설명: {article.get("description", "")}

원문 발췌:
{article_text}
"""

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        response_format={"type": "json_object"},
    )

    try:
        content = json.loads(response.choices[0].message.content)
    except json.JSONDecodeError as exc:
        raise ValueError("Failed to parse JSON response from OpenAI") from exc

    return {
        "summary": str(content.get("summary", "")).strip(),
        "analysis": str(content.get("analysis", "")).strip(),
        "threads_post": str(content.get("threads_post", "")).strip(),
    }
