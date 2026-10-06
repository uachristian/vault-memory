---
author: owner
created: 2026-01-01T00:00:00+00:00
updated: 2026-01-01T00:00:00+00:00
source: owner
status: published
tags: [meta, schema, conventions]
---

# Schema & Conventions

## Frontmatter (required on every note)

```yaml
---
author: hermes | owner | <agent-name>
created: 2026-01-01T09:30:00+00:00
updated: 2026-01-01T09:30:00+00:00
source: owner | hermes | ingest | meeting | voice-memo
status: draft | reviewed | published | applied | superseded | archived
tags: [workflow, current-state]
---
```

- `author` — most recent writer. `source` — content origin; preserve it on edits.
- `created` / `updated` — ISO-8601 with offset. `updated` drives recency ranking.
- `status`
  - `published` — canonical and locked; agents propose via `_drafts/`.
  - `applied` — a retained draft whose content was promoted to `applied-to:`.
  - `superseded` / `archived` — kept for history; ranked down in search.
- `tags` — lowercase, hyphenated. `current-state` marks a living hub (ranked up).

### Optional keys

| Key | Meaning |
|---|---|
| `aliases` | Alternate names; indexed for search |
| `superseded-by` | Path of the note that replaces this one (ranked down) |
| `promoted-from` / `promoted-by` | Trusted-promotion provenance |
| `applied-to` / `applied-by` / `applied-at` | On applied draft snapshots |
| `intended-promotion-path` | On drafts: where the change should land |
| `hermes-capture-id` | Idempotency id of the capture that wrote the note |
| `system` | Optional system inventory id the note belongs to |

## File naming

| Type | Pattern |
|---|---|
| Generic note | `kebab-case.md` |
| Date-anchored | `YYYY-MM-DD-slug.md` |
| Project hub | `<project>-current-state.md` |
| SOP | `<topic>-sop.md` |
| Draft proposal | `<section>/_drafts/<target>-capture-<stamp>.md` |

## Links

- Inside the vault: `[[path/to/note|Label]]` (path-qualified when ambiguous).
- External: `[anchor](https://example.com/...)`.

## Never in the vault

API keys, tokens, passwords, card/account numbers, or private-lane content
copied into shared lanes.
