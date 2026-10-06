---
author: owner
created: 2026-01-01T00:00:00+00:00
updated: 2026-01-01T00:00:00+00:00
source: owner
status: published
tags: [meta, agents, permissions]
---

# Agent Permissions

Agents read this file at session start. Read [[SCHEMA]] first.

## Core rules

1. **One writer per file at a time.** Write atomically (temp file, then rename).
2. **Append-only files are never edited above the cursor** (`log.md`, `10-RAW/`).
3. **`status: published` files are locked.** Agents propose changes as new files
   in `<section>/_drafts/`; the owner (or the section's owning agent) merges.
4. **Private lanes are sealed.** `private/` is unreadable by every agent profile
   unless the owner grants that profile explicitly in plugin config
   (`private_profiles`). A grant is read access for that profile only.
5. **No secrets anywhere**, frontmatter or body.
6. **`source:` is required** on every new file (see [[SCHEMA]]).
7. **Authority is an intersection.** Task scope, this matrix, and the profile's
   grants all apply; the strictest wins. A `_drafts/` folder never bypasses a
   read-only or no-access parent.

## Permission lanes

Legend: `write` = create new files; `draft` = proposals in `_drafts/` only;
`trusted promote` = agent may publish a *new* note it just created (never
replace an existing one); `append` = bottom only; `read`; `none`.

| Path | Agent (default profile) | Other agent profiles | Owner |
|---|---|---|---|
| `00-INDEX.md`, `AGENTS.md`, `SCHEMA.md` | read | read | write |
| `00-SYSTEM/` | read (generator only writes) | read | read |
| `log.md` | append | append | append |
| `10-RAW/inbox/` | write | write | write |
| `20-WIKI/<category>/` | draft + trusted promote | draft + trusted promote | write |
| `20-WIKI/projects/*-current-state.md` | draft | draft | write |
| `30-OUTPUT/` | read | read | write |
| `90-LAB/` | read | per-profile grant | write |
| `99-ARCHIVE/` | none | none | write |
| `private/` | none unless granted | none unless granted | write |

Adjust this table to your setup; keep it consistent with the plugin config
(`allow_paths`, `deny_paths`, `private_paths`, `category_paths`,
`trusted_promotion_paths`, `direct_write_paths`).

## Trusted promotion (new notes only)

An agent may publish a new note into a `20-WIKI/<category>/` lane when all hold:

1. The destination does not exist yet (create-only; never replaces a file).
2. It names an existing published owning hub (`related_hub`) and links to it.
3. The content contains no secrets or private-lane material.
4. It keeps an immutable `status: applied` snapshot in `_drafts/` and stamps
   `promoted-from:` / `promoted-by:` on the published note.
5. It appends a `log.md` line.

If any condition fails, fall back to `_drafts/` or `10-RAW/inbox/`.

## What to capture

Durable facts only — see `docs/capture-policy.md` in the plugin repo. Rules,
decisions, corrections, workflows, root causes, standing preferences: yes.
Balances, totals, quotes, ETAs, today's status: no.

## Logging

Every agent write appends one line to `log.md`:

```
2026-01-01T09:30:00+00:00 [hermes] created 10-RAW/inbox/example.md [capture:<id>:<entry>]
```
