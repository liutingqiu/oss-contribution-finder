#!/usr/bin/env python3
"""OSS Contribution Finder — discover good-first-issue opportunities.

Searches GitHub for open-source projects with "good first issue" labels,
filtered by language, activity, and topic. Outputs a ranked list of
opportunities ready for contribution.

Usage:
    oss-contribution-finder --language python --limit 10
    oss-contribution-finder --topic rust --min-stars 100 --format json
    oss-contribution-finder --language go --format markdown >> opportunities.md
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any


GITHUB_API = "https://api.github.com"
SEARCH_ENDPOINT = f"{GITHUB_API}/search/issues"


def get_token() -> str | None:
    """Get GitHub token from environment."""
    return os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")


def api_request(url: str, token: str | None = None) -> dict | list:
    """Make an authenticated API request."""
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        return {"error": e.code, "message": body}
    except Exception as e:
        return {"error": str(e)}


def search_issues(
    *,
    labels: list[str] | None = None,
    language: str | None = None,
    topic: str | None = None,
    min_stars: int | None = None,
    created_after: str | None = None,
    updated_after: str | None = None,
    sort: str = "updated",
    order: str = "desc",
    per_page: int = 30,
    page: int = 1,
    token: str | None = None,
) -> dict:
    """Search GitHub issues with filters."""
    query_parts = ["state:open", "is:issue"]

    if labels:
        for label in labels:
            query_parts.append(f'label:"{label}"')
    if language:
        query_parts.append(f"language:{language}")
    if topic:
        query_parts.append(f"topic:{topic}")
    if min_stars:
        query_parts.append(f"stars:>={min_stars}")
    if created_after:
        query_parts.append(f"created:>={created_after}")
    if updated_after:
        query_parts.append(f"updated:>={updated_after}")

    query = " ".join(query_parts)
    params = (
        f"q={urllib.parse.quote(query)}"
        f"&sort={sort}&order={order}"
        f"&per_page={per_page}&page={page}"
    )
    url = f"{SEARCH_ENDPOINT}?{params}"
    return api_request(url, token=token)


def get_repo_info(full_name: str, token: str | None = None) -> dict:
    """Get repository metadata."""
    url = f"{GITHUB_API}/repos/{full_name}"
    return api_request(url, token=token)


def rate_limit(token: str | None = None) -> dict:
    """Check current rate limit status."""
    url = f"{GITHUB_API}/rate_limit"
    return api_request(url, token=token)


def _repo_info(opp: dict) -> dict:
    """Return repo metadata, falling back to repository_url when enrich was skipped."""
    repo = opp.get("repo")
    if isinstance(repo, dict) and repo.get("full_name"):
        return repo
    repo_url = opp.get("repository_url", "")
    parts = repo_url.rstrip("/").split("/")[-2:]
    full_name = "/".join(parts) if len(parts) == 2 else "unknown/unknown"
    return {
        "full_name": full_name,
        "html_url": f"https://github.com/{full_name}",
        "stargazers_count": 0,
        "language": "Unknown",
        "description": None,
    }


def format_markdown(opportunities: list[dict]) -> str:
    """Format opportunities as markdown."""
    if not opportunities:
        return "# No opportunities found\n"

    lines = ["# OSS Contribution Opportunities\n"]
    lines.append(f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n")

    for i, opp in enumerate(opportunities, 1):
        repo = _repo_info(opp)
        lines.append(f"## {i}. [{repo['full_name']}]({repo['html_url']})")
        if repo.get("description"):
            lines.append(f"_{repo['description']}_")
        lines.append(f"- **Issue**: [{opp['title']}]({opp['html_url']})")
        labels = opp.get("labels", [])
        if labels and isinstance(labels[0], dict):
            labels = [l.get("name", "") for l in labels]
        lines.append(f"- **Labels**: {', '.join(labels)}")
        lines.append(f"- **Stars**: ⭐ {repo.get('stargazers_count', '?')}")
        lines.append(f"- **Language**: {repo.get('language', 'Unknown')}")
        lines.append(f"- **Updated**: {opp.get('updated_at', '?')[:10]}")
        lines.append("")

    return "\n".join(lines)


def format_json(opportunities: list[dict]) -> str:
    """Format opportunities as JSON."""
    return json.dumps(opportunities, indent=2)


def format_table(opportunities: list[dict]) -> str:
    """Format opportunities as a readable table."""
    if not opportunities:
        return "No opportunities found."

    lines = []
    lines.append(f"{'#':>3} {'Repository':<40} {'Stars':>6} {'Title':<50}")
    lines.append("-" * 105)

    for i, opp in enumerate(opportunities, 1):
        repo = _repo_info(opp)
        repo_name = repo["full_name"]
        stars = repo.get("stargazers_count", 0)
        title = opp["title"][:47] + "..." if len(opp["title"]) > 50 else opp["title"]
        lines.append(f"{i:>3} {repo_name:<40} {stars:>6} {title:<50}")

    return "\n".join(lines)


def dedupe_by_repo(opportunities: list[dict], max_per_repo: int = 3) -> list[dict]:
    """Limit opportunities per repository."""
    counts: dict[str, int] = {}
    result = []
    for opp in opportunities:
        repo = _repo_info(opp)["full_name"]
        count = counts.get(repo, 0)
        if count < max_per_repo:
            result.append(opp)
            counts[repo] = count + 1
    return result


def enrich_opportunities(
    opportunities: list[dict],
    token: str | None = None,
    max_repos: int = 10,
) -> list[dict]:
    """Fetch repo metadata for opportunities (rate-limit aware)."""
    seen = set()
    enriched = []
    for opp in opportunities:
        full_name = opp["repository_url"].split("/")[-2:]
        full_name = "/".join(full_name)
        if full_name in seen:
            continue
        seen.add(full_name)
        if len(seen) > max_repos:
            break
        info = get_repo_info(full_name, token=token)
        if "error" not in info:
            opp["repo"] = info
            enriched.append(opp)
    return enriched


def main():
    parser = argparse.ArgumentParser(
        description="Find OSS contribution opportunities",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  oss-contribution-finder --language python --limit 10
  oss-contribution-finder --topic rust --min-stars 100 --format json
  oss-contribution-finder --language go --format markdown >> opportunities.md
        """,
    )
    parser.add_argument("--version", action="version", version="%(prog)s 0.1.0")
    parser.add_argument(
        "--label",
        action="append",
        default=["good first issue"],
        help="Labels to filter (default: 'good first issue')",
    )
    parser.add_argument("--language", "-l", help="Programming language filter")
    parser.add_argument("--topic", "-t", help="GitHub topic filter")
    parser.add_argument("--min-stars", type=int, help="Minimum star count")
    parser.add_argument(
        "--created-after",
        help="Only issues created after date (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--updated-after",
        help="Only issues updated after date (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--sort",
        choices=["comments", "reactions", "updated"],
        default="updated",
        help="Sort field (default: updated)",
    )
    parser.add_argument("--limit", "-n", type=int, default=20, help="Max results")
    parser.add_argument(
        "--format",
        "-f",
        choices=["table", "markdown", "json"],
        default="table",
        help="Output format (default: table)",
    )    parser.add_argument(
        "--output",
        "-o",
        dest="output_file",
        help="Write output to a file instead of stdout",
    )

    parser.add_argument(
        "--no-enrich",
        action="store_true",
        help="Skip fetching repo metadata (faster, less info)",
    )
    parser.add_argument(
        "--check-rate-limit",
        action="store_true",
        help="Check rate limit and exit",
    )

    args = parser.parse_args()
    token = get_token()

    if args.check_rate_limit:
        rl = rate_limit(token=token)
        core = rl.get("resources", {}).get("core", {})
        search = rl.get("resources", {}).get("search", {})
        print(f"Core: {core.get('remaining', '?')}/{core.get('limit', '?')} (resets at {core.get('reset', '?')})")
        print(f"Search: {search.get('remaining', '?')}/{search.get('limit', '?')} (resets at {search.get('reset', '?')})")
        return

    # Default: only recent issues (last 30 days)
    if not args.updated_after:
        args.updated_after = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")

    per_page = min(args.limit, 100)
    result = search_issues(
        labels=args.label,
        language=args.language,
        topic=args.topic,
        min_stars=args.min_stars,
        created_after=args.created_after,
        updated_after=args.updated_after,
        sort=args.sort,
        per_page=per_page,
        token=token,
    )

    if "error" in result:
        print(f"Error: {result}", file=sys.stderr)
        sys.exit(1)

    items = result.get("items", [])

    # Enrich with repo metadata
    if not args.no_enrich and token:
        items = enrich_opportunities(items, token=token, max_repos=args.limit)

    # Format output
    formatters = {
        "table": format_table,
        "markdown": format_markdown,
        "json": format_json,
    }
    output = formatters[args.format](items)
    if getattr(args, "output_file", None):
        with open(args.output_file, "w", encoding="utf-8") as f:
            f.write(output)
            if not output.endswith("\n"):
                f.write("\n")
    else:
        print(output)


if __name__ == "__main__":
    main()
