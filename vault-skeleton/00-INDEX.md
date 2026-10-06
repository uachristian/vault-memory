---
author: owner
created: 2026-01-01T00:00:00+00:00
updated: 2026-01-01T00:00:00+00:00
source: owner
status: published
tags: [meta, index]
---

# Vault Index

Start here. Agents read [[AGENTS]] (permissions) and [[SCHEMA]] (conventions)
at session start.

| Folder | Purpose |
|---|---|
| `00-SYSTEM/` | Generated pages (never hand-edited) |
| `10-RAW/inbox/` | First-capture lane; agents write new files here directly |
| `20-WIKI/` | Durable knowledge, one folder per category |
| `20-WIKI/projects/` | One `<project>-current-state.md` hub per project |
| `30-OUTPUT/` | Polished deliverables (reports, roll-ups) |
| `90-LAB/` | Experiments and scratch |
| `99-ARCHIVE/` | Owner-only; never indexed or written by agents |
| `private/` | Sealed personal lane; see `private/README.md` |
| `log.md` | Append-only audit trail of every agent write |
