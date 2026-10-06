---
name: obsidian-vault-memory
description: Use when working with the obsidian_vault plugin, vault search/read/capture tools, or Obsidian-backed long-term memory behavior.
---

# Obsidian vault memory

Use this bundled skill when answering from, capturing to, configuring, or
troubleshooting the `obsidian_vault` memory provider.

## Tools

- `obsidian_vault_search` — ranked search (BM25 + recency + current-state boost,
  superseded/archived demoted) with links/backlinks and metadata.
- `obsidian_vault_read` — read one allowed note returned by search.
- `obsidian_vault_capture` — capture a durable fact into the right lane.

## When to search

Before answering anything the vault likely documents: how a workflow or system
works, a past decision, a project's status, or "where did we leave off".
Prefer the project's `*-current-state.md` hub when one exists; it outranks
older notes by design. Treat `status`/`updated` as document lifecycle evidence,
not proof that live work is done — verify live systems separately.

## When to capture (durable facts only)

Capture the moment a fact will still matter in 30+ days: a rule, decision,
correction, workflow/SOP change, system gotcha, root cause, or standing
vendor/customer preference. Do **not** capture balances, totals, quotes, ETAs,
or today's status — the system of record keeps those.

1. Search first. If a canonical note exists, capture still creates a draft
   proposal next to it; never try to edit published notes.
2. For a new wiki note pass `related_hub` (an existing published hub path).
   If no defensible hub exists, use `category: inbox`.
3. Pass `system_id` when the fact belongs to a known system.
4. `force_vault: true` only for a durable rule the gate held by mistake.
5. Mention each capture in your reply: `Captured: <summary> -> <path>`.

When work finishes, record the done-state in the project's current-state hub
(via a capture/draft), so later recall finds "done", not stale "remaining work".

## Guardrails

- Never put secrets, tokens, passwords or credentials in captures (rejected).
- Private lanes are sealed unless the profile is listed in `private_profiles`.
- User style/preferences belong in built-in memory, not the vault.
- Markdown is canonical; the SQLite index can always be rebuilt.

## Troubleshooting

- "index is warming" — first build is running in the background; retry.
- Check `<HERMES_HOME>/state/obsidian_vault/refresh_status.json` for the last
  refresh outcome (`ok`, `timeout`, `error` + fingerprint).
- Gate decisions: `state/obsidian_vault/capture_gate_log.jsonl`; held captures:
  `capture_gate_held.jsonl` (local only, never indexed).
- A running session keeps the old plugin object; restart to pick up changes.
