# How it works

```
turn ─► prefetch(query) ─► read-only last-good index ─► ranked snippets ─► context
                                  ▲
           bounded subprocess ────┘ (refresh: candidate DB, integrity check, atomic swap)

tool: obsidian_vault_capture ─► durability gate ─► bounded capture worker ─► vault + log.md
```

## Read path

1. **Index.** A subprocess walks the allowed lanes and builds an SQLite FTS5
   table (`path, title, aliases, tags, content`) plus `note_meta` (status,
   source, created/updated, fingerprint) and `note_links` (wikilinks/embeds).
2. **Incremental, last-good.** Refreshes copy the current index, re-read only
   notes whose mtime/size changed, drop deleted ones, then integrity-check.
   The live index is replaced only when the candidate is valid and non-empty.
   A scan that seems to delete more than 10% of notes is rejected (a stalled
   cloud-sync folder looks like a mass deletion). Failures write a fingerprint
   to `refresh_status.json` and the old index keeps serving.
3. **Never blocking.** Search reads the existing index; a stale index triggers
   at most one background refresh (process-wide and OS-lock coordinated, with
   a cooldown). macOS File Provider "dataless" placeholders are skipped, not
   downloaded.
4. **Ranking.** `bm25(title 5, aliases 3, tags 2, body 1)`, then a multiplier:
   - `+ weight * 0.5^(age_days/30)` from `updated` (or `created`);
   - `+ weight` for a living hub (`current-state` tag or `*-current-state.md`);
   - `x 0.35` for `status: superseded|archived|retired` or `superseded-by:`.
   This is what makes "done" outrank an older "remaining work" note.
5. **Query-time lane enforcement.** Every hit, link target and backlink source
   is re-checked against the profile's current allow/deny/private policy, so a
   policy change takes effect immediately even with an old index.
6. **Prefetch.** Up to `max_prefetch_results` snippets with
   `status/updated/tags/backlinks` hints are injected each turn. Private-lane
   notes never appear in automatic context unless the profile is granted and
   the message signals a private topic.
7. **Reads** happen in a bounded subprocess that re-authorizes the path.

## Write path

1. **Normalize + reject secrets.** Unknown fields are dropped, sizes bounded,
   every field scanned for token/key/password shapes.
2. **Durability gate** (`capture_gate`): `shadow` logs a verdict; `enforce`
   keeps `daily`/`unsure` captures in a local ledger instead of the vault.
   Gate errors fail open. See [capture-policy.md](capture-policy.md).
3. **Route.** `category -> category_paths[category]/<slug>.md`, or an explicit
   `target_path` (must still be in a permitted lane).
4. **Commit** (in a bounded worker holding a per-vault lock):
   - target exists → new `_drafts/<stem>-capture-<stamp>.md` proposal
     (existing notes are never modified);
   - direct-write lane → create the note;
   - trusted-promotion lane → require a published `related_hub`, write an
     immutable `applied` snapshot in `_drafts/`, then the published note with
     `promoted-from`, a `Related` section, and a hub wiring request in
     `wiring_queue.jsonl` (the hub itself is never edited);
   - frontmatter includes `author`, `source`, `created`, `updated`, `status`,
     `tags`, `hermes-capture-id`, and `system:` when `system_id` was given.
5. **Audit.** One idempotent `log.md` line per write.
6. **Idempotency.** The operation id is a hash of the normalized arguments;
   receipts with SHA-256 proofs make exact retries after a timeout safe.

## Files

| File | Role |
|---|---|
| `__init__.py` | Provider: config, policy, index coordination, search, tools, capture |
| `index_worker.py` | Subprocess entry for refresh / read / capture |
| `capture_gate.py` | Deterministic durable/daily/unsure classifier |
| `skills/obsidian-vault-memory/SKILL.md` | Agent-facing usage guide |
