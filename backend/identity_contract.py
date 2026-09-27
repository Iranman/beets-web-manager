"""Album identity contract for mutation payloads (ARCH-009).

Permanent identities: Release Group ID = canonical album identity; Release ID
= edition identity. A payload may carry either or both, but:

* a Release ID is never copied into a Release Group field;
* when a Release ID is supplied, its authoritative Release Group is resolved
  from MusicBrainz and must agree with any supplied Release Group;
* if that resolution fails, the pairing is unverified and the mutation is
  refused (fail closed) rather than writing an unproven edition.

Field names accepted (legacy aliases have one unambiguous meaning each):
  release group: release_group_id, mb_releasegroupid, rgid
  release:       release_id, mb_albumid, representative_release_id
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

RELEASE_GROUP_FIELDS = ("release_group_id", "mb_releasegroupid", "rgid")
RELEASE_FIELDS = ("release_id", "mb_albumid", "representative_release_id")

ResolveFn = Callable[[str], str]


def _uuid(value: Any) -> str:
    text = ("" if value is None else str(value)).strip().lower()
    return text if _UUID_RE.match(text) else ""


def _first(payload: Mapping[str, Any], names) -> str:
    for name in names:
        value = payload.get(name)
        if value not in (None, ""):
            return ("" if value is None else str(value)).strip().lower()
    return ""


@dataclass(frozen=True)
class AlbumIdentityPayload:
    release_group_id: str
    release_id: str
    error: str = ""
    code: str = ""

    @property
    def ok(self) -> bool:
        return not self.code


def verify_album_identity(
    release_group_id: str = "",
    release_id: str = "",
    *,
    resolve_release_group: ResolveFn,
    require_release_group: bool = True,
) -> AlbumIdentityPayload:
    """Return explicit, verified album identity fields or an error."""
    raw_rg = (release_group_id or "").strip().lower()
    raw_rel = (release_id or "").strip().lower()
    rg, rel = _uuid(raw_rg), _uuid(raw_rel)
    if raw_rg and not rg:
        return AlbumIdentityPayload("", "", "Release Group ID must be a valid MusicBrainz UUID.", "invalid_release_group_id")
    if raw_rel and not rel:
        return AlbumIdentityPayload("", "", "Release ID must be a valid MusicBrainz UUID.", "invalid_release_id")
    if rel:
        authoritative = _uuid(resolve_release_group(rel))
        if not authoritative:
            return AlbumIdentityPayload("", "", "Could not verify which release group this release belongs to.",
                                        "release_group_unverified")
        if rg and rg != authoritative:
            return AlbumIdentityPayload("", "", f"Release {rel} belongs to release group {authoritative}, not {rg}.",
                                        "release_not_in_release_group")
        return AlbumIdentityPayload(authoritative, rel)
    if not rg and require_release_group:
        return AlbumIdentityPayload("", "", "A Release Group ID is required.", "release_group_required")
    return AlbumIdentityPayload(rg, "")


def album_identity_from_payload(payload: Mapping[str, Any], *, resolve_release_group: ResolveFn,
                                require_release_group: bool = True) -> AlbumIdentityPayload:
    """Normalize a request payload's aliases, then verify."""
    payload = payload if isinstance(payload, Mapping) else {}
    return verify_album_identity(
        _first(payload, RELEASE_GROUP_FIELDS),
        _first(payload, RELEASE_FIELDS),
        resolve_release_group=resolve_release_group,
        require_release_group=require_release_group,
    )
