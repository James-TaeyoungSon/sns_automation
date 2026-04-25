# SNS Automation

This repository has two automation paths:

- Existing Google Sheets/Gemini quote posting workflows.
- Notion URL to Threads publishing workflow.

## Notion URL to Threads

Add a URL to the Notion database and set `Status` to `발행`.
GitHub Actions runs every 10 minutes, reads matching rows, generates AI summary/analysis,
posts to Threads, and writes `발행완료`, `Thread Post ID`, and `Published At` back to Notion.

Required Notion properties:

- `URL`
- `Status`
- `AI Summary`
- `AI Analysis`
- `Threads Post`
- `Thread Post ID`
- `Published At`
- `Last Error`

Required GitHub Secrets:

- `OPENAI_API_KEY`
- `NOTION_API_KEY`
- `NOTION_DATABASE_ID`
- `THREADS_ACCESS_TOKEN`
- `THREADS_USER_ID`

Optional GitHub Variable:

- `OPENAI_MODEL` defaults to `gpt-4o-mini`

## Token Refresh

The monthly token refresh workflow updates repository secrets by calling the GitHub API.
It needs this extra secret:

- `GH_PAT`: a GitHub personal access token that can update repository Actions secrets.

Threads long-lived tokens are valid for about 60 days and should be refreshed before expiry.
