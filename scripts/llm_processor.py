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
너는 AI 업계 소식을 선별해 Threads에 올리는 한국어 큐레이터다.
원문을 그대로 베끼지 말고, 핵심을 압축한 뒤 독자의 관점 형성에 도움이 되는
분석 의견을 작성한다. 과장된 투자 조언, 확정적 예언, 클릭베이트는 피한다.

반드시 JSON만 반환한다.
{
  "summary": "3문장 이내 핵심 요약",
  "analysis": "작성자의 관점이 드러나는 4~7문장 분석 글",
  "threads_post": "500자 이내 Threads 게시글. 마지막에 원문 URL을 포함"
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
