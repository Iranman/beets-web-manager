import Alert from '@mui/material/Alert';
import Button from '@mui/material/Button';
import Chip from '@mui/material/Chip';
import LinearProgress from '@mui/material/LinearProgress';
import Switch from '@mui/material/Switch';
import FormControlLabel from '@mui/material/FormControlLabel';
import TextField from '@mui/material/TextField';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  applyFolderPlaceholderAction,
  applySafeFolderPlaceholderRenames,
  getJobResult,
  previewFolderPlaceholderMerge,
  reviewFolderPlaceholder,
  scanFolderPlaceholders,
} from '../../api/client';
import type {
  FolderPlaceholderApplyResult,
  FolderPlaceholderMergePreview,
  FolderPlaceholderReview,
  FolderPlaceholderRow,
  FolderPlaceholderScanResult,
} from '../../api/types';
import {
  CleanActionBar,
  CleanEmptyState,
  CleanMetricGrid,
  CleanPanelHeader,
  CleanSection,
} from '../../components/CleanPanel';
import { JobStatusCard } from '../../components/JobStatusCard';
import { useJobPoll } from '../../lib/hooks';

// ── Action bucket classification ──────────────────────────────────────────────

type FolderActionBucket =
  | 'safe_rename'
  | 'safe_empty_source'
  | 'missing_rgid'
  | 'target_exists_no_db'
  | 'target_exists_db'
  | 'db_tracked'
  | 'needs_review';

function classifyRow(row: FolderPlaceholderRow): FolderActionBucket {
  if (row.missing_rgid) return 'missing_rgid';
  if (row.is_empty && row.target_exists && row.db_item_count === 0) return 'safe_empty_source';
  if (row.safe && !row.target_exists && row.db_item_count === 0) return 'safe_rename';
  if (row.target_exists && row.db_item_count > 0) return 'target_exists_db';
  if (row.target_exists && !row.is_empty) return 'target_exists_no_db';
  if (row.db_item_count > 0 && !row.target_exists) return 'db_tracked';
  return 'needs_review';
}

function bucketLabel(bucket: FolderActionBucket): string {
  switch (bucket) {
    case 'safe_rename':         return 'Safe rename';
    case 'safe_empty_source':   return 'Safe remove empty';
    case 'missing_rgid':        return 'Missing Release Group ID';
    case 'target_exists_no_db': return 'Target exists · review';
    case 'target_exists_db':    return 'Target exists · DB repair';
    case 'db_tracked':          return 'DB repair needed';
    case 'needs_review':        return 'Needs review';
  }
}

function bucketColor(bucket: FolderActionBucket): 'success' | 'warning' | 'error' | 'info' | 'default' {
  if (bucket === 'safe_rename' || bucket === 'safe_empty_source') return 'success';
  if (bucket === 'target_exists_db') return 'error';
  if (bucket === 'missing_rgid') return 'info';
  if (bucket === 'target_exists_no_db' || bucket === 'db_tracked') return 'warning';
  return 'default';
}

function bucketSummary(row: FolderPlaceholderRow, bucket: FolderActionBucket): string {
  switch (bucket) {
    case 'safe_rename':
      return 'No target conflict, no DB items. Folder can be renamed.';
    case 'safe_empty_source':
      return 'Source is empty and target already exists. Empty placeholder folder can be removed after confirmation.';
    case 'missing_rgid':
      return 'No MusicBrainz Release Group ID found in the DB for this album. Cannot propose canonical folder name until the ID is known.';
    case 'target_exists_no_db':
      return `Source has ${row.audio_count} audio file(s); target folder already exists; no DB items tracked here. Compare files before merging or removing.`;
    case 'target_exists_db':
      return `Target exists AND ${row.db_item_count} DB item(s) point to this source path. Fix DB paths first (Leaked DB Paths), then use beets move.`;
    case 'db_tracked':
      return `${row.db_item_count} DB item(s) point to this folder path. A simple rename would break DB paths — use beets move after DB path repair.`;
    case 'needs_review':
      return row.skip_reason || 'Review folder state before taking any action.';
  }
}

// ── Filter / sort ─────────────────────────────────────────────────────────────

type FolderFilter =
  | 'all' | 'safe' | 'safe_rename' | 'safe_empty'
  | 'missing_rgid' | 'target_exists' | 'db_tracked' | 'needs_review';

type FolderSort = 'safety' | 'artist' | 'album' | 'placeholder' | 'target_exists' | 'file_count';

const FILTERS: Array<{ value: FolderFilter; label: string }> = [
  { value: 'all',          label: 'All' },
  { value: 'safe',         label: 'Safe (all)' },
  { value: 'safe_rename',  label: 'Safe rename' },
  { value: 'safe_empty',   label: 'Safe remove empty' },
  { value: 'missing_rgid', label: 'Missing RGID' },
  { value: 'target_exists', label: 'Target exists' },
  { value: 'db_tracked',   label: 'DB tracked' },
  { value: 'needs_review', label: 'Needs review' },
];

const SORTS: Array<{ value: FolderSort; label: string }> = [
  { value: 'safety',        label: 'Action priority' },
  { value: 'artist',        label: 'Artist' },
  { value: 'album',         label: 'Album' },
  { value: 'placeholder',   label: 'Placeholder type' },
  { value: 'target_exists', label: 'Target exists' },
  { value: 'file_count',    label: 'File count' },
];

const BUCKET_ORDER: FolderActionBucket[] = [
  'needs_review', 'target_exists_db', 'db_tracked',
  'target_exists_no_db', 'missing_rgid', 'safe_rename', 'safe_empty_source',
];

const AUTO_APPLY_KEY = 'beets_folder_placeholder_auto_apply_safe';

// ── Helpers ───────────────────────────────────────────────────────────────────

function formatScanTime(timestamp: number | null) {
  if (!timestamp) return 'Not scanned this session';
  return new Date(timestamp).toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
}

function folderIssue(row: FolderPlaceholderRow) {
  if (row.placeholder_type === 'literal_placeholder') return 'Unresolved {Album MbId} placeholder in folder name';
  return row.placeholder_desc || 'Unresolved Beets path-template fragment in folder name';
}

function fmt(n: number) { return n < 0 ? '?' : String(n); }

// ── Inline review panel ───────────────────────────────────────────────────────

function ReviewDetailPanel({
  row,
  onDone,
}: {
  row: FolderPlaceholderRow;
  onDone: () => void;
}) {
  const [review, setReview] = useState<FolderPlaceholderReview | null>(null);
  const [reviewLoading, setReviewLoading] = useState(false);
  const [reviewError, setReviewError] = useState('');

  const [preview, setPreview] = useState<FolderPlaceholderMergePreview | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState('');

  const [applying, setApplying] = useState(false);
  const [applyResult, setApplyResult] = useState<FolderPlaceholderApplyResult | null>(null);
  const [applyError, setApplyError] = useState('');
  const [confirmRemove, setConfirmRemove] = useState(false);
  const [confirmMerge, setConfirmMerge] = useState(false);

  useEffect(() => {
    setReviewLoading(true);
    reviewFolderPlaceholder(row.folder, row.proposed_folder)
      .then((r) => { setReview(r); setReviewLoading(false); })
      .catch((e) => { setReviewError(String(e)); setReviewLoading(false); });
  }, [row.folder, row.proposed_folder]);

  const doPreviewMerge = useCallback(async () => {
    if (!review?.proposed_path) return;
    setPreviewLoading(true);
    setPreviewError('');
    setPreview(null);
    try {
      const p = await previewFolderPlaceholderMerge(row.folder, review.proposed_path);
      setPreview(p);
    } catch (e) {
      setPreviewError(String(e));
    } finally {
      setPreviewLoading(false);
    }
  }, [review?.proposed_path, row.folder]);

  const doRemoveEmpty = useCallback(async () => {
    setConfirmRemove(false);
    setApplying(true);
    setApplyError('');
    try {
      const r = await applyFolderPlaceholderAction('remove_empty_source', row.folder, {
        previewToken: review?.preview_token,
      });
      setApplyResult(r);
    } catch (e) {
      setApplyError(String(e));
    } finally {
      setApplying(false);
    }
  }, [review?.preview_token, row.folder]);

  const doApplyMerge = useCallback(async () => {
    if (!preview?.safe || !preview.preview_token) return;
    setConfirmMerge(false);
    setApplying(true);
    setApplyError('');
    try {
      const r = await applyFolderPlaceholderAction('merge_source_files', row.folder, {
        targetPath: preview.target_path,
        previewToken: preview.preview_token,
      });
      setApplyResult(r);
    } catch (e) {
      setApplyError(String(e));
    } finally {
      setApplying(false);
    }
  }, [preview, row.folder]);

  const action = review?.action ?? classifyRow(row);

  return (
    <div className="mt-3 space-y-3 border-t border-graphite-700/40 pt-3 text-[0.72rem]">
      {reviewLoading && <div className="text-zinc-400">Loading review details…</div>}
      {reviewError && <div className="text-rose-400">Error: {reviewError}</div>}

      {review && !reviewLoading && (
        <>
          {/* Path comparison */}
          <div className="space-y-1.5">
            <div>
              <div className="font-medium uppercase tracking-wide text-zinc-500 text-[0.65rem]">Source path</div>
              <div className="mt-0.5 font-mono text-rose-400 break-all">{review.source_path}</div>
            </div>
            {review.proposed_path ? (
              <div>
                <div className="font-medium uppercase tracking-wide text-zinc-500 text-[0.65rem]">
                  Proposed canonical path
                  {review.target_folder_rgid
                    ? <span className="ml-1 normal-case text-emerald-600">(has Release Group ID)</span>
                    : review.source_known_rgid
                      ? <span className="ml-1 normal-case text-emerald-600">(RGID from DB: {review.source_known_rgid.slice(0, 8)}…)</span>
                      : null}
                </div>
                <div className="mt-0.5 font-mono text-emerald-600 break-all">{review.proposed_path}</div>
              </div>
            ) : (
              <div className="text-amber-300">No proposed path — Release Group ID unknown.</div>
            )}
          </div>

          {/* RGID status */}
          {review.source_has_placeholder && !review.source_known_rgid && (
            <div className="rounded border border-sky-900/50 bg-sky-950/30 px-2.5 py-2 text-sky-200 space-y-1">
              <div className="font-semibold">Missing Release Group ID</div>
              <div>No MusicBrainz Release Group ID found in the DB for this album. The canonical folder name cannot include the Release Group ID. Use Import Review to attach a Release Group ID before renaming.</div>
            </div>
          )}
          {review.source_known_rgid && (
            <div className="rounded border border-emerald-900/50 bg-emerald-950/20 px-2.5 py-1.5 text-emerald-300 text-[0.68rem]">
              <span className="font-semibold">Release Group ID from DB: </span>
              <span className="font-mono">{review.source_known_rgid}</span>
              <span className="ml-1 text-emerald-500">— included in proposed canonical name.</span>
            </div>
          )}

          {/* Target-exists status */}
          {review.target_exists && (
            <div className="rounded border border-amber-900/50 bg-amber-950/30 px-2.5 py-2 text-amber-200 space-y-1">
              <div className="font-semibold">Target folder already exists.</div>
              {review.source_is_empty
                ? <div>Source is empty. Safe to remove after confirmation.</div>
                : review.source_db_items > 0
                  ? <div>{review.source_db_items} DB item(s) point here. DB repair required before any move.</div>
                  : review.conflicting_files.length > 0
                    ? <div>{review.conflicting_files.length} filename conflict(s). Manual file comparison required.</div>
                    : <div>Source has {review.source_audio_count} audio file(s); no DB items tracked here. Preview merge available.</div>
              }
            </div>
          )}

          {/* DB items alert */}
          {review.source_db_items > 0 && (
            <div className="rounded border border-rose-900/50 bg-rose-950/20 px-2.5 py-2 text-rose-300 space-y-1">
              <div className="font-semibold">{review.source_db_items} DB item(s) tracked under this folder.</div>
              <div>A simple folder rename would break these DB paths. Go to Leaked DB Paths to fix them first.</div>
              {review.source_db_album_ids.length > 0 && (
                <div>Album IDs: <span className="font-mono">{review.source_db_album_ids.join(', ')}</span></div>
              )}
            </div>
          )}

          {/* Reasons blocked */}
          {review.reasons_blocked.length > 0 && (
            <div className="space-y-0.5">
              {review.reasons_blocked.map((r, i) => (
                <div key={i} className="text-rose-400">⚠ {r}</div>
              ))}
            </div>
          )}

          {/* File comparison */}
          {(review.source_only_files.length > 0 || review.target_only_files.length > 0 ||
            review.matching_files.length > 0 || review.conflicting_files.length > 0) && (
            <div className="space-y-2">
              <div className="font-medium uppercase tracking-wide text-zinc-500 text-[0.65rem]">File comparison</div>
              <div className="grid grid-cols-2 gap-2 text-[0.68rem]">
                <div>
                  <div className="font-medium text-zinc-400">
                    Source ({review.source_file_count} files, {review.source_audio_count} audio)
                  </div>
                  {review.source_only_files.length > 0 ? (
                    <ul className="mt-1 space-y-0.5 text-zinc-400">
                      {review.source_only_files.slice(0, 12).map((f) => (
                        <li key={f} className="font-mono truncate text-emerald-700" title={f}>+ {f}</li>
                      ))}
                      {review.source_only_files.length > 12 && (
                        <li className="text-zinc-500">…and {review.source_only_files.length - 12} more</li>
                      )}
                    </ul>
                  ) : <div className="text-zinc-600 mt-1">No source-only files.</div>}
                </div>
                <div>
                  <div className="font-medium text-zinc-400">
                    Target ({review.target_file_count} files, {review.target_audio_count} audio)
                  </div>
                  {review.target_only_files.length > 0 ? (
                    <ul className="mt-1 space-y-0.5">
                      {review.target_only_files.slice(0, 12).map((f) => (
                        <li key={f} className="font-mono truncate text-zinc-400" title={f}>{f}</li>
                      ))}
                      {review.target_only_files.length > 12 && (
                        <li className="text-zinc-500">…and {review.target_only_files.length - 12} more</li>
                      )}
                    </ul>
                  ) : <div className="text-zinc-600 mt-1">No target-only files.</div>}
                </div>
              </div>
              {review.conflicting_files.length > 0 && (
                <div>
                  <div className="font-medium text-rose-400 text-[0.68rem]">
                    {review.conflicting_files.length} filename conflict(s) — same name, different size:
                  </div>
                  <ul className="mt-1 space-y-0.5">
                    {review.conflicting_files.slice(0, 8).map((f) => (
                      <li key={f} className="font-mono text-[0.68rem] text-rose-400 truncate">{f}</li>
                    ))}
                  </ul>
                </div>
              )}
              {review.matching_files.length > 0 && review.conflicting_files.length === 0 && (
                <div className="text-zinc-500 text-[0.68rem]">
                  {review.matching_files.length} file(s) exist in both folders with matching size.
                </div>
              )}
            </div>
          )}

          {/* Merge preview result */}
          {previewLoading && <div className="text-zinc-400">Loading preview…</div>}
          {previewError && <div className="text-rose-400">Preview error: {previewError}</div>}
          {preview && (
            <div className="rounded border border-graphite-700/50 bg-graphite-950/40 px-3 py-2.5 space-y-2">
              <div className="font-medium text-zinc-300 text-[0.72rem]">Merge preview (dry-run)</div>
              {preview.safe ? (
                <div className="text-emerald-400 text-[0.68rem] font-medium">✓ Safe to merge — no conflicts, no DB items in source.</div>
              ) : (
                <div className="space-y-1">
                  {preview.blocking_reasons.map((r, i) => (
                    <div key={i} className="text-rose-400 text-[0.68rem]">⚠ {r}</div>
                  ))}
                </div>
              )}
              {preview.moves.length > 0 && (
                <div>
                  <div className="text-[0.68rem] font-medium text-zinc-400">Files to move ({preview.moves.length}):</div>
                  <ul className="mt-1 space-y-0.5">
                    {preview.moves.slice(0, 10).map((m, i) => (
                      <li key={i} className="font-mono text-[0.68rem] text-zinc-400 truncate" title={`${m.from} → ${m.to}`}>
                        {m.from.split('/').at(-1)} → target
                      </li>
                    ))}
                    {preview.moves.length > 10 && <li className="text-zinc-500">…and {preview.moves.length - 10} more</li>}
                  </ul>
                </div>
              )}
              {preview.conflicts.length > 0 && (
                <div>
                  <div className="text-[0.68rem] font-medium text-rose-400">Conflicts ({preview.conflicts.length}):</div>
                  <ul className="mt-1 space-y-0.5">
                    {preview.conflicts.map((c) => (
                      <li key={c.filename} className="font-mono text-[0.68rem] text-rose-400">{c.filename} ({c.source_size}B ≠ {c.target_size}B)</li>
                    ))}
                  </ul>
                </div>
              )}
              {preview.source_will_be_empty_after_move && (
                <div className="text-zinc-400 text-[0.68rem]">Source folder would be empty after move and can be removed.</div>
              )}
            </div>
          )}

          {/* Apply result */}
          {applyResult && (
            <div className="rounded border border-emerald-700/50 bg-emerald-950/30 px-2.5 py-2 text-emerald-300 text-[0.72rem]">
              <div className="font-semibold">✓ Done</div>
              {applyResult.removed && <div>Removed: <span className="font-mono">{applyResult.removed}</span></div>}
              {applyResult.moved && applyResult.moved.length > 0 && (
                <div>Moved {applyResult.moved.length} file(s) into the canonical target folder.</div>
              )}
              {applyResult.removed_folders && applyResult.removed_folders.length > 0 && (
                <div>Removed {applyResult.removed_folders.length} empty folder(s).</div>
              )}
            </div>
          )}
          {applyError && (
            <div className="rounded border border-rose-700/50 bg-rose-950/20 px-2.5 py-2 text-rose-300 text-[0.72rem]">
              Error: {applyError}
            </div>
          )}

          {/* Confirm remove dialog */}
          {confirmRemove && (
            <div className="rounded border border-amber-700/50 bg-amber-950/30 px-3 py-2.5 space-y-2">
              <div className="font-semibold text-amber-200 text-[0.72rem]">Confirm: Remove empty placeholder folder?</div>
              <div className="text-amber-300 text-[0.68rem]">
                This will remove the empty directory <span className="font-mono">{row.folder}</span> from disk.
                The target canonical folder will not be changed. No files will be deleted.
              </div>
              <div className="flex gap-2">
                <Button size="small" color="error" variant="contained" disabled={applying} onClick={() => void doRemoveEmpty()}>
                  {applying ? 'Removing…' : 'Remove empty folder'}
                </Button>
                <Button size="small" variant="outlined" onClick={() => setConfirmRemove(false)}>Cancel</Button>
              </div>
            </div>
          )}

          {confirmMerge && preview && (
            <div className="rounded border border-amber-700/50 bg-amber-950/30 px-3 py-2.5 space-y-2">
              <div className="font-semibold text-amber-200 text-[0.72rem]">Confirm: Apply merge preview?</div>
              <div className="text-amber-300 text-[0.68rem]">
                This will move {preview.moves.length} source-only file(s) into the existing canonical target folder.
                Existing target files will not be overwritten, and DB-tracked source folders are blocked.
              </div>
              <div className="flex gap-2">
                <Button size="small" color="warning" variant="contained" disabled={applying || !preview.preview_token} onClick={() => void doApplyMerge()}>
                  {applying ? 'Applying…' : 'Apply merge'}
                </Button>
                <Button size="small" variant="outlined" onClick={() => setConfirmMerge(false)}>Cancel</Button>
              </div>
            </div>
          )}

          {/* Action buttons */}
          {!applyResult && (
            <div className="flex flex-wrap gap-2 pt-1">
              {action === 'safe_empty_source_remove' || action === 'safe_empty_source' || review.source_is_empty ? (
                !confirmRemove && (
                  <Button
                    size="small"
                    variant="outlined"
                    color="error"
                    disabled={applying || !review.source_is_empty || review.source_db_items > 0}
                    title={review.source_db_items > 0 ? 'DB items still tracked — repair first' : undefined}
                    onClick={() => setConfirmRemove(true)}
                  >
                    Remove empty source
                  </Button>
                )
              ) : null}

              {review.target_exists && !review.source_is_empty && review.source_db_items === 0 && !preview ? (
                <Button
                  size="small"
                  variant="outlined"
                  disabled={previewLoading}
                  onClick={() => void doPreviewMerge()}
                >
                  Preview merge
                </Button>
              ) : null}

              {preview?.safe && !confirmMerge ? (
                <Button
                  size="small"
                  variant="contained"
                  color="warning"
                  disabled={applying || !preview.preview_token}
                  title={!preview.preview_token ? 'Preview token missing; rerun preview before applying.' : undefined}
                  onClick={() => setConfirmMerge(true)}
                >
                  Apply previewed merge
                </Button>
              ) : null}

              <Button size="small" variant="text" color="inherit" onClick={onDone}>
                Close details
              </Button>
            </div>
          )}
        </>
      )}
    </div>
  );
}

// ── FolderRow ─────────────────────────────────────────────────────────────────

function FolderRow({
  row,
  reviewed,
  onMarkReviewed,
  checked,
  onToggle,
  onRenamed,
}: {
  row: FolderPlaceholderRow;
  reviewed: boolean;
  onMarkReviewed: () => void;
  checked?: boolean;
  onToggle?: () => void;
  onRenamed?: () => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [confirmRename, setConfirmRename] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [renameResult, setRenameResult] = useState<FolderPlaceholderApplyResult | null>(null);
  const [renameError, setRenameError] = useState('');

  const bucket = classifyRow(row);
  const isSafe = bucket === 'safe_rename' || bucket === 'safe_empty_source';

  const borderCls = isSafe
    ? 'border-emerald-900/50 bg-emerald-950/10'
    : bucket === 'target_exists_db'
      ? 'border-rose-900/50 bg-rose-950/10'
      : bucket === 'missing_rgid'
        ? 'border-sky-900/50 bg-sky-950/10'
        : bucket === 'needs_review'
          ? 'border-graphite-700/60 bg-graphite-950/40'
          : 'border-amber-900/50 bg-amber-950/15';

  const doRename = useCallback(async () => {
    if (!row.proposed_folder) return;
    setConfirmRename(false);
    setRenaming(true);
    setRenameError('');
    try {
      const r = await applyFolderPlaceholderAction('safe_rename', row.folder, {
        targetPath: row.proposed_folder,
      });
      setRenameResult(r);
      onRenamed?.();
    } catch (e) {
      setRenameError(String(e));
    } finally {
      setRenaming(false);
    }
  }, [row.folder, row.proposed_folder, onRenamed]);

  const dimmed = reviewed || !!renameResult;

  return (
    <div className={`rounded border px-3 py-2.5 text-xs ${borderCls} ${dimmed ? 'opacity-50' : ''}`}>
      <div className="flex items-start gap-2">
        {/* Checkbox (safe_rename only) */}
        {onToggle && bucket === 'safe_rename' && (
          <input
            type="checkbox"
            checked={checked}
            onChange={onToggle}
            className="mt-0.5 shrink-0 cursor-pointer accent-emerald-500"
            aria-label="Select for bulk rename"
          />
        )}

        <div className="min-w-0 flex-1 space-y-1.5">
          {/* Name + badges */}
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="font-mono font-semibold text-zinc-100 break-all">{row.name}</span>
            <Chip size="small" color={bucketColor(bucket)} label={bucketLabel(bucket)} variant="outlined" />
            <Chip
              size="small"
              color="default"
              label={row.placeholder_type === 'literal_placeholder' ? '{Album MbId}' : 'template token'}
            />
            {row.target_exists && <Chip size="small" color="warning" label="target exists" />}
            {row.db_item_count > 0 && <Chip size="small" color="error" label={`${row.db_item_count} DB items`} />}
            {row.is_empty && <Chip size="small" color="default" label="empty source" />}
            {row.missing_rgid && <Chip size="small" color="info" label="no RGID" />}
            {reviewed && <Chip size="small" color="default" label="reviewed" variant="outlined" />}
            {renameResult && <Chip size="small" color="success" label="renamed" />}
          </div>

          {/* Issue + proposed */}
          <div className="text-zinc-500">
            <span className="font-medium text-zinc-400">Issue: </span>{folderIssue(row)}
          </div>
          {row.proposed_folder ? (
            <div>
              <span className="font-medium text-zinc-500">Proposed: </span>
              <span className="font-mono text-emerald-700 break-all">
                {row.proposed_folder.split('/').at(-1)}
              </span>
              {!row.missing_rgid && row.placeholder_type === 'literal_placeholder' && (
                <span className="ml-1 text-emerald-600">(Release Group ID included)</span>
              )}
            </div>
          ) : (
            <div className="text-amber-400 text-[0.72rem]">
              No canonical path — Release Group ID unknown.
            </div>
          )}

          {/* Compact stats */}
          <div className="flex flex-wrap gap-3 text-zinc-500">
            <span>Files: <strong className={row.file_count > 0 ? 'text-zinc-300' : ''}>{fmt(row.file_count)}</strong></span>
            <span>Audio: <strong className={row.audio_count > 0 ? 'text-zinc-300' : ''}>{fmt(row.audio_count)}</strong></span>
            {row.db_item_count > 0 && <span>DB items: <strong className="text-amber-600">{row.db_item_count}</strong></span>}
          </div>

          {/* Status summary */}
          <div className="text-[0.72rem] text-zinc-400">
            <span className="font-medium">Status: </span>{renameResult ? `Renamed → ${renameResult.renamed_to?.split('/').at(-1)}` : bucketSummary(row, bucket)}
          </div>

          {/* Rename confirmation inline */}
          {confirmRename && !renaming && (
            <div className="rounded border border-emerald-800/50 bg-emerald-950/30 px-2.5 py-2 space-y-1.5">
              <div className="font-semibold text-emerald-200">Confirm rename?</div>
              <div className="text-[0.68rem] text-zinc-400 space-y-0.5">
                <div className="font-mono text-rose-400 break-all">From: {row.folder}</div>
                <div className="font-mono text-emerald-600 break-all">To: {row.proposed_folder}</div>
              </div>
              <div className="flex gap-2 pt-0.5">
                <Button size="small" variant="contained" color="success" onClick={() => void doRename()}>
                  Rename
                </Button>
                <Button size="small" variant="outlined" onClick={() => setConfirmRename(false)}>Cancel</Button>
              </div>
            </div>
          )}

          {renameError && (
            <div className="text-rose-400 text-[0.68rem]">Rename error: {renameError}</div>
          )}
        </div>

        {/* Action buttons column */}
        <div className="flex shrink-0 flex-col gap-1 items-end pt-0.5">
          {bucket === 'safe_rename' && !renameResult && (
            <button
              className="text-xs text-emerald-400 hover:text-emerald-200 whitespace-nowrap font-medium"
              disabled={renaming || confirmRename}
              onClick={() => setConfirmRename(true)}
            >
              {renaming ? 'Renaming…' : 'Rename'}
            </button>
          )}
          <button
            className="text-xs text-zinc-400 hover:text-zinc-200 whitespace-nowrap"
            onClick={() => setExpanded((v) => !v)}
          >
            {expanded ? 'Hide details' : 'Review details'}
          </button>
          <button
            className="text-[0.68rem] text-zinc-500 hover:text-zinc-300 whitespace-nowrap"
            onClick={onMarkReviewed}
          >
            {reviewed ? 'Unmark reviewed' : 'Mark reviewed'}
          </button>
          {bucket === 'safe_rename' && !renameResult && (
            <button
              className="text-[0.68rem] text-zinc-600 hover:text-zinc-400 whitespace-nowrap"
              onClick={onMarkReviewed}
            >
              Skip
            </button>
          )}
        </div>
      </div>

      {/* Inline review/preview/apply panel */}
      {expanded && (
        <ReviewDetailPanel row={row} onDone={() => setExpanded(false)} />
      )}
    </div>
  );
}

// ── Main panel ────────────────────────────────────────────────────────────────

type ScanData = FolderPlaceholderScanResult;

export function FolderPlaceholdersPanel() {
  const [scanJobId, setScanJobId] = useState<string | null>(null);
  const [applyJobId, setApplyJobId] = useState<string | null>(null);
  const [scanData, setScanData] = useState<ScanData | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [showAll, setShowAll] = useState(false);
  const [filter, setFilter] = useState<FolderFilter>('all');
  const [sort, setSort] = useState<FolderSort>('safety');
  const [query, setQuery] = useState('');
  const [lastScanAt, setLastScanAt] = useState<number | null>(null);
  const [reviewed, setReviewed] = useState<Set<string>>(new Set());
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [renamedSet, setRenamedSet] = useState<Set<string>>(new Set());
  const [autoApplySafe, setAutoApplySafe] = useState<boolean>(
    () => localStorage.getItem(AUTO_APPLY_KEY) === 'true'
  );
  const prevScanStatus = useRef<string | null>(null);
  const prevApplyStatus = useRef<string | null>(null);

  const { job: scanJob } = useJobPoll(scanJobId);
  const { job: applyJob } = useJobPoll(applyJobId);

  // Handle auto-apply toggle persistence
  const toggleAutoApply = useCallback((val: boolean) => {
    setAutoApplySafe(val);
    localStorage.setItem(AUTO_APPLY_KEY, val ? 'true' : 'false');
  }, []);

  const doScan = useCallback(async () => {
    setError('');
    setBusy(true);
    setScanData(null);
    setRenamedSet(new Set());
    setSelected(new Set());
    try {
      const res = await scanFolderPlaceholders();
      setScanJobId(res.job_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setBusy(false);
    }
  }, []);

  const doApplySafeRenames = useCallback(async (sourcePaths?: string[]) => {
    setError('');
    setBusy(true);
    setApplyJobId(null);
    try {
      const res = await applySafeFolderPlaceholderRenames(sourcePaths);
      setApplyJobId(res.job_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setBusy(false);
    }
  }, []);

  // Scan job completion
  useEffect(() => {
    if (!scanJob || scanJob.status === prevScanStatus.current) return;
    prevScanStatus.current = scanJob.status;
    if (scanJob.status === 'success') {
      const result = getJobResult<ScanData>(scanJob);
      if (result) {
        setScanData(result);
        if (autoApplySafe) {
          const safeRows = result.rows.filter((r) => classifyRow(r) === 'safe_rename');
          if (safeRows.length > 0) {
            void doApplySafeRenames(safeRows.map((r) => r.folder));
            return;
          }
        }
      }
      setLastScanAt(Date.now());
      setBusy(false);
    } else if (scanJob.status === 'failed' || scanJob.status === 'killed') {
      setBusy(false);
    }
  }, [scanJob?.status, autoApplySafe, doApplySafeRenames]);

  // Apply job completion — rescan after bulk rename
  useEffect(() => {
    if (!applyJob || applyJob.status === prevApplyStatus.current) return;
    prevApplyStatus.current = applyJob.status;
    if (applyJob.status === 'success' || applyJob.status === 'failed' || applyJob.status === 'killed') {
      setBusy(false);
      if (applyJob.status === 'success') {
        void doScan();
      }
    }
  }, [applyJob?.status, doScan]);

  const toggleReviewed = useCallback((folder: string) => {
    setReviewed((prev) => {
      const next = new Set(prev);
      if (next.has(folder)) next.delete(folder);
      else next.add(folder);
      return next;
    });
  }, []);

  const toggleSelected = useCallback((folder: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(folder)) next.delete(folder);
      else next.add(folder);
      return next;
    });
  }, []);

  const bucketCounts = useMemo(() => {
    const rows = scanData?.rows ?? [];
    const counts: Record<FolderActionBucket, number> = {
      safe_rename: 0, safe_empty_source: 0, missing_rgid: 0,
      target_exists_no_db: 0, target_exists_db: 0, db_tracked: 0, needs_review: 0,
    };
    for (const row of rows) counts[classifyRow(row)] += 1;
    return counts;
  }, [scanData?.rows]);

  // Safe rename rows for bulk selection
  const safeRenameRows = useMemo(
    () => (scanData?.rows ?? []).filter((r) => classifyRow(r) === 'safe_rename' && !renamedSet.has(r.folder)),
    [scanData?.rows, renamedSet]
  );

  const selectedSafeRows = useMemo(
    () => safeRenameRows.filter((r) => selected.has(r.folder)),
    [safeRenameRows, selected]
  );

  const selectAllSafe = useCallback(() => {
    setSelected(new Set(safeRenameRows.map((r) => r.folder)));
  }, [safeRenameRows]);

  const clearSelection = useCallback(() => {
    setSelected(new Set());
  }, []);

  const metricItems = scanData ? [
    { label: 'Total', value: scanData.total },
    { label: 'Safe rename', value: bucketCounts.safe_rename, tone: bucketCounts.safe_rename ? 'success' as const : 'neutral' as const },
    { label: 'Safe remove empty', value: bucketCounts.safe_empty_source, tone: bucketCounts.safe_empty_source ? 'success' as const : 'neutral' as const },
    { label: 'Missing RGID', value: bucketCounts.missing_rgid, tone: bucketCounts.missing_rgid ? 'warning' as const : 'success' as const },
    { label: 'Target exists', value: bucketCounts.target_exists_no_db + bucketCounts.target_exists_db, tone: (bucketCounts.target_exists_no_db + bucketCounts.target_exists_db) > 0 ? 'warning' as const : 'success' as const },
    { label: 'DB repair needed', value: bucketCounts.db_tracked + bucketCounts.target_exists_db, tone: (bucketCounts.db_tracked + bucketCounts.target_exists_db) > 0 ? 'warning' as const : 'success' as const },
    { label: 'Marked reviewed', value: reviewed.size, tone: 'neutral' as const },
  ] : [];

  const filteredRows = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const rows = (scanData?.rows ?? []).filter((row) => {
      const bucket = classifyRow(row);
      if (filter === 'safe' && !row.safe && bucket !== 'safe_empty_source') return false;
      if (filter === 'safe_rename' && bucket !== 'safe_rename') return false;
      if (filter === 'safe_empty' && bucket !== 'safe_empty_source') return false;
      if (filter === 'missing_rgid' && !row.missing_rgid) return false;
      if (filter === 'target_exists' && !row.target_exists) return false;
      if (filter === 'db_tracked' && row.db_item_count <= 0) return false;
      if (filter === 'needs_review' && (row.safe || bucket === 'safe_empty_source')) return false;
      if (!needle) return true;
      return `${row.artist} ${row.name} ${row.folder} ${row.proposed_folder ?? ''} ${row.skip_reason}`.toLowerCase().includes(needle);
    });
    return rows.sort((a, b) => {
      if (sort === 'artist') return `${a.artist} ${a.name}`.localeCompare(`${b.artist} ${b.name}`);
      if (sort === 'album') return a.name.localeCompare(b.name);
      if (sort === 'placeholder') return a.placeholder_type.localeCompare(b.placeholder_type) || a.name.localeCompare(b.name);
      if (sort === 'target_exists') return Number(b.target_exists) - Number(a.target_exists) || a.name.localeCompare(b.name);
      if (sort === 'file_count') return b.file_count - a.file_count || a.name.localeCompare(b.name);
      const pa = BUCKET_ORDER.indexOf(classifyRow(a));
      const pb = BUCKET_ORDER.indexOf(classifyRow(b));
      return pa - pb || a.name.localeCompare(b.name);
    });
  }, [filter, query, scanData?.rows, sort]);

  const visibleRows = showAll ? filteredRows : filteredRows.slice(0, 40);
  const safeCount = bucketCounts.safe_rename + bucketCounts.safe_empty_source;
  const reviewCount = scanData ? (scanData.total - safeCount) : 0;

  const applyJobResult = applyJob ? getJobResult<{ renamed: number; skipped: number; failed: number; summary: string }>(applyJob) : null;

  return (
    <div>
      <CleanPanelHeader
        title="Folder Placeholder Names"
        description={
          'Find album folders whose names contain unresolved template placeholders like {Album MbId} or $disc_subfolder. ' +
          'Safe renames (no target conflict, no DB items) can be applied in bulk with one click. ' +
          'Target-exists cases require manual review and merge. ' +
          'This scan is read-only — no changes happen until you explicitly apply an action.'
        }
        meta={(
          <>
            <span>Last scan: {formatScanTime(lastScanAt)}</span>
            <span>Status: {busy ? (applyJobId ? 'Applying renames…' : 'Scanning…') : scanData ? 'Scan loaded' : 'Idle'}</span>
          </>
        )}
      />

      {error && <Alert severity="error" sx={{ mt: 1 }}>{error}</Alert>}

      {/* Primary action bar */}
      <CleanActionBar>
        <Button variant="outlined" size="small" onClick={() => void doScan()} disabled={busy}>
          {scanData ? 'Rescan' : 'Scan Folders'}
        </Button>

        {scanData && bucketCounts.safe_rename > 0 && (
          <Button
            variant="contained"
            size="small"
            color="success"
            onClick={() => void doApplySafeRenames()}
            disabled={busy}
            title="Rename all safe folders — only applies rows with no target conflict and no DB items"
          >
            Apply Safe Renames ({bucketCounts.safe_rename})
          </Button>
        )}

        {selectedSafeRows.length > 0 && (
          <Button
            variant="contained"
            size="small"
            color="success"
            onClick={() => void doApplySafeRenames(selectedSafeRows.map((r) => r.folder))}
            disabled={busy}
          >
            Apply Selected ({selectedSafeRows.length})
          </Button>
        )}

        <FormControlLabel
          control={
            <Switch
              size="small"
              checked={autoApplySafe}
              onChange={(e) => toggleAutoApply(e.target.checked)}
            />
          }
          label={<span className="text-[0.72rem] text-zinc-400">Auto-apply safe renames after scan</span>}
          sx={{ ml: 0 }}
        />
      </CleanActionBar>

      {busy && <LinearProgress sx={{ mt: 1 }} />}

      {/* Scan job card */}
      {scanJob && !applyJobId && (
        <div className="mt-3">
          <JobStatusCard job={scanJob} runningLabel="Scanning folder names…" logLines={2} />
        </div>
      )}

      {/* Apply job card */}
      {applyJob && (
        <div className="mt-3">
          <JobStatusCard job={applyJob} runningLabel="Applying safe folder renames…" logLines={4} />
          {applyJobResult && (
            <div className="mt-2 rounded border border-emerald-800/50 bg-emerald-950/20 px-3 py-2 text-[0.72rem] text-emerald-300">
              {applyJobResult.summary} — Results will refresh automatically.
            </div>
          )}
        </div>
      )}

      {scanData && <CleanMetricGrid items={metricItems} />}

      {scanData && scanData.total === 0 && (
        <CleanEmptyState
          title="No placeholder folder names found"
          message="All album folder names look clean — no {Album MbId} or unresolved template tokens detected."
          tone="success"
        />
      )}

      {scanData && scanData.total > 0 && (
        <CleanSection
          title={`Folder results (${filteredRows.length})`}
          description={
            bucketCounts.safe_rename > 0
              ? `${bucketCounts.safe_rename} folder(s) can be safely renamed with one click. ${reviewCount} folder(s) need manual review.`
              : 'Click "Review details" to compare source vs. canonical target inline and access safe apply actions. No changes happen without explicit confirmation.'
          }
          count={<Chip label={`${safeCount} safe · ${reviewCount} review`} size="small" variant="outlined" />}
        >
          <CleanActionBar>
            <TextField
              label="Search folders"
              size="small"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              sx={{ minWidth: 220 }}
            />
            <div className="flex flex-wrap gap-1">
              {FILTERS.map((item) => (
                <Button
                  key={item.value}
                  size="small"
                  variant={filter === item.value ? 'contained' : 'outlined'}
                  onClick={() => setFilter(item.value)}
                >
                  {item.label}
                </Button>
              ))}
            </div>
            <label className="flex min-w-[180px] flex-col gap-1 text-[0.68rem] font-medium uppercase tracking-wide text-zinc-500">
              Sort
              <select
                className="rounded border border-graphite-700 bg-graphite-950 px-2 py-2 text-sm normal-case tracking-normal text-zinc-200"
                value={sort}
                onChange={(event) => setSort(event.target.value as FolderSort)}
              >
                {SORTS.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
              </select>
            </label>
          </CleanActionBar>

          {/* Bulk selection controls */}
          {safeRenameRows.length > 0 && (
            <div className="mt-2 flex items-center gap-3 text-xs text-zinc-500">
              <button className="text-zinc-400 hover:text-zinc-200 underline" onClick={selectAllSafe}>
                Select all safe renames ({safeRenameRows.length})
              </button>
              {selected.size > 0 && (
                <>
                  <span>{selected.size} selected</span>
                  <button className="text-zinc-400 hover:text-zinc-200 underline" onClick={clearSelection}>
                    Clear selection
                  </button>
                </>
              )}
            </div>
          )}

          {reviewed.size > 0 && (
            <div className="mt-2 flex items-center gap-2 text-xs text-zinc-500">
              <span>{reviewed.size} row(s) marked reviewed.</span>
              <button className="text-zinc-400 hover:text-zinc-200 underline" onClick={() => setReviewed(new Set())}>
                Clear all
              </button>
            </div>
          )}

          <div className="mt-3 space-y-2">
            {visibleRows.map((row) => (
              <FolderRow
                key={row.folder}
                row={row}
                reviewed={reviewed.has(row.folder)}
                onMarkReviewed={() => toggleReviewed(row.folder)}
                checked={selected.has(row.folder)}
                onToggle={() => toggleSelected(row.folder)}
                onRenamed={() => setRenamedSet((prev) => new Set([...prev, row.folder]))}
              />
            ))}
          </div>
          {filteredRows.length === 0 && (
            <CleanEmptyState title="No folder rows match the current filters" />
          )}
          {filteredRows.length > 40 && !showAll && (
            <Button size="small" variant="text" sx={{ mt: 1 }} onClick={() => setShowAll(true)}>
              Show all {filteredRows.length} rows
            </Button>
          )}
        </CleanSection>
      )}
    </div>
  );
}
