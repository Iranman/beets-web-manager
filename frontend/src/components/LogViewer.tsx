import Button from '@mui/material/Button';
import { useEffect, useMemo, useRef, useState } from 'react';
import type { RefObject } from 'react';

type FollowMode = 'follow' | 'pause';

type LogViewerProps = {
  lines?: string[];
  text?: string;
  emptyText?: string;
  className?: string;
  showControls?: boolean;
};

function cleanLine(line: string) {
  return line.replace(/^\s*(\[[^\]]+\]\s*)+/, '').trim() || line.trim();
}

export function LogViewer({
  lines,
  text,
  emptyText = '...',
  className = '',
  showControls = true,
}: LogViewerProps) {
  const [mode, setMode] = useState<FollowMode>('follow');
  const ref = useRef<HTMLDivElement | HTMLPreElement | null>(null);
  const content = useMemo(() => {
    const value = text ?? (lines ?? []).join('\n');
    return value || emptyText;
  }, [emptyText, lines, text]);
  const activityLines = useMemo(() => (lines ?? []).filter(Boolean).map(cleanLine), [lines]);
  const useActivityRows = !text && activityLines.length > 0;

  const scrollToBottom = () => {
    window.requestAnimationFrame(() => {
      const el = ref.current;
      if (el) el.scrollTop = el.scrollHeight;
    });
  };

  useEffect(() => {
    if (mode === 'follow') scrollToBottom();
  }, [content, mode]);

  const rawClass = [
    'max-h-56 overflow-y-auto whitespace-pre-wrap rounded-b-md bg-graphite-950/80 p-3',
    'font-mono text-[0.72rem] leading-6 text-zinc-300',
    className,
  ].filter(Boolean).join(' ');

  return (
    <div className="overflow-hidden rounded-md border border-graphite-700 bg-graphite-900/70">
      {showControls ? (
        <div className="flex items-center justify-between gap-2 border-b border-graphite-700 px-2.5 py-1.5">
          <span className="text-xs font-medium text-zinc-500">
            Activity {mode === 'follow' ? 'following' : 'paused'}
          </span>
          <div className="flex items-center gap-1">
            <Button
              color={mode === 'follow' ? 'primary' : 'inherit'}
              size="small"
              variant={mode === 'follow' ? 'contained' : 'text'}
              onClick={() => {
                setMode('follow');
                scrollToBottom();
              }}
            >
              Follow
            </Button>
            <Button
              color={mode === 'pause' ? 'primary' : 'inherit'}
              size="small"
              variant={mode === 'pause' ? 'contained' : 'text'}
              onClick={() => setMode('pause')}
            >
              Pause
            </Button>
            <Button
              color="inherit"
              size="small"
              variant="text"
              onClick={scrollToBottom}
            >
              Bottom
            </Button>
          </div>
        </div>
      ) : null}

      {useActivityRows ? (
        <div ref={ref as RefObject<HTMLDivElement>} className={`max-h-56 overflow-y-auto bg-graphite-950/80 p-2 ${className}`}>
          <ol className="space-y-1">
            {activityLines.map((line, index) => (
              <li key={`${index}-${line}`} className="grid grid-cols-[1.25rem_minmax(0,1fr)] gap-2 rounded px-1.5 py-1 text-[0.74rem] leading-5 text-zinc-300 hover:bg-graphite-850/70">
                <span className="pt-px text-right font-mono text-[0.62rem] text-zinc-600">{index + 1}</span>
                <span className="min-w-0 break-words font-mono text-zinc-300">{line}</span>
              </li>
            ))}
          </ol>
        </div>
      ) : (
        <pre ref={ref as RefObject<HTMLPreElement>} className={rawClass}>
          {content}
        </pre>
      )}
    </div>
  );
}
