import Alert from '@mui/material/Alert';
import Button from '@mui/material/Button';
import TextField from '@mui/material/TextField';
import { useState } from 'react';

const ARTIST_UUID_RE = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i;

export interface ArtistResolutionCardProps {
  artistName: string;
  onSaveManualId: (mbid: string) => void;
  onDismiss: () => void;
  saving?: boolean;
}

/** Blocking first step: resolve (or explicitly skip) the MusicBrainz artist
 * entity before showing tracks/metadata/duplicates/AcoustID. A confident
 * automatic match never reaches this component -- it's attached silently
 * (see Submissions.tsx's artist_match effect) and the page moves straight
 * past the "artist" stage. */
export default function ArtistResolutionCard({ artistName, onSaveManualId, onDismiss, saving }: ArtistResolutionCardProps) {
  const [manualInput, setManualInput] = useState('');
  const extracted = manualInput.match(ARTIST_UUID_RE)?.[0]?.toLowerCase() ?? '';
  const createUrl = `https://musicbrainz.org/artist/create?edit-artist.name=${encodeURIComponent(artistName)}`;

  return (
    <div className="rounded-md border border-graphite-800 bg-graphite-900/70 p-5">
      <h2 className="text-base font-semibold text-zinc-100">MusicBrainz artist not found</h2>
      <p className="mt-1 text-sm text-zinc-400">
        No confident MusicBrainz match was found for <span className="text-zinc-200">{artistName || 'this artist'}</span>.
        Resolving the artist first keeps the rest of this submission pointed at the right entity.
      </p>
      <div className="mt-4 flex flex-wrap gap-2">
        <Button variant="contained" size="small" onClick={() => window.open(createUrl, '_blank', 'noopener,noreferrer')}>
          Create artist on MusicBrainz
        </Button>
        <Button variant="outlined" size="small" onClick={onDismiss}>Skip for now</Button>
      </div>
      <div className="mt-4 flex flex-wrap items-center gap-2">
        <TextField
          size="small"
          label="Or paste an artist MBID / MusicBrainz URL"
          value={manualInput}
          onChange={(event) => setManualInput(event.target.value)}
          sx={{ minWidth: 320 }}
        />
        <Button size="small" variant="outlined" disabled={!extracted || saving} onClick={() => onSaveManualId(extracted)}>
          Use this artist
        </Button>
      </div>
      {manualInput && !extracted ? (
        <Alert severity="warning" sx={{ mt: 2 }}>That doesn't look like a MusicBrainz artist ID or URL.</Alert>
      ) : null}
    </div>
  );
}
