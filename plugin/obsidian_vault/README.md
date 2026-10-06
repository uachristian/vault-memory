# obsidian_vault — Hermes memory provider plugin

Profile-scoped Obsidian vault memory for Hermes. Markdown in your vault is the
canonical memory; a local SQLite FTS5 index is a disposable cache.

- Memory provider: `obsidian_vault`
- Tools: `obsidian_vault_search`, `obsidian_vault_read`, `obsidian_vault_capture`
- Bundled skill: `obsidian_vault:obsidian-vault-memory`

Requires PyYAML (plugin settings, including deny/private lanes, are read from
`config.yaml`). Unparseable settings fail closed: the provider refuses to start.

Install and configure: see [`../../docs/install.md`](../../docs/install.md).
Design: [`../../docs/how-it-works.md`](../../docs/how-it-works.md).
Capture rules: [`../../docs/capture-policy.md`](../../docs/capture-policy.md).

## Retrieval

- BM25 over title (x5), aliases (x3), tags (x2) and body, then a rank multiplier:
  recency half-life (30 days, from `updated`/`created`), a boost for living
  `current-state` hubs (tag `current-state` or filename `*-current-state.md`),
  and a 0.35x penalty for `status: superseded|archived|retired` or a
  `superseded-by:` frontmatter key. `recency_weight: 0` restores pure BM25.
- Applied `_drafts` snapshots collapse into the published note they were
  promoted to, so search does not return duplicates.
- Wikilinks/backlinks are resolved from the index only (never source I/O).
- Every candidate, link and backlink is re-checked against the current profile's
  allow/deny/private lanes at query time, even if the index predates the policy.
  Lane matching is case-insensitive (`Private/` == `private/`).
- Search results carry vault-relative paths only.

## Index durability (last-good)

- Search never rebuilds synchronously. A stale-but-valid index keeps serving
  while one refresh runs in a hard-time-limited subprocess (iCloud / File
  Provider stalls cannot hang a turn).
- Refreshes build a private candidate DB (incremental by path+mtime+size),
  integrity-check it, and atomically replace the live index only on success.
  Timeout, empty output, or a scan that omits >10% of indexed notes leaves the
  last-good index untouched.

## Capture safety

- Secret-shaped values in any capture field are rejected.
- Existing notes are never modified: every update becomes a new `_drafts/`
  proposal (the "draft lane" for locked/published notes).
- New notes in trusted-promotion lanes (by default every wiki category folder)
  require an existing published `related_hub`, and produce a published note plus
  an immutable `applied` draft snapshot with `promoted-from` provenance.
- Direct-write lanes (default `10-RAW/inbox/`) create notes immediately.
- Every write appends one line to `log.md` (idempotent per operation).
- `system_id` adds a `system:` frontmatter pointer; malformed ids are dropped.
- Durability gate (`capture_gate: off | shadow | enforce`, default `shadow`)
  keeps point-in-time detail out of the vault; any gate error fails open.
- Writes use descriptor-relative, no-follow, create-if-absent publication with
  parent identity checks; exact retries are idempotent via local receipts.

## Tests

Needs `pytest` and `PyYAML`:

```bash
cd plugin
uv run --no-project --with pytest --with PyYAML python -m pytest -q obsidian_vault/tests
```

Tests use synthetic temporary vaults and stub the Hermes host modules
(`tests/conftest.py`); they never read `~/.hermes` or a real vault.
