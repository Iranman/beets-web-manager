# ADR 0003: AI Is Optional And Not The Source Of Truth

Date: 2026-07-20

## Status

Accepted

## Context

The application uses deterministic identity evidence from MusicBrainz and AcoustID and also has AI suggestion flows. Repository inspection shows AI availability checks and AI suggestion routes, but AI credentials may be missing, invalid, unauthorized, rate-limited, or unavailable.

## Decision

AI is optional and untrusted. AI may rank, explain, or adjudicate candidates found through deterministic sources. AI must not invent an identity and treat it as verified.

## Consequences

- MusicBrainz and AcoustID matching must continue when AI is unavailable.
- AI failure must be explicitly represented in results.
- AI confidence cannot override MusicBrainz/AcoustID conflicts by itself.
- Backend contracts must distinguish deterministic evidence from AI contribution.