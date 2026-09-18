"""Automation handler: import Last.fm listening history."""

from __future__ import annotations

from typing import Any, Dict

from core.automation.deps import AutomationDeps


def auto_import_lastfm_listening(config: Dict[str, Any], deps: AutomationDeps) -> Dict[str, Any]:
    """Sync every profile's own Last.fm account (M01).

    This automation is a single install-wide schedule, not a per-profile
    one, so "run the Last.fm sync" means "keep everyone's own scrobbles up
    to date" - sequentially, via run_all_profiles, rather than picking one
    profile. An explicit ``profile_id`` in config still targets just that
    one profile, for a "sync my Last.fm now" button scoped to one person.
    """
    worker = deps.lastfm_import_worker
    if worker is None:
        return {"status": "error", "error": "Last.fm listening importer is not available"}

    manual = bool(config.get("_manual_run"))
    enabled = bool(deps.config_manager.get("lastfm.listening_sync_enabled", False))
    if not enabled:
        if not manual:
            return {"status": "skipped", "reason": "Last.fm listening sync is disabled"}
        deps.config_manager.set("lastfm.listening_sync_enabled", True)

    if "profile_id" in config:
        username = config.get("username")
        return worker.run_once(profile_id=int(config["profile_id"]), username=username or None,
                                full=bool(config.get("full")))
    return worker.run_all_profiles(full=bool(config.get("full")))