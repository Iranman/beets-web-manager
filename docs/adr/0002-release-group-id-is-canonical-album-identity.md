# ADR 0002: Release-Group ID Is Canonical Album Identity

Date: 2026-07-20

## Status

Accepted

## Context

The application works with MusicBrainz release groups, releases, and recordings. Existing code stores and displays `mb_releasegroupid`, `mb_albumid`, and `mb_trackid`. Import review, cleanup, duplicate handling, and folder naming can be unsafe if release IDs and release-group IDs are substituted for each other.

## Decision

The canonical album-level identity is the MusicBrainz release-group ID (`mb_releasegroupid`). A release ID (`mb_albumid`) is edition-level secondary data. A recording ID (`mb_trackid`) identifies a track recording.

## Consequences

- Do not write release IDs where release-group IDs are required.
- Do not reject a release-group candidate only because one edition-specific title differs when stronger recording, AcoustID, duration, and position evidence agree.
- Do not write unresolved placeholder IDs into metadata or folder names.
- UI labels and API payloads must say which MusicBrainz entity type an ID represents.