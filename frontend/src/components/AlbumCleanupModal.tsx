import {
  Dialog,
  DialogBackdrop,
  DialogPanel,
  DialogTitle,
} from '@headlessui/react';
import Alert from '@mui/material/Alert';
import Button from '@mui/material/Button';
import Chip from '@mui/material/Chip';
import LinearProgress from '@mui/material/LinearProgress';
import { useCallback, useEffect, useState } from 'react';
import {
  applyAlbumCleanup,
  planAlbumCleanup,
  type AlbumCleanupApplyResponse,
  type AlbumCleanupPlanItem,
  type AlbumCleanupPlanResponse,
} from '../api/client';

export interface AlbumCleanupModalProps {
  open: boolean;
  albumId: number;
  albumTitle: string;
  artistName: string;
  mbReleaseGroupId?: string;
  onClose: () => void;
  onSuccess: () => void;
}

type ModalStep = 'planning' | 'review' | 'applying' | 'completed' | 'stale' | 'partial' | 'failed';

export function AlbumCleanupModal({
  open,
  albumId,
  albumTitle,
  artistName,
  mbReleaseGroupId,
  onClose,
  onSuccess,
}: AlbumCleanupModalProps) {
  const [step, setStep] = useState<ModalStep>('planning');
  const [plan, setPlan] = useState<AlbumCleanupPlanResponse | null>(null);
  const [applyResult, setApplyResult] = useState<AlbumCleanupApplyResponse | null>(null);
  const [errorMsg, setErrorMsg] = useState('');

  const fetchPlan = useCallback(async () => {
    if (!albumId || albumId <= 0) return;
    setStep('planning');
    setErrorMsg('');
    setPlan(null);
    setApplyResult(null);
    try {
      const res = await planAlbumCleanup(albumId);
      if (res.ok) {
        setPlan(res);
        setStep('review');
      } else {
        setErrorMsg(res.error || 'Failed to create album cleanup plan.');
        setStep('failed');
      }
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Engine communication error.';
      setErrorMsg(msg);
      setStep('failed');
    }
  }, [albumId]);

  useEffect(() => {
    if (open && albumId > 0) {
      fetchPlan();
    }
  }, [open, albumId, fetchPlan]);

  const handleApply = async () => {
    if (!plan?.operation_id) return;
    setStep('applying');
    setErrorMsg('');
    try {
      const res = await applyAlbumCleanup(plan.operation_id);
      if (res.ok) {
        setApplyResult(res);
        setStep('completed');
      } else {
        // error_kind is the Web Manager's authoritative classification
        // (see _classify_album_cleanup_apply_failure in app.py) -- it is
        // driven by the engine's own "mutated" flag, never guessed here by
        // matching substrings in the error text. "stale_plan" is the only
        // kind that may ever be presented as "nothing changed";
        // "partial_mutation" means the library was already changed
        // before the failure and must be shown as a distinct, more urgent
        // state, never folded into the same reassuring copy.
        setErrorMsg(res.error || 'Apply failed.');
        if (res.error_kind === 'stale_plan') {
          setStep('stale');
        } else if (res.error_kind === 'partial_mutation') {
          setStep('partial');
        } else {
          setStep('failed');
        }
      }
    } catch (err: unknown) {
      // A thrown error here means the request itself failed (network,
      // engine unreachable, non-2xx with an unparseable body) -- there is
      // no error_kind/mutated signal available, so this can never be
      // presented as "nothing changed."
      const msg = err instanceof Error ? err.message : 'Failed to apply cleanup plan.';
      setErrorMsg(msg);
      setStep('failed');
    }
  };

  // A plan that deletes files needs the typed DELETE ALBUM FILES approval,
  // which lives in Library Changes; this modal only applies row-only plans.
  const deletesFiles = plan?.delete_files === true;
  const items = plan?.items ?? [];

  return (
    <Dialog className="relative z-50" open={open} onClose={onClose}>
      <DialogBackdrop className="fixed inset-0 bg-black/70" />
      <div className="fixed inset-0 overflow-y-auto p-4">
        <div className="flex min-h-full items-center justify-center">
          <DialogPanel className="w-full max-w-3xl rounded-md border border-graphite-700 bg-graphite-950 p-5 shadow-2xl">
            <div className="flex items-start justify-between gap-3 border-b border-graphite-800 pb-3">
              <div>
                <DialogTitle className="text-lg font-semibold text-zinc-100">
                  Remove Album from Library
                </DialogTitle>
                <p className="mt-0.5 text-xs text-zinc-400">
                  Review the engine's plan before confirming. This removes the album and its track
                  records from the Beets library. The audio files stay on disk -- nothing is deleted.
                  It does not detect or remove duplicates selectively.
                </p>
              </div>
              <Button size="small" variant="outlined" onClick={onClose}>
                Close
              </Button>
            </div>

            {/* Step: Planning */}
            {step === 'planning' ? (
              <div className="space-y-4 py-6 text-center">
                <LinearProgress />
                <p className="text-sm text-zinc-300">
                  Generating authoritative album cleanup plan from Beets engine...
                </p>
              </div>
            ) : null}

            {/* Step: Review */}
            {step === 'review' && plan ? (
              <div className="mt-4 space-y-4">
                {/* Album Identity Summary */}
                <div className="rounded-md border border-graphite-800 bg-graphite-900/50 p-3">
                  <div className="grid grid-cols-1 gap-2 text-xs sm:grid-cols-2">
                    <div>
                      <span className="text-zinc-500">Album:</span>{' '}
                      <span className="font-medium text-zinc-200">{plan.album?.album || albumTitle}</span>
                    </div>
                    <div>
                      <span className="text-zinc-500">Artist:</span>{' '}
                      <span className="font-medium text-zinc-200">{plan.album?.albumartist || artistName}</span>
                    </div>
                    <div>
                      <span className="text-zinc-500">Beets Album ID:</span>{' '}
                      <span className="font-mono text-zinc-300">{albumId}</span>
                    </div>
                    {plan.album?.mb_releasegroupid || mbReleaseGroupId ? (
                      <div>
                        <span className="text-zinc-500">MB Release Group ID:</span>{' '}
                        <span className="font-mono text-zinc-300">{plan.album?.mb_releasegroupid || mbReleaseGroupId}</span>
                      </div>
                    ) : null}
                  </div>
                </div>

                {/* Reversibility Status Banner, driven by the plan's own
                    delete_files. planAlbumCleanup sends none, so plans are
                    row-only (#184): Beets rows are removed, files stay on
                    disk. Row removal has no automatic rollback. */}
                {deletesFiles ? (
                  <Alert severity="error">
                    <strong className="font-semibold">This plan deletes files</strong> — Applying it
                    would permanently delete the album&apos;s audio files from disk. It cannot be
                    applied here. Approve it in Library Changes by typing DELETE ALBUM FILES, or close
                    this dialog to leave the library unchanged.
                  </Alert>
                ) : (
                  <>
                <Alert severity="warning">
                  <strong className="font-semibold">Irreversible</strong> — This will remove the
                  album and its track records from the Beets library. The audio files are kept on
                  disk. There is no automatic rollback; to get the album back into the library,
                  re-import its files.
                </Alert>

                {/* Before / After Diff */}
                <div className="rounded-md border border-graphite-800 bg-graphite-900/40 p-3">
                  <h4 className="text-xs font-semibold uppercase tracking-wider text-zinc-400">
                    Before / After State Diff
                  </h4>
                  <div className="mt-2 grid grid-cols-1 gap-3 sm:grid-cols-2 text-xs">
                    <div className="rounded border border-rose-900/40 bg-rose-950/20 p-2 text-rose-200">
                      <div className="font-semibold text-rose-300 mb-1">Before Cleanup</div>
                      <div>• 1 Beets album database record</div>
                      <div>• Track item DB rows for album_id {albumId}</div>
                      <div>• Audio files on disk</div>
                    </div>
                    <div className="rounded border border-emerald-900/40 bg-emerald-950/20 p-2 text-emerald-200">
                      <div className="font-semibold text-emerald-300 mb-1">After Cleanup</div>
                      <div>• Album & item DB rows removed from the library</div>
                      <div>• Audio files kept on disk, unchanged</div>
                    </div>
                  </div>
                  <p className="mt-2 text-[0.7rem] text-zinc-500">
                    No files are deleted or moved. The album directory, its audio files, artwork,
                    and any other files in it stay where they are.
                  </p>
                </div>
                  </>
                )}

                {/* Tracks in the plan (the plan's item snapshot) */}
                <div className="rounded-md border border-graphite-800 bg-graphite-900/50">
                  <div className="flex items-center justify-between border-b border-graphite-800 px-3 py-2">
                    <h4 className="text-xs font-semibold uppercase tracking-wider text-zinc-300">
                      Tracks to remove from the library ({items.length})
                    </h4>
                    <Chip label="IRREVERSIBLE" color="warning" size="small" variant="outlined" />
                  </div>
                  <ul className="max-h-60 overflow-y-auto p-2 space-y-1.5 text-xs">
                    {items.length === 0 ? (
                      <li className="p-2 text-zinc-400">
                        Beets reported no tracks for this album. Only the album record will be removed.
                      </li>
                    ) : items.map((item, idx) => (
                      <li
                        key={item.id ?? idx}
                        className="rounded bg-graphite-950 p-2 border border-graphite-800/80"
                      >
                        <div className="text-zinc-200">
                          <span className="font-mono text-zinc-500">{trackLabel(item)}</span>{' '}
                          {item.title || `Item ${item.id ?? '?'}`}
                        </div>
                        {item.path ? (
                          <div className="mt-0.5 font-mono text-zinc-500 break-all">{item.path}</div>
                        ) : null}
                      </li>
                    ))}
                  </ul>
                </div>

                {/* Confirmation Box */}
                <div className="rounded-md border border-amber-900/40 bg-amber-950/20 p-3 text-xs text-amber-200 space-y-1">
                  <div className="font-semibold">Confirmation Required</div>
                  <p>
                    This will apply the reviewed cleanup plan. The engine will re-check the album state and preconditions before making changes.
                  </p>
                </div>

                {/* Action Buttons */}
                <div className="flex items-center justify-end gap-2 pt-2 border-t border-graphite-800">
                  <Button variant="outlined" onClick={onClose}>
                    Cancel
                  </Button>
                  {deletesFiles ? null : (
                    <Button variant="contained" color="warning" onClick={handleApply}>
                      Apply Cleanup
                    </Button>
                  )}
                </div>
              </div>
            ) : null}

            {/* Step: Applying */}
            {step === 'applying' ? (
              <div className="space-y-4 py-6 text-center">
                <LinearProgress color="warning" />
                <p className="text-sm text-zinc-300">
                  Applying cleanup transaction via engine... Re-verifying preconditions and stat signatures.
                </p>
              </div>
            ) : null}

            {/* Step: Completed */}
            {step === 'completed' && applyResult ? (
              <div className="mt-4 space-y-4">
                <Alert severity="success">
                  <strong className="font-semibold">Album Cleanup Completed</strong> — Transaction ID:{' '}
                  <code className="font-mono">{applyResult.operation_id}</code>
                </Alert>

                <div className="rounded-md border border-graphite-800 bg-graphite-900/50 p-3 text-xs space-y-1.5">
                  <div className="font-semibold text-zinc-200">Execution Summary:</div>
                  <div className="text-zinc-400">
                    • Track row(s) removed from the library: {applyResult.removed_item_ids?.length ?? 0}
                  </div>
                  {applyResult.deleted?.length ? (
                    <div className="text-zinc-400">• Deleted file(s): {applyResult.deleted.length}</div>
                  ) : (
                    <div className="text-zinc-400">• Audio files kept on disk</div>
                  )}
                  {applyResult.log?.length ? (
                    <details className="mt-2 rounded bg-graphite-950 p-2 text-[0.7rem] font-mono text-zinc-400">
                      <summary className="cursor-pointer text-zinc-300">Engine Execution Log</summary>
                      <pre className="mt-1 whitespace-pre-wrap">{applyResult.log.join('\n')}</pre>
                    </details>
                  ) : null}
                </div>

                {/* Rollback Section */}
                <div className="rounded-md border border-graphite-800 bg-graphite-900/40 p-3 text-xs space-y-2">
                  <div className="font-semibold text-zinc-200">Rollback Status</div>
                  <p className="text-zinc-500">
                    Rollback is unavailable for this transaction: removed library rows cannot be restored automatically. The audio files are still on disk -- re-import them to add the album back.
                  </p>
                </div>

                <div className="flex justify-end gap-2 pt-2 border-t border-graphite-800">
                  <Button
                    variant="contained"
                    onClick={() => {
                      onSuccess();
                      onClose();
                    }}
                  >
                    Done & Refresh View
                  </Button>
                </div>
              </div>
            ) : null}

            {/* Step: Stale */}
            {step === 'stale' ? (
              <div className="mt-4 space-y-4">
                <Alert severity="error">
                  <strong className="font-semibold">Stale Plan Refused:</strong> {errorMsg}
                </Alert>
                <p className="text-xs text-zinc-400">
                  The album files or database membership changed after the initial plan was created. The engine refused unsafe execution to prevent data corruption.
                </p>
                <div className="flex justify-end gap-2 pt-2 border-t border-graphite-800">
                  <Button variant="outlined" onClick={onClose}>
                    Close
                  </Button>
                  <Button variant="contained" color="primary" onClick={fetchPlan}>
                    Generate New Plan
                  </Button>
                </div>
              </div>
            ) : null}

            {/* Step: Partial -- distinct from Stale. The Web Manager only
                ever sets this when the engine's "mutated" flag confirms at
                least one library change was already made before the
                failure (see error_kind === 'partial_mutation' in
                handleApply). This must never be presented as "nothing
                changed," and blindly offering "Generate New Plan" here
                would invite acting on a plan built from a state the user
                hasn't actually seen yet -- so this step routes to a refresh
                instead. */}
            {step === 'partial' ? (
              <div className="mt-4 space-y-4">
                <Alert severity="error">
                  <strong className="font-semibold">Cleanup Did Not Complete -- Album Was Partially Modified:</strong>{' '}
                  {errorMsg}
                </Alert>
                <p className="text-xs text-zinc-400">
                  The library was already partly changed before the operation stopped. The album's
                  current state in the database may no longer match what was shown in the review step.
                  This cleanup does not delete audio files. Close this dialog and check the album before deciding what to do
                  next -- do not assume a new plan reflects a clean starting point.
                </p>
                <div className="flex justify-end gap-2 pt-2 border-t border-graphite-800">
                  <Button
                    variant="contained"
                    color="error"
                    onClick={() => {
                      onSuccess();
                      onClose();
                    }}
                  >
                    Close &amp; Refresh Library
                  </Button>
                </div>
              </div>
            ) : null}

            {/* Step: Failed */}
            {step === 'failed' ? (
              <div className="mt-4 space-y-4">
                <Alert severity="error">
                  <strong className="font-semibold">Cleanup Operation Refused:</strong> {errorMsg}
                </Alert>
                <div className="flex justify-end gap-2 pt-2 border-t border-graphite-800">
                  <Button variant="outlined" onClick={onClose}>
                    Close
                  </Button>
                  <Button variant="contained" color="primary" onClick={fetchPlan}>
                    Retry Plan
                  </Button>
                </div>
              </div>
            ) : null}
          </DialogPanel>
        </div>
      </div>
    </Dialog>
  );
}

function trackLabel(item: AlbumCleanupPlanItem): string {
  if (!item.track) return '';
  return item.disc && item.disc > 1 ? `${item.disc}-${item.track}.` : `${item.track}.`;
}
