"""Minimal async GitLab REST v4 client (merge requests, protected branches) – research 20261008-015.

Authentication uses the ``PRIVATE-TOKEN`` header with a token resolved from a secret reference
(``settings.gitlab_token_ref``). The token is resolved lazily, registered with the redactor and never logged.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any
from urllib.parse import quote

import httpx

from hermclaw.core.config import HermclawConfig
from hermclaw.core.logging import get_logger
from hermclaw.core.redaction import DEFAULT_REDACTOR
from hermclaw.core.settings import Settings
from hermclaw.gitops._secrets import read_secret
from hermclaw.gitops.errors import GitLabError
from hermclaw.gitops.types import MergeRequestInfo

log = get_logger(__name__)
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


def _project(project_id: str | int) -> str:
    return quote(str(project_id), safe="")


def _mr_from_json(data: dict[str, Any], *, created: bool) -> MergeRequestInfo:
    return MergeRequestInfo(
        id=int(data["id"]),
        iid=int(data["iid"]),
        web_url=data.get("web_url"),
        state=data.get("state"),
        title=data.get("title"),
        source_branch=str(data.get("source_branch", "")),
        target_branch=str(data.get("target_branch", "")),
        created=created,
    )


class GitLabClient:
    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        token_ref: str | None = None,
        timeout: float = 30.0,
        retries: int = 2,
        backoff_seconds: float = 0.5,
        verify: bool | str = True,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if token is None and token_ref is None:
            raise GitLabError("GitLabClient needs a token or a token_ref")
        base = base_url.rstrip("/")
        self.api_url = base if base.endswith("/api/v4") else base + "/api/v4"
        self._token = token
        self._token_ref = token_ref
        if token:
            DEFAULT_REDACTOR.add_literal(token)
        self._retries = max(0, retries)
        self._backoff = backoff_seconds
        self._client = httpx.AsyncClient(timeout=timeout, verify=verify, transport=transport, follow_redirects=False)

    @classmethod
    def from_config(cls, config: HermclawConfig, settings: Settings, **kwargs: Any) -> GitLabClient | None:
        """Client for the host with role ``gitlab`` (``labels.api_url`` overrides ``http://<address>``)."""
        hosts = config.hosts.by_role("gitlab")
        if not hosts:
            return None
        host = hosts[0]
        base = host.labels.get("api_url") or f"http://{host.address}"
        return cls(base, token_ref=settings.gitlab_token_ref, **kwargs)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> GitLabClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    def _headers(self) -> dict[str, str]:
        if self._token is None:
            assert self._token_ref is not None
            self._token = read_secret(self._token_ref)
        return {"PRIVATE-TOKEN": self._token, "Accept": "application/json"}

    async def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None, json: Any = None) -> httpx.Response:
        url = self.api_url + path
        last_error: str = ""
        for attempt in range(self._retries + 1):
            try:
                resp = await self._client.request(method, url, params=params, json=json, headers=self._headers())
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < self._retries:
                    await asyncio.sleep(self._backoff * (2**attempt))
                    continue
                raise GitLabError(
                    f"GitLab request failed: {DEFAULT_REDACTOR.text(last_error)}",
                    details={"method": method, "path": path, "attempts": attempt + 1},
                ) from exc
            if resp.status_code in RETRY_STATUS and attempt < self._retries:
                last_error = f"HTTP {resp.status_code}"
                await asyncio.sleep(self._backoff * (2**attempt))
                continue
            return resp
        raise GitLabError(f"GitLab request failed: {last_error}", details={"method": method, "path": path})  # pragma: no cover

    @staticmethod
    def _error(resp: httpx.Response, what: str) -> GitLabError:
        message: Any = ""
        with contextlib.suppress(ValueError):
            body = resp.json()
            if isinstance(body, dict):
                message = body.get("message") or body.get("error") or ""
        text = DEFAULT_REDACTOR.text(str(message))[:500]
        return GitLabError(f"GitLab {what} failed: HTTP {resp.status_code} {text}".strip(), details={"status": resp.status_code})

    async def _get_json(self, path: str, *, params: dict[str, Any] | None = None, what: str) -> Any:
        resp = await self._request("GET", path, params=params)
        if resp.status_code != 200:
            raise self._error(resp, what)
        return resp.json()

    async def _paged(self, path: str, *, params: dict[str, Any] | None = None, what: str, max_pages: int = 50) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page = 1
        for _ in range(max_pages):
            resp = await self._request("GET", path, params={**(params or {}), "per_page": 100, "page": page})
            if resp.status_code != 200:
                raise self._error(resp, what)
            data = resp.json()
            if not isinstance(data, list):
                raise GitLabError(f"GitLab {what} returned unexpected payload", details={"status": resp.status_code})
            out.extend(d for d in data if isinstance(d, dict))
            nxt = resp.headers.get("X-Next-Page", "").strip()
            if not nxt.isdigit() or int(nxt) <= page:
                break  # last page (or a malformed/looping header)
            page = int(nxt)
        return out

    # ------------------------------------------------------------------------------------------- API
    async def get_project(self, project_id: str | int) -> dict[str, Any]:
        data = await self._get_json(f"/projects/{_project(project_id)}", what="project lookup")
        if not isinstance(data, dict):
            raise GitLabError("GitLab project lookup returned unexpected payload")
        return data

    async def list_protected_branches(self, project_id: str | int) -> list[str]:
        rows = await self._paged(f"/projects/{_project(project_id)}/protected_branches", what="protected branch listing")
        return [str(r["name"]) for r in rows if r.get("name")]

    async def find_open_merge_request(
        self, project_id: str | int, *, source_branch: str, target_branch: str | None = None
    ) -> MergeRequestInfo | None:
        params: dict[str, Any] = {"source_branch": source_branch, "state": "opened"}
        if target_branch:
            params["target_branch"] = target_branch
        rows = await self._paged(
            f"/projects/{_project(project_id)}/merge_requests", params=params, what="merge request lookup", max_pages=2
        )
        for row in rows:
            if row.get("source_branch") == source_branch and (target_branch is None or row.get("target_branch") == target_branch):
                return _mr_from_json(row, created=False)
        return None

    async def create_merge_request(
        self,
        project_id: str | int,
        *,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str = "",
        remove_source_branch: bool = False,
        labels: list[str] | None = None,
    ) -> MergeRequestInfo:
        """Create an MR unless an open one for the same source/target exists (duplicate detection)."""
        existing = await self.find_open_merge_request(project_id, source_branch=source_branch, target_branch=target_branch)
        if existing is not None:
            return existing
        body: dict[str, Any] = {
            "source_branch": source_branch,
            "target_branch": target_branch,
            "title": DEFAULT_REDACTOR.text(title)[:255],
            "description": DEFAULT_REDACTOR.text(description)[:100_000],
            "remove_source_branch": remove_source_branch,
        }
        if labels:
            body["labels"] = ",".join(labels)
        resp = await self._request("POST", f"/projects/{_project(project_id)}/merge_requests", json=body)
        if resp.status_code == 201:
            data = resp.json()
            if not isinstance(data, dict):
                raise GitLabError("GitLab merge request creation returned unexpected payload")
            return _mr_from_json(data, created=True)
        if resp.status_code == 409:  # raced with another creator: "Another open merge request already exists"
            again = await self.find_open_merge_request(project_id, source_branch=source_branch, target_branch=target_branch)
            if again is not None:
                return again
        raise self._error(resp, "merge request creation")

    async def delete_branch(self, project_id: str | int, branch: str) -> bool:
        resp = await self._request("DELETE", f"/projects/{_project(project_id)}/repository/branches/{quote(branch, safe='')}")
        if resp.status_code in (204, 404):
            return resp.status_code == 204
        raise self._error(resp, "branch deletion")
