"""Build-time, version-aware compatibility patch for the Beets engine image.

Upstream issue: beetbox/beets#6033
Upstream fix:   beetbox/beets#6039 (released in Beets 2.5.0)

Beets 2.4.0's _get_plugin() iterates a plugin module's __dict__ in insertion order
and selects the first concrete BeetsPlugin subclass. When a plugin imports another
concrete plugin class (e.g. beetsplug.chroma imports MusicBrainzPlugin from
beetsplug.musicbrainz, or beetsplug.bpsync imports BeatportPlugin), the imported
class appears before the module's own plugin class in __dict__. This causes chroma
to resolve to MusicBrainzPlugin and bpsync to resolve to BeatportPlugin.

This patch backports the upstream Beets fix by requiring qualifying plugin classes
to be defined in the requested module or its subpackages:
    obj.__module__ == mod.__name__ or obj.__module__.startswith(f"{mod.__name__}.")

Version policy (semantic-version aware, not string equality):
    Beets == 2.4.0:    apply the patch (see above), then verify plugin resolution.
    Beets >= 2.5.0:    do NOT patch -- the fix is already upstream. Only verify
                        plugin resolution behaves correctly on its own.
    Beets <  2.4.0:    unsupported by this engine image; fail the build rather
                        than silently skip or misapply the patch.

This script never modifies source based on a loose text/version-string match:
the patch is only ever applied after both an exact version match AND a strict
structural match of the target code block (see apply_patch()'s orig/patched
block-count check).
"""

import importlib
import re
import sys
import beets
import beets.plugins


TARGET_FILE = "/lsiopy/lib/python3.12/site-packages/beets/plugins.py"

PATCH_MIN_VERSION = (2, 4, 0)   # oldest version this image build supports at all
PATCH_APPLY_VERSION = (2, 4, 0)  # exact version the source patch targets
PATCH_FIXED_UPSTREAM_FROM = (2, 5, 0)  # first version containing the upstream fix

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


def _parse_version(version_string: str) -> tuple:
    """Parse a Beets version string into a comparable integer tuple.

    Deliberately not a full PEP 440/semver parser (Beets versions are plain
    dotted-integer releases like '2.4.0' or '2.13.1') -- just enough to do
    correct numeric (not lexicographic string) comparison. Non-numeric
    trailing components (e.g. a 'rc1' suffix) are dropped rather than
    raising, so a well-formed prerelease version still compares sanely
    against its base release.
    """
    parts = []
    for component in re.split(r"[.\-+]", version_string.strip()):
        match = re.match(r"^\d+", component)
        if not match:
            break
        parts.append(int(match.group()))
    if not parts:
        raise ValueError(f"Could not parse a numeric version from {version_string!r}")
    return tuple(parts)


def _verify_plugin_resolution() -> None:
    """Runtime proof (not source inspection) that Beets' own plugin loader
    resolves chroma/bpsync to their own module's class, not an imported
    dependency's class. Runs regardless of whether the patch was applied in
    this process, so it also catches a hypothetical future regression on
    modern Beets that no longer contains the upstream #6039 fix."""
    importlib.reload(beets.plugins)

    chroma_selected = beets.plugins._get_plugin("chroma")
    if not (
        chroma_selected is not None
        and chroma_selected.__class__.__name__ == "AcoustidPlugin"
        and chroma_selected.__class__.__module__ == "beetsplug.chroma"
    ):
        raise RuntimeError(f"Sanity check failed for chroma: _get_plugin('chroma') returned {chroma_selected}")
    print("Sanity check passed: _get_plugin('chroma') -> beetsplug.chroma.AcoustidPlugin")

    # _get_plugin() never raises to its caller (Beets' own outer try/except
    # logs and returns None on any internal failure, including a genuinely
    # missing/incompatible bpsync dependency), so a None result here means
    # "not importable/loadable" and is skipped rather than treated as a
    # sanity failure. Do NOT wrap this in a broad except: that would also
    # swallow the RuntimeError below on a real class-selection regression.
    bpsync_selected = beets.plugins._get_plugin("bpsync")
    if bpsync_selected is None:
        print("Skipping bpsync runtime check (plugin not importable/loadable in this environment)")
    elif not (
        bpsync_selected.__class__.__name__ == "BPSyncPlugin"
        and bpsync_selected.__class__.__module__ == "beetsplug.bpsync"
    ):
        raise RuntimeError(f"Sanity check failed for bpsync: _get_plugin('bpsync') returned {bpsync_selected}")
    else:
        print("Sanity check passed: _get_plugin('bpsync') -> beetsplug.bpsync.BPSyncPlugin")


def _apply_source_patch() -> None:
    with open(TARGET_FILE, "r", encoding="utf-8") as f:
        content = f.read()

    orig_count = content.count(ORIGINAL_BLOCK)
    patched_count = content.count(PATCHED_BLOCK)

    if orig_count == 1 and patched_count == 0:
        new_content = content.replace(ORIGINAL_BLOCK, PATCHED_BLOCK, 1)
        with open(TARGET_FILE, "w", encoding="utf-8") as f:
            f.write(new_content)
        print("Successfully applied upstream plugin resolution fix (beetbox/beets#6039) to beets/plugins.py.")
    elif orig_count == 0 and patched_count == 1:
        print("Upstream plugin resolution fix (beetbox/beets#6039) is already applied to beets/plugins.py.")
    else:
        raise RuntimeError(
            f"Invalid source state in beets/plugins.py: original_block_count={orig_count}, patched_block_count={patched_count}"
        )


def apply_patch() -> None:
    actual_version_str = getattr(beets, "__version__", "")
    try:
        actual_version = _parse_version(actual_version_str)
    except ValueError as exc:
        raise RuntimeError(f"Cannot determine installed Beets version: {exc}") from exc

    if actual_version == PATCH_APPLY_VERSION:
        print(f"Beets {actual_version_str}: applying pinned plugin-resolution patch (beetbox/beets#6039).")
        _apply_source_patch()
    elif actual_version >= PATCH_FIXED_UPSTREAM_FROM:
        print(
            f"Beets {actual_version_str}: >= {'.'.join(map(str, PATCH_FIXED_UPSTREAM_FROM))}, "
            "upstream already contains the plugin-resolution fix (beetbox/beets#6039). "
            "Skipping the obsolete 2.4.0 source patch."
        )
    elif actual_version >= PATCH_MIN_VERSION:
        # Between the oldest supported version and the version containing the
        # upstream fix, but not exactly the one version this patch's ORIGINAL_BLOCK
        # was captured from -- refuse rather than risk a silent no-op or a
        # text patch applied against source it was never verified against.
        raise RuntimeError(
            f"Beets {actual_version_str} is not explicitly supported by this patch script: "
            f"it is >= the minimum supported {'.'.join(map(str, PATCH_MIN_VERSION))} but not the "
            f"exact patched version {'.'.join(map(str, PATCH_APPLY_VERSION))}, and is older than the "
            f"upstream-fixed {'.'.join(map(str, PATCH_FIXED_UPSTREAM_FROM))}. "
            "Add explicit, tested support before building this version."
        )
    else:
        raise RuntimeError(
            f"Beets {actual_version_str} is older than the minimum supported "
            f"{'.'.join(map(str, PATCH_MIN_VERSION))}. Refusing to build."
        )

    _verify_plugin_resolution()


if __name__ == "__main__":
    apply_patch()
