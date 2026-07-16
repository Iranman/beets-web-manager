import type { ReactNode } from 'react';

type Tone = 'neutral' | 'success' | 'warning' | 'danger' | 'info';

function cx(...classes: Array<string | false | null | undefined>) {
  return classes.filter(Boolean).join(' ');
}

const toneText: Record<Tone, string> = {
  neutral: 'text-zinc-300',
  success: 'text-emerald-300',
  warning: 'text-amber-300',
  danger: 'text-red-300',
  info: 'text-sky-300',
};

const toneBorder: Record<Tone, string> = {
  neutral: 'border-graphite-700 bg-graphite-900/72',
  success: 'border-emerald-900/50 bg-emerald-950/12',
  warning: 'border-amber-900/50 bg-amber-950/12',
  danger: 'border-red-900/50 bg-red-950/12',
  info: 'border-sky-900/50 bg-sky-950/12',
};

export function CleanPanelHeader({
  title,
  description,
  meta,
  actions,
  children,
}: {
  title: string;
  description?: ReactNode;
  meta?: ReactNode;
  actions?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <section className="overflow-hidden rounded-md border border-graphite-700 bg-graphite-900/82 shadow-sm shadow-black/20">
      <div className="h-px bg-gradient-to-r from-red-700 via-red-700/45 to-transparent" />
      <div className="p-3.5">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
          <div className="min-w-0">
            <h2 className="text-base font-semibold text-zinc-100">{title}</h2>
            {description ? <div className="mt-1 text-sm leading-5 text-zinc-400">{description}</div> : null}
            {meta ? <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-xs text-zinc-500">{meta}</div> : null}
          </div>
          {actions ? <div className="flex shrink-0 flex-wrap gap-2">{actions}</div> : null}
        </div>
        {children ? <div className="mt-3">{children}</div> : null}
      </div>
    </section>
  );
}

export function CleanActionBar({
  children,
  sticky = false,
}: {
  children: ReactNode;
  sticky?: boolean;
}) {
  return (
    <div
      className={cx(
        'flex flex-col gap-2 rounded-md border border-graphite-700 bg-graphite-900/72 p-3 shadow-sm shadow-black/15 lg:flex-row lg:items-center',
        sticky && 'sticky bottom-3 z-20 border-zinc-600/80 bg-graphite-900/92 shadow-xl shadow-black/45 backdrop-blur',
      )}
    >
      {children}
    </div>
  );
}

export function CleanEmptyState({
  title,
  message,
  tone = 'neutral',
  action,
}: {
  title: string;
  message?: ReactNode;
  tone?: Tone;
  action?: ReactNode;
}) {
  return (
    <div className={cx('flex flex-col gap-3 rounded-md border border-dashed p-4 sm:flex-row sm:items-center sm:justify-between', toneBorder[tone])}>
      <div className="min-w-0">
        <div className={cx('text-sm font-semibold', toneText[tone])}>{title}</div>
        {message ? <div className="mt-1 text-sm leading-5 text-zinc-400">{message}</div> : null}
      </div>
      {action ? <div className="shrink-0">{action}</div> : null}
    </div>
  );
}

export function CleanSection({
  title,
  description,
  count,
  tone = 'neutral',
  actions,
  children,
}: {
  title: string;
  description?: ReactNode;
  count?: ReactNode;
  tone?: Tone;
  actions?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <section className={cx('overflow-hidden rounded-md border shadow-sm shadow-black/10', toneBorder[tone])}>
      <div className="p-4">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="text-sm font-semibold text-zinc-200">{title}</h3>
              {count ? <span>{count}</span> : null}
            </div>
            {description ? <div className="mt-1 text-xs leading-5 text-zinc-400">{description}</div> : null}
          </div>
          {actions ? <div className="flex shrink-0 flex-wrap gap-2">{actions}</div> : null}
        </div>
        {children ? <div className="mt-3">{children}</div> : null}
      </div>
    </section>
  );
}

export function CleanMetricGrid({
  items,
}: {
  items: Array<{ label: string; value: ReactNode; tone?: Tone }>;
}) {
  return (
    <div className="grid grid-cols-[repeat(auto-fit,minmax(9.5rem,1fr))] gap-2">
      {items.map((item) => (
        <div key={item.label} className="min-h-[4rem] rounded-md border border-graphite-700 bg-graphite-950/55 px-3 py-2.5">
          <div className="text-[0.67rem] font-semibold uppercase text-zinc-500">{item.label}</div>
          <div className={cx('mt-1 text-lg font-semibold tabular-nums', toneText[item.tone ?? 'neutral'])}>
            {item.value}
          </div>
        </div>
      ))}
    </div>
  );
}
