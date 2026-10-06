"""Bounded subprocess entry point for rebuilding the Obsidian FTS index.

The gateway never imports this module. A background coordinator invokes it in a
separate process so a blocked macOS File Provider read cannot block an agent turn.
"""

from __future__ import annotations

import hashlib
import importlib.util
import importlib
import json
import os
import re
import stat
import sys
import types
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - capture remains bounded without flock.
    fcntl = None


def _install_import_stubs_if_needed() -> None:
    """Allow the worker to load plugin helpers without booting Hermes itself."""
    try:
        importlib.import_module("agent.memory_provider")
    except ModuleNotFoundError:
        agent_module = types.ModuleType("agent")
        memory_provider_module = types.ModuleType("agent.memory_provider")

        class MemoryProvider:
            pass

        setattr(memory_provider_module, "MemoryProvider", MemoryProvider)
        setattr(agent_module, "memory_provider", memory_provider_module)
        sys.modules["agent"] = agent_module
        sys.modules["agent.memory_provider"] = memory_provider_module
    try:
        importlib.import_module("tools.registry")
    except ModuleNotFoundError:
        tools_module = types.ModuleType("tools")
        registry_module = types.ModuleType("tools.registry")
        setattr(registry_module, "tool_error", lambda message, **extra: json.dumps({"error": str(message), **extra}))
        setattr(tools_module, "registry", registry_module)
        sys.modules["tools"] = tools_module
        sys.modules["tools.registry"] = registry_module


def _load_private_payload(path: Path) -> dict:
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise RuntimeError("worker payload is not a regular file")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise RuntimeError("worker payload has the wrong owner")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise RuntimeError("worker payload permissions are too broad")
    if info.st_size > 1_000_000:
        raise RuntimeError("worker payload is too large")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("worker payload must be an object")
    return payload


def _load_provider(plugin_path: Path, payload: dict):
    _install_import_stubs_if_needed()
    spec = importlib.util.spec_from_file_location("obsidian_vault_bounded_index_worker", plugin_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("plugin load failed")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    provider = module.ObsidianVaultMemoryProvider()
    provider._vault = Path(payload["vault_path"])
    provider._profile = payload["profile"]
    if payload.get("hermes_home"):
        provider._hermes_home = Path(payload["hermes_home"])
    if payload.get("index_path"):
        provider._index_path = Path(payload["index_path"])
    # The parent forwards its fully-resolved policy. Private grants are already
    # folded into allow/deny, so the worker never re-derives profile access.
    provider._config = {
        "allow_paths": payload["allow_paths"],
        "deny_paths": payload["deny_paths"],
        "private_paths": [],
    }
    for key in ("wiki_root", "category_paths", "direct_write_paths", "trusted_promotion_paths"):
        if key in payload:
            provider._config[key] = payload[key]
    return provider


def main() -> int:
    read_mode = len(sys.argv) == 4 and sys.argv[1] == "--read"
    capture_mode = len(sys.argv) == 4 and sys.argv[1] == "--capture"
    if read_mode or capture_mode:
        plugin_path = Path(sys.argv[2]).resolve()
        payload_path = Path(sys.argv[3]).resolve()
    elif len(sys.argv) == 3:
        plugin_path = Path(sys.argv[1]).resolve()
        payload_path = Path(sys.argv[2]).resolve()
    else:
        return 2

    payload = _load_private_payload(payload_path)
    provider = _load_provider(plugin_path, payload)

    if capture_mode:
        capture_args = payload.get("capture_args")
        operation_id = str(payload.get("operation_id") or "")
        if not isinstance(capture_args, dict) or re.fullmatch(r"[0-9a-f]{32}", operation_id) is None:
            print(json.dumps({"error": "invalid capture worker payload"}, sort_keys=True))
            return 0
        try:
            expected_operation_id = provider._capture_operation_id(capture_args)
        except ValueError:
            expected_operation_id = ""
        if operation_id != expected_operation_id:
            print(json.dumps({"error": "capture operation id does not match normalized arguments"}, sort_keys=True))
            return 0
        expected_lock_root = provider._installation_home() / "state" / "obsidian_vault" / "capture-locks"
        requested_lock_root = Path(str(payload.get("capture_lock_root") or ""))
        if requested_lock_root != expected_lock_root or not requested_lock_root.is_absolute():
            print(json.dumps({"error": "invalid capture lock root"}, sort_keys=True))
            return 0
        requested_lock_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(requested_lock_root, 0o700)
        canonical_vault = provider._vault.resolve()
        lock_name = hashlib.sha256(str(canonical_vault).encode("utf-8")).hexdigest() + ".lock"
        lock_path = requested_lock_root / lock_name
        capture_lock = None
        try:
            if fcntl is not None:
                lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
                capture_lock = os.fdopen(lock_fd, "a+")
                try:
                    fcntl.flock(capture_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    print(json.dumps({"error": "another vault capture is already running; retry shortly"}, sort_keys=True))
                    return 0
            try:
                result = provider._capture_unbounded(capture_args, operation_id)
            except ValueError as exc:
                result = {"error": str(exc)}
            except Exception as exc:
                result = {"error": f"Vault capture failed ({type(exc).__name__})"}
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0
        finally:
            if capture_lock is not None and fcntl is not None:
                try:
                    fcntl.flock(capture_lock.fileno(), fcntl.LOCK_UN)
                finally:
                    capture_lock.close()

    if read_mode:
        try:
            path = provider._resolve_vault_path(str(payload["requested_path"]))
            max_chars = max(1, min(int(payload["max_chars"]), 10_000_000))
            text = path.read_text(errors="ignore")
            result = {
                "path": provider._rel(path),
                "content": text[:max_chars],
                "truncated": len(text) > max_chars,
            }
        except ValueError as exc:
            result = {"error": str(exc)}
        except Exception as exc:
            result = {"error": f"Vault note read failed ({type(exc).__name__})"}
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0

    result = provider._build_index_database(
        Path(payload["destination"]),
        source_index=Path(payload["source_index"]),
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())