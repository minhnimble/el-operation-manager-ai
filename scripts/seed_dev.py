"""
Dev seed script — populate the database with realistic fake data for local testing.

Usage: python -m scripts.seed_dev
"""

import asyncio
import random
from datetime import datetime, timedelta

from app.database import AsyncSessionLocal, create_tables
from app.models.installation import SlackInstallation
from app.models.user import User, UserGitHubLink
from app.models.raw_data import SlackMessage, GitHubActivity

TEAM_ID = "T_DEV001"
BOT_TOKEN = "xoxb-dev-fake-token"

USERS = [
    {"id": "U_ALICE", "name": "alice", "real_name": "Alice Chen", "github": "alice-chen"},
    {"id": "U_BOB", "name": "bob", "real_name": "Bob Smith", "github": "bob-smith"},
    {"id": "U_CAROL", "name": "carol", "real_name": "Carol Johnson", "github": "carol-j"},
]

STANDUPS = [
    "Yesterday: shipped the auth PR, fixed the token refresh bug. Today: writing tests. No blockers.",
    "Yesterday: reviewed 3 PRs, helped Carol debug the caching issue. Today: working on API rate limiting. Blocked on infra team for Redis config.",
    "Yesterday: architecture doc for new microservice. Today: starting scaffold. No blockers.",
    "Yesterday: mentored Bob on async patterns. Today: code review + incident post-mortem. Blocked by waiting on data team.",
    "Yesterday: fixed production incident with DB pool exhaustion. Today: adding monitoring. No blockers.",
]

REPOS = ["org/backend", "org/frontend", "org/infra"]


async def seed():
    await create_tables()
    print("Tables ready.")

    async with AsyncSessionLocal() as db:
        # Slack installation
        install = SlackInstallation(
            team_id=TEAM_ID,
            team_name="Dev Team",
            bot_token=BOT_TOKEN,
            bot_user_id="U_BOT",
        )
        db.add(install)
        await db.flush()

        for u in USERS:
            user = User(
                slack_user_id=u["id"],
                slack_team_id=TEAM_ID,
                slack_display_name=u["name"],
                slack_real_name=u["real_name"],
                opted_in=True,
            )
            db.add(user)
            await db.flush()

            link = UserGitHubLink(
                slack_user_id=u["id"],
                slack_team_id=TEAM_ID,
                github_login=u["github"],
            )
            db.add(link)

        await db.flush()

        # Seed messages: 30 days of standups + discussions
        base_ts = datetime.utcnow() - timedelta(days=30)
        ts_counter = int(base_ts.timestamp() * 1000)

        for day_offset in range(30):
            day = base_ts + timedelta(days=day_offset)
            if day.weekday() >= 5:  # skip weekends
                continue

            for user in USERS:
                # Daily standup
                ts = f"{int(day.replace(hour=9).timestamp())}.{ts_counter:06d}"
                ts_counter += 1
                msg = SlackMessage(
                    slack_team_id=TEAM_ID,
                    slack_user_id=user["id"],
                    channel_id="C_STANDUP",
                    channel_name="daily-standup",
                    message_ts=ts,
                    text=random.choice(STANDUPS),
                    is_standup_channel=True,
                    is_thread_reply=False,
                    timestamp=day.replace(hour=9),
                )
                db.add(msg)

                # Random discussion messages
                for _ in range(random.randint(0, 5)):
                    ts2 = f"{int(day.replace(hour=random.randint(10,18)).timestamp())}.{ts_counter:06d}"
                    ts_counter += 1
                    db.add(SlackMessage(
                        slack_team_id=TEAM_ID,
                        slack_user_id=user["id"],
                        channel_id="C_ENG",
                        channel_name="engineering",
                        message_ts=ts2,
                        text=f"Discussion message {ts_counter}",
                        is_standup_channel=False,
                        is_thread_reply=random.random() < 0.3,
                        timestamp=day.replace(hour=random.randint(10, 18)),
                    ))

        # Seed GitHub activities
        for user in USERS:
            for day_offset in range(30):
                day = base_ts + timedelta(days=day_offset)
                if day.weekday() >= 5:
                    continue
                repo = random.choice(REPOS)
                # Commits
                for i in range(random.randint(0, 4)):
                    db.add(GitHubActivity(
                        slack_team_id=TEAM_ID,
                        slack_user_id=user["id"],
                        github_login=user["github"],
                        activity_type="commit",
                        repo_full_name=repo,
                        ref_id=f"sha{ts_counter:08x}",
                        title=f"fix: some improvement #{ts_counter}",
                        activity_at=day.replace(hour=random.randint(9, 18)),
                    ))
                    ts_counter += 1

                # PRs
                if random.random() < 0.3:
                    pr_num = ts_counter
                    db.add(GitHubActivity(
                        slack_team_id=TEAM_ID,
                        slack_user_id=user["id"],
                        github_login=user["github"],
                        activity_type="pr_opened",
                        repo_full_name=repo,
                        ref_id=str(pr_num),
                        title=f"feat: new feature #{pr_num}",
                        activity_at=day.replace(hour=10),
                    ))
                    ts_counter += 1

        await db.commit()
        print(f"Seeded {len(USERS)} users, 30 days of activity.")


if __name__ == "__main__":
    asyncio.run(seed())
