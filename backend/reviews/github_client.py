"""Minimal GitHub REST client and PR snapshot/publishing helpers.

No local clone: the changed files and unified diff come straight from the API.
Findings are published as a single sticky summary comment plus inline review
comments anchored to added lines in the current diff.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

# GitHub's PR diff media type is all-or-nothing; over this many changed lines the
# API returns 406 and we fall back to reconstructing the diff from /files.
GITHUB_PR_DIFF_LINE_LIMIT = 20_000

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_INLINE_MARKER_RE = re.compile(r"<!-- prcheck:inline:([0-9a-f]{16}) -->")
_SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}


class GitHubAPIError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class GitHubAPI:
    def __init__(self, repo: str, token: str, base: str = "https://api.github.com"):
        self.repo = repo
        self.token = token
        self.base = base

    @property
    def enabled(self) -> bool:
        return bool(self.repo and self.token)

    def request(self, method: str, path: str, body: dict | None = None):
        url = f"{self.base}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "prcheck-reviews/1.0",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                text = resp.read().decode()
                return json.loads(text) if text.strip() else {}
        except urllib.error.HTTPError as exc:
            raise GitHubAPIError(
                f"{method} {path} -> {exc.code}: {exc.read().decode()[:500]}",
                status_code=exc.code,
            ) from exc
        except urllib.error.URLError as exc:
            raise GitHubAPIError(f"{method} {path}: {exc.reason}") from exc

    def get(self, path: str):
        return self.request("GET", path)

    def post(self, path: str, body: dict):
        return self.request("POST", path, body)

    def patch(self, path: str, body: dict):
        return self.request("PATCH", path, body)

    def delete(self, path: str):
        return self.request("DELETE", path)

    def get_text(self, path: str, *, accept: str, timeout: float = 60) -> str:
        url = f"{self.base}{path}"
        req = urllib.request.Request(url, method="GET", headers={
            "Authorization": f"Bearer {self.token}",
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "prcheck-reviews/1.0",
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode()
        except urllib.error.HTTPError as exc:
            raise GitHubAPIError(
                f"GET {path} -> {exc.code}: {exc.read().decode()[:500]}",
                status_code=exc.code,
            ) from exc
        except urllib.error.URLError as exc:
            raise GitHubAPIError(f"GET {path}: {exc.reason}") from exc

    def list_pages(self, path: str, *, per_page: int = 100, max_pages: int = 50) -> list:
        items: list = []
        sep = "&" if "?" in path else "?"
        for page in range(1, max_pages + 1):
            chunk = self.get(f"{path}{sep}per_page={per_page}&page={page}")
            if not isinstance(chunk, list) or not chunk:
                break
            items.extend(chunk)
            if len(chunk) < per_page:
                break
        return items

    def pull_request(self, pr_number: int) -> dict:
        result = self.get(f"/repos/{self.repo}/pulls/{pr_number}")
        return result if isinstance(result, dict) else {}

    def pull_request_diff(self, pr_number: int) -> str:
        return self.get_text(
            f"/repos/{self.repo}/pulls/{pr_number}",
            accept="application/vnd.github.diff",
        )

    def pull_request_files(self, pr_number: int) -> list[dict]:
        rows = self.list_pages(f"/repos/{self.repo}/pulls/{pr_number}/files")
        return [row for row in rows if isinstance(row, dict)]

    def file_content(self, path: str, ref: str) -> str:
        """Return a file's raw text at ``ref`` (empty string if unavailable)."""
        from urllib.parse import quote
        try:
            return self.get_text(
                f"/repos/{self.repo}/contents/{quote(path)}?ref={ref}",
                accept="application/vnd.github.raw",
            )
        except GitHubAPIError:
            return ""  # binary, too large, or missing — fall back to diff-only


@dataclass(frozen=True)
class PullSnapshot:
    title: str
    url: str
    head_sha: str
    files: list[dict]
    diff_text: str
    additions: int
    deletions: int

    @property
    def changed_files(self) -> list[dict]:
        """Compact per-file metadata for the reviewer prompt."""
        out = []
        for item in self.files:
            out.append({
                "path": str(item.get("filename") or ""),
                "status": str(item.get("status") or "modified"),
                "additions": int(item.get("additions") or 0),
                "deletions": int(item.get("deletions") or 0),
            })
        return out


def fetch_pull_snapshot(api: GitHubAPI, pr_number: int) -> PullSnapshot:
    """Load PR metadata, changed files, and a unified diff (no local clone)."""
    pr = api.pull_request(pr_number)
    files = api.pull_request_files(pr_number)
    additions = sum(int(item.get("additions") or 0) for item in files)
    deletions = sum(int(item.get("deletions") or 0) for item in files)

    diff_text = ""
    if additions + deletions < GITHUB_PR_DIFF_LINE_LIMIT:
        try:
            diff_text = (api.pull_request_diff(pr_number) or "").strip()
        except GitHubAPIError as exc:
            if not _is_pr_diff_too_large(exc):
                raise
    if not diff_text:
        diff_text = unified_diff_from_pull_files(files)

    return PullSnapshot(
        title=str(pr.get("title") or f"PR #{pr_number}"),
        url=str(pr.get("html_url") or f"https://github.com/{api.repo}/pull/{pr_number}"),
        head_sha=str((pr.get("head") or {}).get("sha") or ""),
        files=files,
        diff_text=diff_text,
        additions=additions,
        deletions=deletions,
    )


def _is_pr_diff_too_large(exc: GitHubAPIError) -> bool:
    if exc.status_code not in (None, 406):
        return False
    text = str(exc).lower()
    return "too_large" in text or "maximum number of lines" in text or "diff exceeded" in text


def unified_diff_from_pull_files(files: list[dict]) -> str:
    """Rebuild a unified diff from paginated ``/pulls/{n}/files`` rows."""
    parts: list[str] = []
    for item in files:
        patch = item.get("patch")
        if not patch:
            continue
        filename = str(item.get("filename") or "")
        previous = str(item.get("previous_filename") or filename)
        status = str(item.get("status") or "modified")
        parts.append(f"diff --git a/{previous} b/{filename}\n")
        if status == "added":
            parts.append(f"--- /dev/null\n+++ b/{filename}\n")
        elif status in {"removed", "deleted"}:
            parts.append(f"--- a/{previous}\n+++ /dev/null\n")
        else:
            parts.append(f"--- a/{previous}\n+++ b/{filename}\n")
        text = str(patch)
        parts.append(text if text.endswith("\n") else text + "\n")
    return "".join(parts)


def added_lines_by_file(diff_text: str) -> dict[str, set[int]]:
    """Parse the set of added (RIGHT-side) line numbers per file from a diff."""
    result: dict[str, set[int]] = {}
    path: str | None = None
    new_line: int | None = None
    for raw in diff_text.splitlines():
        if raw.startswith("diff --git "):
            path, new_line = None, None
            continue
        if raw.startswith("--- ") and new_line is None:
            path, new_line = None, None
            continue
        if raw.startswith("+++ b/"):
            path = raw[6:]
            result.setdefault(path, set())
            new_line = None
            continue
        match = _HUNK_RE.match(raw)
        if match:
            new_line = int(match.group(1))
            continue
        if new_line is None or path is None:
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            result.setdefault(path, set()).add(new_line)
            new_line += 1
        elif raw.startswith("-") and not raw.startswith("---"):
            continue
        elif raw.startswith("\\"):
            continue
        else:
            new_line += 1
    return result


# --------------------------------------------------------------------------- #
# Publishing
# --------------------------------------------------------------------------- #

def _summary_marker(pr: int) -> str:
    return f"<!-- prcheck:summary:{pr} -->"


def render_summary(verdict: str, conditions: list[str], findings: list[dict]) -> str:
    icon = {"block": "\U0001f6d1", "request-changes": "\U0001f527",
            "approve-with-conditions": "\u26a0\ufe0f", "approve": "\u2705"}.get(verdict, "\U0001f50d")
    lines = [f"## {icon} prcheck review: `{verdict}`", ""]
    if conditions:
        lines.append("**Conditions:**")
        lines += [f"- {c}" for c in conditions]
        lines.append("")
    if not findings:
        lines.append("No blocking issues found in the changed lines.")
    else:
        lines.append(f"**{len(findings)} finding(s):**")
        lines.append("")
        lines.append("| Severity | Category | Location | Issue |")
        lines.append("|---|---|---|---|")
        for f in _sorted(findings):
            loc = f"`{f.get('path','')}:{f.get('line','')}`"
            text = str(f.get("text", "")).replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {f.get('severity','')} | {f.get('category','')} | {loc} | {text} |")
    lines += ["", "_Automated review by prcheck._"]
    return "\n".join(lines)


def _sorted(findings: list[dict]) -> list[dict]:
    return sorted(findings, key=lambda f: _SEVERITY_RANK.get(str(f.get("severity") or "").lower(), 4))


def _inline_marker(finding: dict) -> str:
    import hashlib
    key = f"{finding.get('path')}:{finding.get('line')}:{finding.get('text')}"
    return f"<!-- prcheck:inline:{hashlib.sha256(key.encode()).hexdigest()[:16]} -->"


def _render_inline(finding: dict) -> str:
    sev = str(finding.get("severity", "")).upper()
    cat = finding.get("category", "")
    return f"**prcheck [{sev}/{cat}]**\n\n{finding.get('text','')}\n\n{_inline_marker(finding)}"


def upsert_summary_comment(api: GitHubAPI, pr: int, body: str) -> None:
    if not api.enabled:
        return
    mark = _summary_marker(pr)
    body = f"{body}\n\n{mark}"
    comments = api.get(f"/repos/{api.repo}/issues/{pr}/comments?per_page=100")
    existing = None
    if isinstance(comments, list):
        existing = next((c for c in comments if isinstance(c, dict) and mark in (c.get("body") or "")), None)
    if existing:
        api.patch(f"/repos/{api.repo}/issues/comments/{existing['id']}", {"body": body})
    else:
        api.post(f"/repos/{api.repo}/issues/{pr}/comments", {"body": body})


# A check run gives the PR a visible "prcheck is reviewing…" status that
# resolves to pass/fail, separate from the review comments.
_VERDICT_CONCLUSION = {
    "block": "failure",
    "request-changes": "failure",
    "approve-with-conditions": "neutral",
    "approve": "success",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_check_run(api: GitHubAPI, head_sha: str) -> int | None:
    """Open an in-progress check run; returns its id (or None on failure)."""
    if not api.enabled or not head_sha:
        return None
    try:
        result = api.post(f"/repos/{api.repo}/check-runs", {
            "name": "prcheck / review",
            "head_sha": head_sha,
            "status": "in_progress",
            "started_at": _now_iso(),
            "output": {"title": "prcheck is reviewing…",
                       "summary": "Running the code review on this pull request."},
        })
        return result.get("id") if isinstance(result, dict) else None
    except GitHubAPIError:
        return None  # missing Checks:write permission should not fail the review


def complete_check_run(api: GitHubAPI, check_run_id: int | None, verdict: str,
                       conditions: list[dict], findings: list[dict]) -> None:
    if not api.enabled or not check_run_id:
        return
    title = f"prcheck: {verdict} — {len(findings)} finding(s)"
    try:
        api.patch(f"/repos/{api.repo}/check-runs/{check_run_id}", {
            "status": "completed",
            "conclusion": _VERDICT_CONCLUSION.get(verdict, "neutral"),
            "completed_at": _now_iso(),
            "output": {"title": title, "summary": render_summary(verdict, conditions, findings)},
        })
    except GitHubAPIError:
        pass


def publish_inline_comments(api: GitHubAPI, pr: int, head_sha: str, diff_text: str,
                            findings: list[dict], *, limit: int = 40) -> int:
    """Post findings as inline review comments on changed lines. Returns count posted."""
    if not api.enabled or not head_sha:
        return 0
    changed = added_lines_by_file(diff_text)
    candidates: list[tuple[dict, str, int]] = []
    for finding in _sorted(findings):
        path = str(finding.get("path") or "").strip()
        line = finding.get("line")
        if not path or type(line) is not int:
            continue
        if line not in changed.get(path, set()):
            continue
        candidates.append((finding, path, line))
        if len(candidates) >= limit:
            break

    posted = 0
    for finding, path, line in candidates:
        api.post(
            f"/repos/{api.repo}/pulls/{pr}/comments",
            {
                "body": _render_inline(finding),
                "commit_id": head_sha,
                "path": path,
                "line": line,
                "side": "RIGHT",
            },
        )
        posted += 1
    return posted
