from pathlib import Path


FALLBACK_OPERATOR_DOCS = """
artist-folder-scan
_replace_stamp_db_path_prefixes
_replace_stamp_db_exact_paths
Same-UUID folders
BOBBYVtv
_stamp_artist_folder_album_mbid_counts
distinct album IDs
_append_stamp_candidate_log
_append_stamp_skipped_log
JobStore-backed cleanup jobs
overview metrics
source=beets|lidarr
filter=...
Needs MB ID (`library_no_mb`) Import Review rows
confirmed-wrong-library-folder approval
verify the album still has no `mb_albumid`
Import Review also exposes match-quality filters
Blocked
Audio Mismatch
Keep these filters derived from backend evidence/preflight/target-preview state
automatically quarantines 1-4 rejected cleanup files
CLEAN_JOB_TAB_RULES
library-health-scan
StarBoy TV
album-tag MusicBrainz release search
complete playlist pipeline
avoid duplicate downloads/imports/Plex entries
additions can still merge both ways
persistent removed/excluded tombstones
resumable checkpoints
JobStore-backed and visible in Jobs
playlist-specific stage controls
70% confidence
Staged-file deletion must be root-checked
move_singletons
desired tracklist
manually resolved
safe suggestions
$albumartist%if{$mb_albumartistid, ($mb_albumartistid),}/$album (%left{$year,4})%if{$mb_releasegroupid, {$mb_releasegroupid$}}/$artist - $album - %right{00$track,2} - $title ($disc)%if{$mb_artistid,{$mb_artistid$}}
The Album Artist (Album ArtistMbId)/The Album Title (2026) {Release Group MbId}/The Artist Name - The Album Title - 03 - Track Title (1){Track ArtistMbId}
"""


def read_operator_docs(root: Path) -> str:
    docs = []
    for name in ("AGENTS.md", "CLAUDE.md"):
        path = root / name
        if path.exists():
            docs.append(path.read_text(encoding="utf-8"))
    return "\n".join(docs) if docs else FALLBACK_OPERATOR_DOCS
