"""Read preferences from / write status to the repo via the GitHub REST API.

Used by the Actions runner. Preferences live on the default branch
(preferences.json) so you can edit them from the dashboard or GitHub's web
editor. Live status is written to a dedicated `bot-state` branch so it never
pollutes main and the static dashboard can read it from the raw CDN.

Auth: the workflow's GITHUB_TOKEN (passed as env GH_TOKEN). It has contents
read/write on this repo, which is all we need.
"""
from __future__ import annotations

import base64
import json
import os
from typing import Any, Optional

import httpx

API = "https://api.github.com"


class GitHubState:
    def __init__(self) -> None:
        self.repo = os.environ["GITHUB_REPOSITORY"]            # "owner/name"
        self.token = os.environ.get("GH_TOKEN") or os.environ["GITHUB_TOKEN"]
        self.state_branch = os.getenv("STATE_BRANCH", "bot-state")
        self.prefs_path = os.getenv("PREFS_PATH", "preferences.json")
        self.state_path = os.getenv("STATE_PATH", "state.json")
        self.http = httpx.AsyncClient(
            base_url=API,
            timeout=30.0,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        self._default_branch: Optional[str] = None
        self._state_sha: Optional[str] = None

    async def close(self) -> None:
        await self.http.aclose()

    async def default_branch(self) -> str:
        if self._default_branch is None:
            r = await self.http.get(f"/repos/{self.repo}")
            r.raise_for_status()
            self._default_branch = r.json().get("default_branch", "main")
        return self._default_branch

    async def ensure_state_branch(self) -> None:
        r = await self.http.get(f"/repos/{self.repo}/branches/{self.state_branch}")
        if r.status_code == 200:
            return
        base = await self.default_branch()
        ref = await self.http.get(f"/repos/{self.repo}/git/ref/heads/{base}")
        ref.raise_for_status()
        sha = ref.json()["object"]["sha"]
        await self.http.post(
            f"/repos/{self.repo}/git/refs",
            json={"ref": f"refs/heads/{self.state_branch}", "sha": sha},
        )

    async def read_prefs(self) -> dict[str, Any]:
        branch = await self.default_branch()
        r = await self.http.get(
            f"/repos/{self.repo}/contents/{self.prefs_path}",
            params={"ref": branch},
        )
        if r.status_code != 200:
            return {}
        try:
            return json.loads(base64.b64decode(r.json()["content"]))
        except Exception:  # noqa: BLE001
            return {}

    async def read_state(self) -> dict[str, Any]:
        r = await self.http.get(
            f"/repos/{self.repo}/contents/{self.state_path}",
            params={"ref": self.state_branch},
        )
        if r.status_code != 200:
            return {}
        try:
            self._state_sha = r.json()["sha"]
            return json.loads(base64.b64decode(r.json()["content"]))
        except Exception:  # noqa: BLE001
            return {}

    async def write_state(self, obj: dict[str, Any]) -> None:
        content = base64.b64encode(
            json.dumps(obj, ensure_ascii=False, default=str, indent=2).encode()
        ).decode()
        payload: dict[str, Any] = {
            "message": "state update [skip ci]",
            "content": content,
            "branch": self.state_branch,
        }
        if self._state_sha:
            payload["sha"] = self._state_sha
        r = await self.http.put(
            f"/repos/{self.repo}/contents/{self.state_path}", json=payload
        )
        if r.status_code in (200, 201):
            self._state_sha = r.json()["content"]["sha"]
        elif r.status_code == 409 or r.status_code == 422:
            # sha out of date — refetch and retry once
            await self.read_state()
            if self._state_sha:
                payload["sha"] = self._state_sha
            await self.http.put(
                f"/repos/{self.repo}/contents/{self.state_path}", json=payload
            )
