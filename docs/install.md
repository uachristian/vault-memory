# Install

Requirements: Hermes Agent with plugin support, Python 3.10+ (stdlib `sqlite3`
with FTS5) and **PyYAML** (required: the plugin reads its settings, including
deny/private lanes, from `config.yaml`). Hermes installs normally ship PyYAML.
If the plugin's section exists but PyYAML is missing, or `config.yaml` /
`obsidian_vault.json` cannot be parsed, the provider **fails closed**: it
refuses to start and every tool call returns an error, rather than silently
falling back to default lanes.

In this guide `$HERMES_HOME` is your Hermes home (`~/.hermes` by default, or
`~/.hermes/profiles/<name>` for a named profile).

## 1. Copy the plugin

Copy `plugin/obsidian_vault` to `$HERMES_HOME/plugins/`:

```bash
git clone https://github.com/uachristian/vault-memory.git
mkdir -p "$HERMES_HOME/plugins"
cp -R vault-memory/plugin/obsidian_vault "$HERMES_HOME/plugins/"
```

Hermes discovers user plugin directories that contain a `MemoryProvider`; the
provider name is `obsidian_vault`.

## 2. Create (or point at) a vault

Start from the skeleton, or use an existing vault that follows the same layout:

```bash
cp -R vault-memory/vault-skeleton "$HOME/Documents/Obsidian Vault"
```

The skeleton files carry placeholder `created:`/`updated:` dates
(`2026-01-01T00:00:00+00:00`). On first install, restamp both fields in every
copied `.md` file to the current time so recency ranking starts from your
install date.

The vault **must** contain `AGENTS.md`; capture refuses to write without it.

Canonical layout (vault-relative):

| Path | Lane |
|---|---|
| `AGENTS.md`, `SCHEMA.md`, `00-INDEX.md` | readable policy/index files |
| `20-WIKI/` | readable; trusted-promotion lanes per category folder |
| `10-RAW/inbox/` | readable; direct-write capture lane |
| `private/` | **the restricted lane**: sealed for every profile unless listed in `private_profiles` (read-only even then) |
| `99-ARCHIVE/`, `.obsidian/` | always denied |
| `00-SYSTEM/`, `30-OUTPUT/`, `90-LAB/` | not in the default allow list |

Lane matching is case-insensitive, so `Private/` and `PRIVATE/` are the same
sealed lane on case-insensitive filesystems.

## 3. Configure `$HERMES_HOME/config.yaml`

Set `memory.provider: obsidian_vault` and `plugins.obsidian_vault.vault_path`:

```yaml
memory:
  provider: obsidian_vault

plugins:
  obsidian_vault:
    vault_path: ~/Documents/Obsidian Vault   # or set OBSIDIAN_VAULT_PATH in .env
    capture_gate: shadow          # off | shadow (log only) | enforce
    recency_weight: 1.0           # 0 = pure BM25, max 2
    refresh_seconds: 600          # background index refresh interval
    max_prefetch_results: 6       # snippets injected per turn
    max_related_notes: 4          # links/backlinks per search result
    # Lanes (vault-relative; trailing "/" = folder). Defaults shown.
    allow_paths: [AGENTS.md, SCHEMA.md, 00-INDEX.md, 20-WIKI/, 10-RAW/inbox/]
    deny_paths: []                # added to the built-in .obsidian/ and 99-ARCHIVE/
    private_paths: [private/]     # sealed unless the profile is listed below
    private_profiles: []          # e.g. [personal] — read access only
    direct_write_paths: [10-RAW/inbox/]
    wiki_root: 20-WIKI/
    # Category -> folder map. Entries extend/override the defaults below.
    category_paths:
      concept: 20-WIKI/concepts
      workflow: 20-WIKI/workflows
      sop: 20-WIKI/sops
      system: 20-WIKI/systems
      integration: 20-WIKI/integrations
      project: 20-WIKI/projects
      vendor: 20-WIKI/vendors
      customer: 20-WIKI/customers
      inbox: 10-RAW/inbox
      other: 10-RAW/inbox
    # trusted_promotion_paths defaults to every category folder under wiki_root.
```

Settings resolve in this order (later wins): built-in defaults, then
`plugins.obsidian_vault` in the profile's `config.yaml`, then
`$HERMES_HOME/obsidian_vault.json`. The JSON file takes the same keys and is
what `save_config` writes.

Vault path resolution when `vault_path` is unset: `OBSIDIAN_VAULT_PATH`
environment variable, then the profile `.env`, then the installation-root
`.env`, then `~/Documents/Obsidian Vault`.

Other tunables (seconds): `rebuild_timeout_seconds` (45),
`initial_wait_seconds` (2), `refresh_retry_seconds` (60),
`read_timeout_seconds` (5), `capture_timeout_seconds` (15). Search extras:
`short_search_terms` (two-letter acronyms kept in queries),
`private_intent_terms`, `private_autocontext_profiles`.

## 4. Verify

```bash
hermes memory status          # should show obsidian_vault as the active provider
```

Run the plugin tests (they need `pytest` and `PyYAML`), from `plugin/`:

```bash
cd vault-memory/plugin
uv run --no-project --with pytest --with PyYAML python -m pytest -q obsidian_vault/tests
```

or in a throwaway venv:

```bash
python3 -m venv /tmp/ov-test && /tmp/ov-test/bin/pip install pytest PyYAML
/tmp/ov-test/bin/python -m pytest -q obsidian_vault/tests
```

Start a new session (running sessions keep the previously loaded provider) and
ask about something in your vault. The first index builds in the background;
the first search may say the index is warming.

## State files

All under `$HERMES_HOME/state/obsidian_vault/` (local, never in the vault;
directories are created `0700`, files `0600`):
`index.sqlite3` (disposable), `refresh_status.json`, `capture_receipts/`,
`wiring_queue.jsonl`, `capture_gate_log.jsonl`, `capture_gate_held.jsonl`,
optional `capture_retirements.json`.

## Uninstall

Set `memory.provider` to something else (or `hermes memory off`) and delete the
plugin directory. Vault markdown is never deleted.
