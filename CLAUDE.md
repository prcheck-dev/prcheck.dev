# Project instructions

Guidance for anyone (human or AI) working in this repo. Keep changes consistent
with what is already here.

## Structure

```
backend/    Django REST API + GitHub-OAuth-only auth  (see backend/README.md)
frontend/   React + Vite app
```

## Commands

```bash
# backend
cd backend && source venv/bin/activate
python manage.py runserver
python manage.py test

# frontend
cd frontend && npm run dev
```

## Code commenting

Comments explain **why**, not **what**. The code already shows what it does; a
good comment adds the reasoning, the constraint, or the gotcha that the code
can't express on its own.

**Do**

- Write a short module docstring stating the file's purpose and any non-obvious
  contract. Keep it to a few lines.
- Comment the *reason* behind a non-obvious decision — a security choice, an edge
  case, a workaround, an ordering requirement. Example:
  ```python
  # State cookie must survive GitHub's top-level GET redirect back to us, so
  # SameSite=Lax (not Strict).
  ```
- Use a one-line section divider to group large config blocks, matching the
  existing style:
  ```python
  # --------------------------------------------------------------------------- #
  # SimpleJWT
  # --------------------------------------------------------------------------- #
  ```
- Keep comments short — one or two lines. If it needs a paragraph, it usually
  belongs in the module docstring or the README.
- Put the comment directly above the code it describes.
- Write full sentences, capitalized, no trailing noise.

**Don't**

- Don't restate the code: `# increment i` above `i += 1`.
- Don't scatter comments randomly or comment every line. A dense wall of
  comments hides the important ones.
- Don't leave commented-out code — delete it; git remembers.
- Don't write TODOs without context. Say what and why, or open an issue.
- Don't let comments drift out of date. If you change the code, fix or remove the
  comment.

**Rule of thumb:** if a comment only tells you what the next line literally does,
delete it. If it tells you something you couldn't learn by reading that line,
keep it and keep it short.

## General conventions

- Match the surrounding code's style, naming, and comment density.
- Backend: settings are environment-driven (`django-environ`); never hard-code
  secrets. New endpoints default to authenticated — opt out explicitly.
- Frontend: match the existing component structure.
