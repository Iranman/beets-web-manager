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
  rollbackAlbumCleanup,
  type AlbumCleanupApplyResponse,
  type AlbumCleanupPlanResponse,
  type AlbumCleanupRollbackResponse,
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

type ModalStep = 'planning' | 'review' | 'applying' | 'completed' | 'stale' | 'failed';

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
  const [rollbackResult, setRollbackResult] = useState<AlbumCleanupRollbackResponse | null>(null);
  const [errorMsg, setErrorMsg] = useState('');
  const [rollbackLoading, setRollbackLoading] = useState(false);

  const fetchPlan = useCallback(async () => {
    if (!albumId || albumId <= 0) return;
    setStep('planning');
    setErrorMsg('');
    setPlan(null);
    setApplyResult(null);
    setRollbackResult(null);
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
        const err = res.error || 'Apply failed.';
        if (
          err.toLowerCase().includes('stale') ||
          err.toLowerCase().includes('changed') ||
          err.toLowerCase().includes('precondition')
        ) {
          setErrorMsg(
            'The album changed after this cleanup plan was created. Nothing was changed. Generate a new plan to continue.'
          );
          setStep('stale');
        } else {
          setErrorMsg(err);
          setStep('failed');
        }
      }
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Failed to apply cleanup plan.';
      if (
        msg.toLowerCase().includes('stale') ||
        msg.toLowerCase().includes('changed') ||
        msg.toLowerCase().includes('precondition')
      ) {
        setErrorMsg(
          'The album changed after this cleanup plan was created. Nothing was changed. Generate a new plan to continue.'
        );
        setStep('stale');
      } else {
        setErrorMsg(msg);
        setStep('failed');
      }
    }
  };

  const handleRollback = async () => {
    const opId = applyResult?.operation_id || plan?.operation_id;
    if (!opId) return;
    setRollbackLoading(true);
    setErrorMsg('');
    try {
      const res = await rollbackAlbumCleanup(opId);
      if (res.ok) {
        setRollbackResult(res);
      } else {
        setErrorMsg(res.error || 'Rollback failed.');
      }
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Rollback execution failed.';
      setErrorMsg(msg);
    } finally {
      setRollbackLoading(false);
    }
  };

  const txMeta = (plan?.transaction as Record<string, unknown> | undefined)?.metadata as Record<string, unknown> | undefined;
  const stepsList = (txMeta?.steps as Array<Record<string, unknown>>) || [];
  const reversibility = (txMeta?.reversibility as string) || (plan as Record<string, unknown> | null)?.reversibility as string || 'IRREVERSIBLE';
  const isIrreversible = reversibility === 'IRREVERSIBLE';
  const rollbackAvailable = Boolean(txMeta?.rollback_available);

  return (
    <Dialog className="relative z-50" open={open} onClose={onClose}>
      <DialogBackdrop className="fixed inset-0 bg-black/70" />
      <div className="fixed inset-0 overflow-y-auto p-4">
        <div className="flex min-h-full items-center justify-center">
          <DialogPanel className="w-full max-w-3xl rounded-md border border-graphite-700 bg-graphite-950 p-5 shadow-2xl">
            <div className="flex items-start justify-between gap-3 border-b border-graphite-800 pb-3">
              <div>
                <DialogTitle className="text-lg font-semibold text-zinc-100">
                  Clean Up Album
                </DialogTitle>
                <p className="mt-0.5 text-xs text-zinc-400">
                  Review engine cleanup plan for album identity and file mutations before confirming.
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
                      <span className="font-medium text-zinc-200">{albumTitle}</span>
                    </div>
                    <div>
                      <span className="text-zinc-500">Artist:</span>{' '}
                      <span className="font-medium text-zinc-200">{artistName}</span>
                    </div>
                    <div>
                      <span className="text-zinc-500">Beets Album ID:</span>{' '}
                      <span className="font-mono text-zinc-300">{albumId}</span>
                    </div>
                    {mbReleaseGroupId ? (
                      <div>
                        <span className="text-zinc-500">MB Release Group ID:</span>{' '}
                        <span className="font-mono text-zinc-300">{mbReleaseGroupId}</span>
                      </div>
                    ) : null}
                    <div className="sm:col-span-2">
                      <span className="text-zinc-500">Target Album Directory:</span>{' '}
                      <span className="font-mono text-zinc-300 break-all">{plan.target_path}</span>
                    </div>
                  </div>
                </div>

                {/* Reversibility Status Banner */}
                {isIrreversible ? (
                  <Alert severity="warning">
                    <strong className="font-semibold">Reversibility: Irreversible</strong> — This cleanup plan contains permanent file unlinks and database row deletions.
                  </Alert>
                ) : (
                  <Alert severity="info">
                    <strong className="font-semibold">Reversibility: Recoverable</strong> — Files will be safely quarantined to date-partitioned storage.
                  </Alert>
                )}

                {/* Before / After Diff */}
                <div className="rounded-md border border-graphite-800 bg-graphite-900/40 p-3">
                  <h4 className="text-xs font-semibold uppercase tracking-wider text-zinc-400">
                    Before / After State Diff
                  </h4>
                  <div className="mt-2 grid grid-cols-1 gap-3 sm:grid-cols-2 text-xs">
                    <div className="rounded border border-rose-900/40 bg-rose-950/20 p-2 text-rose-200">
                      <div className="font-semibold text-rose-300 mb-1">Before Cleanup</div>
                      <div>• {plan.file_count} track audio file(s) on disk</div>
                      <div>• 1 Beets album database record</div>
                      <div>• Track item DB rows for album_id {albumId}</div>
                    </div>
                    <div className="rounded border border-emerald-900/40 bg-emerald-950/20 p-2 text-emerald-200">
                      <div className="font-semibold text-emerald-300 mb-1">After Cleanup</div>
                      <div>• {isIrreversible ? 'Files deleted from disk' : 'Files moved to quarantine'}</div>
                      <div>• Album & item DB rows deleted</div>
                      <div>• Empty parent directories removed</div>
                    </div>
                  </div>
                </div>

                {/* Itemized Steps */}
                <div className="rounded-md border border-graphite-800 bg-graphite-900/50">
                  <div className="flex items-center justify-between border-b border-graphite-800 px-3 py-2">
                    <h4 className="text-xs font-semibold uppercase tracking-wider text-zinc-300">
                      Proposed Mutations ({stepsList.length} steps)
                    </h4>
                    <Chip
                      label={isIrreversible ? 'IRREVERSIBLE' : 'RECOVERABLE'}
                      color={isIrreversible ? 'warning' : 'success'}
                      size="small"
                      variant="outlined"
                    />
                  </div>
                  <div className="max-h-60 overflow-y-auto p-2 space-y-1.5 text-xs font-mono">
                    {stepsList.map((st, idx) => {
                      const type = (st.type as string) || 'mutation';
                      const src = (st.source as string) || (st.target as string) || `Album ID ${albumId}`;
                      return (
                        <div
                          key={(st.step_id as string) || idx}
                          className="flex items-start justify-between gap-2 rounded bg-graphite-950 p-2 border border-graphite-800/80"
                        >
                          <div className="min-w-0 flex-1 break-all">
                            <span className="text-amber-300 uppercase font-semibold">[{type}]</span>{' '}
                            <span className="text-zinc-300">{src}</span>
                          </div>
                          <span className="shrink-0 text-zinc-500">
                            {(st.reversibility as string) || reversibility}
                          </span>
                        </div>
                      );
                    })}
                  </div>
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
                  <Button
                    variant="contained"
                    color={isIrreversible ? 'warning' : 'primary'}
                    onClick={handleApply}
                  >
                    Apply Cleanup
                  </Button>
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
                    • Deleted file(s): {applyResult.deleted?.length || 0}
                  </div>
                  {applyResult.moved?.length ? (
                    <div className="text-zinc-400">• Quarantined file(s): {applyResult.moved.length}</div>
                  ) : null}
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
                  {rollbackAvailable ? (
                    <div className="space-y-2">
                      <p className="text-zinc-400">
                        This transaction is recoverable. You can restore quarantined files to their original location.
                      </p>
                      {rollbackResult ? (
                        <Alert severity="info">
                          Rollback executed successfully. Status: {String(rollbackResult.status || 'Rolled Back')}
                        </Alert>
                      ) : (
                        <Button
                          size="small"
                          variant="outlined"
                          color="info"
                          disabled={rollbackLoading}
                          onClick={handleRollback}
                        >
                          {rollbackLoading ? 'Rolling back...' : 'Rollback Cleanup'}
                        </Button>
                      )}
                    </div>
                  ) : (
                    <p className="text-zinc-500">
                      Rollback is unavailable for this transaction (contains irreversible file unlinks or DB deletions).
                    </p>
                  )}
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
