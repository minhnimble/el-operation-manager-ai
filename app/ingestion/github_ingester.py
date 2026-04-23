"""
GitHub Ingester — pulls commits, PRs, reviews, and issues per user.

Uses the GitHub REST API v3 with the user's personal OAuth token.
Stores raw activity into GitHubActivity table.
"""

import asyncio
import logging
import time
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.models.raw_data import GitHubActivity
from app.models.user import UserGitHubLink

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"


def _fmt_err(e: BaseException) -> str:
    """Format an exception for user-facing sync logs.

    ``httpx.HTTPStatusError`` already stringifies with status + URL, which is
    what we want.  For other exceptions we fall back to ``type: message`` so
    the log is never just a bare object repr.
    """
    if isinstance(e, httpx.HTTPStatusError):
        return f"{e.response.status_code} {e.response.reason_phrase} on {e.request.url}"
    if isinstance(e, httpx.HTTPError):
        return f"{type(e).__name__}: {e}"
    return f"{type(e).__name__}: {e}"

# HTTP statuses that represent "this resource is unavailable / has no data for
# this caller" rather than a transient failure.  Retrying them wastes quota and
# produces confusing RetryError messages for the user, so we treat them as an
# empty result and move on.
#
#   404 — repo not visible to the PAT (private to another org, deleted, etc.)
#   409 — "Git Repository is empty" (common on /commits for bare doc repos)
#   410 — repo was deleted
#   422 — malformed search query (Search API surface)
_TERMINAL_EMPTY_STATUSES = frozenset({404, 409, 410, 422})

# Upper bound on any single rate-limit wait. GitHub's hourly reset can be up
# to an hour away, and blocking a Streamlit sync for that long is worse than
# surfacing the error to the user — they can rerun later.
_MAX_RATE_LIMIT_WAIT_SECONDS = 60


class GitHubIngester:
    def __init__(self, access_token: str, github_login: str):
        self.github_login = github_login
        self._client = httpx.AsyncClient(
            base_url=GITHUB_API_BASE,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
        )

    async def close(self) -> None:
        await self._client.aclose()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=1, max=10),
        # reraise=True so callers see the real HTTPStatusError (with status +
        # URL in the message) instead of tenacity's opaque
        # "RetryError[<Future ... raised HTTPStatusError>]" wrapper.
        reraise=True,
        # Only retry on transient network / 5xx errors. Terminal 4xx (404,
        # 409, 410, 422) are handled inline below and never raise, so they
        # never reach tenacity. Rate-limit is handled inline with an explicit
        # sleep, then we raise a RuntimeError to trigger a retry.
        retry=retry_if_exception_type((httpx.HTTPError, RuntimeError)),
    )
    async def _get(self, path: str, params: dict | None = None) -> Any:
        resp = await self._client.get(path, params=params or {})

        # ── Terminal "no data" statuses — don't retry, don't raise ──────────
        # These repos/queries can't be satisfied for this PAT (missing, empty,
        # or deleted). Returning [] lets the pagination / caller code treat
        # them as "no results" without burning retry attempts and without
        # polluting the sync log with scary-looking RetryError messages.
        if resp.status_code in _TERMINAL_EMPTY_STATUSES:
            logger.debug(
                "GitHub %s on %s — treated as empty (%s)",
                resp.status_code, resp.url, resp.reason_phrase,
            )
            return []

        # ── Rate limiting ────────────────────────────────────────────────────
        # GitHub uses two distinct mechanisms:
        #   • Primary   — 403 with x-ratelimit-remaining: 0, reset via
        #                 x-ratelimit-reset (unix timestamp).
        #   • Secondary — 403 or 429 with a Retry-After header (seconds).
        # Both need an actual sleep, not blind exponential backoff.
        if resp.status_code in (403, 429):
            wait_s = self._rate_limit_wait_seconds(resp)
            if wait_s is not None:
                logger.warning(
                    "GitHub rate limit on %s — sleeping %ds before retry",
                    path, wait_s,
                )
                await asyncio.sleep(wait_s)
                # Raise so tenacity drives the retry loop; message is only
                # used if we exhaust attempts, in which case the user gets a
                # readable error.
                raise RuntimeError(
                    f"GitHub rate limit on {path} (slept {wait_s}s) — retrying"
                )
            # Non-rate-limit 403 (SAML SSO enforcement, blocked org, token
            # missing scope). Treat as terminal: skip this resource rather
            # than retrying something that will never succeed.
            logger.warning(
                "GitHub 403 on %s (%s) — skipping: %s",
                path, resp.url, (resp.text or "")[:200],
            )
            return []

        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _rate_limit_wait_seconds(resp: httpx.Response) -> int | None:
        """Return the number of seconds to sleep for a rate-limit response,
        or ``None`` if this 403/429 isn't actually a rate-limit response.

        Capped at :data:`_MAX_RATE_LIMIT_WAIT_SECONDS` so a badly-timed sync
        doesn't block on a full-hour reset window.
        """
        retry_after = resp.headers.get("retry-after")
        if retry_after:
            try:
                return min(max(int(retry_after), 1), _MAX_RATE_LIMIT_WAIT_SECONDS)
            except ValueError:
                pass  # malformed header — fall through to reset check

        remaining = resp.headers.get("x-ratelimit-remaining")
        reset = resp.headers.get("x-ratelimit-reset")
        if remaining == "0" and reset:
            try:
                delta = int(reset) - int(time.time()) + 1
            except ValueError:
                return None
            return min(max(delta, 1), _MAX_RATE_LIMIT_WAIT_SECONDS)

        return None

    async def _paginate(self, path: str, params: dict | None = None) -> list[dict]:
        results = []
        page = 1
        while True:
            p = {**(params or {}), "per_page": 100, "page": page}
            data = await self._get(path, p)
            if not data:
                break
            results.extend(data)
            if len(data) < 100:
                break
            page += 1
        return results

    async def get_repos(self) -> list[dict]:
        return await self._paginate("/user/repos", {"type": "all", "sort": "pushed"})

    async def get_contribution_repo_names(
        self, since: datetime, until: datetime | None = None,
    ) -> set[str]:
        """Return full_names of repos where `self.github_login` has any PR/issue
        activity (authored, assigned, mentioned, commented, reviewed) in
        [since, until]. Uses Search API — cross-org, no per-repo loop.

        Does NOT cover commits-only contributions (commit search is heavily
        rate-limited); callers should union with a commits-based probe if needed.
        """
        s = since.date().isoformat()
        u = (until or datetime.utcnow()).date().isoformat()
        rng = f"{s}..{u}"
        repos: set[str] = set()
        # `involves:` matches author, assignee, mentions, commenter. Plus
        # reviewed-by explicitly for PRs the user only reviewed.
        queries = [
            f"involves:{self.github_login} updated:{rng}",
            f"reviewed-by:{self.github_login} updated:{rng}",
        ]
        for q in queries:
            try:
                items = await self._search_issues(q)
            except Exception as e:
                logger.warning("Search repos failed (%s): %s", q, _fmt_err(e))
                continue
            for it in items:
                name = self._repo_full_name_from_url(it.get("repository_url", ""))
                if name:
                    repos.add(name)
        return repos

    async def get_commits(
        self, repo_full_name: str, since: datetime | None = None
    ) -> list[dict]:
        params: dict = {"author": self.github_login}
        if since:
            params["since"] = since.isoformat() + "Z"
        return await self._paginate(f"/repos/{repo_full_name}/commits", params)

    async def get_pull_requests(
        self, repo_full_name: str, state: str = "all"
    ) -> list[dict]:
        return await self._paginate(
            f"/repos/{repo_full_name}/pulls",
            {"state": state, "sort": "updated", "direction": "desc"},
        )

    async def get_pr_reviews(self, repo_full_name: str, pr_number: int) -> list[dict]:
        return await self._paginate(
            f"/repos/{repo_full_name}/pulls/{pr_number}/reviews"
        )

    async def get_issues(
        self, repo_full_name: str, since: datetime | None = None
    ) -> list[dict]:
        params: dict = {"state": "all", "creator": self.github_login}
        if since:
            params["since"] = since.isoformat() + "Z"
        return await self._paginate(f"/repos/{repo_full_name}/issues", params)

    async def ingest_single_repo(
        self,
        db: AsyncSession,
        slack_team_id: str,
        slack_user_id: str,
        repo: dict,
        since: datetime | None = None,
    ) -> dict[str, int]:
        """Ingest activity for a single repo. Returns counts by type."""
        counts: dict[str, int] = {"commits": 0, "prs": 0, "reviews": 0, "issues": 0}
        repo_name = repo["full_name"]

        # Commits
        try:
            commits = await self.get_commits(repo_name, since=since)
            for commit in commits:
                sha = commit["sha"]
                await self._upsert_activity(
                    db,
                    slack_team_id=slack_team_id,
                    slack_user_id=slack_user_id,
                    activity_type="commit",
                    repo_full_name=repo_name,
                    ref_id=sha,
                    title=commit["commit"]["message"].split("\n")[0][:512],
                    url=commit["html_url"],
                    raw_payload=commit,
                    activity_at=datetime.fromisoformat(
                        commit["commit"]["author"]["date"].replace("Z", "+00:00")
                    ).replace(tzinfo=None),
                )
                counts["commits"] += 1
        except Exception as e:
            logger.warning("Failed commits for %s: %s", repo_name, _fmt_err(e))

        # Pull Requests + Reviews
        try:
            prs = await self.get_pull_requests(repo_name)
            for pr in prs:
                if pr["user"]["login"] != self.github_login:
                    continue
                if since and datetime.fromisoformat(
                    pr["created_at"].replace("Z", "+00:00")
                ).replace(tzinfo=None) < since:
                    continue

                pr_type = "pr_merged" if pr.get("merged_at") else "pr_opened"
                await self._upsert_activity(
                    db,
                    slack_team_id=slack_team_id,
                    slack_user_id=slack_user_id,
                    activity_type=pr_type,
                    repo_full_name=repo_name,
                    ref_id=str(pr["number"]),
                    title=pr["title"][:512],
                    url=pr["html_url"],
                    raw_payload={
                        "number": pr["number"],
                        "title": pr["title"],
                        "state": pr["state"],
                        "draft": pr.get("draft"),
                        "labels": [l["name"] for l in pr.get("labels", [])],
                        "created_at": pr["created_at"],
                        "merged_at": pr.get("merged_at"),
                    },
                    activity_at=datetime.fromisoformat(
                        pr["created_at"].replace("Z", "+00:00")
                    ).replace(tzinfo=None),
                )
                counts["prs"] += 1

                reviews = await self.get_pr_reviews(repo_name, pr["number"])
                for review in reviews:
                    if review["user"]["login"] != self.github_login:
                        continue
                    review_key = f"{pr['number']}-{review['id']}"
                    await self._upsert_activity(
                        db,
                        slack_team_id=slack_team_id,
                        slack_user_id=slack_user_id,
                        activity_type="pr_review",
                        repo_full_name=repo_name,
                        ref_id=review_key,
                        title=f"Review on PR #{pr['number']}: {pr['title'][:400]}",
                        url=review.get("html_url", pr["html_url"]),
                        raw_payload={
                            "pr_number": pr["number"],
                            "state": review["state"],
                            "submitted_at": review.get("submitted_at"),
                        },
                        activity_at=datetime.fromisoformat(
                            review["submitted_at"].replace("Z", "+00:00")
                        ).replace(tzinfo=None)
                        if review.get("submitted_at")
                        else datetime.utcnow(),
                    )
                    counts["reviews"] += 1

        except Exception as e:
            logger.warning("Failed PRs for %s: %s", repo_name, _fmt_err(e))

        return counts

    async def ingest_user_activity(
        self,
        db: AsyncSession,
        slack_team_id: str,
        slack_user_id: str,
        since: datetime | None = None,
    ) -> dict[str, int]:
        """Pull all recent GitHub activity for the user. Returns counts by type."""
        counts: dict[str, int] = {
            "commits": 0, "prs": 0, "reviews": 0, "issues": 0
        }

        repos = await self.get_repos()

        for repo in repos:
            repo_name = repo["full_name"]

            # Commits
            try:
                commits = await self.get_commits(repo_name, since=since)
                for commit in commits:
                    sha = commit["sha"]
                    await self._upsert_activity(
                        db,
                        slack_team_id=slack_team_id,
                        slack_user_id=slack_user_id,
                        activity_type="commit",
                        repo_full_name=repo_name,
                        ref_id=sha,
                        title=commit["commit"]["message"].split("\n")[0][:512],
                        url=commit["html_url"],
                        raw_payload=commit,
                        activity_at=datetime.fromisoformat(
                            commit["commit"]["author"]["date"].replace("Z", "+00:00")
                        ).replace(tzinfo=None),
                    )
                    counts["commits"] += 1
            except Exception as e:
                logger.warning("Failed commits for %s: %s", repo_name, _fmt_err(e))

            # Pull Requests
            try:
                prs = await self.get_pull_requests(repo_name)
                for pr in prs:
                    if pr["user"]["login"] != self.github_login:
                        continue
                    if since and datetime.fromisoformat(
                        pr["created_at"].replace("Z", "+00:00")
                    ).replace(tzinfo=None) < since:
                        continue

                    pr_type = "pr_merged" if pr.get("merged_at") else "pr_opened"
                    await self._upsert_activity(
                        db,
                        slack_team_id=slack_team_id,
                        slack_user_id=slack_user_id,
                        activity_type=pr_type,
                        repo_full_name=repo_name,
                        ref_id=str(pr["number"]),
                        title=pr["title"][:512],
                        url=pr["html_url"],
                        raw_payload={
                            "number": pr["number"],
                            "title": pr["title"],
                            "state": pr["state"],
                            "draft": pr.get("draft"),
                            "labels": [l["name"] for l in pr.get("labels", [])],
                            "created_at": pr["created_at"],
                            "merged_at": pr.get("merged_at"),
                        },
                        activity_at=datetime.fromisoformat(
                            pr["created_at"].replace("Z", "+00:00")
                        ).replace(tzinfo=None),
                    )
                    counts["prs"] += 1

                    # Reviews given by this user on other PRs
                    reviews = await self.get_pr_reviews(repo_name, pr["number"])
                    for review in reviews:
                        if review["user"]["login"] != self.github_login:
                            continue
                        review_key = f"{pr['number']}-{review['id']}"
                        await self._upsert_activity(
                            db,
                            slack_team_id=slack_team_id,
                            slack_user_id=slack_user_id,
                            activity_type="pr_review",
                            repo_full_name=repo_name,
                            ref_id=review_key,
                            title=f"Review on PR #{pr['number']}: {pr['title'][:400]}",
                            url=review.get("html_url", pr["html_url"]),
                            raw_payload={
                                "pr_number": pr["number"],
                                "state": review["state"],
                                "submitted_at": review.get("submitted_at"),
                            },
                            activity_at=datetime.fromisoformat(
                                review["submitted_at"].replace("Z", "+00:00")
                            ).replace(tzinfo=None)
                            if review.get("submitted_at")
                            else datetime.utcnow(),
                        )
                        counts["reviews"] += 1

            except Exception as e:
                logger.warning("Failed PRs for %s: %s", repo_name, _fmt_err(e))

        logger.info(
            "GitHub ingestion complete for %s: %s", self.github_login, counts
        )
        return counts

    # ──────────────────────────────────────────────────────────────────────────
    # Overview / Search-API mode — mirrors what the GitHub overview page shows
    # (https://github.com/<user>?tab=overview&from=YYYY-MM-DD&to=YYYY-MM-DD).
    #
    # Uses the Search API so we get PRs across **all** organizations the PAT
    # can see, without iterating every repo. Far faster and cross-org.
    # ──────────────────────────────────────────────────────────────────────────

    async def _search_issues(self, query: str, max_pages: int = 10) -> list[dict]:
        """Paginate /search/issues. Hard-capped (Search API max = 1000 results)."""
        results: list[dict] = []
        for page in range(1, max_pages + 1):
            data = await self._get(
                "/search/issues",
                {"q": query, "per_page": 100, "page": page, "sort": "created", "order": "desc"},
            )
            items = (data or {}).get("items", []) if isinstance(data, dict) else []
            if not items:
                break
            results.extend(items)
            if len(items) < 100:
                break
        return results

    @staticmethod
    def _repo_full_name_from_url(repo_url: str) -> str:
        # repository_url looks like "https://api.github.com/repos/owner/repo"
        return "/".join(repo_url.rstrip("/").split("/")[-2:])

    async def ingest_via_search(
        self,
        db: AsyncSession,
        slack_team_id: str,
        slack_user_id: str,
        since: datetime,
        until: datetime | None = None,
    ) -> dict[str, int]:
        """Ingest PRs created and reviewed by `self.github_login` in [since, until].

        Mirrors the GitHub user overview page. Cross-org, single PAT.
        Skips commits (Search API for commits is heavily rate-limited).
        """
        counts: dict[str, int] = {"prs": 0, "reviews": 0}
        s_date = since.date().isoformat()
        u_date = (until or datetime.utcnow()).date().isoformat()
        date_range = f"{s_date}..{u_date}"

        # ── PRs authored ─────────────────────────────────────────────────────
        try:
            authored = await self._search_issues(
                f"is:pr author:{self.github_login} created:{date_range}"
            )
        except Exception as e:
            logger.warning(
                "Search authored PRs failed for %s: %s",
                self.github_login, _fmt_err(e),
            )
            authored = []

        for pr in authored:
            repo_name = self._repo_full_name_from_url(pr.get("repository_url", ""))
            pr_number = pr.get("number")
            if not pr_number or not repo_name:
                continue
            merged_at = (pr.get("pull_request") or {}).get("merged_at")
            pr_type = "pr_merged" if merged_at else "pr_opened"
            await self._upsert_activity(
                db,
                slack_team_id=slack_team_id,
                slack_user_id=slack_user_id,
                activity_type=pr_type,
                repo_full_name=repo_name,
                ref_id=str(pr_number),
                title=(pr.get("title") or "")[:512],
                url=pr.get("html_url"),
                raw_payload={
                    "number": pr_number,
                    "title": pr.get("title"),
                    "state": pr.get("state"),
                    "draft": pr.get("draft"),
                    "labels": [l.get("name") for l in pr.get("labels", [])],
                    "created_at": pr.get("created_at"),
                    "merged_at": merged_at,
                    "via": "search",
                },
                activity_at=datetime.fromisoformat(
                    pr["created_at"].replace("Z", "+00:00")
                ).replace(tzinfo=None),
            )
            counts["prs"] += 1

        # ── PRs reviewed (exclude self-authored) ─────────────────────────────
        try:
            reviewed = await self._search_issues(
                f"is:pr reviewed-by:{self.github_login} -author:{self.github_login} "
                f"updated:{date_range}"
            )
        except Exception as e:
            logger.warning(
                "Search reviewed PRs failed for %s: %s",
                self.github_login, _fmt_err(e),
            )
            reviewed = []

        for pr in reviewed:
            repo_name = self._repo_full_name_from_url(pr.get("repository_url", ""))
            pr_number = pr.get("number")
            if not pr_number or not repo_name:
                continue
            try:
                reviews = await self.get_pr_reviews(repo_name, pr_number)
            except Exception as e:
                logger.warning(
                    "get_pr_reviews failed %s#%s: %s",
                    repo_name, pr_number, _fmt_err(e),
                )
                continue

            for review in reviews:
                if (review.get("user") or {}).get("login") != self.github_login:
                    continue
                submitted = review.get("submitted_at")
                if submitted:
                    sub_dt = datetime.fromisoformat(submitted.replace("Z", "+00:00")).replace(tzinfo=None)
                    if sub_dt < since:
                        continue
                    if until and sub_dt > until:
                        continue
                else:
                    sub_dt = datetime.utcnow()

                review_key = f"{pr_number}-{review['id']}"
                await self._upsert_activity(
                    db,
                    slack_team_id=slack_team_id,
                    slack_user_id=slack_user_id,
                    activity_type="pr_review",
                    repo_full_name=repo_name,
                    ref_id=review_key,
                    title=f"Review on PR #{pr_number}: {(pr.get('title') or '')[:400]}",
                    url=review.get("html_url") or pr.get("html_url"),
                    raw_payload={
                        "pr_number": pr_number,
                        "state": review.get("state"),
                        "submitted_at": submitted,
                        "via": "search",
                    },
                    activity_at=sub_dt,
                )
                counts["reviews"] += 1

        logger.info(
            "GitHub overview-search ingest for %s [%s..%s]: %s",
            self.github_login, s_date, u_date, counts,
        )
        return counts

    async def _upsert_activity(
        self,
        db: AsyncSession,
        **kwargs: Any,
    ) -> None:
        ref_id = kwargs["ref_id"]
        repo = kwargs["repo_full_name"]
        activity_type = kwargs["activity_type"]

        existing = await db.execute(
            select(GitHubActivity).where(
                GitHubActivity.ref_id == ref_id,
                GitHubActivity.repo_full_name == repo,
                GitHubActivity.activity_type == activity_type,
            )
        )
        if existing.scalar_one_or_none():
            return

        record = GitHubActivity(github_login=self.github_login, **kwargs)
        db.add(record)
        await db.flush()


async def get_github_ingester(
    db: AsyncSession, slack_user_id: str, slack_team_id: str
) -> GitHubIngester | None:
    """Build an ingester using the server-wide PAT + the user's stored login."""
    from app.config import get_settings
    pat = (get_settings().github_pat or "").strip()
    if not pat:
        return None

    result = await db.execute(
        select(UserGitHubLink.github_login).where(
            UserGitHubLink.slack_user_id == slack_user_id,
            UserGitHubLink.slack_team_id == slack_team_id,
        )
    )
    login = result.scalar_one_or_none()
    if not login:
        return None
    return GitHubIngester(access_token=pat, github_login=login)
