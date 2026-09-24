"""Minimal GitHub REST client and PR snapshot/publishing helpers.

No local clone: the changed files and unified diff come straight from the API.
Findings are published as a single sticky summary comment plus one batched
review whose inline comments sit on changed lines. Inline comments already
posted by an earlier run are not repeated, so a push never re-spams the PR.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import re
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote

from .diffs import line_map
from .findings import SEVERITY_RANK, sort_findings

LOGGER = logging.getLogger("reviews.github")

# GitHub's PR diff media type is all-or-nothing; over this many changed lines the
# API returns 406 and we fall back to reconstructing the diff from /files.
GITHUB_PR_DIFF_LINE_LIMIT = 20_000

_INLINE_MARKER_RE = re.compile(r"<!-- prcheck:inline:([0-9a-f]{16}) -->")


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
        try:
            return self.get_text(
                f"/repos/{self.repo}/contents/{quote(path)}?ref={ref}",
                accept="application/vnd.github.raw",
            )
        except GitHubAPIError:
            return ""  # binary, too large, or missing — fall back to diff-only

    def repository_tree(self, ref: str) -> list[str]:
        """Return blob paths reachable from ``ref`` for bounded symbol lookup."""
        try:
            result = self.get(f"/repos/{self.repo}/git/trees/{quote(ref, safe='')}?recursive=1")
        except GitHubAPIError:
            return []
        if not isinstance(result, dict):
            return []
        return [
            str(item.get("path"))
            for item in result.get("tree", [])
            if isinstance(item, dict) and item.get("type") == "blob" and item.get("path")
        ]

    def commit_check_runs(self, ref: str) -> list[dict]:
        """Return check runs for a commit, or an empty list when unavailable."""
        result = self.get(f"/repos/{self.repo}/commits/{quote(ref, safe='')}/check-runs?per_page=100")
        runs = result.get("check_runs") if isinstance(result, dict) else []
        return [run for run in runs if isinstance(run, dict)]

    def commit_statuses(self, ref: str) -> list[dict]:
        """Return legacy commit statuses for a commit, or an empty list."""
        result = self.get(f"/repos/{self.repo}/commits/{quote(ref, safe='')}/status")
        statuses = result.get("statuses") if isinstance(result, dict) else []
        return [status for status in statuses if isinstance(status, dict)]


@dataclass(frozen=True)
class PullSnapshot:
    title: str
    url: str
    head_sha: str
    files: list[dict]
    diff_text: str
    additions: int
    deletions: int
    # The merge-base commit is trusted context: PR changes must not be able to
    # rewrite the review rules used to judge those same changes.
    base_sha: str = ""

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
        base_sha=str((pr.get("base") or {}).get("sha") or ""),
    )


_TRUSTED_GUIDANCE_PATHS = (
    ".qwen/review-rules.md",
    ".github/copilot-instructions.md",
    "copilot-instructions.md",
    "AGENTS.md",
    "QWEN.md",
    "CONTRIBUTING.md",
    ".github/CONTRIBUTING.md",
)
_MANIFEST_PATH = ".qwen/review-context.json"
_MANIFEST_FIELDS = (
    "domains", "relatedPaths", "recommendedTests", "requiredConfigurations",
    "requiredAgents", "unverifiedDimensions", "verificationNotes",
)


def _path_matches(path: str, patterns) -> bool:
    if isinstance(patterns, str):
        patterns = [patterns]
    return any(
        isinstance(pattern, str) and fnmatch.fnmatchcase(path, pattern)
        for pattern in (patterns or [])
    )


def fetch_repository_guidance(
    api: GitHubAPI,
    *,
    ref: str,
    changed_paths: list[str],
    max_bytes: int = 24_000,
) -> str:
    """Load review policy from a trusted ref, scoped to changed paths.

    The Qwen-style manifest and instructions are read from the merge-base, not
    the PR head, so a change cannot modify the rules used to review itself.
    Returns prompt-ready text and degrades to an empty string on missing files.
    """
    if not api.enabled or not ref:
        return ""

    sections: list[str] = []
    remaining = max(0, max_bytes)
    found_copilot = False
    for path in _TRUSTED_GUIDANCE_PATHS:
        if path == "copilot-instructions.md" and found_copilot:
            continue
        content = api.file_content(path, ref)
        if not content:
            continue
        if path.endswith("copilot-instructions.md"):
            found_copilot = True
        take = min(remaining, len(content))
        if take <= 0:
            break
        sections.append(f"Repository guidance: {path}\n{content[:take]}")
        remaining -= take

    manifest_raw = api.file_content(_MANIFEST_PATH, ref)
    if manifest_raw and remaining > 0:
        try:
            manifest = json.loads(manifest_raw)
        except (TypeError, ValueError):
            manifest = None
        if isinstance(manifest, dict) and isinstance(manifest.get("rules"), list):
            matched_rules = []
            for rule in manifest["rules"]:
                if not isinstance(rule, dict):
                    continue
                patterns = rule.get("paths")
                if _path_matches_any(changed_paths, patterns):
                    matched = {key: rule.get(key) for key in _MANIFEST_FIELDS if rule.get(key)}
                    if matched:
                        matched_rules.append({"paths": patterns, **matched})
            if matched_rules:
                rendered = json.dumps(matched_rules, indent=2, ensure_ascii=False)
                take = min(remaining, len(rendered))
                sections.append(
                    "Repository review context manifest (matched changed paths):\n"
                    + rendered[:take]
                )

    return "\n\n".join(sections)


def _path_matches_any(paths: list[str], patterns) -> bool:
    return any(_path_matches(path, patterns) for path in paths if path)


def fetch_ci_evidence(api: GitHubAPI, head_sha: str) -> dict | None:
    """Summarize external CI state without treating API failures as failures."""
    if not api.enabled or not head_sha:
        return None
    try:
        check_runs = api.commit_check_runs(head_sha)
    except GitHubAPIError:
        return None
    try:
        statuses = api.commit_statuses(head_sha)
    except GitHubAPIError:
        statuses = []

    failed: list[str] = []
    pending: list[str] = []
    for run in check_runs:
        if str(run.get("name") or "") == "prcheck / review":
            continue
        name = str(run.get("name") or run.get("app", {}).get("name") or "check")
        status = str(run.get("status") or "").lower()
        conclusion = str(run.get("conclusion") or "").lower()
        if status != "completed":
            pending.append(name)
        elif conclusion in {"failure", "timed_out", "cancelled", "action_required", "startup_failure"}:
            failed.append(name)

    for status in statuses:
        name = str(status.get("context") or status.get("description") or "status")
        state = str(status.get("state") or "").lower()
        if state == "pending":
            pending.append(name)
        elif state in {"failure", "error"}:
            failed.append(name)

    return {
        "available": True,
        "failed": list(dict.fromkeys(failed)),
        "pending": list(dict.fromkeys(pending)),
        "total": len(check_runs) + len(statuses),
    }


_PY_IMPORT_RE = re.compile(r"^\s*from\s+([.\w]+)\s+import\s+([^#\n]+)", re.M)
_PY_MODULE_RE = re.compile(r"^\s*import\s+([.\w, ]+)", re.M)
_JS_IMPORT_RE = re.compile(r"\b(?:from|import)\s*[('\\\"]([^'\"()]+)", re.M)
_BASE_RE = re.compile(
    r"\b(?:class|interface)\s+\w+\s*(?:\(([^)]*)\)|extends\s+([\w.$<>]+)|implements\s+([^\{]+))",
    re.M,
)
_SYMBOL_RE = re.compile(r"\b(?:class|interface|enum|type|struct)\s+([A-Z][A-Za-z0-9_]*)")
_RELATED_EXTENSIONS = (".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".java", ".kt", ".go", ".rs", ".rb", ".cs")


def _normalise_import_path(source_path: str, imported: str) -> str | None:
    """Map a source-relative import to a repository-relative module path."""
    imported = imported.strip().split("#", 1)[0].strip()
    if not imported or imported.startswith("@"):
        return None
    if imported.startswith("."):
        from posixpath import dirname, normpath
        base = dirname(source_path)
        dots = len(imported) - len(imported.lstrip("."))
        base_parts = base.split("/") if base else []
        base = "/".join(base_parts[:max(0, len(base_parts) - dots + 1)])
        imported = imported[dots:]
        return normpath("/".join(part for part in (base, imported) if part))
    return imported.replace(".", "/").strip("/")


def _related_candidate_paths(source_path: str, content: str, tree_paths: list[str]) -> list[str]:
    """Find likely definitions without pretending to be a full language server."""
    tree = {path for path in tree_paths if path.rsplit("/", 1)[-1].lower().endswith(_RELATED_EXTENSIONS)}
    if not tree or not content:
        return []

    symbols: list[str] = []
    for match in _BASE_RE.finditer(content):
        for group in match.groups():
            if group:
                symbols.extend(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", group))
    for match in _PY_IMPORT_RE.finditer(content):
        symbols.extend(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", match.group(2)))
    for match in _SYMBOL_RE.finditer(content):
        symbols.append(match.group(1))
    symbols = list(dict.fromkeys(symbol for symbol in symbols if len(symbol) > 1))

    module_paths: list[str] = []
    for match in _PY_IMPORT_RE.finditer(content):
        module = _normalise_import_path(source_path, match.group(1))
        if module:
            module_paths.append(module)
    for match in _PY_MODULE_RE.finditer(content):
        for module in match.group(1).split(","):
            normalised = _normalise_import_path(source_path, module)
            if normalised:
                module_paths.append(normalised)
    for match in _JS_IMPORT_RE.finditer(content):
        module = _normalise_import_path(source_path, match.group(1))
        if module:
            module_paths.append(module)

    selected: list[str] = []
    source_dir = source_path.rsplit("/", 1)[0] if "/" in source_path else ""

    def add(path: str) -> None:
        if path in tree and path != source_path and path not in selected:
            selected.append(path)

    # Imports are the strongest signal: resolve exact module files first.
    for module in module_paths:
        for ext in _RELATED_EXTENSIONS:
            add(module + ext)
        for ext in _RELATED_EXTENSIONS:
            add(module + "/__init__" + ext)
        if module.startswith("."):
            add(f"{source_dir}/{module.lstrip('./')}")

    # Then locate definitions by symbol, preferring nearby paths and exact stems.
    for symbol in symbols:
        matches = [
            path for path in tree
            if path.rsplit("/", 1)[-1].rsplit(".", 1)[0] == symbol
        ]
        matches.sort(key=lambda path: (0 if path.startswith(source_dir + "/") else 1, path))
        for path in matches[:3]:
            add(path)
    return selected


def fetch_related_definitions(
    api: GitHubAPI,
    *,
    ref: str,
    changed_files: list[dict],
    file_contents: dict[str, str],
    max_files: int = 40,
    max_definitions: int = 12,
    max_bytes: int = 16_000,
) -> dict[str, dict[str, str]]:
    """Fetch a small, source-keyed set of definitions related to changed files.

    This is intentionally conservative. It uses import/base-class names and a
    recursive tree listing to approximate ``getDefinition`` without cloning the
    repository or flooding every reviewer prompt with unrelated files.
    """
    if not api.enabled or not ref or not file_contents:
        return {}
    tree_paths = api.repository_tree(ref)
    if not tree_paths:
        return {}

    selected_by_source: dict[str, list[str]] = {}
    selected: list[str] = []
    for item in changed_files[:max_files]:
        source = str(item.get("path") or "")
        content = file_contents.get(source, "")
        if not source or not content or item.get("status") in {"removed", "deleted"}:
            continue
        for path in _related_candidate_paths(source, content, tree_paths):
            if path not in selected:
                if len(selected) >= max_definitions:
                    break
                selected.append(path)
            selected_by_source.setdefault(source, []).append(path)

    contents: dict[str, str] = {}
    for path in selected:
        text = api.file_content(path, ref)
        if text:
            contents[path] = text[:max_bytes]
    return {
        source: {path: contents[path] for path in paths if path in contents}
        for source, paths in selected_by_source.items()
        if any(path in contents for path in paths)
    }


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
    return {path: lines["added"] for path, lines in line_map(diff_text).items()}


# --------------------------------------------------------------------------- #
# Publishing
# --------------------------------------------------------------------------- #

def _summary_marker(pr: int) -> str:
    return f"<!-- prcheck:summary:{pr} -->"


_SEVERITY_ICON = {"critical": "\U0001f534", "high": "\U0001f7e0", "medium": "\U0001f7e1", "low": "\u26aa"}
_VERDICT_LABEL = {
    "block": ("\U0001f6d1", "Block"),
    "request-changes": ("\U0001f527", "Changes requested"),
    "approve-with-conditions": ("\u26a0\ufe0f", "Approve with conditions"),
    "approve": ("\u2705", "Approve"),
}
# GitHub rejects check-run summaries and comments over 65,535 characters.
_MAX_BODY = 60_000


def _one_line(text) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _render_finding(finding: dict, blob_base: str) -> str:
    severity = str(finding.get("severity") or "").lower()
    path, line = finding.get("path", ""), finding.get("line", "")
    location = f"`{path}:{line}`"
    if blob_base and path:
        location = f"[{location}]({blob_base}/{quote(str(path))}#L{line})"
    out = (
        f"- {_SEVERITY_ICON.get(severity, '')} **{severity.capitalize()}** · "
        f"{finding.get('category', '')} · {location}\n  {_one_line(finding.get('text'))}"
    )
    if finding.get("suggestion"):
        out += f"\n  **Fix:** {_one_line(finding['suggestion'])}"
    return out


def render_summary(verdict: str, conditions: list[str], findings: list[dict], *,
                   blob_base: str = "") -> str:
    """Render the sticky summary; ``blob_base`` links locations to the head commit."""
    icon, label = _VERDICT_LABEL.get(verdict, ("\U0001f50d", verdict))
    ordered = sort_findings(findings)
    lines = [f"## {icon} prcheck: {label}", ""]
    if ordered:
        counts = Counter(str(f.get("severity") or "").lower() for f in ordered)
        files = len({f.get("path") for f in ordered})
        tally = " · ".join(f"{counts[s]} {s}" for s in SEVERITY_RANK if counts[s])
        lines += [f"**{tally}** across {files} file(s)", ""]
    if conditions:
        lines.append("**Conditions**")
        lines += [f"- {c}" for c in conditions]
        lines.append("")
    if not ordered:
        lines.append("No issues found in the changed lines.")
    else:
        major = [f for f in ordered if str(f.get("severity") or "").lower() != "low"]
        minor = [f for f in ordered if str(f.get("severity") or "").lower() == "low"]
        lines += [_render_finding(f, blob_base) for f in major]
        if minor:
            # Low-severity notes are collapsed so they never bury the real issues.
            lines += ["", f"<details><summary>{len(minor)} low-severity note(s)</summary>", ""]
            lines += [_render_finding(f, blob_base) for f in minor]
            lines += ["", "</details>"]
    lines += ["", "<sub>Automated review by prcheck.</sub>"]
    body = "\n".join(lines)
    if len(body) > _MAX_BODY:
        body = body[:_MAX_BODY] + "\n\n_[summary truncated]_"
    return body


def _inline_key(finding: dict) -> str:
    # Keyed on normalized wording, not line, so a finding that shifts lines
    # after a push is still recognised as already posted.
    key = f"{finding.get('path')}:{_one_line(finding.get('text')).casefold()}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _render_inline(finding: dict) -> str:
    severity = str(finding.get("severity") or "").lower()
    body = (
        f"**{_SEVERITY_ICON.get(severity, '')} {severity.capitalize()} · {finding.get('category', '')}**"
        f"\n\n{finding.get('text', '')}"
    )
    if finding.get("suggestion"):
        body += f"\n\n**Suggested fix:** {finding['suggestion']}"
    return f"{body}\n\n<!-- prcheck:inline:{_inline_key(finding)} -->"


def upsert_summary_comment(api: GitHubAPI, pr: int, body: str) -> None:
    if not api.enabled:
        return
    mark = _summary_marker(pr)
    body = f"{body}\n\n{mark}"
    comments = api.list_pages(f"/repos/{api.repo}/issues/{pr}/comments")
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
                       conditions: list[str], findings: list[dict], *, blob_base: str = "") -> None:
    if not api.enabled or not check_run_id:
        return
    title = f"prcheck: {verdict} — {len(findings)} finding(s)"
    try:
        api.patch(f"/repos/{api.repo}/check-runs/{check_run_id}", {
            "status": "completed",
            "conclusion": _VERDICT_CONCLUSION.get(verdict, "neutral"),
            "completed_at": _now_iso(),
            "output": {"title": title, "summary": render_summary(verdict, conditions, findings, blob_base=blob_base)},
        })
    except GitHubAPIError:
        pass


def publish_inline_comments(api: GitHubAPI, pr: int, head_sha: str, diff_text: str,
                            findings: list[dict], *, limit: int = 40,
                            min_severity: str = "medium") -> int:
    """Post new findings as one batched review on changed lines. Returns count posted.

    Findings below ``min_severity`` stay in the summary only, and findings an
    earlier run already posted (same marker, or a prcheck comment on the same
    line) are skipped.
    """
    if not api.enabled or not head_sha:
        return 0
    commentable = line_map(diff_text)
    floor = SEVERITY_RANK.get(min_severity, len(SEVERITY_RANK))
    posted_keys: set[str] = set()
    posted_lines: set[tuple[str, int]] = set()
    for comment in api.list_pages(f"/repos/{api.repo}/pulls/{pr}/comments"):
        body = str((comment or {}).get("body") or "")
        match = _INLINE_MARKER_RE.search(body)
        if match:
            posted_keys.add(match.group(1))
            if comment.get("line"):
                posted_lines.add((str(comment.get("path")), int(comment["line"])))

    comments: list[dict] = []
    for finding in sort_findings(findings):
        path = str(finding.get("path") or "").strip()
        line = finding.get("line")
        if SEVERITY_RANK.get(str(finding.get("severity") or "").lower(), len(SEVERITY_RANK)) > floor:
            continue
        if not path or type(line) is not int or line not in commentable.get(path, {}).get("commentable", set()):
            continue
        if _inline_key(finding) in posted_keys or (path, line) in posted_lines:
            continue
        comments.append({"path": path, "line": line, "side": "RIGHT", "body": _render_inline(finding)})
        if len(comments) >= limit:
            break
    if not comments:
        return 0

    try:
        api.post(f"/repos/{api.repo}/pulls/{pr}/reviews", {
            "commit_id": head_sha,
            "event": "COMMENT",
            "body": f"prcheck left {len(comments)} inline comment(s); the verdict and full list are in the summary comment.",
            "comments": comments,
        })
        return len(comments)
    except GitHubAPIError as exc:
        # One unanchorable comment fails the whole batch (422); fall back to
        # posting individually so the rest still land.
        LOGGER.warning("reviews_batch_review_failed pr=%s %s", pr, exc)
    posted = 0
    for comment in comments:
        try:
            api.post(f"/repos/{api.repo}/pulls/{pr}/comments", {**comment, "commit_id": head_sha})
            posted += 1
        except GitHubAPIError as exc:
            LOGGER.warning("reviews_inline_comment_failed pr=%s path=%s %s", pr, comment["path"], exc)
    return posted
