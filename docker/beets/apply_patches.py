"""Build-time patch script for Beets 2.4.0 plugin resolution defect.

Upstream issue: beetbox/beets#6033
Upstream fix:   beetbox/beets#6039

Beets 2.4.0's _get_plugin() iterates a plugin module's __dict__ in insertion order
and selects the first concrete BeetsPlugin subclass. When a plugin imports another
concrete plugin class (e.g. beetsplug.chroma imports MusicBrainzPlugin from
beetsplug.musicbrainz, or beetsplug.bpsync imports BeatportPlugin), the imported
class appears before the module's own plugin class in __dict__. This causes chroma
to resolve to MusicBrainzPlugin and bpsync to resolve to BeatportPlugin.

This patch backports the upstream Beets fix by requiring qualifying plugin classes
to be defined in the requested module or its subpackages:
    obj.__module__ == mod.__name__ or obj.__module__.startswith(f"{mod.__name__}.")

Note: Beets 2.5.0 and later already contain this fix upstream. Remove this local
patch script when upgrading the pinned Beets Docker image beyond 2.4.0.
"""

import importlib
import inspect
import sys
import beets
import beets.plugins


EXPECTED_BEETS_VERSION = "2.4.0"
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


def apply_patch():
    # 1. Version Guard
    actual_version = getattr(beets, "__version__", "")
    if actual_version != EXPECTED_BEETS_VERSION:
        raise RuntimeError(
            f"Patch target version mismatch: expected Beets {EXPECTED_BEETS_VERSION}, got '{actual_version}'"
        )

    # 2. Read file content
    with open(TARGET_FILE, "r", encoding="utf-8") as f:
        content = f.read()

    orig_count = content.count(ORIGINAL_BLOCK)
    patched_count = content.count(PATCHED_BLOCK)

    # 3. Strict structural verification
    if orig_count == 1 and patched_count == 0:
        # Unpatched: apply the replacement
        new_content = content.replace(ORIGINAL_BLOCK, PATCHED_BLOCK, 1)
        with open(TARGET_FILE, "w", encoding="utf-8") as f:
            f.write(new_content)
        print("Successfully applied upstream plugin resolution fix (beetbox/beets#6039) to beets/plugins.py.")
    elif orig_count == 0 and patched_count == 1:
        # Already correctly patched: proceed to runtime sanity verification
        print("Upstream plugin resolution fix (beetbox/beets#6039) is already applied to beets/plugins.py.")
    else:
        raise RuntimeError(
            f"Invalid source state in beets/plugins.py: original_block_count={orig_count}, patched_block_count={patched_count}"
        )

    # 4. Runtime sanity verification (always executed)
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
