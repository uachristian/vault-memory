"""Obsidian vault MemoryProvider for Hermes.

Read path: FTS5 indexes allowed vault markdown lanes and prefetches compact
source snippets before each turn. Ranking is BM25 weighted by recency, with a
boost for living ``current-state`` hubs and a penalty for superseded/archived
notes. Lane policy (allow/deny/private) is enforced again at query time.

Write path: exposes capture tools that follow the existing vault contract:
frontmatter, trusted promotion for new active notes, immutable draft proposals
for every existing source file, and append-only log.md.
Markdown remains canonical; the SQLite index is disposable.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import unicodedata
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Optional

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback remains process-local.
    _fcntl = None

from agent.memory_provider import MemoryProvider
from tools.registry import tool_error

# Default vault layout. Every value is overridable in plugin config; see
# docs/install.md. Paths are vault-relative and use a trailing "/" for lanes.
_DEFAULT_WIKI_ROOT = "20-WIKI/"
_DEFAULT_ALLOW_PATHS = ["AGENTS.md", "SCHEMA.md", "00-INDEX.md", "20-WIKI/", "10-RAW/inbox/"]
_DEFAULT_DENY_PATHS = [".obsidian/", "99-ARCHIVE/"]
_DEFAULT_PRIVATE_PATHS = ["private/"]
_DEFAULT_DIRECT_WRITE_PATHS = ["10-RAW/inbox/"]
_DEFAULT_CATEGORY_PATHS = {
    "concept": "20-WIKI/concepts",
    "workflow": "20-WIKI/workflows",
    "sop": "20-WIKI/sops",
    "system": "20-WIKI/systems",
    "integration": "20-WIKI/integrations",
    "project": "20-WIKI/projects",
    "vendor": "20-WIKI/vendors",
    "customer": "20-WIKI/customers",
    "inbox": "10-RAW/inbox",
    "other": "10-RAW/inbox",
}
# Queries matching one of these words may receive private-lane auto-context
# (only for profiles already granted private read access); others never do.
_DEFAULT_PRIVATE_INTENT_TERMS = [
    "personal", "private", "family", "health", "finance", "finances", "budget",
    "journal", "relationship", "birthday", "vacation", "workout", "sleep",
]
_DEFAULT_SHORT_SEARCH_TERMS = ["ai", "ap", "ar", "hr", "ml", "po", "qa", "ui"]
_CATEGORY_RE = re.compile(r"[a-z][a-z0-9_-]{0,39}")

_PROVIDER_NAME = "obsidian_vault"
_MAX_FILE_CHARS = 220_000
_DEFAULT_REFRESH_SECONDS = 600
_DEFAULT_REBUILD_TIMEOUT_SECONDS = 45
_DEFAULT_INITIAL_WAIT_SECONDS = 2
_DEFAULT_REFRESH_RETRY_SECONDS = 60
_DEFAULT_READ_TIMEOUT_SECONDS = 5
_DEFAULT_CAPTURE_TIMEOUT_SECONDS = 15
_MAX_CAPTURE_CONTENT_CHARS = 100_000
_MAX_CAPTURE_PAYLOAD_BYTES = 200_000
_MAX_CAPTURE_FIELD_CHARS = 1_000
_MAX_CAPTURE_RELATED_PATHS = 20
_MAX_CAPTURE_SOURCE_BYTES = 2_000_000
_MAX_CAPTURE_RECEIPTS = 512
_MAX_CAPTURE_RETIREMENT_BYTES = 64 * 1024
_MAX_CAPTURE_RETIREMENT_TARGETS = 128
_CAPTURE_RECEIPT_MAX_AGE_SECONDS = 90 * 24 * 60 * 60
_MACOS_DATALESS_FLAG = 0x40000000

# Provider instances can be recreated while the gateway remains alive. Keep refresh
# coordination process-wide so concurrent sessions cannot launch competing iCloud
# walks against the same disposable index.
_INDEX_REFRESH_LOCK = threading.Lock()
_INDEX_REFRESH_THREADS: dict[str, threading.Thread] = {}
_INDEX_REFRESH_LAST_ATTEMPT: dict[str, float] = {}

SEARCH_SCHEMA = {
    "name": "obsidian_vault_search",
    "description": (
        "Search the Obsidian vault index for canonical workflow/project/system context. "
        "Use this before answering questions about documented workflows, SOPs, projects, systems, integrations, vendors, customers, or 'where did we leave off'. "
        "Results include note metadata plus outgoing links/backlinks when available."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search terms or question."},
            "limit": {"type": "integer", "description": "Max results, default 8, max 20."},
            "path_prefix": {"type": "string", "description": "Optional vault-relative prefix to narrow search, e.g. 20-WIKI/projects/."},
        },
        "required": ["query"],
    },
}

READ_SCHEMA = {
    "name": "obsidian_vault_read",
    "description": "Read an allowed Obsidian vault markdown note by vault-relative path returned from obsidian_vault_search.",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Vault-relative path, e.g. 20-WIKI/concepts/foo.md."},
            "max_chars": {"type": "integer", "description": "Maximum characters to return, default 12000, max 50000."},
        },
        "required": ["path"],
    },
}

CAPTURE_SCHEMA = {
    "name": "obsidian_vault_capture",
    "description": (
        "Capture a durable workflow/project/system fact to the Obsidian vault using the vault's AGENTS.md policy. "
        "Use whenever a durable fact, correction, decision, SOP/workflow change, or system gotcha surfaces. "
        "The tool routes to the right lane, creates trusted-promoted notes when safe, stages draft proposals for locked published notes, and appends log.md. "
        "Never pass secrets; the tool rejects likely credentials."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "enum": sorted(_DEFAULT_CATEGORY_PATHS),
                "description": "Routing category (configurable category -> folder map). Use inbox/other when unsure.",
            },
            "content": {"type": "string", "description": "The durable fact/update to capture. No secrets."},
            "title": {"type": "string", "description": "Optional title for a new note or draft proposal."},
            "slug": {"type": "string", "description": "Optional filename slug without .md."},
            "target_path": {"type": "string", "description": "Optional explicit vault-relative .md path. Must be in an allowed Hermes write/draft lane."},
            "related_hub": {
                "type": "string",
                "description": (
                    "Existing active vault-relative .md path for the canonical owning hub. "
                    "Required for every new active trusted-promoted note; `_drafts` paths are rejected."
                ),
            },
            "related": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional existing active vault-relative .md paths for meaningful peer-note links.",
            },
            "source": {"type": "string", "description": "Source frontmatter value, default hermes. Use owner when the fact came directly from the vault owner."},
            "system_id": {
                "type": "string",
                "description": "Optional system inventory id this fact belongs to (e.g. backup-runner). Adds a `system:` frontmatter pointer; never blocks capture.",
            },
            "force_vault": {
                "type": "boolean",
                "description": "Only for a durable rule/decision that the durability gate held as day-to-day. Never use for balances, totals, quotes or today's status.",
            },

        },
        "required": ["category", "content"],
    },
}

_REDACT_PATTERNS = [
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{16,}\b", re.I),
    re.compile(r"\b(?:sk|pk|rk|ghp|gho|github_pat|xox[baprs]|ya29)_[A-Za-z0-9_\-]{16,}\b", re.I),
    re.compile(r"\bglpat-[A-Za-z0-9_\-]{16,}\b", re.I),
    re.compile(r"\bSG\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{16,}\b"),
    re.compile(r"-----BEGIN (?:RSA |DSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"),
    re.compile(r"\b[A-Za-z0-9_\-]{24,}\.[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{20,}\b"),
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password|authorization)\b\s*[:=]\s*\S+"),
]


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _timestamp_slug() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d-%H%M%S")


def _slugify(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text[:80] or f"capture-{_timestamp_slug()}"


def _parse_simple_dotenv(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    try:
        for line in path.read_text(errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip().strip('"').strip("'")
            env[k.strip()] = v
    except Exception:
        pass
    return env


def _profile_name_from_home(hermes_home: Path) -> str:
    parts = hermes_home.parts
    try:
        idx = parts.index("profiles")
        return parts[idx + 1]
    except Exception:
        return "default"


_FALLBACK_VAULT_PATH = "~/Documents/Obsidian Vault"


def _default_hermes_home() -> Path:
    """Profile-aware Hermes home; standalone (test/worker) use falls back to env."""
    try:
        from hermes_constants import get_hermes_home  # type: ignore

        return Path(get_hermes_home()).expanduser()
    except Exception:
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()


def _installation_root(hermes_home: Path) -> Path:
    parts = hermes_home.parts
    try:
        idx = parts.index("profiles")
    except ValueError:
        return hermes_home
    return Path(*parts[:idx]) if idx >= 1 else hermes_home


def _default_vault_path(hermes_home: Path) -> Optional[Path]:
    """Return the configured/default vault path without touching source storage.

    Order: OBSIDIAN_VAULT_PATH env, profile .env, installation-root .env, then
    the documented fallback ``~/Documents/Obsidian Vault``.
    """
    candidates: list[str] = []
    if os.environ.get("OBSIDIAN_VAULT_PATH"):
        candidates.append(os.environ["OBSIDIAN_VAULT_PATH"])
    for env_path in (hermes_home / ".env", _installation_root(hermes_home) / ".env"):
        val = _parse_simple_dotenv(env_path).get("OBSIDIAN_VAULT_PATH")
        if val:
            candidates.append(val)
    candidates.append(_FALLBACK_VAULT_PATH)
    return Path(candidates[0]).expanduser()


class ConfigPolicyError(RuntimeError):
    """Plugin settings exist but cannot be read; access policy must fail closed."""


_POLICY_LIST_KEYS = ("allow_paths", "deny_paths", "private_paths", "private_profiles", "private_autocontext_profiles")


def _load_yaml_plugin_section(hermes_home: Path) -> dict[str, Any]:
    """Read ``plugins.obsidian_vault`` from the profile config.yaml.

    Fails closed: deny/private paths live here, so an unreadable or malformed
    section raises instead of silently falling back to the default policy.
    """
    cfg_path = hermes_home / "config.yaml"
    if not cfg_path.is_file():
        return {}
    try:
        raw = cfg_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigPolicyError(f"cannot read {cfg_path.name}: {exc.__class__.__name__}") from exc
    try:
        import yaml  # type: ignore
    except ImportError as exc:
        if _PROVIDER_NAME in raw:
            raise ConfigPolicyError(
                f"{cfg_path.name} has a plugins.{_PROVIDER_NAME} section but PyYAML is not installed"
            ) from exc
        return {}
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigPolicyError(f"{cfg_path.name} is not valid YAML") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigPolicyError(f"{cfg_path.name} top level is not a mapping")
    plugins = data.get("plugins")
    if plugins is None:
        return {}
    if not isinstance(plugins, dict):
        raise ConfigPolicyError(f"{cfg_path.name} plugins section is not a mapping")
    section = plugins.get(_PROVIDER_NAME)
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise ConfigPolicyError(f"{cfg_path.name} plugins.{_PROVIDER_NAME} is not a mapping")
    return dict(section)


def _policy_key(value: str) -> str:
    """Case- and normalization-insensitive form for lane comparisons.

    Vaults commonly live on case-insensitive filesystems (APFS/NTFS default),
    where ``Private/`` and ``private/`` are the same folder.
    """
    return unicodedata.normalize("NFC", value).casefold()


def _ensure_private_dir(path: Path) -> None:
    """Create ``path`` (and missing parents) owner-only; tighten the leaf."""
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        if current.parent == current:
            break
        current = current.parent
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _frontmatter_block(text: str) -> tuple[str, str]:
    """Return (frontmatter_block, body) for a markdown note."""
    if not text.startswith("---\n"):
        return "", text
    end = text.find("\n---", 4)
    if end == -1:
        return "", text
    return text[4:end], text[end + 4 :].lstrip("\n")


def _frontmatter_scalar(value: str) -> str:
    """Parse the conservative scalar subset capture policy uses.

    YAML treats an unquoted `` #`` suffix as a comment. Supporting that
    subset prevents ``status: published # locked`` from becoming a distinct,
    apparently writable status without adding a runtime YAML dependency.
    """
    value = value.strip()
    if value.startswith('"'):
        escaped = False
        for index in range(1, len(value)):
            character = value[index]
            if character == '"' and not escaped:
                token = value[: index + 1]
                try:
                    parsed = json.loads(token)
                    return parsed if isinstance(parsed, str) else token[1:-1]
                except json.JSONDecodeError:
                    return token[1:-1]
            escaped = character == "\\" and not escaped
    elif value.startswith("'"):
        index = 1
        parsed: list[str] = []
        while index < len(value):
            if value[index] == "'":
                if index + 1 < len(value) and value[index + 1] == "'":
                    parsed.append("'")
                    index += 2
                    continue
                return "".join(parsed)
            parsed.append(value[index])
            index += 1
    return re.split(r"\s+#", value, maxsplit=1)[0].strip()


def _frontmatter(text: str) -> dict[str, str]:
    block, _ = _frontmatter_block(text)
    if not block:
        return {}
    data: dict[str, str] = {}
    for line in block.splitlines():
        if line.startswith((" ", "\t", "-")):
            continue
        if ":" in line:
            k, v = line.split(":", 1)
            data[k.strip()] = _frontmatter_scalar(v)
    return data


def _parse_frontmatter_lists(text: str) -> dict[str, list[str]]:
    """Parse common scalar/list frontmatter fields without requiring PyYAML."""
    block, _ = _frontmatter_block(text)
    values: dict[str, list[str]] = {}
    current: str | None = None
    for raw in block.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not raw.startswith((" ", "\t")) and ":" in line:
            key, val = line.split(":", 1)
            current = key.strip()
            val = val.strip()
            if not val:
                values.setdefault(current, [])
                continue
            if val.startswith("[") and val.endswith("]"):
                inner = val[1:-1].strip()
                values[current] = [x.strip().strip('"').strip("'") for x in inner.split(",") if x.strip()]
            else:
                values[current] = [_frontmatter_scalar(val)]
        elif current and stripped.startswith("- "):
            values.setdefault(current, []).append(stripped[2:].strip().strip('"').strip("'"))
    return values


def _strip_markdown_code(text: str) -> str:
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    return re.sub(r"`[^`]*`", "", text)


def _split_unescaped_pipe(value: str) -> tuple[str, str]:
    escaped = False
    for i, ch in enumerate(value):
        if ch == "\\" and not escaped:
            escaped = True
            continue
        if ch == "|" and not escaped:
            return value[:i], value[i + 1 :]
        escaped = False
    return value, ""


def _note_slug_from_target(target: str) -> str:
    target = target.split("#", 1)[0].split("^", 1)[0].strip()
    target = target.replace("\\|", "|")
    name = Path(target).name
    if name.lower().endswith(".md"):
        name = name[:-3]
    return name.strip().lower()


def _note_slug_from_path(path: str) -> str:
    name = Path(path).name
    if name.lower().endswith(".md"):
        name = name[:-3]
    return name.lower()


def _extract_wikilinks(text: str) -> list[dict[str, Any]]:
    cleaned = _strip_markdown_code(text)
    links: list[dict[str, Any]] = []
    for match in re.finditer(r"(!?)\[\[([^\]\n]+)\]\]", cleaned):
        raw = match.group(2).strip()
        target, display = _split_unescaped_pipe(raw)
        target = target.strip()
        slug = _note_slug_from_target(target)
        if not slug:
            continue
        links.append({
            "target": target.replace("\\|", "|"),
            "target_slug": slug,
            "display": display.strip().replace("\\|", "|"),
            "is_embed": 1 if match.group(1) else 0,
        })
    return links


def _note_metadata(rel: str, text: str) -> dict[str, Any]:
    fm = _frontmatter(text)
    lists = _parse_frontmatter_lists(text)
    title = fm.get("title", "")
    if not title:
        for line in text.splitlines()[:80]:
            if line.startswith("# "):
                title = line[2:].strip()
                break
    aliases = lists.get("aliases", [])
    tags = lists.get("tags", [])
    # Normalize inline #tags from either YAML style; preserve explicit frontmatter first.
    tags = [t.lstrip("#") for t in tags if t]
    return {
        "path": rel,
        "slug": _note_slug_from_path(rel),
        "title": title,
        "aliases": aliases,
        "tags": tags,
        "status": fm.get("status", ""),
        "source": fm.get("source", ""),
        "updated": fm.get("updated", ""),
        "created": fm.get("created", ""),
        "links": _extract_wikilinks(text),
    }


def _json_list(value: Any) -> list[Any]:
    if not value:
        return []
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []


class ObsidianVaultMemoryProvider(MemoryProvider):
    """Profile-scoped Obsidian retrieval and AGENTS.md-compliant capture."""

    def __init__(self) -> None:
        self._hermes_home: Path = _default_hermes_home()
        self._profile = "default"
        self._vault: Optional[Path] = None
        self._config: dict[str, Any] = {}
        self._config_error: Optional[str] = None
        self._index_path: Optional[Path] = None
        self._last_index = 0.0
        self._index_dirty = False
        self._index_dirty_generation = 0

    @property
    def name(self) -> str:
        return _PROVIDER_NAME

    def _load_config(self, hermes_home: Optional[Path] = None) -> dict[str, Any]:
        """config.yaml ``plugins.obsidian_vault`` overlaid by ``obsidian_vault.json``."""
        home = hermes_home or self._hermes_home
        cfg: dict[str, Any] = _load_yaml_plugin_section(home)
        cfg_path = home / "obsidian_vault.json"
        if cfg_path.exists():
            try:
                loaded = json.loads(cfg_path.read_text())
            except (OSError, ValueError) as exc:
                raise ConfigPolicyError(f"{cfg_path.name} is not valid JSON") from exc
            if not isinstance(loaded, dict):
                raise ConfigPolicyError(f"{cfg_path.name} is not a JSON object")
            cfg.update(loaded)
        for key in _POLICY_LIST_KEYS:
            if key in cfg and cfg[key] is not None and not isinstance(cfg[key], list):
                raise ConfigPolicyError(f"plugins.{_PROVIDER_NAME}.{key} must be a list")
        return cfg

    def is_available(self) -> bool:
        try:
            home = _default_hermes_home()
            cfg = self._load_config(home)
            # Availability runs during agent construction. It must never touch
            # iCloud/File Provider paths; real source checks happen only inside
            # bounded workers.
            vault = cfg.get("vault_path") or _default_vault_path(home)
            return bool(vault)
        except Exception:
            return False

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        home = Path(hermes_home).expanduser()
        cfg_path = home / "obsidian_vault.json"
        existing = {}
        if cfg_path.exists():
            try:
                existing = json.loads(cfg_path.read_text())
            except Exception:
                existing = {}
        existing.update(values)
        cfg_path.write_text(json.dumps(existing, indent=2, sort_keys=True) + "\n")

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {"key": "vault_path", "description": "Absolute Obsidian vault path", "default": f"OBSIDIAN_VAULT_PATH or {_FALLBACK_VAULT_PATH}"},
            {"key": "refresh_seconds", "description": "Index refresh interval", "default": str(_DEFAULT_REFRESH_SECONDS)},
            {"key": "rebuild_timeout_seconds", "description": "Hard deadline for an isolated vault index rebuild", "default": str(_DEFAULT_REBUILD_TIMEOUT_SECONDS)},
            {"key": "initial_wait_seconds", "description": "Maximum wait for a first index; searches remain bounded", "default": str(_DEFAULT_INITIAL_WAIT_SECONDS)},
            {"key": "refresh_retry_seconds", "description": "Cooldown after starting a refresh", "default": str(_DEFAULT_REFRESH_RETRY_SECONDS)},
            {"key": "read_timeout_seconds", "description": "Hard deadline for reading one iCloud-backed note", "default": str(_DEFAULT_READ_TIMEOUT_SECONDS)},
            {"key": "capture_timeout_seconds", "description": "Hard deadline for one isolated vault capture transaction", "default": str(_DEFAULT_CAPTURE_TIMEOUT_SECONDS)},
            {"key": "capture_gate", "description": "Durability gate for captures: off | shadow (log only) | enforce", "default": "shadow"},
            {"key": "max_prefetch_results", "description": "Relevant snippets injected per turn", "default": "6"},
            {"key": "max_related_notes", "description": "Outgoing links/backlinks returned per search result", "default": "4"},
            {"key": "recency_weight", "description": "0 = pure BM25; 1 = default recency/current-state boost; max 2", "default": "1.0"},
        ]

    def initialize(self, session_id: str, **kwargs) -> None:
        self._hermes_home = Path(kwargs.get("hermes_home") or _default_hermes_home()).expanduser()
        self._profile = kwargs.get("agent_identity") or _profile_name_from_home(self._hermes_home)
        try:
            self._config = self._load_config(self._hermes_home)
        except ConfigPolicyError as exc:
            # Fail closed: no vault, no lanes, every tool call refused.
            self._config = {}
            self._vault = None
            self._config_error = str(exc)
            raise RuntimeError(f"obsidian_vault refused to start: {exc}") from exc
        self._config_error = None
        vault_value = self._config.get("vault_path")
        self._vault = Path(vault_value).expanduser() if vault_value else _default_vault_path(self._hermes_home)
        self._index_path = self._hermes_home / "state" / "obsidian_vault" / "index.sqlite3"
        if self._index_path:
            _ensure_private_dir(self._index_path.parent)

    def system_prompt_block(self) -> str:
        if not self._vault:
            return ""
        return (
            "# Obsidian Vault Memory Provider\n"
            f"Active for profile `{self._profile}`. Vault markdown is the canonical durable workflow/project/system memory.\n"
            "Before answering documented workflow/project/system questions, rely on injected Obsidian Vault Context and use `obsidian_vault_search` / `obsidian_vault_read` for deeper grounding.\n"
            "When a durable workflow, system, integration, SOP, project, vendor or customer fact/correction/decision surfaces, use `obsidian_vault_capture` in the same turn instead of waiting to be asked. "
            "Capture durable facts only: never balances, totals, quotes or today's status.\n"
            "For the user's personal/style preferences, use built-in memory; for durable project/system facts, use the vault. Never put secrets in vault captures."
        )

    # ----- path policy -------------------------------------------------

    def _normalize_policy_prefix(self, value: str) -> str:
        """Normalize a vault policy prefix while preserving directory markers."""
        raw = str(value).strip().lstrip("/")
        if raw and str(value).strip().endswith("/") and not raw.endswith("/"):
            raw += "/"
        return raw

    def _cfg_list(self, key: str, default: list[str]) -> list[str]:
        value = self._config.get(key)
        if isinstance(value, list):
            return [self._normalize_policy_prefix(str(x)) for x in value if str(x).strip()]
        return list(default)

    def _private_prefixes(self) -> list[str]:
        """Sealed private lanes: unreadable unless the profile is granted below."""
        return self._cfg_list("private_paths", _DEFAULT_PRIVATE_PATHS)

    def _private_access(self) -> bool:
        profiles = self._config.get("private_profiles") or []
        return isinstance(profiles, list) and self._profile in {str(p) for p in profiles}

    def _allow_prefixes(self) -> list[str]:
        cfg_allow = self._config.get("allow_paths")
        if isinstance(cfg_allow, list) and cfg_allow:
            allow = [self._normalize_policy_prefix(str(x)) for x in cfg_allow]
        else:
            allow = list(_DEFAULT_ALLOW_PATHS)
        if self._private_access():
            allow.extend(p for p in self._private_prefixes() if p not in allow)
        return allow

    def _deny_prefixes(self) -> list[str]:
        deny = list(_DEFAULT_DENY_PATHS)
        if not self._private_access():
            deny.extend(self._private_prefixes())
        cfg_deny = self._config.get("deny_paths")
        if isinstance(cfg_deny, list):
            deny.extend(self._normalize_policy_prefix(str(x)) for x in cfg_deny)
        return deny

    def _wiki_root(self) -> str:
        root = self._normalize_policy_prefix(str(self._config.get("wiki_root") or _DEFAULT_WIKI_ROOT))
        return root if root.endswith("/") else root + "/"

    def _category_paths(self) -> dict[str, str]:
        """Category -> vault folder map; config entries extend/override defaults."""
        paths = dict(_DEFAULT_CATEGORY_PATHS)
        cfg = self._config.get("category_paths")
        if isinstance(cfg, dict):
            for key, value in cfg.items():
                name = str(key).strip().lower()
                folder = str(value or "").strip().strip("/")
                if _CATEGORY_RE.fullmatch(name) and folder:
                    paths[name] = folder
        paths.setdefault("other", "10-RAW/inbox")
        return paths

    def _direct_write_prefixes(self) -> list[str]:
        return [p if p.endswith("/") else p + "/" for p in self._cfg_list("direct_write_paths", _DEFAULT_DIRECT_WRITE_PATHS)]

    def _is_direct_write(self, rel: str) -> bool:
        return any(rel.startswith(p) for p in self._direct_write_prefixes())

    def _trusted_promotion_prefixes(self) -> list[str]:
        """New-note trusted-promotion lanes; default = every wiki category folder."""
        cfg = self._config.get("trusted_promotion_paths")
        if isinstance(cfg, list):
            roots = [self._normalize_policy_prefix(str(x)) for x in cfg if str(x).strip()]
        else:
            roots = [v.strip("/") + "/" for v in self._category_paths().values()]
        wiki = self._wiki_root()
        return sorted({r if r.endswith("/") else r + "/" for r in roots if (r if r.endswith("/") else r + "/").startswith(wiki)})

    def _policy_payload(self) -> dict[str, Any]:
        """Effective policy forwarded to bounded workers (they never reread config)."""
        return {
            "allow_paths": self._allow_prefixes(),
            "deny_paths": self._deny_prefixes(),
            "wiki_root": self._wiki_root(),
            "category_paths": self._category_paths(),
            "direct_write_paths": self._direct_write_prefixes(),
            "trusted_promotion_paths": self._trusted_promotion_prefixes(),
        }

    def _rel(self, path: Path) -> str:
        assert self._vault is not None
        return path.resolve().relative_to(self._vault.resolve()).as_posix()

    def _is_allowed_rel(self, rel: str) -> bool:
        if self._config_error:
            return False
        rel = _policy_key(rel.strip("/"))
        for d in self._deny_prefixes():
            d = _policy_key(d.lstrip("/"))
            if d.endswith("/"):
                if rel.startswith(d):
                    return False
            elif rel == d or rel.startswith(d + "/"):
                return False
        for a in self._allow_prefixes():
            a = _policy_key(a.lstrip("/"))
            if a.endswith("/"):
                if rel.startswith(a):
                    return True
            elif rel == a:
                return True
        return False

    def _resolve_vault_path(self, path: str) -> Path:
        if not self._vault:
            raise ValueError("Vault path is not configured")
        p = Path(path).expanduser()
        if not p.is_absolute():
            p = self._vault / path.strip("/")
        resolved = p.resolve()
        try:
            rel = resolved.relative_to(self._vault.resolve()).as_posix()
        except Exception:
            raise ValueError("Path is outside the configured vault")
        if not self._is_allowed_rel(rel):
            raise ValueError(f"Path is outside this profile's allowed vault lanes: {rel}")
        return resolved

    def _iter_markdown(self) -> Iterable[Path]:
        if not self._vault:
            return []
        files: list[Path] = []
        for prefix in self._allow_prefixes():
            p = self._vault / prefix.strip("/")
            if prefix.endswith("/"):
                if p.exists():
                    files.extend(p.rglob("*.md"))
            elif p.exists() and p.suffix == ".md":
                files.append(p)
        out = []
        seen = set()
        for p in files:
            try:
                rel = self._rel(p)
            except Exception:
                continue
            if rel in seen or not self._is_allowed_rel(rel):
                continue
            if any(part.startswith(".") for part in Path(rel).parts):
                continue
            seen.add(rel)
            out.append(p)
        return out

    # ----- indexing/search --------------------------------------------

    def _mark_index_dirty(self) -> None:
        self._index_dirty = True
        self._index_dirty_generation += 1

    def _conn(self, path: Optional[Path] = None, *, readonly: bool = False) -> sqlite3.Connection:
        target = path or self._index_path
        if not target:
            raise RuntimeError("index path unavailable")
        if readonly:
            conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True, timeout=0.25)
        else:
            _ensure_private_dir(target.parent)
            conn = sqlite3.connect(str(target), timeout=2.0)
            conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
            os.chmod(target, 0o600)
        return conn

    def _create_index_tables(self, conn: sqlite3.Connection) -> None:
        # This method is called only against a private build database. The live
        # index is never dropped or mutated during a refresh.
        conn.execute("DROP TABLE IF EXISTS vault_fts")
        conn.execute("DROP TABLE IF EXISTS note_meta")
        conn.execute("DROP TABLE IF EXISTS note_links")
        conn.execute(
            "CREATE VIRTUAL TABLE vault_fts USING fts5("
            "path UNINDEXED, title, aliases, tags, content)"
        )
        conn.execute(
            "CREATE TABLE note_meta ("
            "path TEXT PRIMARY KEY, slug TEXT, title TEXT, aliases_json TEXT, tags_json TEXT, "
            "status TEXT, source TEXT, updated TEXT, created TEXT, links_json TEXT, "
            "source_mtime_ns INTEGER NOT NULL, source_size INTEGER NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE note_links ("
            "source_path TEXT, target TEXT, target_slug TEXT, display TEXT, is_embed INTEGER)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_note_meta_slug ON note_meta(slug)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_note_links_source ON note_links(source_path)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_note_links_target ON note_links(target_slug)")

    def _is_dataless(self, path: Path) -> bool:
        """Return True for a macOS File Provider placeholder with no local bytes."""
        try:
            info = path.stat()
        except OSError:
            return True
        if not stat.S_ISREG(info.st_mode):
            return True
        return bool(getattr(info, "st_flags", 0) & _MACOS_DATALESS_FLAG)

    def _read_index_text(self, path: Path) -> str:
        return path.read_text(errors="ignore")[:_MAX_FILE_CHARS]

    def _build_index_database(self, destination: Path, *, source_index: Optional[Path] = None) -> dict[str, Any]:
        """Build a complete candidate or incrementally refresh a copied index.

        The first index reads each allowed note once. A schema-v2 last-good index
        is copied and upgraded in place so scan omissions cannot discard old rows.
        Later refreshes compare path/mtime/size metadata and open only changed or new notes. This
        still runs only inside the bounded subprocess worker, so a blocked macOS
        File Provider traversal cannot block an agent/tool thread.
        """
        _ensure_private_dir(destination.parent)
        destination.unlink(missing_ok=True)
        mode = "full"
        conn: Optional[sqlite3.Connection] = None

        if source_index and source_index.is_file() and source_index.resolve() != destination.resolve():
            source_conn: Optional[sqlite3.Connection] = None
            try:
                source_conn = self._conn(source_index, readonly=True)
                columns = {row[1] for row in source_conn.execute("PRAGMA table_info(note_meta)")}
                required = {"source_mtime_ns", "source_size"}
                indexed_files = int(source_conn.execute("SELECT count(*) FROM vault_fts").fetchone()[0])
                if indexed_files > 0:
                    candidate_conn = sqlite3.connect(str(destination), timeout=2.0)
                    try:
                        source_conn.backup(candidate_conn)
                    finally:
                        candidate_conn.close()
                    mode = "incremental" if required.issubset(columns) else "upgrade"
            except (OSError, sqlite3.Error, TypeError, ValueError):
                mode = "full"
                destination.unlink(missing_ok=True)
            finally:
                if source_conn is not None:
                    source_conn.close()

        if mode == "full":
            destination.unlink(missing_ok=True)
            conn = self._conn(destination)
            conn.execute("PRAGMA journal_mode=DELETE")
            self._create_index_tables(conn)
            existing: dict[str, tuple[int, int]] = {}
        else:
            conn = self._conn(destination)
            conn.execute("PRAGMA journal_mode=DELETE")
            if mode == "upgrade":
                conn.execute(
                    "ALTER TABLE note_meta ADD COLUMN source_mtime_ns INTEGER NOT NULL DEFAULT -1"
                )
                conn.execute(
                    "ALTER TABLE note_meta ADD COLUMN source_size INTEGER NOT NULL DEFAULT -1"
                )
            existing = {
                row[0]: (int(row[1]), int(row[2]))
                for row in conn.execute("SELECT path, source_mtime_ns, source_size FROM note_meta")
            }

        changed_files = 0
        unchanged_files = 0
        removed_files = 0
        skipped_dataless = 0
        skipped_errors = 0
        seen: set[str] = set()
        try:
            for p in self._iter_markdown():
                try:
                    rel = self._rel(p)
                except Exception:
                    skipped_errors += 1
                    continue
                seen.add(rel)
                if self._is_dataless(p):
                    skipped_dataless += 1
                    continue
                try:
                    info = p.stat()
                except OSError:
                    skipped_errors += 1
                    continue
                fingerprint = (int(info.st_mtime_ns), int(info.st_size))
                if mode in {"incremental", "upgrade"} and existing.get(rel) == fingerprint:
                    unchanged_files += 1
                    continue
                try:
                    text = self._read_index_text(p)
                except Exception:
                    # Preserve the prior row for changed-but-temporarily-unreadable
                    # notes. The next bounded refresh can retry it.
                    skipped_errors += 1
                    continue
                meta = _note_metadata(rel, text)
                aliases = meta["aliases"]
                tags = meta["tags"]
                if mode in {"incremental", "upgrade"} and rel in existing:
                    conn.execute("DELETE FROM vault_fts WHERE path = ?", (rel,))
                    conn.execute("DELETE FROM note_meta WHERE path = ?", (rel,))
                    conn.execute("DELETE FROM note_links WHERE source_path = ?", (rel,))
                conn.execute(
                    "INSERT INTO vault_fts(path, title, aliases, tags, content) VALUES (?, ?, ?, ?, ?)",
                    (rel, meta["title"], " ".join(aliases), " ".join(tags), text),
                )
                conn.execute(
                    "INSERT INTO note_meta(path, slug, title, aliases_json, tags_json, status, source, updated, created, links_json, source_mtime_ns, source_size) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        rel,
                        meta["slug"],
                        meta["title"],
                        json.dumps(aliases, ensure_ascii=False),
                        json.dumps(tags, ensure_ascii=False),
                        meta["status"],
                        meta["source"],
                        meta["updated"],
                        meta["created"],
                        json.dumps(meta["links"], ensure_ascii=False),
                        fingerprint[0],
                        fingerprint[1],
                    ),
                )
                conn.executemany(
                    "INSERT INTO note_links(source_path, target, target_slug, display, is_embed) VALUES (?, ?, ?, ?, ?)",
                    [
                        (rel, link["target"], link["target_slug"], link["display"], link["is_embed"])
                        for link in meta["links"]
                    ],
                )
                changed_files += 1

            if mode == "incremental":
                missing = set(existing) - seen
                removal_budget = max(1, int(len(existing) * 0.10))
                if len(missing) > removal_budget:
                    raise RuntimeError(
                        f"vault scan omitted {len(missing)} indexed notes; refusing a mass-removal candidate"
                    )
                for rel in sorted(missing):
                    conn.execute("DELETE FROM vault_fts WHERE path = ?", (rel,))
                    conn.execute("DELETE FROM note_meta WHERE path = ?", (rel,))
                    conn.execute("DELETE FROM note_links WHERE source_path = ?", (rel,))
                    removed_files += 1

            indexed_files = int(conn.execute("SELECT count(*) FROM vault_fts").fetchone()[0])
            indexed_at = time.time()
            conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('indexed_at', ?)", (str(indexed_at),))
            conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('schema_version', '3')")
            conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('indexed_files', ?)", (str(indexed_files),))
            conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('skipped_dataless', ?)", (str(skipped_dataless),))
            conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('skipped_errors', ?)", (str(skipped_errors),))
            conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('refresh_mode', ?)", (mode,))
            conn.commit()
            if indexed_files <= 0:
                raise RuntimeError("new vault index is empty")
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("new vault index failed integrity_check")
        finally:
            conn.close()
        return {
            "mode": mode,
            "indexed_files": indexed_files,
            "changed_files": changed_files,
            "unchanged_files": unchanged_files,
            "removed_files": removed_files,
            "skipped_dataless": skipped_dataless,
            "skipped_errors": skipped_errors,
        }

    def _index_state(self, path: Optional[Path] = None) -> dict[str, Any]:
        target = path or self._index_path
        state: dict[str, Any] = {"valid": False, "indexed_at": 0.0, "indexed_files": 0}
        if not target or not target.is_file():
            return state
        try:
            conn = self._conn(target, readonly=True)
            try:
                required = {"vault_fts", "note_meta", "note_links", "meta"}
                present = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
                if not required.issubset(present):
                    return state
                indexed_files = int(conn.execute("SELECT count(*) FROM vault_fts").fetchone()[0])
                row = conn.execute("SELECT value FROM meta WHERE key='indexed_at'").fetchone()
                indexed_at = float(row[0]) if row else 0.0
                state.update(valid=indexed_files > 0 and indexed_at > 0, indexed_at=indexed_at, indexed_files=indexed_files)
                return state
            finally:
                conn.close()
        except (OSError, sqlite3.Error, TypeError, ValueError):
            return state

    def _write_refresh_status(self, payload: dict[str, Any]) -> None:
        if not self._index_path:
            return
        status_path = self._index_path.parent / "refresh_status.json"
        tmp = status_path.with_name(f".{status_path.name}.{os.getpid()}.tmp")
        body = {"updated_at": _now(), **payload}
        tmp.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(status_path)

    def _read_text_bounded(self, requested_path: str, max_chars: int) -> tuple[str, str, bool]:
        """Resolve, authorize, and read one note inside one bounded subprocess."""
        if not self._vault or not self._index_path:
            raise RuntimeError("Vault path is not configured")
        timeout = max(0.1, min(float(self._config.get("read_timeout_seconds") or _DEFAULT_READ_TIMEOUT_SECONDS), 30.0))
        state_dir = self._index_path.parent
        state_dir.mkdir(parents=True, exist_ok=True)
        token = f"{os.getpid()}-{time.time_ns()}"
        payload_path = state_dir / f".read-{token}.json"
        payload_path.write_text(
            json.dumps({
                "vault_path": str(self._vault),
                "profile": self._profile,
                **self._policy_payload(),
                "requested_path": str(requested_path),
                "max_chars": int(max_chars),
            }, sort_keys=True),
            encoding="utf-8",
        )
        os.chmod(payload_path, 0o600)
        worker_path = Path(__file__).with_name("index_worker.py")
        try:
            completed = subprocess.run(
                [sys.executable, str(worker_path), "--read", str(Path(__file__).absolute()), str(payload_path)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"Vault note read timed out after {timeout:g}s; the request remained bounded") from exc
        finally:
            payload_path.unlink(missing_ok=True)
        if completed.returncode != 0:
            stderr_hash = hashlib.sha256(completed.stderr.encode("utf-8", errors="ignore")).hexdigest()[:16]
            raise RuntimeError(f"vault read worker exited {completed.returncode} (stderr_sha256={stderr_hash})")
        try:
            payload = json.loads(completed.stdout)
            if payload.get("error"):
                raise RuntimeError(str(payload["error"]))
            return str(payload["path"]), str(payload["content"]), bool(payload["truncated"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("vault read worker returned an invalid response") from exc

    def _capture_state_dir(self) -> Path:
        if self._index_path is not None:
            return self._index_path.parent
        # A partially initialized provider still uses profile-local Hermes
        # state. Never fall back to placing worker payloads inside the vault.
        return self._hermes_home / "state" / "obsidian_vault"

    def _installation_home(self) -> Path:
        return _installation_root(self._hermes_home)

    def _normalize_capture_args(self, args: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(args, dict):
            raise ValueError("capture arguments must be an object")
        categories = set(self._category_paths())
        raw_category = args.get("category")
        if raw_category is not None and not isinstance(raw_category, str):
            raise ValueError("capture category must be a string")
        category = (raw_category or "other").strip().lower()
        if category not in categories:
            raise ValueError("capture category is not supported")
        raw_content = args.get("content")
        if not isinstance(raw_content, str):
            raise ValueError("capture content must be a string")
        content = raw_content.strip()
        if not content:
            raise ValueError("content is required")
        if len(content) > _MAX_CAPTURE_CONTENT_CHARS:
            raise ValueError(f"capture content exceeds {_MAX_CAPTURE_CONTENT_CHARS} characters")

        normalized: dict[str, Any] = {"category": category, "content": content}
        for key in ("title", "slug", "target_path", "related_hub", "source", "system_id"):
            raw_value = args.get(key)
            if raw_value is not None and not isinstance(raw_value, str):
                raise ValueError(f"capture {key} must be a string")
            value = (raw_value or "").strip()
            if len(value) > _MAX_CAPTURE_FIELD_CHARS:
                raise ValueError(f"capture {key} exceeds {_MAX_CAPTURE_FIELD_CHARS} characters")
            if any(character in value for character in ("\x00", "\r", "\n")):
                raise ValueError(f"capture {key} contains a control character")
            if key == "source" and value and re.fullmatch(r"[A-Za-z0-9._-]{1,80}", value) is None:
                raise ValueError("capture source contains unsupported characters")
            if key == "system_id" and value and re.fullmatch(r"[a-z0-9][a-z0-9-]{1,62}", value) is None:
                value = ""  # fail open: malformed system ids are dropped, capture proceeds
            if value:
                normalized[key] = value

        related = args.get("related") or []
        if not isinstance(related, list):
            raise ValueError("related must be an array of vault-relative markdown paths")
        if len(related) > _MAX_CAPTURE_RELATED_PATHS:
            raise ValueError(f"related exceeds {_MAX_CAPTURE_RELATED_PATHS} paths")
        normalized_related: list[str] = []
        for value in related:
            if not isinstance(value, str):
                raise ValueError("related contains a non-string path")
            item = value.strip()
            if not item or len(item) > _MAX_CAPTURE_FIELD_CHARS or any(
                character in item for character in ("\x00", "\r", "\n")
            ):
                raise ValueError("related contains an invalid path")
            normalized_related.append(item)
        if normalized_related:
            normalized["related"] = normalized_related

        secret_material = "\n".join(
            value
            for value in normalized.values()
            if isinstance(value, str)
        )
        if normalized_related:
            secret_material += "\n" + "\n".join(normalized_related)
        if self._contains_secret(secret_material):
            raise ValueError("capture rejected: an argument looks like it may contain a secret/token/password")
        return normalized

    def _capture_operation_id(self, args: dict[str, Any]) -> str:
        canonical = self._normalize_capture_args(args)
        encoded = json.dumps(
            {"profile": self._profile, "capture": canonical},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()[:32]

    def _capture_receipt_path(self, operation_id: str) -> Path:
        return self._capture_state_dir() / "capture_receipts" / f"{operation_id}.json"

    def _load_capture_receipt(self, operation_id: str) -> Optional[dict[str, Any]]:
        path = self._capture_receipt_path(operation_id)
        try:
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > 256_000:
                return None
            if hasattr(os, "getuid") and info.st_uid != os.getuid():
                return None
            if stat.S_IMODE(info.st_mode) & 0o077:
                return None
            payload = json.loads(path.read_text(encoding="utf-8"))
            result = payload.get("result") if isinstance(payload, dict) else None
            return result if isinstance(result, dict) else None
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _write_capture_receipt(self, operation_id: str, result: dict[str, Any]) -> None:
        path = self._capture_receipt_path(operation_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
        encoded = json.dumps({
            "operation_id": operation_id,
            "completed_at": _now(),
            "result": result,
        }, ensure_ascii=False, sort_keys=True).encode("utf-8")
        temp_fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(temp_fd, "wb") as destination:
                destination.write(encoded)
                destination.flush()
                os.fsync(destination.fileno())
            tmp.replace(path)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        self._prune_capture_receipts(path.parent, keep=path)

    def _prune_capture_receipts(self, directory: Path, *, keep: Optional[Path] = None) -> None:
        """Bound idempotency state without following links or deleting foreign files."""
        now = time.time()
        candidates: list[tuple[float, Path]] = []
        try:
            entries = list(directory.glob("*.json"))
        except OSError:
            return
        for entry in entries:
            try:
                info = entry.lstat()
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            if hasattr(os, "getuid") and info.st_uid != os.getuid():
                continue
            if keep is not None and entry == keep:
                candidates.append((info.st_mtime, entry))
                continue
            if now - info.st_mtime > _CAPTURE_RECEIPT_MAX_AGE_SECONDS:
                entry.unlink(missing_ok=True)
                continue
            candidates.append((info.st_mtime, entry))
        if len(candidates) <= _MAX_CAPTURE_RECEIPTS:
            return
        removable = [item for item in sorted(candidates) if keep is None or item[1] != keep]
        for _mtime, entry in removable[: len(candidates) - _MAX_CAPTURE_RECEIPTS]:
            entry.unlink(missing_ok=True)

    def _prune_stale_capture_payloads(self, state_dir: Path) -> None:
        cutoff = time.time() - 24 * 60 * 60
        try:
            entries = list(state_dir.glob(".capture-*.json"))
        except OSError:
            return
        for entry in entries:
            try:
                info = entry.lstat()
                if (
                    stat.S_ISREG(info.st_mode)
                    and info.st_mtime < cutoff
                    and (not hasattr(os, "getuid") or info.st_uid == os.getuid())
                ):
                    entry.unlink(missing_ok=True)
            except OSError:
                continue

    def _capture_receipt_is_current(self, operation_id: str, result: dict[str, Any]) -> bool:
        """Verify a receipt still identifies the exact committed source bytes."""
        proofs = result.get("_capture_proofs")
        if not isinstance(proofs, dict) or not proofs:
            return False
        status = str(result.get("status") or "")
        if status == "drafted":
            expected_paths = {str(result.get("draft_path") or "")}
        elif status == "created_trusted_promote":
            if result.get("wiring_queue_recorded") is not True:
                # The source commit is durable, but an exact retry must resume
                # the auxiliary queue until its record is confirmed present.
                return False
            expected_paths = {
                str(result.get("path") or ""),
                str(result.get("draft_path") or ""),
            }
        elif status in {"created", "appended"}:
            expected_paths = {str(result.get("path") or "")}
        else:
            return False
        if "" in expected_paths or set(proofs) != expected_paths:
            return False
        try:
            for rel, expected_digest in proofs.items():
                if not re.fullmatch(r"[0-9a-f]{64}", str(expected_digest)):
                    return False
                current = self._secure_read_rel(str(rel))
                current_digest = hashlib.sha256(current.encode("utf-8")).hexdigest()
                if current_digest != expected_digest:
                    return False
            return True
        except (OSError, ValueError):
            return False

    def _with_capture_proofs(
        self,
        result: dict[str, Any],
        committed: dict[str, str],
    ) -> dict[str, Any]:
        result["_capture_proofs"] = {
            rel: hashlib.sha256(text.encode("utf-8")).hexdigest()
            for rel, text in committed.items()
        }
        return result

    def _capture_bounded(self, args: dict[str, Any]) -> dict[str, Any]:
        """Execute all source-backed capture work inside one killable worker."""
        if not self._vault:
            raise ValueError("Vault path is not configured")
        capture_args = self._normalize_capture_args(args)

        timeout = max(0.1, min(float(self._config.get("capture_timeout_seconds") or _DEFAULT_CAPTURE_TIMEOUT_SECONDS), 60.0))
        state_dir = self._capture_state_dir()
        state_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(state_dir, 0o700)
        self._prune_stale_capture_payloads(state_dir)
        token = f"{os.getpid()}-{time.time_ns()}"
        payload_path = state_dir / f".capture-{token}.json"
        operation_id = self._capture_operation_id(capture_args)
        payload = {
            "vault_path": str(self._vault),
            "profile": self._profile,
            "hermes_home": str(self._hermes_home),
            "capture_lock_root": str(self._installation_home() / "state" / "obsidian_vault" / "capture-locks"),
            "index_path": str(self._index_path or (state_dir / "index.sqlite3")),
            **self._policy_payload(),
            "capture_args": capture_args,
            "operation_id": operation_id,
        }
        payload_bytes = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        if len(payload_bytes) > _MAX_CAPTURE_PAYLOAD_BYTES:
            raise ValueError(f"capture request exceeds {_MAX_CAPTURE_PAYLOAD_BYTES} encoded bytes")
        payload_fd = os.open(payload_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(payload_fd, "wb") as payload_file:
                payload_file.write(payload_bytes)
                payload_file.flush()
                os.fsync(payload_file.fileno())
        except Exception:
            payload_path.unlink(missing_ok=True)
            raise
        worker_path = Path(__file__).with_name("index_worker.py")
        try:
            completed = subprocess.run(
                [sys.executable, str(worker_path), "--capture", str(Path(__file__).absolute()), str(payload_path)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"Vault capture timed out after {timeout:g}s; outcome may be pending, and an exact retry is idempotent"
            ) from exc
        finally:
            payload_path.unlink(missing_ok=True)
            # A killed worker may have committed immediately before the
            # deadline. Refreshing an unchanged index is cheaper than hiding a
            # durable capture from other turns.
            self._mark_index_dirty()
        if completed.returncode != 0:
            stderr_hash = hashlib.sha256(completed.stderr.encode("utf-8", errors="ignore")).hexdigest()[:16]
            raise RuntimeError(f"vault capture worker exited {completed.returncode} (stderr_sha256={stderr_hash})")
        try:
            result = json.loads(completed.stdout)
            if not isinstance(result, dict):
                raise TypeError("worker result is not an object")
            if result.get("error"):
                raise RuntimeError(str(result["error"]))
            result.pop("_capture_proofs", None)
            return result
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("vault capture worker returned an invalid response") from exc

    def _refresh_index_bounded(self, key: str, dirty_generation: int) -> None:
        assert self._vault is not None and self._index_path is not None
        state_dir = self._index_path.parent
        token = f"{os.getpid()}-{time.time_ns()}"
        build_path = state_dir / f".{self._index_path.name}.build-{token}"
        payload_path = state_dir / f".refresh-{token}.json"
        timeout = max(5, min(int(self._config.get("rebuild_timeout_seconds") or _DEFAULT_REBUILD_TIMEOUT_SECONDS), 300))
        payload = {
            "vault_path": str(self._vault),
            "profile": self._profile,
            **self._policy_payload(),
            "source_index": str(self._index_path),
            "destination": str(build_path),
        }
        refresh_lock = None
        try:
            state_dir.mkdir(parents=True, exist_ok=True)
            if _fcntl is not None:
                lock_fd = os.open(state_dir / "index-refresh.lock", os.O_CREAT | os.O_RDWR, 0o600)
                refresh_lock = os.fdopen(lock_fd, "a+")
                try:
                    _fcntl.flock(refresh_lock.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
                except BlockingIOError:
                    # Another Hermes process is already refreshing this exact
                    # profile index. Continue serving the last-good database.
                    return
            payload_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
            os.chmod(payload_path, 0o600)
            worker_path = Path(__file__).with_name("index_worker.py")
            completed = subprocess.run(
                [sys.executable, str(worker_path), str(Path(__file__).resolve()), str(payload_path)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
                check=False,
            )
            if completed.returncode != 0:
                stderr_hash = hashlib.sha256(completed.stderr.encode("utf-8", errors="ignore")).hexdigest()[:16]
                raise RuntimeError(f"index worker exited {completed.returncode} (stderr_sha256={stderr_hash})")
            try:
                refresh_stats = json.loads(completed.stdout)
                if not isinstance(refresh_stats, dict):
                    raise TypeError("worker result is not an object")
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise RuntimeError("index worker returned invalid refresh statistics") from exc
            candidate = self._index_state(build_path)
            if not candidate["valid"]:
                raise RuntimeError("index worker produced an empty or invalid candidate")
            build_path.replace(self._index_path)
            self._last_index = candidate["indexed_at"]
            if self._index_dirty_generation == dirty_generation:
                self._index_dirty = False
            self._write_refresh_status({"status": "ok", **candidate, "refresh": refresh_stats})
        except subprocess.TimeoutExpired:
            self._write_refresh_status({"status": "timeout", "timeout_seconds": timeout})
        except Exception as exc:
            fingerprint = hashlib.sha256(f"{type(exc).__name__}:{exc}".encode("utf-8", errors="ignore")).hexdigest()[:16]
            self._write_refresh_status({"status": "error", "error_type": type(exc).__name__, "fingerprint": fingerprint})
        finally:
            payload_path.unlink(missing_ok=True)
            build_path.unlink(missing_ok=True)
            for suffix in ("-journal", "-wal", "-shm"):
                build_path.with_name(build_path.name + suffix).unlink(missing_ok=True)
            if refresh_lock is not None and _fcntl is not None:
                try:
                    _fcntl.flock(refresh_lock.fileno(), _fcntl.LOCK_UN)
                finally:
                    refresh_lock.close()
            with _INDEX_REFRESH_LOCK:
                current = _INDEX_REFRESH_THREADS.get(key)
                if current is threading.current_thread():
                    _INDEX_REFRESH_THREADS.pop(key, None)

    def _start_index_refresh(self, *, force: bool = False) -> Optional[threading.Thread]:
        if not self._vault or not self._index_path:
            return None
        key = str(self._index_path.resolve())
        retry = max(1, int(self._config.get("refresh_retry_seconds") or _DEFAULT_REFRESH_RETRY_SECONDS))
        now = time.monotonic()
        with _INDEX_REFRESH_LOCK:
            existing = _INDEX_REFRESH_THREADS.get(key)
            if existing and existing.is_alive():
                return existing
            last_attempt = _INDEX_REFRESH_LAST_ATTEMPT.get(key)
            # Even a dirty/forced refresh must obey cooldown after a failed attempt;
            # otherwise each search can launch another expensive iCloud walk.
            if last_attempt is not None and now - last_attempt < retry:
                return None
            _INDEX_REFRESH_LAST_ATTEMPT[key] = now
            thread = threading.Thread(
                target=self._refresh_index_bounded,
                args=(key, self._index_dirty_generation),
                name="obsidian-vault-index-refresh",
                daemon=True,
            )
            _INDEX_REFRESH_THREADS[key] = thread
            thread.start()
            return thread

    def _ensure_index(self, force: bool = False) -> bool:
        if not self._vault or not self._index_path:
            return False
        state = self._index_state()
        refresh = max(0, int(self._config.get("refresh_seconds") or _DEFAULT_REFRESH_SECONDS))
        stale = not state["valid"] or force or self._index_dirty
        if state["valid"] and refresh == 0:
            stale = True
        elif state["valid"] and refresh > 0 and time.time() - state["indexed_at"] >= refresh:
            stale = True

        thread: Optional[threading.Thread] = None
        if stale:
            thread = self._start_index_refresh(force=force or self._index_dirty)

        if not state["valid"] and thread is not None:
            initial_wait = max(0.0, min(float(self._config.get("initial_wait_seconds") or _DEFAULT_INITIAL_WAIT_SECONDS), 5.0))
            thread.join(timeout=initial_wait)
            state = self._index_state()

        if state["valid"]:
            self._last_index = state["indexed_at"]
            return True
        return False

    def _fts_query(self, query: str) -> str:
        # Keep FTS syntax simple and safe: split punctuation-heavy aliases like
        # "message-intel" into separate prefix terms instead of emitting a bare
        # hyphenated token, which FTS5 parses as NOT/column syntax.
        raw_terms = re.findall(r"[A-Za-z0-9]{2,}", query.lower())
        short = self._config.get("short_search_terms")
        short_terms = {str(t).lower() for t in short} if isinstance(short, list) else set(_DEFAULT_SHORT_SEARCH_TERMS)
        terms = [term for term in raw_terms if len(term) >= 3 or term in short_terms]
        stop = {"the", "and", "for", "with", "that", "this", "what", "where", "when", "about", "from", "into", "vault", "obsidian"}
        uniq = []
        for t in terms:
            if t in stop or t in uniq:
                continue
            uniq.append(t)
            if len(uniq) >= 10:
                break
        return " OR ".join(f'{t}*' for t in uniq) or '""'

    def _indexed_link_resolver(self, conn: sqlite3.Connection):
        """Request-local caches; lexical paths and indexed rows, never source I/O."""
        exact_cache: dict[str, Any] = {}
        bare_cache: dict[str, Any] = {}
        resolved_cache: dict[tuple[str, str], Any] = {}

        def exact(path):
            if path not in exact_cache:
                exact_cache[path] = conn.execute(
                    "SELECT path, title FROM note_meta WHERE path = ?", (path,)
                ).fetchone()
            return exact_cache[path]

        def allowed(row):
            return row if row and self._is_allowed_rel(row[0]) else None

        def resolve(source, target):
            key = (source, target)
            if key in resolved_cache:
                return resolved_cache[key]
            value = target.split("#", 1)[0].split("^", 1)[0].strip()
            result = None
            if value and not value.startswith("/") and "\\" not in value and "\x00" not in value:
                relative = value.startswith(("./", "../"))
                qualified = "/" in value
                parts = source.split("/")[:-1] if relative or not qualified else []
                valid = True
                for part in value.split("/"):
                    if part in {"", "."}:
                        continue
                    if part == "..":
                        if not parts:
                            valid = False
                            break
                        parts.pop()
                    else:
                        parts.append(part)
                if valid and parts:
                    candidate = "/".join(parts)
                    if not candidate.lower().endswith(".md"):
                        candidate += ".md"
                    row = exact(candidate)
                    result = allowed(row)
                    # A denied sibling is not permission to pick a different note.
                    if row is None and not qualified:
                        slug = _note_slug_from_target(value)
                        if slug not in bare_cache:
                            matches = conn.execute(
                                "SELECT path, title FROM note_meta WHERE slug = ? ORDER BY path LIMIT 2",
                                (slug,),
                            ).fetchall()
                            bare_cache[slug] = allowed(matches[0]) if len(matches) == 1 else None
                        result = bare_cache[slug]
            resolved_cache[key] = result
            return result

        return resolve

    def _related_for_path(self, conn: sqlite3.Connection, path: str, limit: int = 4, *, resolver=None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        limit = max(0, min(int(limit), 10))
        if not limit or not self._is_allowed_rel(path):
            return [], []
        resolve = resolver or self._indexed_link_resolver(conn)
        links: list[dict[str, Any]] = []
        backlinks: list[dict[str, Any]] = []
        seen = set()
        # Inspect at most 256 source edges per direction, not a multiplying join.
        for target, display, is_embed in conn.execute(
            "SELECT target, display, is_embed FROM note_links "
            "WHERE source_path = ? ORDER BY rowid LIMIT 256", (path,)
        ):
            row = resolve(path, target)
            edge = row[0] if row else target
            if edge in seen:
                continue
            seen.add(edge)
            links.append({
                "target": target, "path": row[0] if row else None,
                "title": row[1] if row else None, "display": display, "embed": bool(is_embed),
            })
            if len(links) >= limit:
                break
        seen = set()
        for source_path, source_title, target, display, is_embed in conn.execute(
            "SELECT l.source_path, m.title, l.target, l.display, l.is_embed "
            "FROM note_links l JOIN note_meta m ON m.path = l.source_path "
            "WHERE l.target_slug = ? AND l.source_path != ? "
            "ORDER BY l.source_path, l.rowid LIMIT 256", (_note_slug_from_path(path), path)
        ):
            if source_path in seen or not self._is_allowed_rel(source_path):
                continue
            row = resolve(source_path, target)
            if not row or row[0] != path:
                continue
            seen.add(source_path)
            backlinks.append({
                "path": source_path, "title": source_title,
                "display": display, "embed": bool(is_embed),
            })
            if len(backlinks) >= limit:
                break
        return links, backlinks

    def _applied_target_exists(self, target: str) -> bool:
        """True when an applied draft's exact target note is present (fail-safe False on any error)."""
        try:
            if not self._vault or not (Path(self._vault) / target).is_file():
                return False
            return _frontmatter(self._secure_read_rel(target)).get("status") == "published"
        except (OSError, ValueError):
            return False

    def _rank_score(self, r: Any) -> float:
        """Recency/supersession-aware rank (lower is better, like bm25).

        BM25 alone lets a long, older "remaining work" note outrank the newer
        note that records completion. Boost recent notes (half-life style decay),
        boost living `current-state` hubs, and demote notes marked superseded,
        archived, or `superseded-by:`. `recency_weight: 0` restores pure BM25.
        Fail-safe: any parse problem returns the raw score.
        """
        base = float(r[3] or 0.0)
        try:
            weight = max(0.0, min(float(self._config.get("recency_weight", 1.0)), 2.0))
            if weight == 0.0:
                return base
            mult = 1.0
            stamp = str(r[8] or r[9] or "")[:10]
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", stamp):
                age = max(0.0, (datetime.now() - datetime.strptime(stamp, "%Y-%m-%d")).days)
                mult += weight * (0.5 ** (age / 30.0))
            tags = {t.lower() for t in _json_list(r[5])}
            if "current-state" in tags or PurePosixPath(str(r[0])).stem.endswith("-current-state"):
                mult += weight
            status = str(r[6] or "").lower()
            fm = _frontmatter(r[10] or "") if r[10] else {}
            if status in {"superseded", "archived", "retired"} or fm.get("superseded-by"):
                mult *= 0.35
            # bm25 scores are negative: a larger multiplier ranks the note higher
            return base * mult
        except Exception:
            return base

    def _search(self, query: str, limit: int = 8, path_prefix: str = "") -> list[dict[str, Any]]:
        if not self._ensure_index():
            raise RuntimeError("Vault index is warming or its last rebuild failed; retry shortly. The search request remained bounded.")
        limit = max(1, min(int(limit or 8), 20))
        fts = self._fts_query(query)
        if fts == '""':
            return []
        conn = self._conn(readonly=True)
        try:
            sql = (
                "SELECT f.path, f.title, snippet(vault_fts, 4, '[', ']', '…', 14), "
                "bm25(vault_fts, 0.0, 5.0, 3.0, 2.0, 1.0) AS score, "
                "m.aliases_json, m.tags_json, m.status, m.source, m.updated, m.created, "
                "substr(f.content, 1, 16384) "
                "FROM vault_fts f LEFT JOIN note_meta m ON m.path = f.path "
                "WHERE vault_fts MATCH ?"
            )
            params: list[Any] = [fts]
            # Literal, case-sensitive prefix; do not strip a directory's slash.
            if path_prefix:
                sql += " AND substr(f.path, 1, ?) = ?"
                params.extend([len(path_prefix), path_prefix])
            candidate_cap = min(200, max(40, limit * 10))
            rows = conn.execute(sql + " ORDER BY score, f.path LIMIT ?", [*params, candidate_cap]).fetchall()
            rows = [r for r in rows if self._is_allowed_rel(r[0])]
            matching = {r[0]: r for r in rows}
            selected = {}
            keep_history = "_drafts" in path_prefix.split("/")
            for r in rows:
                if not keep_history and "_drafts" in r[0].split("/") and r[6] == "applied":
                    target = _frontmatter(r[10] or "").get("applied-to", "")
                    # Applied-to is an exact path, not a wikilink or a fuzzy title.
                    parts = target.split("/")
                    if (target.endswith(".md") and all(p not in {"", ".", "..", "_drafts"} for p in parts)
                            and "\\" not in target and "\x00" not in target
                            and self._is_allowed_rel(target) and target.startswith(path_prefix)):
                        if target not in matching:
                            matching[target] = conn.execute(
                                sql + " AND f.path = ? LIMIT 1", [*params, target]
                            ).fetchone()
                        canonical = matching[target]
                        if canonical and canonical[6] == "published" and r[1] and canonical[1] == r[1]:
                            r = canonical
                        elif self._applied_target_exists(target):
                            # Already-merged capture copy: the published note is canonical; hide the duplicate.
                            if canonical and canonical[6] == "published":
                                r = canonical
                            else:
                                continue
                selected[r[0]] = r
            rows = sorted(selected.values(), key=lambda r: (self._rank_score(r), r[0]))[:limit]
            related_setting = self._config.get("max_related_notes")
            related_limit = max(0, min(int(4 if related_setting is None else related_setting), 10))
            resolver = self._indexed_link_resolver(conn)
            results: list[dict[str, Any]] = []
            for r in rows:
                links, backlinks = self._related_for_path(conn, r[0], related_limit, resolver=resolver)
                results.append({
                    "path": r[0],
                    "title": r[1],
                    "snippet": re.sub(r"\s+", " ", r[2] or "").strip(),
                    "score": r[3],
                    "metadata": {
                        "aliases": _json_list(r[4]),
                        "tags": _json_list(r[5]),
                        "status": r[6] or "",
                        "source": r[7] or "",
                        "updated": r[8] or "",
                        "created": r[9] or "",
                    },
                    "links": links,
                    "backlinks": backlinks,
                })
            return results
        finally:
            conn.close()

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if not self._vault or not query:
            return ""
        try:
            limit = int(self._config.get("max_prefetch_results") or 6)
            if self._private_autocontext(query):
                results = self._search(query, limit=limit)
            else:
                # Auto-context on non-private topics never surfaces private-lane
                # notes, even for granted profiles; over-fetch so the count holds.
                private = tuple(_policy_key(p) for p in self._private_prefixes())
                results = [
                    r for r in self._search(query, limit=limit * 2)
                    if not (private and _policy_key(str(r.get("path", ""))).startswith(private))
                ][:limit]
            if not results:
                return ""
            lines = [
                "## Obsidian Vault Context",
                "Relevant canonical vault notes. Use as grounding; verify live systems separately for operational data.",
            ]
            for r in results[:limit]:
                label = f"{r['path']}"
                if r.get("title"):
                    label += f" — {r['title']}"
                meta = r.get("metadata") or {}
                bits = []
                if meta.get("status"):
                    bits.append(f"status={meta['status']}")
                if meta.get("updated"):
                    bits.append(f"updated={meta['updated']}")
                tags = meta.get("tags") or []
                if tags:
                    bits.append("tags=" + ",".join(tags[:4]))
                if r.get("backlinks"):
                    bits.append("backlinks=" + ",".join(b.get("path", "") for b in r["backlinks"][:2] if b.get("path")))
                suffix = f" ({'; '.join(bits)})" if bits else ""
                lines.append(f"- {label}{suffix}: {r['snippet'][:420]}")
            return "\n".join(lines)
        except Exception:
            return ""

    def _private_autocontext(self, query: str) -> bool:
        if not self._private_access():
            return False
        always = self._config.get("private_autocontext_profiles") or []
        if isinstance(always, list) and self._profile in {str(p) for p in always}:
            return True
        terms = self._config.get("private_intent_terms")
        words = [str(t) for t in terms] if isinstance(terms, list) else _DEFAULT_PRIVATE_INTENT_TERMS
        if not words:
            return False
        pattern = r"\b(" + "|".join(re.escape(w) for w in words if w) + r")\b"
        return re.search(pattern, query, re.IGNORECASE) is not None

    # ----- tools -------------------------------------------------------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        capture = json.loads(json.dumps(CAPTURE_SCHEMA))
        capture["parameters"]["properties"]["category"]["enum"] = sorted(self._category_paths())
        return [SEARCH_SCHEMA, READ_SCHEMA, capture]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if self._config_error:
            return tool_error(f"obsidian_vault is disabled: {self._config_error}")
        try:
            if tool_name == "obsidian_vault_search":
                results = self._search(args.get("query", ""), int(args.get("limit") or 8), args.get("path_prefix") or "")
                return json.dumps({"results": results, "count": len(results)})
            if tool_name == "obsidian_vault_read":
                max_chars = max(1000, min(int(args.get("max_chars") or 12000), 50000))
                rel, text, truncated = self._read_text_bounded(str(args["path"]), max_chars)
                return json.dumps({"path": rel, "content": text, "truncated": truncated})
            if tool_name == "obsidian_vault_capture":
                held = self._capture_gate(args)
                if held is not None:
                    return json.dumps(held)
                return json.dumps(self._capture_bounded(args))
            return tool_error(f"Unknown tool: {tool_name}")
        except Exception as e:
            return tool_error(str(e))

    def _capture_gate(self, args: Dict[str, Any]) -> Optional[dict[str, Any]]:
        """Durability gate. off: no-op. shadow (default): classify and log, never block.
        enforce: day-to-day captures go to a local ledger and unsure ones to the weekly
        review queue instead of the vault. Any gate error fails open to a normal capture."""
        mode = str(self._config.get("capture_gate") or "shadow").strip().lower()
        if mode not in {"shadow", "enforce"} or args.get("force_vault") is True:
            return None
        # Secret-shaped input must be rejected by capture, never copied into
        # the local held ledger first.
        if self._contains_secret(json.dumps(args, ensure_ascii=False, default=str)):
            return None
        try:
            import importlib.util as _ilu
            _spec = _ilu.spec_from_file_location("obsidian_vault_capture_gate", Path(__file__).with_name("capture_gate.py"))
            _gate = _ilu.module_from_spec(_spec)
            assert _spec.loader is not None
            _spec.loader.exec_module(_gate)
        except Exception:
            return None
        try:
            content = str(args.get("content") or "")
            verdict = _gate.classify(content, str(args.get("category") or ""), str(args.get("title") or ""))
            state = self._capture_state_dir()
            state.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256(content.encode("utf-8", errors="ignore")).hexdigest()[:16]
            entry = {"at": _now(), "profile": self._profile, "mode": mode, "category": args.get("category"),
                     "title": args.get("title"), "content_sha": digest, **verdict.as_dict(),
                     "system_id": args.get("system_id") or None}
            held = mode == "enforce" and verdict.label in {"daily", "unsure"}
            if held:
                # The ledger keeps the full text locally so nothing is lost; it is never vault-indexed.
                entry["content"] = content[:8000]
            ledger = state / ("capture_gate_held.jsonl" if held else "capture_gate_log.jsonl")
            fd = os.open(ledger, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            if not held:
                return None
            return {"status": "held_" + verdict.label, "vault_write": False, "reasons": verdict.reasons,
                    "note": ("Day-to-day detail kept out of the vault (your system of record keeps it). "
                             if verdict.label == "daily" else "Queued for the weekly owner review. ")
                            + "Re-capture with force_vault=true only if this is a durable rule or decision."}
        except Exception:
            return None

    def _contains_secret(self, text: str) -> bool:
        return any(p.search(text or "") for p in _REDACT_PATTERNS)

    def _route(self, category: str, slug: str, target_path: str = "") -> str:
        if target_path:
            return target_path.strip("/")
        category = (category or "other").lower()
        roots = self._category_paths()
        root = roots.get(category) or roots["other"]
        return f"{root.strip('/')}/{slug}.md"

    def _capture_rel_parts(self, rel: str) -> tuple[str, ...]:
        candidate = PurePosixPath(str(rel).strip("/"))
        parts = candidate.parts
        if candidate.is_absolute() or not parts or any(part in {"", ".", ".."} for part in parts):
            raise ValueError("capture path must be a safe vault-relative path")
        return parts

    def _open_capture_parent(self, rel: str, *, create: bool) -> tuple[int, str]:
        if not self._vault:
            raise ValueError("Vault path is not configured")
        parts = self._capture_rel_parts(rel)
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        current_fd = os.open(self._vault, directory_flags)
        try:
            for component in parts[:-1]:
                try:
                    next_fd = os.open(component, directory_flags, dir_fd=current_fd)
                except FileNotFoundError:
                    if not create:
                        raise
                    os.mkdir(component, 0o755, dir_fd=current_fd)
                    next_fd = os.open(component, directory_flags, dir_fd=current_fd)
                os.close(current_fd)
                current_fd = next_fd
            return current_fd, parts[-1]
        except Exception:
            os.close(current_fd)
            raise

    def _secure_read_rel(self, rel: str) -> str:
        parent_fd, name = self._open_capture_parent(rel, create=False)
        try:
            fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
            with os.fdopen(fd, "r", encoding="utf-8", errors="ignore") as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("capture source is not a regular file")
                if info.st_size > _MAX_CAPTURE_SOURCE_BYTES:
                    raise ValueError(f"capture source exceeds {_MAX_CAPTURE_SOURCE_BYTES} bytes")
                return source.read()
        finally:
            os.close(parent_fd)

    def _secure_exists_rel(self, rel: str) -> bool:
        try:
            parent_fd, name = self._open_capture_parent(rel, create=False)
        except FileNotFoundError:
            return False
        try:
            info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            return stat.S_ISREG(info.st_mode)
        except FileNotFoundError:
            return False
        finally:
            os.close(parent_fd)

    def _require_current_capture_parent(self, rel: str, parent_fd: int) -> None:
        """Fail if an opened parent directory moved away from the live vault path."""
        try:
            current_fd, current_name = self._open_capture_parent(rel, create=False)
        except (FileNotFoundError, NotADirectoryError, OSError) as exc:
            raise RuntimeError("capture parent changed during commit") from exc
        try:
            expected_name = self._capture_rel_parts(rel)[-1]
            opened = os.fstat(parent_fd)
            current = os.fstat(current_fd)
            if current_name != expected_name or (opened.st_dev, opened.st_ino) != (
                current.st_dev,
                current.st_ino,
            ):
                raise RuntimeError("capture parent changed during commit")
        finally:
            os.close(current_fd)

    def _unlink_capture_inode(self, parent_fd: int, name: str, expected: os.stat_result) -> None:
        """Remove only the inode this capture published; never delete a replacement."""
        try:
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if (current.st_dev, current.st_ino) == (expected.st_dev, expected.st_ino):
            os.unlink(name, dir_fd=parent_fd)

    def _unlink_capture_file_if_content_matches(self, rel: str, expected_content: str) -> None:
        """Best-effort cleanup for a file this capture created before a later failure."""
        parent_fd, name = self._open_capture_parent(rel, create=False)
        try:
            fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    return
                with os.fdopen(fd, "r", encoding="utf-8", errors="ignore") as source:
                    current_content = source.read()
                    fd = -1
                if current_content == expected_content:
                    self._unlink_capture_inode(parent_fd, name, info)
                    os.fsync(parent_fd)
            finally:
                if fd >= 0:
                    os.close(fd)
        except FileNotFoundError:
            return
        finally:
            os.close(parent_fd)

    def _atomic_write(self, rel: str, content: str, *, expect_absent: bool = False) -> None:
        """Create a confined source file atomically without replacing an inode.

        Capture never mutates an existing vault note. ``link`` provides an
        atomic create-if-absent publish step. Parent-inode checks immediately
        before and after publication also fail closed if a human/File Provider
        moves that directory away from the live vault path during the commit.
        """
        if not expect_absent:
            raise ValueError("capture source replacement is prohibited")
        parent_fd, name = self._open_capture_parent(rel, create=True)
        temp_name = f".{name}.tmp.{os.getpid()}.{time.time_ns()}"
        temp_created = False
        published = False
        temp_info: Optional[os.stat_result] = None
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            temp_fd = os.open(temp_name, flags, 0o600, dir_fd=parent_fd)
            temp_created = True
            with os.fdopen(temp_fd, "w", encoding="utf-8") as destination:
                destination.write(content)
                destination.flush()
                os.fsync(destination.fileno())
                temp_info = os.fstat(destination.fileno())
            self._require_current_capture_parent(rel, parent_fd)
            os.link(
                temp_name,
                name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
            published = True
            os.fsync(parent_fd)
            self._require_current_capture_parent(rel, parent_fd)
            target_info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if temp_info is None or (target_info.st_dev, target_info.st_ino) != (
                temp_info.st_dev,
                temp_info.st_ino,
            ):
                raise RuntimeError("capture target changed during commit")
            os.unlink(temp_name, dir_fd=parent_fd)
            temp_created = False
            os.fsync(parent_fd)
            self._require_current_capture_parent(rel, parent_fd)
        except Exception:
            if published and temp_info is not None:
                self._unlink_capture_inode(parent_fd, name, temp_info)
                try:
                    os.fsync(parent_fd)
                except OSError:
                    pass
            raise
        finally:
            if temp_created:
                try:
                    os.unlink(temp_name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
            os.close(parent_fd)

    def _append_log(self, rel: str, note: str) -> None:
        if not self._vault:
            return
        line = f"{_now()} [hermes] {note} {rel}\n".encode("utf-8")
        parent_fd, name = self._open_capture_parent("log.md", create=True)
        log_fd = -1
        created_by_capture = False
        original_size = 0
        opened_info: Optional[os.stat_result] = None
        appended_bytes = 0

        def rollback_append() -> None:
            if log_fd < 0 or opened_info is None:
                return
            current = os.fstat(log_fd)
            if (current.st_dev, current.st_ino) != (opened_info.st_dev, opened_info.st_ino):
                return
            # Restore only the exact size resulting from this write attempt.
            # Any additional bytes may be concurrent human/audit data and win
            # over cleanup, even when that leaves a warning for manual review.
            if current.st_size == original_size + appended_bytes:
                os.ftruncate(log_fd, original_size)
                os.fsync(log_fd)
                if created_by_capture and original_size == 0:
                    self._unlink_capture_inode(parent_fd, name, opened_info)
                    os.fsync(parent_fd)

        try:
            self._require_current_capture_parent("log.md", parent_fd)
            flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
            try:
                log_fd = os.open(name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent_fd)
                created_by_capture = True
            except FileExistsError:
                log_fd = os.open(name, flags, dir_fd=parent_fd)
            try:
                opened_info = os.fstat(log_fd)
                original_size = opened_info.st_size
                if not stat.S_ISREG(opened_info.st_mode):
                    raise ValueError("vault audit log is not a regular file")
                try:
                    self._require_current_capture_parent("log.md", parent_fd)
                except Exception:
                    rollback_append()
                    raise
                try:
                    written = os.write(log_fd, line)
                    appended_bytes = written
                    if written != len(line):
                        raise OSError("short write to vault audit log")
                    os.fsync(log_fd)
                    self._require_current_capture_parent("log.md", parent_fd)
                except Exception:
                    rollback_append()
                    raise
            finally:
                if log_fd >= 0:
                    os.close(log_fd)
        finally:
            os.close(parent_fd)

    def _append_capture_log_once(self, rel: str, note: str, operation_id: str) -> None:
        if not self._vault:
            return
        entry_id = hashlib.sha256(f"{rel}\n{note}".encode("utf-8")).hexdigest()[:12]
        marker = f"[capture:{operation_id}:{entry_id}]"
        if self._secure_exists_rel("log.md") and marker in self._secure_read_rel("log.md"):
            return
        self._append_log(rel, f"{note} {marker}")

    def _capture_log_warnings(
        self,
        entries: Iterable[tuple[str, str]],
        operation_id: str,
    ) -> list[str]:
        warnings: list[str] = []
        for rel, note in entries:
            try:
                self._append_capture_log_once(rel, note, operation_id)
            except Exception as exc:
                fingerprint = hashlib.sha256(
                    f"{type(exc).__name__}:{exc}".encode("utf-8", errors="ignore")
                ).hexdigest()[:16]
                warnings.append(f"audit log append failed ({fingerprint})")
        return warnings

    def _has_draft_component(self, rel: str) -> bool:
        return "_drafts" in Path(rel.strip("/")).parts

    def _trusted_promotion_allowed(self, rel: str) -> bool:
        return any(rel.startswith(root) for root in self._trusted_promotion_prefixes())

    def _validate_related_path(self, value: str, label: str) -> str:
        rel = str(value or "").strip().strip("/")
        if not rel:
            raise ValueError(f"{label} is required")
        if not rel.endswith(".md"):
            rel += ".md"
        path = self._resolve_vault_path(rel)
        rel = self._rel(path)
        if self._has_draft_component(rel):
            raise ValueError(f"{label} cannot contain _drafts: {rel}")
        if not rel.startswith(self._wiki_root()):
            raise ValueError(f"{label} must reference an active {self._wiki_root().rstrip('/')} note: {rel}")
        if not self._secure_exists_rel(rel):
            raise ValueError(f"{label} must reference an existing active markdown note: {rel}")
        status = _frontmatter(self._secure_read_rel(rel)).get("status", "")
        if status != "published":
            raise ValueError(f"{label} must reference a published active note: {rel}")
        return rel

    def _note_title(self, rel: str) -> str:
        path = self._resolve_vault_path(rel)
        rel = self._rel(path)
        text = self._secure_read_rel(rel)
        for line in text.splitlines():
            if line.startswith("# "):
                return line[2:].strip()
        return path.stem.replace("-", " ").strip().title()

    def _wiki_link(self, rel: str) -> str:
        target = Path(rel).with_suffix("").as_posix()
        return f"[[{target}|{self._note_title(rel)}]]"

    def _related_section(self, related_paths: list[str]) -> str:
        if not related_paths:
            return ""
        links = "\n".join(f"- {self._wiki_link(rel)}" for rel in related_paths)
        return f"\n\n## Related\n\n{links}\n"

    def _queue_path(self) -> Path:
        if self._index_path:
            return self._index_path.parent / "wiring_queue.jsonl"
        return self._hermes_home / "state" / "obsidian_vault" / "wiring_queue.jsonl"

    def _emit_wiring_queue(self, target_rel: str, hub_rel: str, source_draft_rel: str) -> bool:
        queue_path = self._queue_path()
        _ensure_private_dir(queue_path.parent)
        record_id = hashlib.sha256(f"{target_rel}\n{hub_rel}".encode("utf-8")).hexdigest()[:20]
        if queue_path.exists():
            for line in queue_path.read_text(errors="ignore").splitlines():
                try:
                    if json.loads(line).get("id") == record_id:
                        return True
                except Exception:
                    continue
        record = {
            "id": record_id,
            "created_at": _now(),
            "profile": self._profile,
            "status": "pending",
            "reason": "related_hub_is_published_and_locked",
            "target_path": target_rel,
            "related_hub": hub_rel,
            "source_draft": source_draft_rel,
        }
        fd = os.open(queue_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        return True

    def _wiring_queue_outcome(
        self,
        target_rel: str,
        hub_rel: str,
        source_draft_rel: str,
    ) -> tuple[bool, list[str]]:
        """Return a committed-source outcome without hiding queue ambiguity."""
        try:
            return self._emit_wiring_queue(target_rel, hub_rel, source_draft_rel), []
        except Exception as exc:
            fingerprint = hashlib.sha256(
                f"{type(exc).__name__}:{exc}".encode("utf-8", errors="ignore")
            ).hexdigest()[:16]
            return False, [
                f"wiring queue append failed ({fingerprint}); exact retry will resume queue work"
            ]

    def _new_note_body(
        self,
        title: str,
        content: str,
        source: str,
        status: str,
        extra: Optional[dict[str, str]] = None,
        *,
        category: str = "",
        related_paths: Optional[list[str]] = None,
    ) -> str:
        ts = _now()
        tags = ["hermes-capture", "obsidian-vault-memory"]
        category_tag = _slugify(category) if category else ""
        if category_tag and category_tag not in tags:
            tags.insert(0, category_tag)
        fm = {
            "author": "hermes",
            "source": source,
            "created": ts,
            "updated": ts,
            "status": status,
            "tags": "[" + ", ".join(tags) + "]",
        }
        if extra:
            fm.update(extra)
        system_id = getattr(self, "_capture_system_id", "")
        if system_id and "system" not in fm:
            fm["system"] = system_id
        normalized_content = content.strip()
        lines = normalized_content.splitlines()
        if lines and lines[0].strip().casefold() == f"# {title}".casefold():
            normalized_content = "\n".join(lines[1:]).lstrip()
        body = "---\n" + "\n".join(f"{k}: {v}" for k, v in fm.items()) + "\n---\n\n"
        body += f"# {title}\n\n{normalized_content}\n"
        body = body.rstrip() + self._related_section(related_paths or [])
        return body

    def _trusted_applied_draft_body(
        self,
        *,
        title: str,
        marked_payload: str,
        source: str,
        category: str,
        target_rel: str,
        operation_id: str,
    ) -> str:
        applied_at = _now()
        return self._new_note_body(
            title,
            marked_payload,
            source,
            "applied",
            {
                "intended-promotion-path": target_rel,
                "hermes-capture-id": operation_id,
                "applied-to": target_rel,
                "applied-by": "hermes",
                "applied-at": applied_at,
            },
            category=category,
        )

    def _capture_target(self, args: dict[str, Any]) -> tuple[str, str]:
        """Use the same effective routing for retirement checks and capture."""
        content = str(args["content"])
        title = str(args.get("title") or content.splitlines()[0][:80] or "Vault Capture")
        slug = _slugify(str(args.get("slug") or title))
        rel = self._route(str(args["category"]), slug, str(args.get("target_path") or ""))
        if not rel.endswith(".md"):
            rel += ".md"
        return title, self._rel(self._resolve_vault_path(rel))

    def _load_capture_retirements(self) -> dict[str, str]:
        """Read the owner-managed v1 retirement map only inside the capture worker."""
        path = self._queue_path().parent / "capture_retirements.json"

        def identity(info):
            return (info.st_dev, info.st_ino, info.st_mode, info.st_uid,
                    info.st_size, info.st_mtime_ns, info.st_ctime_ns)

        def unique_fields(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate field")
                result[key] = value
            return result

        try:
            try:
                before = path.lstat()
            except FileNotFoundError:
                return {}
            if (
                not stat.S_ISREG(before.st_mode)
                or (hasattr(os, "getuid") and before.st_uid != os.getuid())
                or stat.S_IMODE(before.st_mode) & 0o077
                or before.st_size > _MAX_CAPTURE_RETIREMENT_BYTES
            ):
                raise ValueError("unsafe registry file")
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            fd = os.open(path, flags)
            with os.fdopen(fd, "rb") as source:
                if identity(os.fstat(source.fileno())) != identity(before):
                    raise ValueError("registry changed")
                raw = source.read(_MAX_CAPTURE_RETIREMENT_BYTES + 1)
                if (
                    len(raw) > _MAX_CAPTURE_RETIREMENT_BYTES
                    or identity(os.fstat(source.fileno())) != identity(before)
                    or identity(path.lstat()) != identity(before)
                ):
                    raise ValueError("registry changed")
            registry = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_fields)
            if (
                not isinstance(registry, dict)
                or set(registry) != {"version", "profile", "vault_path", "targets"}
                or type(registry["version"]) is not int
                or registry["version"] != 1
                or registry["profile"] != self._profile
                or self._vault is None
                or registry["vault_path"] != str(self._vault.resolve())
            ):
                raise ValueError("invalid registry envelope")
            targets = registry["targets"]
            if not isinstance(targets, dict) or len(targets) > _MAX_CAPTURE_RETIREMENT_TARGETS:
                raise ValueError("invalid targets")
            for old, new in targets.items():
                for rel in (old, new):
                    if (
                        not isinstance(rel, str)
                        or not rel.startswith(self._wiki_root())
                        or not rel.endswith(".md")
                        or re.search(r"[\x00-\x1f\x7f-\x9f\\]", rel)
                        or any(part in {"", ".", "..", "_drafts"} for part in rel.split("/"))
                    ):
                        raise ValueError("invalid registry path")
                    rel.encode("utf-8")  # Reject escaped lone surrogates too.
                    if self._rel(self._vault / rel) != rel:
                        raise ValueError("noncanonical registry path")
            # Case variants can address the same retired filename on a
            # case-insensitive vault filesystem. Deny aliases; never rewrite paths.
            sources = {old.lower() for old in targets}
            destinations = {new.lower() for new in targets.values()}
            if (len(sources) != len(targets) or len(destinations) != len(targets)
                    or destinations.intersection(sources)):
                raise ValueError("duplicate identity, destination, self mapping, chain or cycle")
            return targets
        except (OSError, ValueError, TypeError, RuntimeError):
            # Never echo malformed state or filesystem details into tool output.
            raise ValueError("capture retirement registry is invalid or unsafe; capture refused") from None

    def _guard_capture_retirement(self, args: dict[str, Any]) -> None:
        # The registry lives in this profile's own state dir and must name this
        # profile; another profile's registry is never consulted.
        targets = self._load_capture_retirements()
        if targets:
            _, rel = self._capture_target(args)
            replacement = {old.lower(): new for old, new in targets.items()}.get(rel.lower())
            if replacement is not None:
                raise ValueError(
                    f"capture target is retired: {rel}; replacement location: {replacement}. "
                    "Capture was refused; no redirect or additional write permission is granted."
                )

    def _capture_unbounded(
        self,
        args: dict[str, Any],
        operation_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Worker-only capture implementation with idempotent local receipts."""
        args = self._normalize_capture_args(args)
        self._guard_capture_retirement(args)
        operation_id = operation_id or self._capture_operation_id(args)
        receipt = self._load_capture_receipt(operation_id)
        if receipt is not None and self._capture_receipt_is_current(operation_id, receipt):
            return {**receipt, "deduplicated": True, "operation_id": operation_id}
        try:
            result = self._capture_impl(args, operation_id)
        finally:
            # This runs in the worker and in direct isolated tests. The parent
            # separately marks its own provider instance dirty.
            self._mark_index_dirty()
        result = {**result, "operation_id": operation_id}
        try:
            self._write_capture_receipt(operation_id, result)
        except Exception as exc:
            fingerprint = hashlib.sha256(
                f"{type(exc).__name__}:{exc}".encode("utf-8", errors="ignore")
            ).hexdigest()[:16]
            result.setdefault("warnings", []).append(f"capture receipt write failed ({fingerprint})")
        return result

    def _find_capture_draft(
        self,
        target_rel: str,
        operation_id: str,
        marked_payload: str,
    ) -> Optional[tuple[str, str]]:
        if not self._vault:
            return None
        draft_dir = self._vault / Path(target_rel).parent / "_drafts"
        if not draft_dir.is_dir():
            return None
        marker = f"hermes-capture-id: {operation_id}"
        for candidate in sorted(draft_dir.glob(f"{Path(target_rel).stem}*.md")):
            try:
                candidate_rel = self._rel(candidate)
                text = self._secure_read_rel(candidate_rel)
            except (OSError, ValueError):
                continue
            if (
                marker in text
                and marked_payload in text
                and _frontmatter(text).get("status") != "applied"
            ):
                return candidate_rel, text
        return None

    def _capture_impl(self, args: dict[str, Any], operation_id: str) -> dict[str, Any]:
        if not self._vault:
            raise ValueError("Vault path is not configured")
        try:
            self._secure_read_rel("AGENTS.md")
        except (FileNotFoundError, OSError, ValueError) as exc:
            raise ValueError("vault policy AGENTS.md is missing or unsafe") from exc

        args = self._normalize_capture_args(args)
        content = str(args["content"])
        source = str(args.get("source") or "hermes")
        self._capture_system_id = str(args.get("system_id") or "")
        category = str(args["category"])
        title, rel = self._capture_target(args)
        if self._has_draft_component(rel):
            raise ValueError(f"target_path cannot contain _drafts: {rel}")
        if not (self._is_direct_write(rel) or self._trusted_promotion_allowed(rel)):
            raise ValueError("target is outside the approved trusted-promotion lanes and direct write lane")

        marker = f"<!-- hermes-capture:{operation_id} -->"
        payload_content = content.strip()
        payload_lines = payload_content.splitlines()
        if payload_lines and payload_lines[0].strip().casefold() == f"# {title}".casefold():
            payload_content = "\n".join(payload_lines[1:]).lstrip()
        marked_payload = f"{payload_content.rstrip()}\n\n{marker}"

        target_exists = self._secure_exists_rel(rel)
        if target_exists:
            existing = self._secure_read_rel(rel)
            fm = _frontmatter(existing)

            # A marker is not enough: the exact normalized payload must still be
            # present, otherwise a retry repairs/stages the removed fact.
            if marked_payload in existing and (marker in existing or fm.get("hermes-capture-id") == operation_id):
                if fm.get("promoted-from"):
                    draft_rel = fm["promoted-from"]
                    try:
                        draft_text = self._secure_read_rel(draft_rel)
                    except FileNotFoundError:
                        draft_text = self._trusted_applied_draft_body(
                            title=title,
                            marked_payload=marked_payload,
                            source=source,
                            category=category,
                            target_rel=rel,
                            operation_id=operation_id,
                        )
                        self._atomic_write(draft_rel, draft_text, expect_absent=True)
                    draft_fm = _frontmatter(draft_text)
                    if marked_payload not in draft_text or draft_fm.get("status") != "applied":
                        raise ValueError("trusted-promotion applied draft is missing or changed")
                    hub_rel = self._validate_related_path(str(args.get("related_hub") or ""), "related_hub")
                    queue_recorded, queue_warnings = self._wiring_queue_outcome(rel, hub_rel, draft_rel)
                    result: dict[str, Any] = {
                        "status": "created_trusted_promote",
                        "draft_path": draft_rel,
                        "path": rel,
                        "related_hub": hub_rel,
                        "wiring_queue_recorded": queue_recorded,
                        "wiring_queue_path": str(self._queue_path()),
                        "deduplicated": True,
                        "recovered": True,
                    }
                    warnings = queue_warnings + self._capture_log_warnings([
                        (draft_rel, "created trusted-promote draft"),
                        (rel, "created trusted-promoted published note"),
                        (draft_rel, "marked trusted-promote draft applied"),
                    ], operation_id)
                    if warnings:
                        result["warnings"] = warnings
                    return self._with_capture_proofs(result, {rel: existing, draft_rel: draft_text})
                status = "created" if fm.get("hermes-capture-id") == operation_id else "appended"
                result = {
                    "status": status,
                    "path": rel,
                    "deduplicated": True,
                    "recovered": True,
                }
                warnings = self._capture_log_warnings([(rel, status)], operation_id)
                if warnings:
                    result["warnings"] = warnings
                return self._with_capture_proofs(result, {rel: existing})

            # Existing source notes are immutable from this model-callable tool.
            # Every update is a new proposal file, regardless of current status,
            # so a human/File Provider change can never race a read-modify-replace.
            prior_draft = self._find_capture_draft(rel, operation_id, marked_payload)
            if prior_draft is not None:
                draft_rel, draft_text = prior_draft
                result = {
                    "status": "drafted",
                    "reason": "target exists; direct source edits are locked",
                    "draft_path": draft_rel,
                    "target_path": rel,
                    "deduplicated": True,
                    "recovered": True,
                }
                return self._with_capture_proofs(result, {draft_rel: draft_text})
            draft_rel = (
                Path(rel).parent
                / "_drafts"
                / f"{Path(rel).stem}-capture-{_timestamp_slug()}-{time.time_ns()}.md"
            ).as_posix()
            draft = self._new_note_body(
                f"Draft update: {Path(rel).stem}",
                f"Intended target: `{rel}`\n\n## Proposed append\n{marked_payload}\n",
                source,
                "draft",
                {"intended-promotion-path": rel, "hermes-capture-id": operation_id},
                category=category,
            )
            self._atomic_write(draft_rel, draft, expect_absent=True)
            result = {
                "status": "drafted",
                "reason": "target exists; direct source edits are locked",
                "draft_path": draft_rel,
                "target_path": rel,
            }
            warnings = self._capture_log_warnings(
                [(draft_rel, "proposed existing-note update at")], operation_id
            )
            if warnings:
                result["warnings"] = warnings
            return self._with_capture_proofs(result, {draft_rel: draft})

        # New inbox notes are direct writes; approved active lanes use trusted promotion.
        if self._is_direct_write(rel):
            body = self._new_note_body(
                title,
                marked_payload,
                source,
                "published",
                {"hermes-capture-id": operation_id},
                category=category,
            )
            self._atomic_write(rel, body, expect_absent=True)
            result = {"status": "created", "path": rel}
            warnings = self._capture_log_warnings([(rel, "created")], operation_id)
            if warnings:
                result["warnings"] = warnings
            return self._with_capture_proofs(result, {rel: body})

        hub_rel = self._validate_related_path(str(args.get("related_hub") or ""), "related_hub")
        related_paths = [hub_rel]
        for value in args.get("related") or []:
            peer_rel = self._validate_related_path(str(value), "related")
            if peer_rel not in related_paths:
                related_paths.append(peer_rel)
        if rel in related_paths:
            raise ValueError("related_hub/related cannot point to the note being created")

        draft_rel = (
            Path(rel).parent
            / "_drafts"
            / f"{Path(rel).stem}-capture-{operation_id[:16]}.md"
        ).as_posix()
        applied_draft = self._trusted_applied_draft_body(
            title=title,
            marked_payload=marked_payload,
            source=source,
            category=category,
            target_rel=rel,
            operation_id=operation_id,
        )
        final = self._new_note_body(
            title,
            marked_payload,
            source,
            "published",
            {
                "promoted-from": draft_rel,
                "promoted-by": "hermes",
                "hermes-capture-id": operation_id,
            },
            category=category,
            related_paths=related_paths,
        )
        queue_recorded = False
        draft_created = False
        try:
            existing_draft = self._secure_read_rel(draft_rel)
        except FileNotFoundError:
            self._atomic_write(draft_rel, applied_draft, expect_absent=True)
            draft_created = True
        else:
            if (
                marked_payload not in existing_draft
                or _frontmatter(existing_draft).get("status") != "applied"
            ):
                raise ValueError("trusted-promotion applied draft path is occupied")
            applied_draft = existing_draft
        try:
            self._atomic_write(rel, final, expect_absent=True)
        except Exception:
            if draft_created:
                self._unlink_capture_file_if_content_matches(draft_rel, applied_draft)
            raise
        queue_recorded, queue_warnings = self._wiring_queue_outcome(rel, hub_rel, draft_rel)
        result = {
            "status": "created_trusted_promote",
            "draft_path": draft_rel,
            "path": rel,
            "related_hub": hub_rel,
            "wiring_queue_recorded": queue_recorded,
            "wiring_queue_path": str(self._queue_path()),
        }
        warnings = queue_warnings + self._capture_log_warnings([
            (draft_rel, "created trusted-promote draft"),
            (rel, "created trusted-promoted published note"),
            (draft_rel, "marked trusted-promote draft applied"),
        ], operation_id)
        if warnings:
            result["warnings"] = warnings
        return self._with_capture_proofs(result, {rel: final, draft_rel: applied_draft})


def register(ctx) -> None:
    ctx.register_memory_provider(ObsidianVaultMemoryProvider())
