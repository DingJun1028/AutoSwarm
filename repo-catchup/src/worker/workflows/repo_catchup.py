import json
import re
from datetime import date, timedelta
from typing import Any

import pydantic
import mistralai.workflows as workflows
from mistralai.workflows import Depends
from mistralai.workflows.client import get_mistral_client
from mistralai.workflows.plugins.mistralai.connectors import (
    ToolCallClient,
    connector,
    uses_connectors,
)

github = connector("github_app")
slack = connector("slack")

SUMMARY_MODEL = "mistral-medium-latest"
MAX_PRS_PER_SECTION = 30


class RepoCatchupInput(pydantic.BaseModel):
    repo: str
    since: str


class PRItem(pydantic.BaseModel):
    number: int
    title: str
    url: str
    author: str
    merged_at: str | None = None
    updated_at: str | None = None
    labels: list[str] = []


class PRSummary(pydantic.BaseModel):
    number: int
    summary: str


class DigestResult(pydantic.BaseModel):
    repo: str
    since: str
    merged_count: int
    open_count: int
    message: str
    slack_channel: str | None = None
    slack_ts: str | None = None


def parse_repo_handle(repo: str) -> tuple[str, str]:
    match = re.search(r"github\.com[:/]([^/]+)/([^/#?]+)", repo)
    if match:
        return match.group(1), match.group(2).removesuffix(".git")
    parts = [p for p in repo.strip().strip("/").split("/") if p]
    if len(parts) != 2:
        raise ValueError(f"repo must be 'owner/name' or a GitHub URL, got: {repo!r}")
    return parts[0], parts[1].removesuffix(".git")


def parse_since(since: str) -> str:
    return date.fromisoformat(since.strip()).isoformat()


def extract_payload(result: Any) -> Any:
    content = getattr(result, "content", result)
    if isinstance(content, str):
        try:
            return json.loads(content)
        except (json.JSONDecodeError, TypeError):
            return content
    if isinstance(content, list):
        for block in content:
            text = getattr(block, "text", None)
            if isinstance(text, str) and text.strip():
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return text
        return content
    return content


def extract_items(payload: Any) -> list[dict]:
    if isinstance(payload, dict):
        for key in ("items", "results", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return [payload]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def extract_json_object(text: str) -> dict:
    text = text.strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    return {}


@workflows.activity(start_to_close_timeout=timedelta(minutes=2))
async def search_pull_requests(
    query: str,
    owner: str,
    repo: str,
    github_client: ToolCallClient = Depends(github),
) -> list[PRItem]:
    result = await github_client.call_tool(
        "search_pull_requests",
        {
            "query": query,
            "owner": owner,
            "repo": repo,
            "sort": "updated",
            "order": "desc",
            "perPage": MAX_PRS_PER_SECTION,
            "fields": [
                "number",
                "title",
                "state",
                "html_url",
                "user",
                "created_at",
                "updated_at",
                "closed_at",
                "pull_request",
                "labels",
                "draft",
            ],
        },
    )
    prs: list[PRItem] = []
    for item in extract_items(extract_payload(result)):
        pr = item.get("pull_request") or {}
        user = item.get("user") or {}
        labels = [
            label.get("name", "")
            for label in (item.get("labels") or [])
            if isinstance(label, dict)
        ]
        prs.append(
            PRItem(
                number=int(item.get("number") or 0),
                title=item.get("title") or "",
                url=item.get("html_url") or pr.get("html_url") or "",
                author=user.get("login") or "",
                merged_at=pr.get("merged_at"),
                updated_at=item.get("updated_at"),
                labels=labels,
            )
        )
    return prs


@workflows.activity(start_to_close_timeout=timedelta(minutes=3))
async def summarize_prs(
    prs: list[PRItem],
    section: str,
    client: Any = Depends(get_mistral_client),
) -> list[PRSummary]:
    if not prs:
        return []
    listing = "\n".join(f"#{p.number} {p.title} (by {p.author})" for p in prs)
    prompt = (
        f"You write one-line summaries of GitHub pull requests for a '{section}' digest.\n"
        "For each PR below, write exactly one line of at most 140 characters stating what the "
        "change does, in present tense, without naming the author and without bullet markers.\n"
        'Return ONLY a JSON object mapping PR number (as a string) to its one-line summary, '
        'e.g. {"12": "Adds retry backoff to the webhook sender"}.\n\n'
        f"PRs:\n{listing}"
    )
    response = await client.chat.complete_async(
        model=SUMMARY_MODEL,
        messages=[{"role": "user", "content": prompt}],
    )
    content = response.choices[0].message.content or ""
    summaries = extract_json_object(content)
    return [
        PRSummary(
            number=pr.number,
            summary=str(summaries.get(str(pr.number)) or pr.title)[:200],
        )
        for pr in prs
    ]


def build_digest_message(
    repo: str,
    since: str,
    merged: list[PRItem],
    merged_summaries: list[PRSummary],
    open_prs: list[PRItem],
    open_summaries: list[PRSummary],
) -> str:
    summary_by_number = {s.number: s.summary for s in merged_summaries + open_summaries}

    def line(pr: PRItem) -> str:
        summary = summary_by_number.get(pr.number, pr.title)
        return f"• #{pr.number} — {summary} — {pr.url}"

    lines = [
        f"*Repo catch-up: {repo}* (since {since})",
        "",
        f"*Merged ({len(merged)})*",
    ]
    lines += [line(pr) for pr in merged] or ["• none"]
    lines += ["", f"*Waiting on review ({len(open_prs)})*"]
    lines += [line(pr) for pr in open_prs] or ["• none"]
    return "\n".join(lines)


@workflows.activity(start_to_close_timeout=timedelta(minutes=2))
async def post_digest_to_self_chat(
    message: str,
    slack_client: ToolCallClient = Depends(slack),
) -> dict:
    me = extract_payload(await slack_client.call_tool("slack_get_own_user_info", {}))
    user_id = None
    if isinstance(me, dict):
        user_id = me.get("id") or (me.get("user") or {}).get("id") or me.get("user_id")
    if not user_id:
        raise ValueError(f"could not resolve own Slack user id from: {me!r}")
    sent = extract_payload(
        await slack_client.call_tool(
            "slack_send_message",
            {"channel_id": user_id, "text": message},
        )
    )
    ts = None
    if isinstance(sent, dict):
        ts = sent.get("ts") or (sent.get("message") or {}).get("ts")
    return {"channel": user_id, "ts": ts}


@workflows.workflow.define(
    name="repo-catchup",
    workflow_display_name="Repo catch-up digest",
    workflow_description=(
        "Given a GitHub repo handle and a date, lists pull requests merged since that date "
        "and open pull requests waiting on review, summarizes each in one line, and posts "
        "the digest to the triggering user's Slack self-chat."
    ),
    on_behalf_of=True,
)
@uses_connectors(github, slack)
class RepoCatchupWorkflow:
    @workflows.workflow.entrypoint
    async def run(self, input: RepoCatchupInput) -> DigestResult:
        owner, repo = parse_repo_handle(input.repo)
        since = parse_since(input.since)

        merged = await search_pull_requests(
            f"is:merged merged:>={since}", owner, repo
        )
        open_prs = await search_pull_requests("is:open -is:draft", owner, repo)

        merged_summaries = await summarize_prs(merged, "merged")
        open_summaries = await summarize_prs(open_prs, "awaiting review")

        message = build_digest_message(
            input.repo, since, merged, merged_summaries, open_prs, open_summaries
        )
        posted = await post_digest_to_self_chat(message)

        return DigestResult(
            repo=input.repo,
            since=since,
            merged_count=len(merged),
            open_count=len(open_prs),
            message=message,
            slack_channel=posted["channel"],
            slack_ts=posted["ts"],
        )
