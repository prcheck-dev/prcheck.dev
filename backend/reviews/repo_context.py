"""Symbol-level repository context for a PR.

Many defects the reviewer misses need a second place in the codebase: the
definition of a function the new code calls (its parameters, return shape,
abstract methods), or the places that call a function the PR changes.

``targeted_definitions`` finds the definitions of names the changed lines
call inside files already fetched over the API (import-resolved related files
and the PR's other changed files) and returns the definition itself rather
than the head of the file. ``call_sites`` finds where those same files call
the functions a changed file defines or modifies. No model is involved.
"""
from __future__ import annotations

import re

_CALL_RE = re.compile(r"(?<![\w$])([A-Za-z_$][\w$]*)\s*\(")
# Names a file defines, so calls to its own helpers are not looked up elsewhere.
_DEFINED_RE = re.compile(
    r"\b(?:def|function|func|class|interface)[ \t]+([A-Za-z_$][\w$]*)"
    r"|\bfunc[ \t]*\([^)\n]*\)[ \t]*([A-Za-z_$][\w$]*)"
    r"|\b(?:const|let|var)[ \t]+([A-Za-z_$][\w$]*)[ \t]*=[ \t]*(?:async[ \t]*)?\("
    r"|^[ \t]*(?:(?:public|private|protected|static|final|async|override|export)[ \t]+)+[\w<>,.?\[\] \t]*?\b([A-Za-z_$][\w$]*)[ \t]*\(",
    re.M,
)
_KEYWORDS = frozenset("""
if elif else for while switch case catch return throw new await yield with not and or in is
print len str int float bool dict list set tuple super self this isinstance getattr setattr hasattr
range enumerate zip map filter sorted min max sum any all open type repr format assert require
function def class lambda typeof instanceof sizeof make append delete panic string error nil
""".split())


def _added_lines(block_diff: str) -> list[str]:
    return [line[1:] for line in block_diff.splitlines() if line.startswith("+") and not line.startswith("+++")]


def _is_test(path: str) -> bool:
    lowered = path.lower()
    return any(marker in lowered for marker in ("/test", "test_", "_test.", ".test.", ".spec.", "/spec/", "tests/"))


def symbols_for_file(block_diff: str, file_content: str) -> list[str]:
    """Names the changed lines call that the file itself does not define."""
    defined_here = {g for m in _DEFINED_RE.finditer(file_content) for g in m.groups() if g}
    calls: list[str] = []
    for name in _CALL_RE.findall("\n".join(_added_lines(block_diff))):
        if len(name) < 3 or name.lower() in _KEYWORDS or name in defined_here or name in calls:
            continue
        calls.append(name)
    return calls


def _definition_pattern(name: str) -> str:
    # Same-line whitespace only ([ \t]): patterns that can span lines backtrack
    # badly on large files.
    n = re.escape(name)
    modifiers = r"(?:public|private|protected|static|final|async|export|override|abstract|synchronized|default)"
    return (
        rf"(?:\b(?:def|function|func|class|interface|type|module|trait|struct)[ \t]+{n}\b"
        rf"|\bfunc[ \t]*\([^)\n]*\)[ \t]*{n}[ \t]*\("
        rf"|\b(?:const|let|var)[ \t]+{n}[ \t]*=[ \t]*(?:async[ \t]*)?(?:\(|function\b|\w+[ \t]*=>)"
        rf"|^[ \t]*(?:{modifiers}[ \t]+)+[\w<>,.?\[\] \t]*?\b{n}[ \t]*\("
        rf"|^[ \t]*(?:async[ \t]+)?{n}[ \t]*\([^)\n]*\)[ \t]*(?::[^{{\n]*)?\{{)"
    )


def targeted_definitions(calls: list[str], search_space: dict[str, str], *,
                         max_bytes: int = 24_000, max_names: int = 12,
                         max_files_per_name: int = 2, lines_after: int = 40) -> dict[str, str]:
    """Return ``{"<path> (definition of <name>)": snippet}`` for names called by changed lines.

    A name defined in more than ``max_files_per_name`` searched files is too
    generic to be worth the space and is skipped.
    """
    found: dict[str, str] = {}
    used = 0
    for name in calls[:max_names * 3]:
        if len(found) >= max_names:
            break
        pattern = re.compile(_definition_pattern(name), re.M)
        hits = []
        for path, text in search_space.items():
            match = pattern.search(text)
            if match:
                hits.append((path, text.count("\n", 0, match.start()) + 1, text))
        if not hits or len(hits) > max_files_per_name:
            continue
        hits.sort(key=lambda h: (_is_test(h[0]), h[0]))
        path, line, text = hits[0]
        lines = text.splitlines()
        start = max(1, line - 2)
        snippet = "\n".join(f"{n:>6}  {lines[n - 1]}" for n in range(start, min(line + lines_after, len(lines)) + 1))
        if used + len(snippet) > max_bytes:
            break
        found[f"{path} (definition of {name})"] = snippet
        used += len(snippet)
    return found


_HUNK_CONTEXT_RE = re.compile(r"^@@[^@]*@@(.*)$", re.M)


def changed_functions(block_diff: str) -> list[str]:
    """Functions a file's diff defines or edits: new definitions plus hunk-header context."""
    text = "\n".join(_added_lines(block_diff)) + "\n" + "\n".join(_HUNK_CONTEXT_RE.findall(block_diff))
    names = [g for m in _DEFINED_RE.finditer(text) for g in m.groups() if g]
    return [n for n in dict.fromkeys(names) if len(n) >= 3 and n.lower() not in _KEYWORDS]


def call_sites(names: list[str], search_space: dict[str, str], *, max_bytes: int = 12_000,
               per_name: int = 4, max_hits: int = 12, radius: int = 3) -> dict[str, str]:
    """Return ``{"<path>:<line> (call of <name>)": snippet}`` for calls outside the defining file.

    Names called from more than ``max_hits`` places are too common to show usefully.
    """
    found: dict[str, str] = {}
    used = 0
    for name in names:
        call = re.compile(rf"(?<![\w$]){re.escape(name)}[ \t]*\(")
        definition = re.compile(_definition_pattern(name))
        hits = []
        for path, text in search_space.items():
            for number, line in enumerate(text.splitlines(), 1):
                if call.search(line) and not definition.search(line):
                    hits.append((path, number, text))
        if not hits or len(hits) > max_hits:
            continue
        hits.sort(key=lambda h: (_is_test(h[0]), h[0], h[1]))
        for path, number, text in hits[:per_name]:
            lines = text.splitlines()
            lo, hi = max(1, number - radius), min(len(lines), number + radius)
            snippet = "\n".join(f"{n:>6}  {lines[n - 1]}" for n in range(lo, hi + 1))
            if used + len(snippet) > max_bytes:
                return found
            found[f"{path}:{number} (call of {name})"] = snippet
            used += len(snippet)
    return found
