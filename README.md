# vault-memory

An Obsidian vault as long-term memory for [Hermes Agent](https://hermes-agent.nousresearch.com/docs):
a memory-provider plugin, an empty vault skeleton, and an operating guide.

- **Markdown is canonical.** The search index is a disposable local SQLite cache.
- **Retrieval that recalls the present.** BM25 weighted by recency, boosted for
  `current-state` hubs, demoted for superseded/archived notes.
- **Lanes, enforced at query time.** Allow/deny/private paths are re-checked on
  every search hit, link and read (case-insensitive); `private/` is sealed by
  default; unparseable settings fail closed.
- **Safe capture.** Durable-facts gate (off/shadow/enforce, fail-open), secret
  rejection, existing notes never edited (draft proposals instead), trusted
  promotion of new notes with provenance, `log.md` audit, `system:` stamping.
- **Never hangs a turn.** Index refresh, reads and writes run in bounded
  subprocesses; a failed refresh keeps the last good index.

## Layout

```
plugin/obsidian_vault/   installable plugin (-> ~/.hermes/plugins/obsidian_vault)
vault-skeleton/          empty vault: AGENTS.md lanes, SCHEMA.md, log.md, folders
docs/                    how-it-works, install, capture-policy
skills/                  agent skill (also bundled inside the plugin)
```

## Quick start

```bash
cp -R plugin/obsidian_vault ~/.hermes/plugins/obsidian_vault
cp -R vault-skeleton "$HOME/Documents/Obsidian Vault"
```

```yaml
# ~/.hermes/config.yaml
memory:
  provider: obsidian_vault
plugins:
  obsidian_vault:
    vault_path: ~/Documents/Obsidian Vault
```

Full options: [docs/install.md](docs/install.md).

## Tests

```bash
cd plugin
uv run --no-project --with pytest --with PyYAML python -m pytest -q obsidian_vault/tests
```

Synthetic temporary vaults only; Hermes host modules are stubbed.

## License

MIT — see [LICENSE](LICENSE).
