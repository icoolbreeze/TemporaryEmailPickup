"""Discover the user's real Brave / Chrome named profiles.

The app historically launches Chromium with a *blank* ``--user-data-dir`` under
``%LOCALAPPDATA%\\TemporaryEmailPickup\\browser_profiles_<slug>\\``. That
isolated profile is a brand-new fingerprint every time, so Cloudflare's
challenge always fires. The user already keeps named profiles in their real
Brave / Chrome ``User Data`` (``Default`` -> 工作, ``Profile 4`` -> CF, …)
that have Cloudflare clearance, so this module helps the app open **those**
instead.

The module is stdlib only — no Selenium, no ``psutil``. ``Local State`` is
read directly and ``profile.info_cache`` is parsed for display names.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path


#: Default ``Local State`` paths searched on Windows. Other platforms will
#: return an empty list (the app is Windows-only but the discovery helper is
#: kept portable for tests).
DEFAULT_USER_DATA_PATHS: dict[str, Path] = {
    "Brave": Path(os.environ.get("LOCALAPPDATA", "")) / "BraveSoftware" / "Brave-Browser" / "User Data",
    "Chrome": Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data",
}


@dataclass(frozen=True, slots=True)
class ChromiumNamedProfile:
    """One named Chromium profile discovered in a real ``User Data`` root."""

    browser: str          # "Brave" or "Chrome"
    directory: str        # e.g. "Default" or "Profile 4"
    name: str             # display name, e.g. "CF" or "工作"
    user_data_dir: Path   # the real User Data root, *not* the per-profile dir


def _candidate_user_data_roots() -> list[tuple[str, Path]]:
    """Return the ``(browser, path)`` pairs the discovery step will read."""
    roots: list[tuple[str, Path]] = []
    for browser, path in DEFAULT_USER_DATA_PATHS.items():
        if path and path.is_dir():
            roots.append((browser, path))
    return roots


def parse_local_state_payload(
    payload: object,
    *,
    browser: str,
    user_data_dir: Path,
) -> list[ChromiumNamedProfile]:
    """Parse the ``profile.info_cache`` portion of a ``Local State`` blob.

    Tests use this directly so they can run without a real Brave/Chrome
    install: pass a parsed dict shaped like ``{... "profile": {"info_cache":
    {"Default": {"name": "工作"}, "Profile 4": {"name": "CF"}, ...}}}``.

    Entries without a ``name`` field, or whose ``name`` is the empty string,
    are kept and exposed by directory so the UI can still list them; entries
    whose ``name`` is ``None`` are dropped. Chromium never uses the literal
    string ``"Profile 4"`` as a human-readable name, so it is safe to
    fall back to ``directory`` whenever the cache entry is malformed.
    """
    if not isinstance(payload, dict):
        return []
    info_cache = payload.get("profile", {}).get("info_cache", {}) if isinstance(
        payload.get("profile"), dict
    ) else {}
    if not isinstance(info_cache, dict):
        return []

    profiles: list[ChromiumNamedProfile] = []
    for directory, entry in info_cache.items():
        if not isinstance(directory, str) or not directory:
            continue
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if name is None:
            continue
        if not isinstance(name, str):
            name = str(name)
        display_name = name.strip() or directory
        profiles.append(
            ChromiumNamedProfile(
                browser=browser,
                directory=directory,
                name=display_name,
                user_data_dir=Path(user_data_dir),
            )
        )
    return profiles


def parse_local_state_file(path: Path, *, browser: str) -> list[ChromiumNamedProfile]:
    """Read ``Local State`` JSON from ``path`` and return its named profiles."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return []
    try:
        payload = json.loads(text)
    except ValueError:
        return []
    return parse_local_state_payload(
        payload,
        browser=browser,
        user_data_dir=path.parent,
    )


def discover_named_profiles(
    roots: list[tuple[str, Path]] | None = None,
) -> list[ChromiumNamedProfile]:
    """Return every named profile from the supplied (or default) User Data roots.

    The order matches the order of the supplied ``roots`` list, and within a
    single root the order is the natural JSON insertion order — that is the
    same order Brave / Chrome shows in the "Who's using …?" picker. Stable
    ordering matters because :func:`cf_pool_profiles` returns a list, and
    callers treat the first element as the default for the shared session.
    """
    pairs = roots if roots is not None else _candidate_user_data_roots()
    profiles: list[ChromiumNamedProfile] = []
    for browser, root in pairs:
        local_state = root / "Local State"
        profiles.extend(parse_local_state_file(local_state, browser=browser))
    return profiles


#: Regex used by :func:`cf_pool_profiles`. ``CF`` is allowed alone,
#: ``CF2`` ... ``CF9`` ... ``CF10`` ... are allowed with any number of
#: trailing digits, and casing does not matter. ``CHATGPT`` /
#: ``GPT LOGIN`` are deliberately excluded so they never get handed
#: out to a mailbox.
_CF_POOL_PATTERN = re.compile(r"^CF\d*$", re.IGNORECASE)


def cf_pool_profiles(
    profiles: list[ChromiumNamedProfile] | tuple[ChromiumNamedProfile, ...],
) -> list[ChromiumNamedProfile]:
    r"""Return the subset of ``profiles`` whose display name matches ``CF\d*``.

    Matches ``CF`` (no suffix) and any non-empty digit suffix, e.g. ``CF``,
    ``CF2`` ... ``CF9`` ... ``CF10`` ... . The user explicitly listed CF,
    CF2, CF3 on the test machine, and ``CHATGPT`` / ``GPT LOGIN`` are
    deliberately excluded.
    """
    pool: list[ChromiumNamedProfile] = []
    for profile in profiles:
        if not isinstance(profile, ChromiumNamedProfile):
            continue
        if _CF_POOL_PATTERN.match(profile.name.strip()):
            pool.append(profile)
    return pool


def find_cf_pool(
    roots: list[tuple[str, Path]] | None = None,
) -> list[ChromiumNamedProfile]:
    r"""Discover named profiles and return only the CF\* subset."""
    return cf_pool_profiles(discover_named_profiles(roots))


def get_user_data_dir_for_browser(
    browser: str,
    *,
    roots: list[tuple[str, Path]] | None = None,
) -> Path | None:
    """Return the real User Data root for ``browser`` (Brave or Chrome)."""
    pairs = roots if roots is not None else _candidate_user_data_roots()
    for candidate_browser, root in pairs:
        if candidate_browser == browser:
            return root
    return None
