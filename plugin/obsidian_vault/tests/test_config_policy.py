"""Config-driven routing, private lanes, and config loading (synthetic tmp vaults only)."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]


def load_plugin():
    spec = importlib.util.spec_from_file_location("obsidian_vault_config_under_test", PLUGIN_DIR / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def make_provider(tmp_path, config=None, profile="default"):
    mod = load_plugin()
    provider = mod.ObsidianVaultMemoryProvider()
    provider._vault = tmp_path / "vault"
    provider._vault.mkdir(exist_ok=True)
    provider._profile = profile
    provider._hermes_home = tmp_path / "home"
    provider._config = dict(config or {})
    provider._index_path = provider._hermes_home / "state" / "obsidian_vault" / "index.sqlite3"
    (provider._vault / "AGENTS.md").write_text("policy")
    return provider


def write_hub(vault: Path, rel: str, title: str = "Hub") -> None:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nstatus: published\n---\n# {title}\n")


@pytest.mark.parametrize("category, folder", [
    ("concept", "20-WIKI/concepts"), ("workflow", "20-WIKI/workflows"), ("sop", "20-WIKI/sops"),
    ("system", "20-WIKI/systems"), ("integration", "20-WIKI/integrations"),
    ("project", "20-WIKI/projects"), ("vendor", "20-WIKI/vendors"),
    ("customer", "20-WIKI/customers"), ("inbox", "10-RAW/inbox"), ("other", "10-RAW/inbox"),
])
def test_default_category_map_routes_generic_lanes(tmp_path, category, folder):
    provider = make_provider(tmp_path)
    assert provider._route(category, "note") == f"{folder}/note.md"


def test_capture_schema_enum_tracks_config_categories(tmp_path):
    provider = make_provider(tmp_path, {"category_paths": {"recipe": "20-WIKI/kitchen/recipes", "Bad Key!": "x"}})
    capture = next(s for s in provider.get_tool_schemas() if s["name"] == "obsidian_vault_capture")
    enum = capture["parameters"]["properties"]["category"]["enum"]
    assert "recipe" in enum and "concept" in enum and "Bad Key!" not in enum
    # The module-level schema is never mutated by a per-instance config.
    mod = load_plugin()
    assert "recipe" not in mod.CAPTURE_SCHEMA["parameters"]["properties"]["category"]["enum"]


def test_custom_category_trusted_promotes_into_configured_folder(tmp_path):
    provider = make_provider(tmp_path, {"category_paths": {"recipe": "20-WIKI/kitchen/recipes"}})
    write_hub(provider._vault, "20-WIKI/kitchen/README.md", "Kitchen")
    result = provider._capture_unbounded({
        "category": "recipe",
        "title": "Sourdough Starter Ratio",
        "content": "Always feed the starter 1:1:1 by weight because it stays predictable.",
        "related_hub": "20-WIKI/kitchen/README.md",
    })
    assert result["status"] == "created_trusted_promote"
    assert result["path"] == "20-WIKI/kitchen/recipes/sourdough-starter-ratio.md"
    note = (provider._vault / result["path"]).read_text()
    assert "status: published" in note and "[[20-WIKI/kitchen/README|Kitchen]]" in note
    assert "[hermes] created trusted-promoted published note" in (provider._vault / "log.md").read_text()


def test_unknown_category_is_rejected(tmp_path):
    provider = make_provider(tmp_path)
    out = json.loads(provider.handle_tool_call("obsidian_vault_capture", {"category": "widgets", "content": "x"}))
    assert "category is not supported" in out["error"]


def test_trusted_promotion_paths_cannot_leave_wiki_root(tmp_path):
    provider = make_provider(tmp_path, {"trusted_promotion_paths": ["20-WIKI/concepts/", "99-ARCHIVE/", "10-RAW/"]})
    assert provider._trusted_promotion_prefixes() == ["20-WIKI/concepts/"]


def test_category_outside_wiki_is_not_trusted_promotable(tmp_path):
    provider = make_provider(tmp_path, {"category_paths": {"scratch": "90-LAB/scratch"}})
    out = json.loads(provider.handle_tool_call("obsidian_vault_capture", {
        "category": "scratch", "content": "Lab note.", "title": "Lab", "related_hub": "20-WIKI/x.md",
    }))
    assert "error" in out
    assert not (provider._vault / "90-LAB").exists()


def test_private_lane_sealed_by_default_for_every_profile(tmp_path):
    for profile in ("default", "work"):
        provider = make_provider(tmp_path, profile=profile)
        assert provider._is_allowed_rel("20-WIKI/concepts/a.md")
        assert not provider._is_allowed_rel("private/journal.md")
        assert not provider._is_allowed_rel("99-ARCHIVE/old.md")
        assert not provider._is_allowed_rel(".obsidian/app.json")


def test_private_lane_readable_only_by_granted_profile(tmp_path):
    cfg = {"private_profiles": ["personal"]}
    assert make_provider(tmp_path, cfg, profile="personal")._is_allowed_rel("private/journal.md")
    assert not make_provider(tmp_path, cfg, profile="work")._is_allowed_rel("private/journal.md")


def test_private_autocontext_requires_grant_and_intent(tmp_path):
    granted = make_provider(tmp_path, {"private_profiles": ["default"]})
    assert granted._private_autocontext("what is my budget this month") is True
    assert granted._private_autocontext("how does the backup job work") is False
    always = make_provider(tmp_path, {"private_profiles": ["p"], "private_autocontext_profiles": ["p"]}, profile="p")
    assert always._private_autocontext("anything") is True
    ungranted = make_provider(tmp_path, {"private_autocontext_profiles": ["default"]})
    assert ungranted._private_autocontext("my budget") is False


def test_worker_payload_carries_resolved_policy_without_reprivileging(tmp_path):
    provider = make_provider(tmp_path, {"category_paths": {"recipe": "20-WIKI/recipes"}}, profile="work")
    payload = provider._policy_payload()
    assert "private/" in payload["deny_paths"]
    assert payload["category_paths"]["recipe"] == "20-WIKI/recipes"
    assert "20-WIKI/recipes/" in payload["trusted_promotion_paths"]


def test_config_yaml_section_is_loaded_and_json_overrides(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(
        "memory:\n  provider: obsidian_vault\n"
        "plugins:\n  obsidian_vault:\n    vault_path: /tmp/yaml-vault\n    capture_gate: enforce\n    recency_weight: 0.5\n"
    )
    (home / "obsidian_vault.json").write_text(json.dumps({"capture_gate": "off"}))
    provider = load_plugin().ObsidianVaultMemoryProvider()
    cfg = provider._load_config(home)
    assert cfg["vault_path"] == "/tmp/yaml-vault"
    assert cfg["recency_weight"] == 0.5
    assert cfg["capture_gate"] == "off"


@pytest.mark.parametrize("body", [
    "plugins: [unterminated\n",
    "plugins:\n  obsidian_vault: just-a-string\n",
    "plugins:\n  obsidian_vault:\n    deny_paths: 20-WIKI/secret/\n",
])
def test_malformed_config_fails_closed(tmp_path, body):
    mod = load_plugin()
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(body)
    provider = mod.ObsidianVaultMemoryProvider()
    with pytest.raises(mod.ConfigPolicyError):
        provider._load_config(home)
    with pytest.raises(RuntimeError, match="refused to start"):
        provider.initialize("s", hermes_home=str(home))
    assert provider._vault is None
    assert not provider._is_allowed_rel("20-WIKI/concepts/a.md")
    out = json.loads(provider.handle_tool_call("obsidian_vault_read", {"path": "20-WIKI/concepts/a.md"}))
    assert "disabled" in out["error"]


def test_malformed_json_overlay_fails_closed(tmp_path):
    mod = load_plugin()
    home = tmp_path / "home"
    home.mkdir()
    (home / "obsidian_vault.json").write_text("{not json")
    with pytest.raises(mod.ConfigPolicyError):
        mod.ObsidianVaultMemoryProvider()._load_config(home)


def test_plugin_section_without_pyyaml_fails_closed(tmp_path, monkeypatch):
    import builtins

    mod = load_plugin()
    home = tmp_path / "home"
    home.mkdir()
    real_import = builtins.__import__

    def no_yaml(name, *args, **kwargs):
        if name == "yaml":
            raise ImportError("no yaml")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_yaml)
    (home / "config.yaml").write_text("memory:\n  provider: builtin\n")
    assert mod.ObsidianVaultMemoryProvider()._load_config(home) == {}
    (home / "config.yaml").write_text("plugins:\n  obsidian_vault:\n    deny_paths: [20-WIKI/secret/]\n")
    with pytest.raises(mod.ConfigPolicyError, match="PyYAML"):
        mod.ObsidianVaultMemoryProvider()._load_config(home)


def test_config_without_plugin_section_uses_defaults(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text("memory:\n  provider: builtin\n")
    assert load_plugin().ObsidianVaultMemoryProvider()._load_config(home) == {}


@pytest.mark.parametrize("rel", [
    "20-WIKI/Secret/plan.md", "20-wiki/SECRET/plan.md", "20-WIKI/secret/plan.md",
    "Private/journal.md", "PRIVATE/journal.md", "99-archive/old.md", ".Obsidian/app.json",
])
def test_deny_and_private_lanes_are_case_insensitive(tmp_path, rel):
    provider = make_provider(tmp_path, {"deny_paths": ["20-WIKI/secret/"]})
    assert not provider._is_allowed_rel(rel)


def test_mixed_case_read_of_denied_path_is_refused(tmp_path):
    provider = make_provider(tmp_path, {"deny_paths": ["20-WIKI/secret/"]})
    for rel in ("20-WIKI/secret/plan.md", "private/journal.md"):
        (provider._vault / rel).parent.mkdir(parents=True, exist_ok=True)
        (provider._vault / rel).write_text("# Sealed\nsealed sentinel\n")
    for requested in ("20-WIKI/SeCrEt/plan.md", "PRIVATE/journal.md", "Private/journal.md"):
        with pytest.raises(ValueError, match="allowed vault lanes"):
            provider._resolve_vault_path(requested)
    # Allowed lanes still match regardless of request capitalization.
    assert make_provider(tmp_path)._is_allowed_rel("20-wiki/concepts/a.md")


def test_state_dir_and_wiring_queue_are_owner_only(tmp_path):
    import stat

    provider = make_provider(tmp_path)
    provider._index_path = tmp_path / "home" / "state" / "obsidian_vault" / "index.sqlite3"
    provider._conn().close()
    state = provider._index_path.parent
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "home" / "state").stat().st_mode) == 0o700
    assert stat.S_IMODE(provider._index_path.stat().st_mode) == 0o600
    assert provider._emit_wiring_queue("20-WIKI/concepts/a.md", "20-WIKI/concepts/README.md", "") is True
    assert stat.S_IMODE((state / "wiring_queue.jsonl").stat().st_mode) == 0o600


def test_vault_path_falls_back_to_env_then_documented_default(tmp_path, monkeypatch):
    mod = load_plugin()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.delenv("OBSIDIAN_VAULT_PATH", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    assert mod._default_vault_path(home) == tmp_path / "fakehome" / "Documents" / "Obsidian Vault"
    (home / ".env").write_text('OBSIDIAN_VAULT_PATH="/srv/vault"\n')
    assert mod._default_vault_path(home) == Path("/srv/vault")
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(tmp_path / "env-vault"))
    assert mod._default_vault_path(home) == tmp_path / "env-vault"


def test_profile_env_and_installation_root_env(tmp_path, monkeypatch):
    mod = load_plugin()
    monkeypatch.delenv("OBSIDIAN_VAULT_PATH", raising=False)
    root = tmp_path / "hermes"
    profile_home = root / "profiles" / "work"
    profile_home.mkdir(parents=True)
    (root / ".env").write_text("OBSIDIAN_VAULT_PATH=/srv/root-vault\n")
    assert mod._default_vault_path(profile_home) == Path("/srv/root-vault")
    (profile_home / ".env").write_text("OBSIDIAN_VAULT_PATH=/srv/profile-vault\n")
    assert mod._default_vault_path(profile_home) == Path("/srv/profile-vault")


def test_system_id_frontmatter_stamp_and_malformed_id_dropped(tmp_path):
    provider = make_provider(tmp_path)
    ok = provider._capture_unbounded({"category": "inbox", "title": "Stamped", "content": "Fact.", "system_id": "backup-runner"})
    assert "system: backup-runner" in (provider._vault / ok["path"]).read_text()
    bad = provider._capture_unbounded({"category": "inbox", "title": "Unstamped", "content": "Fact two.", "system_id": "Bad ID!"})
    assert bad["status"] == "created"
    assert "system:" not in (provider._vault / bad["path"]).read_text()


def test_retirement_registry_is_profile_bound(tmp_path):
    provider = make_provider(tmp_path, profile="work")
    state = provider._index_path.parent
    state.mkdir(parents=True)
    registry = state / "capture_retirements.json"
    registry.write_text(json.dumps({
        "version": 1, "profile": "work", "vault_path": str(provider._vault.resolve()),
        "targets": {"20-WIKI/concepts/old.md": "20-WIKI/concepts/new.md"},
    }))
    registry.chmod(0o600)
    with pytest.raises(ValueError, match="retired"):
        provider._guard_capture_retirement({"category": "concept", "content": "x", "slug": "old"})
    other = make_provider(tmp_path, profile="other")
    other._index_path = provider._index_path
    with pytest.raises(ValueError, match="invalid or unsafe"):
        other._guard_capture_retirement({"category": "concept", "content": "x", "slug": "old"})
