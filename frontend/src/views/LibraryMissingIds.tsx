import { Link } from 'react-router';
import { ImportReviewPage } from '../features/importReview/ImportReviewPage';

/** Library albums and tracks without MusicBrainz IDs. These used to sit in the
 * Import Review Queue as "Needs MusicBrainz ID" rows; they are library repair
 * work, not imports, so they live under Library. */
export default function LibraryMissingIds() {
  return (
    <section className="space-y-4">
      <div className="rounded-md border border-graphite-800 bg-graphite-950/45 px-4 py-3">
        <Link className="text-xs text-zinc-400 hover:text-zinc-200" to="/library">
          ← Back to Library
        </Link>
        <p className="mt-2 text-xs font-semibold uppercase text-red-400">Library</p>
        <h1 className="mt-1 text-2xl font-semibold text-zinc-100">Missing MusicBrainz IDs</h1>
        <p className="mt-2 max-w-3xl text-sm text-zinc-400">
          These albums and tracks are already in your Beets library but have no MusicBrainz release or recording ID,
          so duplicate checks, Find Music, and artwork lookups cannot identify them. Pick a match for each one;
          nothing changes until you apply it.
        </p>
      </div>
      <ImportReviewPage active mode="missing_ids" />
    </section>
  );
}
