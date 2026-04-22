"""
GitHub Ingester — pulls commits, PRs, reviews, and issues per user.

Uses the GitHub REST API v3 with the user's personal OAuth token.
Stores raw activity into GitHubActivity table.
"""

import logging
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from tenacity import retry, stop_after_attempt, wait_exponential

from app.models.raw_data import GitHubActivity
from app.models.user import UserGitHubLink

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"


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

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    async def _get(self, path: str, params: dict | None = None) -> Any:
        resp = await self._client.get(path, params=params or {})
        if resp.status_code == 422:
            return []
        resp.raise_for_status()
        return resp.json()

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
                logger.warning("Search repos failed (%s): %s", q, e)
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
            logger.warning("Failed commits for %s: %s", repo_name, e)

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
            logger.warning("Failed PRs for %s: %s", repo_name, e)

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
                logger.warning("Failed commits for %s: %s", repo_name, e)

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
                logger.warning("Failed PRs for %s: %s", repo_name, e)

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
            logger.warning("Search authored PRs failed for %s: %s", self.github_login, e)
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
            logger.warning("Search reviewed PRs failed for %s: %s", self.github_login, e)
            reviewed = []

        for pr in reviewed:
            repo_name = self._repo_full_name_from_url(pr.get("repository_url", ""))
            pr_number = pr.get("number")
            if not pr_number or not repo_name:
                continue
            try:
                reviews = await self.get_pr_reviews(repo_name, pr_number)
            except Exception as e:
                logger.warning("get_pr_reviews failed %s#%s: %s", repo_name, pr_number, e)
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
