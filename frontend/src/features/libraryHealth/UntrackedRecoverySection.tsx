import { errorMessage } from '../../lib/errors';
import Alert from '@mui/material/Alert';
import Box from '@mui/material/Box';
import Button from '@mui/material/Button';
import Chip from '@mui/material/Chip';
import MenuItem from '@mui/material/MenuItem';
import Paper from '@mui/material/Paper';
import TextField from '@mui/material/TextField';
import Table from '@mui/material/Table';
import TableBody from '@mui/material/TableBody';
import TableCell from '@mui/material/TableCell';
import TableContainer from '@mui/material/TableContainer';
import TableHead from '@mui/material/TableHead';
import TableRow from '@mui/material/TableRow';
import Typography from '@mui/material/Typography';
import React, { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router';
import {
  getUntrackedCandidates,
  getUntrackedInventorySummary,
  planUntrackedRecovery,
  startUntrackedInventory,
} from '../../api/client';
import type {
  UntrackedCandidatesResponse,
  UntrackedInventorySummaryResponse,
  UntrackedRecoveryPlanResponse,
} from '../../api/types';
import { CleanSection } from '../../components/CleanPanel';

const CATEGORIES = [
  '',
  'canonical_album_file_missing_from_beets',
  'same_recording_other_encoding',
  'exact_duplicate_of_tracked',
  'import_artifact',
  'unknown',
];

/**
 * Untracked files (ARCH-021). The backend owns every decision -- action,
 * eligibility, safety result, reason -- and re-proves identity when a plan
 * is made; this panel only lists, filters and makes plans. Approving, applying
 * and rolling back a plan happen in one place: Workbench > Library Changes.
 */
export const UntrackedRecoverySection: React.FC = () => {
  const [summary, setSummary] = useState<UntrackedInventorySummaryResponse['summary'] | null>(null);
  const [category, setCategory] = useState('canonical_album_file_missing_from_beets');
  const [rows, setRows] = useState<UntrackedCandidatesResponse['rows']>([]);
  const [total, setTotal] = useState(0);
  const [plan, setPlan] = useState<UntrackedRecoveryPlanResponse | null>(null);
  const [message, setMessage] = useState<{ severity: 'success' | 'error' | 'info'; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  const loadSummary = useCallback(async () => {
    try {
      const res = await getUntrackedInventorySummary();
      setSummary(res.summary ?? null);
    } catch {
      setSummary(null);
    }
  }, []);

  const loadRows = useCallback(async () => {
    try {
      const res = await getUntrackedCandidates(category);
      setRows(res.rows ?? []);
      setTotal(res.total ?? 0);
    } catch (err) {
      setRows([]);
      setTotal(0);
      setMessage({ severity: 'info', text: errorMessage(err, 'Run the inventory first.') });
    }
  }, [category]);

  useEffect(() => {
    void loadSummary();
  }, [loadSummary]);
  useEffect(() => {
    void loadRows();
  }, [loadRows]);

  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    setMessage(null);
    try {
      await fn();
    } catch (err) {
      setMessage({ severity: 'error', text: errorMessage(err, 'Request failed') });
    } finally {
      setBusy(false);
    }
  };

  const startInventory = () =>
    run(async () => {
      const res = await startUntrackedInventory();
      setMessage({ severity: 'info', text: `Inventory started (job ${res.job_id ?? ''}). It is read-only and incremental.` });
    });

  const planRow = (action: string, path: string) =>
    run(async () => {
      const res = await planUntrackedRecovery(action, [path]);
      setPlan(res);
    });

  return (
    <CleanSection
      title="Untracked files"
      description="Audio under the music root that Beets does not track. Nothing changes until you approve a reviewed plan."
    >
      <Box sx={{ mb: 2, display: 'flex', gap: 2, alignItems: 'center', flexWrap: 'wrap' }}>
        <Button variant="contained" onClick={startInventory} disabled={busy}>Run inventory</Button>
        <TextField select size="small" label="Category" value={category} onChange={(e) => setCategory(e.target.value)} sx={{ minWidth: 280 }}>
          {CATEGORIES.map((c) => (
            <MenuItem key={c || 'all'} value={c}>{c || 'all'}</MenuItem>
          ))}
        </TextField>
        {summary && (
          <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap' }}>
            {Object.entries(summary.counts ?? {}).map(([name, count]) => (
              <Chip key={name} size="small" variant="outlined" label={`${name}: ${count}`} />
            ))}
          </Box>
        )}
      </Box>

      {message && <Alert severity={message.severity} sx={{ mb: 2 }}>{message.text}</Alert>}

      {plan && (
        <Paper variant="outlined" sx={{ p: 2, mb: 2 }}>
          {plan.ok ? (
            <>
              <Typography variant="subtitle2">Plan {plan.operation_id} ({plan.action ?? 'quarantine'}) -- review the evidence:</Typography>
              <Box component="pre" sx={{ fontSize: '0.75rem', whiteSpace: 'pre-wrap' }}>{JSON.stringify(plan.evidence ?? plan.files ?? {}, null, 1)}</Box>
              <Typography variant="body2">
                Nothing has changed yet.{' '}
                <Link className="text-brass-300 underline underline-offset-4 hover:text-brass-200" to={`/import?tab=changes&tx=${encodeURIComponent(plan.operation_id ?? '')}`}>
                  Review, approve or roll back this plan in Library Changes
                </Link>
              </Typography>
            </>
          ) : (
            <Alert severity="warning">Refused ({plan.code}): {plan.error}</Alert>
          )}
        </Paper>
      )}

      <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>{total} file(s) in this category (first 50 shown).</Typography>
      <TableContainer component={Paper} variant="outlined" sx={{ maxHeight: 420 }}>
        <Table size="small" stickyHeader>
          <TableHead>
            <TableRow>
              <TableCell>Path</TableCell>
              <TableCell>Action</TableCell>
              <TableCell>Eligibility</TableCell>
              <TableCell>Reason</TableCell>
              <TableCell />
            </TableRow>
          </TableHead>
          <TableBody>
            {rows.map((row) => (
              <TableRow key={row.path}>
                <TableCell sx={{ fontFamily: 'monospace', fontSize: '0.75rem' }}>{row.path}</TableCell>
                <TableCell>{row.action ?? '—'}</TableCell>
                <TableCell>{row.action_eligibility}</TableCell>
                <TableCell sx={{ fontSize: '0.8rem' }}>{row.reason}</TableCell>
                <TableCell>
                  {row.action && (
                    <Button size="small" onClick={() => planRow(row.action as string, row.path)} disabled={busy}>Plan</Button>
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </TableContainer>
    </CleanSection>
  );
};
