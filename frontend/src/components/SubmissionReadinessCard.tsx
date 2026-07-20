import Button from '@mui/material/Button';
import Chip from '@mui/material/Chip';
import { useState } from 'react';
import type { SubmissionCheckGroup, SubmissionPreflight, SubmissionPreflightCheck } from '../api/types';

/** Plain-language label for each action_type the backend can send. Shared
 * with Submissions.tsx so the readiness card and the sticky footer always
 * describe the same fix the same way. */
export const ACTION_LABEL: Record<string, string> = {
  rescan: 'Scan local files',
  resolve_artist: 'Resolve artist',
  open_import_review: 'Open Import Review',
  edit_metadata: 'Edit metadata',
  edit_tracks: 'Review tracks',
  review_duplicates: 'Review candidates',
  open_mb_handoff: 'Open MusicBrainz handoff',
  open_settings: 'Open Integration Settings',
  view_setup_details: 'View setup details',
};

const STAGE_TITLE: Record<string, string> = {
  artist: 'Artist readiness',
  identify: 'Review readiness',
  musicbrainz_prep: 'MusicBrainz readiness',
  attach_ids: 'Publish readiness',
  acoustid: 'AcoustID readiness',
  complete: 'Submission complete',
};
const GROUP_LABEL: Record<string, string> = {
  local_files: 'Local files',
  metadata: 'Metadata',
  musicbrainz: 'MusicBrainz',
  acoustid: 'AcoustID',
  system: 'System checks',
};
const GROUP_ORDER: SubmissionCheckGroup[] = ['local_files', 'metadata', 'musicbrainz', 'acoustid', 'system'];
const SEVERITY_LABEL: Record<string, string> = { blocked: 'Blocked', needs_attention: 'Needs attention', ready: 'Ready' };

/** Current-stage blockers/warnings/passed, for both the card and the
 * sticky-footer primary action so they can never disagree. */
export function readinessSummary(preflight: SubmissionPreflight | null) {
  const checks = (preflight?.checks || []).filter((c) => c.current_stage_relevant !== false);
  const blockers = checks.filter((c) => c.severity === 'blocked');
  const warnings = checks.filter((c) => c.severity === 'needs_attention');
  const passedCount = checks.filter((c) => c.severity === 'ready').length;
  return { blockers, warnings, passedCount, firstBlocker: blockers[0] };
}

function SeverityDot({ severity }: { severity?: string }) {
  const color = severity === 'blocked' ? 'bg-red-500' : severity === 'needs_attention' ? 'bg-amber-500' : 'bg-emerald-500';
  return <span className={`mt-1 inline-block h-2 w-2 shrink-0 rounded-full ${color}`} aria-hidden="true" />;
}

export interface SubmissionReadinessCardProps {
  preflight: SubmissionPreflight | null;
  loading?: boolean;
  primaryAction: { label: string; disabled: boolean; action: () => void };
  onAction: (actionType: string, actionTarget?: string) => void;
}

export default function SubmissionReadinessCard({ preflight, loading, primaryAction, onAction }: SubmissionReadinessCardProps) {
  const [expanded, setExpanded] = useState(false);
  const [openGroups, setOpenGroups] = useState<Record<string, boolean>>({});

  if (loading) {
    return <div className="rounded-md border border-graphite-800 bg-graphite-900/70 p-4 text-sm text-zinc-400">Checking readiness…</div>;
  }
  if (!preflight) {
    return <div className="rounded-md border border-graphite-800 bg-graphite-900/70 p-4 text-sm text-zinc-500">Select a resolvable review item to see submission readiness.</div>;
  }

  const stage = preflight.current_stage || 'identify';
  const { blockers, warnings, passedCount, firstBlocker } = readinessSummary(preflight);
  const topIssues: SubmissionPreflightCheck[] = [...blockers, ...warnings].slice(0, 3);
  const showAcoustidNotice = stage !== 'acoustid' && stage !== 'complete';

  const groups = GROUP_ORDER
    .map((group) => ({ group, checks: preflight.checks.filter((c) => c.group === group) }))
    .filter((g) => g.checks.length);

  function isGroupOpen(group: string): boolean {
    if (group in openGroups) return openGroups[group];
    return preflight!.checks.some((c) => c.group === group && c.severity === 'blocked');
  }

  return (
    <div className="rounded-md border border-graphite-800 bg-graphite-900/70 p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-sm font-semibold text-zinc-100">{STAGE_TITLE[stage] || 'Submission readiness'}</h2>
        {blockers.length
          ? <Chip size="small" color="error" label={`${blockers.length} blocker${blockers.length === 1 ? '' : 's'}`} />
          : <Chip size="small" color="success" label="Ready to continue" />}
      </div>

      <p className="mt-1 text-xs text-zinc-400">
        {warnings.length ? `${warnings.length} recommendation${warnings.length === 1 ? '' : 's'} · ` : ''}
        {passedCount} check{passedCount === 1 ? '' : 's'} passed
      </p>

      {topIssues.length ? (
        <ul className="mt-3 space-y-1.5 text-xs">
          {topIssues.map((check) => (
            <li key={check.id} className="flex items-start gap-2">
              <SeverityDot severity={check.severity} />
              <span className={check.severity === 'blocked' ? 'text-red-300' : 'text-amber-300'}>{check.explanation || check.label}</span>
            </li>
          ))}
        </ul>
      ) : null}

      {showAcoustidNotice ? (
        <p className="mt-3 text-[0.7rem] text-zinc-500">AcoustID · available after MusicBrainz recording IDs are attached.</p>
      ) : null}

      <div className="mt-3 flex flex-wrap gap-2">
        <Button
          size="small"
          variant="contained"
          disabled={firstBlocker ? false : primaryAction.disabled}
          onClick={() => {
            if (!firstBlocker) { primaryAction.action(); return; }
            if (firstBlocker.action_type === 'view_setup_details') { setExpanded(true); return; }
            onAction(firstBlocker.action_type || '', firstBlocker.action_target);
          }}
        >
          {firstBlocker ? (ACTION_LABEL[firstBlocker.action_type || ''] || 'Fix required item') : primaryAction.label}
        </Button>
        <Button size="small" variant="outlined" onClick={() => setExpanded((value) => !value)}>
          {expanded ? 'Hide all checks' : 'View all checks'}
        </Button>
      </div>

      {expanded ? (
        <div className="mt-4 space-y-2 border-t border-graphite-800 pt-3">
          {groups.map(({ group, checks }) => {
            const open = isGroupOpen(group);
            const groupBlockers = checks.filter((c) => c.severity === 'blocked').length;
            return (
              <div key={group} className="rounded border border-graphite-800 bg-graphite-950/35">
                <button
                  type="button"
                  onClick={() => setOpenGroups((current) => ({ ...current, [group]: !isGroupOpen(group) }))}
                  className="flex w-full items-center justify-between gap-2 px-3 py-2 text-left text-xs font-semibold text-zinc-200"
                  aria-expanded={open}
                >
                  <span>{GROUP_LABEL[group] || group}</span>
                  <span className="flex items-center gap-2 text-[0.68rem] font-normal text-zinc-500">
                    {groupBlockers ? <span className="text-red-400">{groupBlockers} blocked</span> : null}
                    <span>{open ? 'Hide' : 'Show'}</span>
                  </span>
                </button>
                {open ? (
                  <div className="space-y-1 px-3 pb-3">
                    {checks.map((check) => (
                      <div key={check.id} className="rounded border border-graphite-800/70 bg-graphite-900/40 px-2 py-1.5 text-xs">
                        <div className="flex items-center justify-between gap-2">
                          <span className="flex items-center gap-2 text-zinc-200">
                            {check.severity === 'ready' ? <span className="text-emerald-400">✓</span> : <SeverityDot severity={check.severity} />}
                            {check.label}
                          </span>
                          {check.severity !== 'ready' ? (
                            <span className={check.severity === 'blocked' ? 'text-red-400' : 'text-amber-400'}>
                              {SEVERITY_LABEL[check.severity] || check.severity}
                            </span>
                          ) : null}
                        </div>
                        {check.severity !== 'ready' && check.explanation ? <p className="mt-1 text-zinc-400">{check.explanation}</p> : null}
                        {check.severity !== 'ready' && check.action ? (
                          <div className="mt-1.5 flex items-center justify-between gap-2">
                            <span className="text-zinc-500">{check.action}</span>
                            {check.action_type ? (
                              <Button size="small" variant="text" onClick={() => onAction(check.action_type || '', check.action_target)}>
                                {ACTION_LABEL[check.action_type] || 'Fix'}
                              </Button>
                            ) : null}
                          </div>
                        ) : null}
                        {check.affected?.length ? <p className="mt-1 truncate text-zinc-600">{check.affected.slice(0, 4).join(', ')}</p> : null}
                      </div>
                    ))}
                  </div>
                ) : null}
              </div>
            );
          })}
        </div>
      ) : null}
    </div>
  );
}
