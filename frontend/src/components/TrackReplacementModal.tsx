import {
  Dialog,
  DialogBackdrop,
  DialogPanel,
  DialogTitle,
} from '@headlessui/react';
import Alert from '@mui/material/Alert';
import Button from '@mui/material/Button';
import LinearProgress from '@mui/material/LinearProgress';
import TextField from '@mui/material/TextField';
import { useState } from 'react';
import {
  applyTrackReplacement,
  planTrackReplacement,
  rollbackTrackReplacement,
  type TrackReplacementApplyResponse,
  type TrackReplacementPlanResponse,
} from '../api/client';

export interface TrackReplacementModalProps {
  open: boolean;
  itemId: number;
  trackTitle: string;
  originalPath: string;
  onClose: () => void;
  onSuccess: () => void;
}

type ModalStep = 'input' | 'planning' | 'review' | 'applying' | 'completed' | 'stale' | 'partial' | 'failed';

/**
 * Manual review flow for replacing a single track's file (SEC-002 Wave 17).
 *
 * This is the human-reviewed counterpart to the pre-existing automatic
 * Music Format Replacement background job (which already searches for,
 * downloads, and AcoustID-verifies a replacement without a per-track
 * click -- see docs/TECHNICAL_DEBT.md ARCH-003 for why that path is
 * authorized to skip this review step). This modal is for a user who
 * already has a specific candidate file in mind: Plan verifies it via the
 * same AcoustID fingerprint infrastructure server-side and never mutates
 * anything by itself; only the explicit "Apply Replacement" click does.
 */
export function TrackReplacementModal({
  open,
  itemId,
  trackTitle,
  originalPath,
  onClose,
  onSuccess,
}: TrackReplacementModalProps) {
  const [step, setStep] = useState<ModalStep>('input');
  const [candidatePath, setCandidatePath] = useState('');
  const [reason, setReason] = useState('');
  const [plan, setPlan] = useState<TrackReplacementPlanResponse | null>(null);
  const [applyResult, setApplyResult] = useState<TrackReplacementApplyResponse | null>(null);
  const [rollbackResult, setRollbackResult] = useState<TrackReplacementApplyResponse | null>(null);
  const [errorMsg, setErrorMsg] = useState('');
  const [rollbackLoading, setRollbackLoading] = useState(false);

  const reset = () => {
    setStep('input');
    setPlan(null);
    setApplyResult(null);
    setRollbackResult(null);
    setErrorMsg('');
    setCandidatePath('');
    setReason('');
  };

  const handleClose = () => {
    reset();
    onClose();
  };

  const handlePlan = async () => {
    if (!candidatePath.trim()) return;
    setStep('planning');
    setErrorMsg('');
    try {
      const res = await planTrackReplacement(itemId, candidatePath.trim(), reason.trim() || undefined);
      if (res.ok) {
        setPlan(res);
        setStep('review');
      } else {
        setErrorMsg(res.error || 'Failed to create replacement plan.');
        setStep('failed');
      }
    } catch (err: unknown) {
      setErrorMsg(err instanceof Error ? err.message : 'Engine communication error.');
      setStep('failed');
    }
  };

  const handleApply = async () => {
    if (!plan?.operation_id) return;
    setStep('applying');
    setErrorMsg('');
    try {
      const res = await applyTrackReplacement(itemId, plan.operation_id);
      if (res.ok) {
        setApplyResult(res);
        setStep('completed');
      } else {
        setErrorMsg(res.error || 'Apply failed.');
        // Same truthful classification contract as Album Cleanup (Wave
        // 16): only present "nothing changed" when the engine's own
        // "mutated" flag confirms it.
        if (res.mutated) {
          setStep('partial');
        } else if ((res.code || '').includes('toctou') || (res.code || '') === 'quarantine_destination_exists') {
          setStep('stale');
        } else {
          setStep('failed');
        }
      }
    } catch (err: unknown) {
      setErrorMsg(err instanceof Error ? err.message : 'Failed to apply replacement.');
      setStep('failed');
    }
  };

  const handleRollback = async () => {
    const opId = applyResult?.operation_id || plan?.operation_id;
    if (!opId) return;
    setRollbackLoading(true);
    setErrorMsg('');
    try {
      const res = await rollbackTrackReplacement(opId);
      if (res.ok) {
        setRollbackResult(res);
      } else {
        setErrorMsg(res.error || 'Rollback failed.');
      }
    } catch (err: unknown) {
      setErrorMsg(err instanceof Error ? err.message : 'Rollback execution failed.');
    } finally {
      setRollbackLoading(false);
    }
  };

  const meta = plan?.plan?.metadata;
  const contract = meta?.matching_contract;
  const rollbackAvailable = Boolean(meta?.rollback_available);

  return (
    <Dialog className="relative z-50" open={open} onClose={handleClose}>
      <DialogBackdrop className="fixed inset-0 bg-black/70" />
      <div className="fixed inset-0 overflow-y-auto p-4">
        <div className="flex min-h-full items-center justify-center">
          <DialogPanel className="w-full max-w-2xl rounded-md border border-graphite-700 bg-graphite-950 p-5 shadow-2xl">
            <div className="flex items-start justify-between gap-3 border-b border-graphite-800 pb-3">
              <div>
                <DialogTitle className="text-lg font-semibold text-zinc-100">
                  Replace Track File
                </DialogTitle>
                <p className="mt-0.5 text-xs text-zinc-400">{trackTitle}</p>
              </div>
              <Button size="small" variant="outlined" onClick={handleClose}>
                Close
              </Button>
            </div>

            {step === 'input' ? (
              <div className="mt-4 space-y-4">
                <div className="rounded-md border border-graphite-800 bg-graphite-900/50 p-3 text-xs text-zinc-400">
                  <span className="text-zinc-500">Current file:</span>{' '}
                  <span className="font-mono text-zinc-300 break-all">{originalPath || '(unknown)'}</span>
                </div>
                <TextField
                  fullWidth
                  size="small"
                  label="Replacement file path"
                  value={candidatePath}
                  onChange={(e) => setCandidatePath(e.target.value)}
                  helperText="The engine will verify this is the same recording via AcoustID fingerprint before any change is planned."
                />
                <TextField
                  fullWidth
                  size="small"
                  label="Reason (optional)"
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                />
                <div className="flex justify-end gap-2 pt-2 border-t border-graphite-800">
                  <Button variant="outlined" onClick={handleClose}>
                    Cancel
                  </Button>
                  <Button variant="contained" disabled={!candidatePath.trim()} onClick={handlePlan}>
                    Generate Plan
                  </Button>
                </div>
              </div>
            ) : null}

            {step === 'planning' ? (
              <div className="space-y-4 py-6 text-center">
                <LinearProgress />
                <p className="text-sm text-zinc-300">
                  Verifying candidate identity and generating an engine-owned plan...
                </p>
              </div>
            ) : null}

            {step === 'review' && plan ? (
              <div className="mt-4 space-y-4">
                <Alert severity="info">
                  <strong className="font-semibold">Recoverable</strong> — the original file is quarantined
                  (not deleted) and can be restored via rollback until it is manually cleared.
                </Alert>

                <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 text-xs">
                  <div className="rounded border border-graphite-800 bg-graphite-900/50 p-2">
                    <div className="font-semibold text-zinc-300 mb-1">Existing track</div>
                    <div className="text-zinc-500">Path:</div>
                    <div className="font-mono text-zinc-300 break-all mb-1">{meta?.original_path}</div>
                    <div className="text-zinc-500">Recording ID:</div>
                    <div className="font-mono text-zinc-300 break-all">{contract?.original_recording_id || '(none on file)'}</div>
                  </div>
                  <div className="rounded border border-graphite-800 bg-graphite-900/50 p-2">
                    <div className="font-semibold text-zinc-300 mb-1">Replacement candidate</div>
                    <div className="text-zinc-500">Path:</div>
                    <div className="font-mono text-zinc-300 break-all mb-1">{meta?.replacement_path}</div>
                    <div className="text-zinc-500">Verified Recording ID:</div>
                    <div className="font-mono text-zinc-300 break-all">{contract?.replacement_recording_id}</div>
                  </div>
                </div>

                <div className="rounded-md border border-amber-900/40 bg-amber-950/20 p-3 text-xs text-amber-200 space-y-1">
                  <div className="font-semibold">Why this is authorized</div>
                  <p>Identity source: {contract?.identity_source || 'unknown'}</p>
                  <p>{contract?.decision_reason || 'AcoustID fingerprint evidence confirmed a matching recording.'}</p>
                  {contract?.original_release_group_id ? (
                    <p>Release Group preserved: {contract.original_release_group_id}</p>
                  ) : null}
                </div>

                <div className="rounded-md border border-graphite-800 bg-graphite-900/40 p-3 text-xs text-zinc-400 space-y-1">
                  <div className="font-semibold text-zinc-200">Planned changes</div>
                  <div>• Original file moved to server-managed quarantine</div>
                  <div>• Original's database row removed (the replacement is a separate, already-catalogued file and is not moved)</div>
                  <div>• Engine will re-verify both files immediately before making any change</div>
                </div>

                <div className="flex items-center justify-end gap-2 pt-2 border-t border-graphite-800">
                  <Button variant="outlined" onClick={handleClose}>
                    Cancel
                  </Button>
                  <Button variant="contained" color="warning" onClick={handleApply}>
                    Apply Replacement
                  </Button>
                </div>
              </div>
            ) : null}

            {step === 'applying' ? (
              <div className="space-y-4 py-6 text-center">
                <LinearProgress color="warning" />
                <p className="text-sm text-zinc-300">
                  Applying replacement via engine... re-verifying preconditions and stat signatures.
                </p>
              </div>
            ) : null}

            {step === 'completed' && applyResult ? (
              <div className="mt-4 space-y-4">
                <Alert severity="success">
                  <strong className="font-semibold">Replacement Completed</strong> — Transaction ID:{' '}
                  <code className="font-mono">{applyResult.operation_id}</code>
                </Alert>
                <div className="rounded-md border border-graphite-800 bg-graphite-900/40 p-3 text-xs space-y-2">
                  <div className="font-semibold text-zinc-200">Rollback Status</div>
                  {rollbackAvailable ? (
                    rollbackResult ? (
                      <Alert severity="info">
                        Rollback executed. Status: {String(rollbackResult.status || 'Rolled Back')}
                      </Alert>
                    ) : (
                      <Button size="small" variant="outlined" color="info" disabled={rollbackLoading} onClick={handleRollback}>
                        {rollbackLoading ? 'Rolling back...' : 'Rollback Replacement'}
                      </Button>
                    )
                  ) : (
                    <p className="text-zinc-500">Rollback is not available for this transaction.</p>
                  )}
                </div>
                <div className="flex justify-end gap-2 pt-2 border-t border-graphite-800">
                  <Button
                    variant="contained"
                    onClick={() => {
                      onSuccess();
                      handleClose();
                    }}
                  >
                    Done &amp; Refresh View
                  </Button>
                </div>
              </div>
            ) : null}

            {step === 'stale' ? (
              <div className="mt-4 space-y-4">
                <Alert severity="error">
                  <strong className="font-semibold">Plan Refused:</strong> {errorMsg}
                </Alert>
                <p className="text-xs text-zinc-400">
                  The original or candidate file changed after the plan was created. Nothing was changed.
                  Generate a new plan to continue.
                </p>
                <div className="flex justify-end gap-2 pt-2 border-t border-graphite-800">
                  <Button variant="outlined" onClick={handleClose}>Close</Button>
                  <Button variant="contained" onClick={() => setStep('input')}>Start Over</Button>
                </div>
              </div>
            ) : null}

            {step === 'partial' ? (
              <div className="mt-4 space-y-4">
                <Alert severity="error">
                  <strong className="font-semibold">Replacement Did Not Complete -- State May Be Partially Changed:</strong>{' '}
                  {errorMsg}
                </Alert>
                <p className="text-xs text-zinc-400">
                  Some part of this operation already occurred before the failure. Close this dialog and check
                  the track before deciding what to do next.
                </p>
                <div className="flex justify-end gap-2 pt-2 border-t border-graphite-800">
                  <Button
                    variant="contained"
                    color="error"
                    onClick={() => {
                      onSuccess();
                      handleClose();
                    }}
                  >
                    Close &amp; Refresh
                  </Button>
                </div>
              </div>
            ) : null}

            {step === 'failed' ? (
              <div className="mt-4 space-y-4">
                <Alert severity="error">
                  <strong className="font-semibold">Operation Refused:</strong> {errorMsg}
                </Alert>
                <div className="flex justify-end gap-2 pt-2 border-t border-graphite-800">
                  <Button variant="outlined" onClick={handleClose}>Close</Button>
                  <Button variant="contained" onClick={() => setStep('input')}>Try Again</Button>
                </div>
              </div>
            ) : null}
          </DialogPanel>
        </div>
      </div>
    </Dialog>
  );
}
