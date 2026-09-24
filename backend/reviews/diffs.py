"""Unified-diff helpers shared by every review stage.

The model is shown a *numbered* diff: each new-side line carries its line number
in the head file, so a finding's ``line`` is copied rather than counted from
``@@`` headers (models count badly, and a wrong line is an unpublishable
finding). The same parser yields the per-file line map used to ground findings.
"""
from __future__ import annotations

import fnmatch
import re

DIFF_FILE_RE = re.compile(r"^diff --git a/(.*?) b/(.*?)$", re.M)
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_TRUNCATED = "\n[... truncated ...]\n"

# Lockfiles, build output, vendored and generated code: reviewing them costs
# tokens and only produces noise, since nobody hand-edits them.
DEFAULT_IGNORE_GLOBS = (
    "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml",
    "poetry.lock", "Pipfile.lock", "uv.lock", "Cargo.lock", "Gemfile.lock",
    "composer.lock", "go.sum", "*.lock",
    "*.min.js", "*.min.css", "*.map", "*.snap",
    "*.svg", "*.png", "*.jpg", "*.jpeg", "*.gif", "*.ico", "*.pdf", "*.woff", "*.woff2",
    "*_pb2.py", "*_pb2_grpc.py", "*.pb.go", "*.generated.*",
    "dist/*", "build/*", "vendor/*", "node_modules/*", "third_party/*",
)


def fence(text: str, limit: int) -> str:
    """Wrap untrusted text in markers, clipping the middle when over ``limit``."""
    if limit <= 0:
        text = ""
    elif len(text) > limit:
        half = max(1, (limit - len(_TRUNCATED)) // 2)
        text = text[:half] + _TRUNCATED + text[-half:]
    # A PR could otherwise close the fence itself and smuggle instructions out.
    text = text.replace("</untrusted>", "<\\/untrusted>")
    return f"<untrusted>\n{text}\n</untrusted>"


def diff_blocks(diff_text: str) -> list[tuple[str | None, str]]:
    """Split a unified diff into ``(new_path, block_text)`` per file."""
    matches = list(DIFF_FILE_RE.finditer(diff_text))
    if not matches:
        return [(None, diff_text)]
    blocks: list[tuple[str | None, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(diff_text)
        blocks.append((match.group(2), diff_text[match.start():end]))
    return blocks


def _walk(diff_text: str):
    """Yield ``(path, kind, new_line, raw)`` for every diff line.

    ``kind`` is ``header``, ``hunk``, ``added``, ``context``, ``removed`` or
    ``meta``; ``new_line`` is set for added and context lines only.
    """
    path: str | None = None
    new_line: int | None = None
    for raw in diff_text.splitlines():
        if raw.startswith("diff --git "):
            match = DIFF_FILE_RE.match(raw)
            path, new_line = (match.group(2) if match else None), None
            yield path, "header", None, raw
            continue
        if new_line is None and (raw.startswith("--- ") or raw.startswith("+++ ")):
            if raw.startswith("+++ "):
                target = raw[4:].strip()
                path = None if target == "/dev/null" else target.removeprefix("b/")
            yield path, "header", None, raw
            continue
        match = _HUNK_RE.match(raw)
        if match:
            new_line = int(match.group(1))
            yield path, "hunk", None, raw
            continue
        if new_line is None or path is None:
            yield path, "meta", None, raw
        elif raw.startswith("+"):
            yield path, "added", new_line, raw
            new_line += 1
        elif raw.startswith("-"):
            yield path, "removed", None, raw
        elif raw.startswith("\\"):
            yield path, "meta", None, raw
        else:
            yield path, "context", new_line, raw
            new_line += 1


def line_map(diff_text: str) -> dict[str, dict[str, set[int]]]:
    """Map each head-side path to its ``added`` and ``commentable`` line sets.

    GitHub accepts RIGHT-side review comments on any line inside a hunk, so
    context lines are commentable too.
    """
    result: dict[str, dict[str, set[int]]] = {}
    for path, kind, new_line, _ in _walk(diff_text):
        if path is None:
            continue
        entry = result.setdefault(path, {"added": set(), "commentable": set()})
        if kind == "added":
            entry["added"].add(new_line)
            entry["commentable"].add(new_line)
        elif kind == "context":
            entry["commentable"].add(new_line)
    return result


def number_diff(diff_text: str) -> str:
    """Prefix every head-side line with its head-file line number."""
    out: list[str] = []
    for _, kind, new_line, raw in _walk(diff_text):
        if kind in {"added", "context"}:
            out.append(f"{new_line:>5} {raw}")
        elif kind == "removed":
            out.append(f"{'':>5} {raw}")
        else:
            out.append(raw)
    return "\n".join(out) + ("\n" if diff_text.endswith("\n") else "")


def is_ignored_path(path: str, globs=DEFAULT_IGNORE_GLOBS) -> bool:
    name = path.rsplit("/", 1)[-1]
    for pattern in globs:
        if "/" in pattern:
            if fnmatch.fnmatchcase(path, pattern) or fnmatch.fnmatchcase(path, "*/" + pattern):
                return True
        elif fnmatch.fnmatchcase(name, pattern):
            return True
    return False


def filter_reviewable(changed_files: list[dict], diff_text: str, extra_globs=()) -> tuple[list[dict], str, list[str]]:
    """Drop low-signal files; returns ``(files, diff, skipped_paths)``."""
    globs = (*DEFAULT_IGNORE_GLOBS, *[g for g in extra_globs if g])
    skipped = [
        str(item.get("path") or "") for item in changed_files
        if is_ignored_path(str(item.get("path") or ""), globs)
    ]
    if not skipped:
        return changed_files, diff_text, []
    skip = set(skipped)
    files = [item for item in changed_files if str(item.get("path") or "") not in skip]
    blocks = diff_blocks(diff_text)
    if blocks and blocks[0][0] is None:
        return files, diff_text, skipped
    kept = "".join(text for path, text in blocks if path not in skip)
    return files, kept, skipped
