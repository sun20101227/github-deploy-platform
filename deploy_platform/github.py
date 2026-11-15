from __future__ import annotations

from urllib.parse import quote

import httpx


API = "https://api.github.com/"
MAX_REPOSITORY_PAGES = 10  # 100 per page


async def repositories(access_token: str) -> list[dict]:
    items = []
    url = API + "user/repos?per_page=100&sort=updated"
    async with httpx.AsyncClient(timeout=20) as client:
        for _ in range(MAX_REPOSITORY_PAGES):
            response = await client.get(
                url,
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github+json"},
            )
            response.raise_for_status()
            items += [
                {"full_name": r["full_name"], "default_branch": r.get("default_branch") or "main", "private": r["private"]}
                for r in response.json()
            ]
            url = response.links.get("next", {}).get("url", "")
            # The token goes to whatever this URL names, so follow it only inside the GitHub API.
            if not url.startswith(API):
                break
    return items


async def latest_commit(access_token: str, repository: str, branch: str) -> dict:
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(
            f"https://api.github.com/repos/{repository}/commits/{quote(branch, safe='')}",
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github+json"},
        )
        response.raise_for_status()
        commit = response.json()
    return {
        "sha": commit["sha"],
        "message": commit.get("commit", {}).get("message", "").splitlines()[0],
        "date": commit.get("commit", {}).get("author", {}).get("date", ""),
    }

