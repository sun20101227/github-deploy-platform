from __future__ import annotations

import secrets
from urllib.parse import urlencode

import httpx

from . import config


def authorization_url() -> tuple[str, str]:
    if not config.GITHUB_CLIENT_ID or not config.GITHUB_CLIENT_SECRET:
        raise RuntimeError(
            "GitHub OAuth 尚未配置：请设置 GITHUB_CLIENT_ID 和 GITHUB_CLIENT_SECRET，"
            "或改用服务器端 Deploy Key 模式。"
        )
    state = secrets.token_urlsafe(32)
    query = urlencode(
        {
            "client_id": config.GITHUB_CLIENT_ID,
            "redirect_uri": config.GITHUB_REDIRECT_URI,
            "scope": "read:user repo",
            "state": state,
        }
    )
    return f"https://github.com/login/oauth/authorize?{query}", state


async def exchange_code(code: str) -> dict:
    async with httpx.AsyncClient(timeout=20) as client:
        token_response = await client.post(
            "https://github.com/login/oauth/access_token",
            data={
                "client_id": config.GITHUB_CLIENT_ID,
                "client_secret": config.GITHUB_CLIENT_SECRET,
                "code": code,
            },
            headers={"Accept": "application/json"},
        )
        token_response.raise_for_status()
        token = token_response.json().get("access_token")
        if not token:
            raise ValueError("GitHub 没有返回访问令牌")
        user_response = await client.get(
            "https://api.github.com/user",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        )
        user_response.raise_for_status()
        user = user_response.json()
    return {"github_id": str(user["id"]), "login": user["login"], "access_token": token}


async def repositories(access_token: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(
            "https://api.github.com/user/repos?per_page=100&sort=updated",
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github+json"},
        )
        response.raise_for_status()
        return [
            {"full_name": r["full_name"], "default_branch": r.get("default_branch") or "main", "private": r["private"]}
            for r in response.json()
        ]


async def latest_commit(access_token: str, repository: str, branch: str) -> dict:
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(
            f"https://api.github.com/repos/{repository}/commits/{branch}",
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github+json"},
        )
        response.raise_for_status()
        commit = response.json()
    return {
        "sha": commit["sha"],
        "message": commit.get("commit", {}).get("message", "").splitlines()[0],
        "date": commit.get("commit", {}).get("author", {}).get("date", ""),
    }

