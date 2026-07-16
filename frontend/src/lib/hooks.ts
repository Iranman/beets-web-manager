import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { getDedupScan } from '../api/client';
import { ApiError, apiGet, apiPost } from './api';
import type { Job } from '../types/api';
import type { DedupScanState } from '../api/types';

const JOB_POLL_MS = 3_000;
const GLOBAL_JOBS_RUNNING_POLL_MS = 15_000;
const GLOBAL_JOBS_FAILED_POLL_MS = 60_000;

// ── useInterval ───────────────────────────────────────────────────────────────
// Runs a callback on a fixed interval. Pass null to pause.
export function useInterval(callback: () => void, delay: number | null): void {
  const savedCb = useRef(callback);
  useEffect(() => {
    savedCb.current = callback;
  }, [callback]);
  useEffect(() => {
    if (delay === null) return;
    const id = setInterval(() => savedCb.current(), delay);
    return () => clearInterval(id);
  }, [delay]);
}

// ── useJobPoll ────────────────────────────────────────────────────────────────
// Polls a single job until it reaches a terminal state.
export function useJobPoll(jobId: string | null, enabled = true): {
  job: Job | null;
  error: string | null;
} {
  const query = useQuery({
    queryKey: ['job', jobId],
    queryFn: () => {
      if (!jobId) throw new Error('Missing job id');
      return apiGet<Job & { ok: boolean }>(`/api/jobs/${jobId}`);
    },
    enabled: Boolean(jobId) && enabled,
    retry: (failureCount, error) => {
      if (error instanceof ApiError && error.httpStatus === 404) return false;
      return failureCount < 2;
    },
    staleTime: 1_000,
    gcTime: 5 * 60_000,
    refetchInterval: (q) => (q.state.data?.status === 'running' ? JOB_POLL_MS : false),
  });

  return {
    job: jobId ? query.data ?? null : null,
    error: query.error ? (query.error instanceof Error ? query.error.message : String(query.error)) : null,
  };
}

// ── useGlobalJobs ─────────────────────────────────────────────────────────────
// Keeps running/failed job counts fresh for nav badge display.
// Polls while jobs are active, and pauses when all clear.
export function useGlobalJobs(): {
  running: number;
  failed: number;
  refresh: () => void;
} {
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: ['jobs'],
    queryFn: async () => {
      const d = await apiGet<{ jobs: Job[] }>('/api/jobs');
      return d.jobs ?? [];
    },
    staleTime: 5_000,
    gcTime: 5 * 60_000,
    refetchInterval: (q) => {
      const jobs = q.state.data ?? [];
      const running = jobs.filter((j) => j.status === 'running').length;
      const failed = jobs.filter((j) => j.status === 'failed').length;
      return running > 0 ? GLOBAL_JOBS_RUNNING_POLL_MS : failed > 0 ? GLOBAL_JOBS_FAILED_POLL_MS : false;
    },
  });

  const jobs = query.data ?? [];
  const { running, failed } = useMemo(() => ({
    running: jobs.filter((j) => j.status === 'running').length,
    failed: jobs.filter((j) => j.status === 'failed').length,
  }), [jobs]);

  const refresh = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ['jobs'] });
  }, [queryClient]);

  useEffect(() => {
    const handleJobsChanged = () => {
      refresh();
    };
    window.addEventListener('beets:jobs-changed', handleJobsChanged);
    return () => window.removeEventListener('beets:jobs-changed', handleJobsChanged);
  }, [refresh]);

  return { running, failed, refresh };
}

// ── useDedupScan ──────────────────────────────────────────────────────────────
// Polls GET /api/dedup/scan/<jid> (separate from the regular job system).
export function useDedupScan(jid: string | null): {
  scan: (DedupScanState & { ok: boolean }) | null;
  error: string | null;
} {
  const [scan, setScan] = useState<(DedupScanState & { ok: boolean }) | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!jid) { setScan(null); setError(null); return; }
    const scanJid = jid;
    let active = true;

    async function poll() {
      while (active) {
        try {
          const s = await getDedupScan(scanJid);
          if (!active) return;
          setScan(s);
          if (s.status === 'done') return;
        } catch (err) {
          if (!active) return;
          setError(err instanceof Error ? err.message : String(err));
          return;
        }
        await new Promise<void>((r) => setTimeout(r, 1500));
      }
    }

    poll();
    return () => { active = false; };
  }, [jid]);

  return { scan, error };
}

// ── useJobKill ────────────────────────────────────────────────────────────────
export function useJobKill(): {
  kill: (jobId: string) => Promise<void>;
  killing: string | null;
} {
  const [killing, setKilling] = useState<string | null>(null);

  const kill = useCallback(async (jobId: string) => {
    setKilling(jobId);
    try {
      await apiPost(`/api/jobs/${jobId}/kill`);
    } finally {
      setKilling(null);
    }
  }, []);

  return { kill, killing };
}
