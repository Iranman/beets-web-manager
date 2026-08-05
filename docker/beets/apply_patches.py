"""Build-time patch script for a historical Beets plugin resolution defect,
plus an always-on runtime sanity check for that same defect class.

Upstream issue: beetbox/beets#6033
Upstream fix:   beetbox/beets#6039 (landed in Beets 2.5.0)

Beets < 2.5.0's _get_plugin() iterates a plugin module's __dict__ in insertion order
and selects the first concrete BeetsPlugin subclass. When a plugin imports another
concrete plugin class (e.g. beetsplug.chroma imports MusicBrainzPlugin from
beetsplug.musicbrainz, or beetsplug.bpsync imports BeatportPlugin), the imported
class appears before the module's own plugin class in __dict__. This causes chroma
to resolve to MusicBrainzPlugin and bpsync to resolve to BeatportPlugin.

This patch backports the upstream Beets fix by requiring qualifying plugin classes
to be defined in the requested module or its subpackages:
    obj.__module__ == mod.__name__ or obj.__module__.startswith(f"{mod.__name__}.")

Dockerfile.beets's base image floats on `lscr.io/linuxserver/beets:latest`
(deliberate choice, 2026-08-05 -- see scripts/validate_compose_security.py's
comment on APPROVED_BEETS_BASE_IMAGES for the trade-off) rather than a version
pinned at build time, so the exact installed Beets version is not known ahead
of time and can change on any rebuild. apply_patch() therefore only rewrites
beets/plugins.py on versions that still carry the defect (< 2.5.0); on a fixed
version it skips the source patch entirely. Either way, it always runs the
runtime sanity check afterward, so a future upstream regression of this exact
defect class still fails the build loudly instead of silently shipping broken
chroma/bpsync plugin resolution.
"""

import importlib
import inspect
import sys
import beets
import beets.plugins


FIXED_UPSTREAM_FROM = (2, 5, 0)
TARGET_FILE = "/lsiopy/lib/python3.12/site-packages/beets/plugins.py"

ORIGINAL_BLOCK = """        for obj in getattr(namespace, name).__dict__.values():
            if (
                inspect.isclass(obj)
                and not isinstance(
                    obj, GenericAlias
                )  # seems to be needed for python <= 3.9 only
                and issubclass(obj, BeetsPlugin)
                and obj != BeetsPlugin
                and not inspect.isabstract(obj)
            ):
                return obj()"""

PATCHED_BLOCK = """        mod = getattr(namespace, name)
        for obj in mod.__dict__.values():
            if (
                inspect.isclass(obj)
                and not isinstance(
                    obj, GenericAlias
                )  # seems to be needed for python <= 3.9 only
                and issubclass(obj, BeetsPlugin)
                and obj != BeetsPlugin
                and not inspect.isabstract(obj)
                and (
                    obj.__module__ == mod.__name__
                    or obj.__module__.startswith(f"{mod.__name__}.")
                )
            ):
                return obj()"""


def _version_tuple(version_string):
    """Parse a Beets version string's leading (major, minor, patch) as ints.

    Tolerant of suffixes Beets/PyPI versions can carry (e.g. "2.13.1",
    "2.5.0.dev0") -- only the leading digit run of each of the first three
    dot-separated components is used; a missing/non-numeric component
    parses as 0 rather than raising, since this only needs to answer
    "does this version predate the upstream fix", not fully validate the
    version string.
    """
    parts = []
    for chunk in str(version_string or "").split(".")[:3]:
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def apply_patch():
    actual_version = getattr(beets, "__version__", "")
    needs_patch = _version_tuple(actual_version) < FIXED_UPSTREAM_FROM

    if needs_patch:
        # 1. Read file content
        with open(TARGET_FILE, "r", encoding="utf-8") as f:
            content = f.read()

        orig_count = content.count(ORIGINAL_BLOCK)
        patched_count = content.count(PATCHED_BLOCK)

        # 2. Strict structural verification
        if orig_count == 1 and patched_count == 0:
            # Unpatched: apply the replacement
            new_content = content.replace(ORIGINAL_BLOCK, PATCHED_BLOCK, 1)
            with open(TARGET_FILE, "w", encoding="utf-8") as f:
                f.write(new_content)
            print(
                f"Beets {actual_version} predates the upstream fix: applied plugin "
                f"resolution patch (beetbox/beets#6039) to beets/plugins.py."
            )
        elif orig_count == 0 and patched_count == 1:
            # Already correctly patched: proceed to runtime sanity verification
            print(
                f"Beets {actual_version} predates the upstream fix, but the plugin "
                f"resolution patch (beetbox/beets#6039) is already applied to beets/plugins.py."
            )
        else:
            raise RuntimeError(
                f"Invalid source state in beets/plugins.py: original_block_count={orig_count}, patched_block_count={patched_count}"
            )
    else:
        print(
            f"Beets {actual_version} already contains the upstream plugin resolution "
            f"fix (beetbox/beets#6039, landed in 2.5.0); no source patch needed."
        )

    # 3. Runtime sanity verification (always executed, patched or not). This
    # is what actually protects a floating base image: if a future Beets
    # release ever regresses this exact defect class, this fails the build
    # instead of silently shipping broken chroma/bpsync plugin resolution.
    importlib.reload(beets.plugins)

    # Verify Chroma plugin resolution
    chroma_selected = beets.plugins._get_plugin("chroma")
    if not (
        chroma_selected is not None
        and chroma_selected.__class__.__name__ == "AcoustidPlugin"
        and chroma_selected.__class__.__module__ == "beetsplug.chroma"
    ):
        raise RuntimeError(f"Sanity check failed for chroma: _get_plugin('chroma') returned {chroma_selected}")
    print("Sanity check passed: _get_plugin('chroma') -> beetsplug.chroma.AcoustidPlugin")

    # Verify BPSync plugin resolution if importable. _get_plugin() itself
    # never raises to its caller (Beets' own outer try/except in _get_plugin
    # logs and returns None on any internal failure, including a genuinely
    # missing bpsync dependency), so a None result here means "not
    # importable" and is skipped rather than treated as a sanity failure.
    # Do NOT wrap this in a broad except: that would also swallow the
    # RuntimeError below on a real class-selection regression.
    bpsync_selected = beets.plugins._get_plugin("bpsync")
    if bpsync_selected is None:
        print("Skipping bpsync runtime check (plugin not importable in this environment)")
    elif not (
        bpsync_selected.__class__.__name__ == "BPSyncPlugin"
        and bpsync_selected.__class__.__module__ == "beetsplug.bpsync"
    ):
        raise RuntimeError(f"Sanity check failed for bpsync: _get_plugin('bpsync') returned {bpsync_selected}")
    else:
        print("Sanity check passed: _get_plugin('bpsync') -> beetsplug.bpsync.BPSyncPlugin")


if __name__ == "__main__":
    apply_patch()
