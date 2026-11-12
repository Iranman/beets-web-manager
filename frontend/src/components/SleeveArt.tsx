import { useEffect, useState } from 'react';

/** Art URLs to try in order, from the existing endpoints only. Empty when the data names no album. */
export function albumArtSources({
  albumId,
  releaseGroupId,
  diskArtPath,
}: {
  albumId?: number | string | null;
  releaseGroupId?: string | null;
  diskArtPath?: string | null;
}) {
  const out: string[] = [];
  if (albumId !== undefined && albumId !== null && String(albumId).trim() && Number(albumId) > 0) out.push(`/api/albums/${encodeURIComponent(String(albumId))}/art`);
  if (releaseGroupId && /^[0-9a-f-]{36}$/i.test(releaseGroupId)) out.push(`/api/release-art-cache/${releaseGroupId}`);
  if (diskArtPath) out.push(`/api/disk-art?path=${encodeURIComponent(diskArtPath)}`);
  return out;
}

/**
 * A record sleeve: the album's cover when one loads, otherwise an authored blank sleeve with a
 * record peeking out. Fixed square size so nothing shifts while art loads. Decorative by default
 * (alt=""): the album name is always printed beside it.
 */
export function SleeveArt({
  sources,
  size,
  alt = '',
  className = '',
}: {
  sources: string[];
  size: number;
  alt?: string;
  className?: string;
}) {
  const key = sources.join('|');
  const [index, setIndex] = useState(0);
  useEffect(() => setIndex(0), [key]);
  const src = sources[index];
  return (
    <span className={`ops-sleeve ${className}`} style={{ width: size, height: size }} aria-hidden={alt ? undefined : true}>
      {src ? (
        <img
          alt={alt}
          className="ops-sleeve-img"
          decoding="async"
          height={size}
          loading="lazy"
          src={src}
          width={size}
          onError={() => setIndex((value) => value + 1)}
        />
      ) : (
        <BlankSleeve />
      )}
    </span>
  );
}

/** Sleeve paper with a record half out of it: the stand-in when no cover exists. */
export function BlankSleeve() {
  return (
    <svg aria-hidden="true" className="ops-sleeve-blank" viewBox="0 0 64 64" preserveAspectRatio="xMidYMid slice">
      <circle cx="44" cy="32" r="24" className="ops-vinyl" />
      <circle cx="44" cy="32" r="19" className="ops-groove" />
      <circle cx="44" cy="32" r="14" className="ops-groove" />
      <circle cx="44" cy="32" r="7" className="ops-label" />
      <rect x="0" y="0" width="40" height="64" className="ops-paper" />
      <path d="M6 50h22M6 55h14" className="ops-print" />
    </svg>
  );
}
