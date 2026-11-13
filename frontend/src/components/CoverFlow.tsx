import { useEffect, useRef, useState, type KeyboardEvent, type ReactNode } from 'react';

// Cover Flow: sleeves standing on a glossy shelf, the centred one facing you, the rest turned
// away on either side. Controlled: the parent owns the centred index. Browse-only: activating the
// centred sleeve is the one action, and what it does is the parent's call.

export type CoverFlowItem = {
  key: string;
  /** Accessible name, e.g. "Album X by Y, 3 of 120". */
  label: string;
  title: string;
  subtitle: string;
  art: ReactNode;
};

/** The slice of items to render around the centre: [start, end) clamped to the list. */
export function flowWindow(index: number, count: number, radius: number): [number, number] {
  if (count <= 0) return [0, 0];
  const centre = clampIndex(index, count);
  return [Math.max(0, centre - radius), Math.min(count, centre + radius + 1)];
}

export function clampIndex(index: number, count: number) {
  if (count <= 0) return 0;
  return Math.min(count - 1, Math.max(0, Math.round(index)));
}

/** Where a key press moves the centre, or null when the key is not a flow key. */
export function flowKeyTarget(key: string, index: number, count: number): number | null {
  if (count <= 0) return null;
  switch (key) {
    case 'ArrowLeft':
      return clampIndex(index - 1, count);
    case 'ArrowRight':
      return clampIndex(index + 1, count);
    case 'Home':
      return 0;
    case 'End':
      return count - 1;
    default:
      return null;
  }
}

// Geometry follows the react-coverflow interaction model: the stage is split into
// (2 * displayQuantityOfSide + 1) slots of one base width; the active cover sits in the middle slot,
// flat and enlarged; side covers step one slot apart, turned 40deg about Y, smaller and further back.
export const ACTIVE_SCALE = 1.5;
export const SIDE_SCALE = 0.8;
export const SIDE_ANGLE = 40;

/** Covers shown on each side of the active one (react-coverflow's displayQuantityOfSide). */
export function sideCount(width: number) {
  if (width < 480) return 1;
  if (width < 900) return 2;
  return 3;
}

/** Window radius actually rendered: the visible sides plus two entering from the edge, never past 8. */
export function windowRadius(width: number) {
  return Math.min(8, sideCount(width) + 2);
}

/** One slot's width in px: the stage width over (2 * sides + 1), capped so a wide screen keeps sane sleeves. */
export function baseWidth(width: number) {
  return Math.round(Math.min(170, width / (sideCount(width) * 2 + 1)));
}

/** The transform for a cover `offset` slots from the active one. Reduced motion: a flat strip. */
export function coverTransform(offset: number, base: number, reduced: boolean) {
  const x = Math.round(offset * base);
  if (reduced) return `translateX(${x}px) scale(${offset === 0 ? 1.2 : SIDE_SCALE})`;
  if (offset === 0) return `translateX(0px) translateZ(0px) rotateY(0deg) scale(${ACTIVE_SCALE})`;
  const angle = offset < 0 ? SIDE_ANGLE : -SIDE_ANGLE;
  return `translateX(${x}px) translateZ(${Math.round(-base * 0.25)}px) rotateY(${angle}deg) scale(${SIDE_SCALE})`;
}

/** Opacity by distance: the active cover full, its visible sides near full, the entering ones hidden. */
export function coverOpacity(offset: number, sides: number) {
  const distance = Math.abs(offset);
  if (distance === 0) return 1;
  return distance <= sides ? 0.92 - (distance - 1) * 0.04 : 0;
}

function useReducedMotion() {
  const [reduced, setReduced] = useState(
    () => typeof window !== 'undefined' && Boolean(window.matchMedia?.('(prefers-reduced-motion: reduce)').matches),
  );
  useEffect(() => {
    const query = window.matchMedia?.('(prefers-reduced-motion: reduce)');
    if (!query) return undefined;
    const update = () => setReduced(query.matches);
    query.addEventListener?.('change', update);
    return () => query.removeEventListener?.('change', update);
  }, []);
  return reduced;
}

// Wheel/trackpad and drag distances that move the flow by one cover.
const WHEEL_STEP = 60;
const DRAG_STEP_RATIO = 0.35;

export default function CoverFlow({
  count,
  item,
  index,
  label,
  onIndexChange,
  onActivate,
  onBack,
  autoFocus = false,
}: {
  count: number;
  /** Built only for the covers in the window, so a long list costs nothing off-screen. */
  item: (index: number) => CoverFlowItem;
  index: number;
  label: string;
  onIndexChange: (index: number) => void;
  onActivate: (index: number) => void;
  /** Escape or Backspace on the focused flow: up one level (the parent decides where). */
  onBack?: () => void;
  /** Take focus when mounted, so a keyboard drill-in or back lands on the flow. */
  autoFocus?: boolean;
}) {
  const centre = clampIndex(index, count);
  const reduced = useReducedMotion();
  const stageRef = useRef<HTMLDivElement | null>(null);
  const [width, setWidth] = useState(960);
  const base = baseWidth(width);
  const sides = sideCount(width);
  const [start, end] = flowWindow(centre, count, windowRadius(width));

  // Latest values for the native listeners below, so they are bound once.
  const live = useRef({ centre, count, onIndexChange });
  live.current = { centre, count, onIndexChange };

  useEffect(() => {
    const stage = stageRef.current;
    if (!stage) return undefined;
    setWidth(stage.clientWidth || 960);
    if (typeof ResizeObserver === 'undefined') return undefined;
    const observer = new ResizeObserver((entries) => {
      const next = entries[0]?.contentRect.width;
      if (next) setWidth(next);
    });
    observer.observe(stage);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    if (autoFocus) stageRef.current?.focus();
    // Mount only: a later prop change must not steal focus.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Any wheel over the stage pans the flow: a horizontal wheel (trackpad swipe or Shift+wheel) by
  // deltaX, a plain vertical wheel by deltaY (scroll down/forward -> next cover). Non-passive so a
  // swipe does not also trigger browser back/forward. At either end the event is left alone so the
  // page scrolls normally and the reader is never trapped on content above or below.
  useEffect(() => {
    const stage = stageRef.current;
    if (!stage) return undefined;
    let accumulated = 0;
    const onWheel = (event: WheelEvent) => {
      const { centre: at, count: n, onIndexChange: move } = live.current;
      const horizontal = Math.abs(event.deltaX) > Math.abs(event.deltaY);
      const delta = horizontal ? event.deltaX : event.deltaY;
      const target = clampIndex(at + Math.sign(delta), n);
      if (!delta || target === at) return; // at an end: let the page scroll on
      event.preventDefault();
      accumulated += delta * (event.deltaMode === 1 ? 20 : 1);
      if (Math.abs(accumulated) >= WHEEL_STEP) {
        accumulated = 0;
        move(target);
      }
    };
    stage.addEventListener('wheel', onWheel, { passive: false });
    return () => stage.removeEventListener('wheel', onWheel);
  }, []);

  // Drag or swipe: every DRAG_STEP_RATIO of a cover moves one place; a drag never counts as a click.
  const drag = useRef<{ x: number; moved: boolean; id: number } | null>(null);
  const suppressClick = useRef(false);

  function onKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    // Keys from the flow or a sleeve inside it (a click leaves focus on the sleeve); never from a
    // dialog or input rendered beneath it in the React tree.
    const from = event.target as HTMLElement;
    if (from !== event.currentTarget && !(from.classList?.contains('cf-item') && event.currentTarget.contains(from))) return;
    if (onBack && (event.key === 'Escape' || event.key === 'Backspace')) {
      event.preventDefault();
      onBack();
      return;
    }
    if (event.key === 'Enter' || event.key === ' ') {
      if (!count) return;
      event.preventDefault();
      onActivate(centre);
      return;
    }
    const target = flowKeyTarget(event.key, centre, count);
    if (target === null) return;
    event.preventDefault();
    if (target !== centre) onIndexChange(target);
  }

  // A nav arrow disables itself at an end; hand focus to the flow so the keyboard keeps working.
  function step(to: number) {
    onIndexChange(to);
    if (to <= 0 || to >= count - 1) stageRef.current?.focus();
  }

  const current = count ? item(centre) : undefined;

  return (
    <div className={reduced ? 'cf is-reduced' : 'cf'}>
      <div className="cf-frame">
      <div
        ref={stageRef}
        aria-label={label}
        aria-roledescription="carousel"
        className="cf-stage"
        role="region"
        style={{ height: Math.round(base * 2.15) }}
        tabIndex={0}
        onKeyDown={onKeyDown}
        onPointerDown={(event) => {
          if (event.button !== 0) return;
          drag.current = { x: event.clientX, moved: false, id: event.pointerId };
          suppressClick.current = false;
        }}
        onPointerMove={(event) => {
          const state = drag.current;
          if (!state || state.id !== event.pointerId) return;
          const dx = event.clientX - state.x;
          const step = base * DRAG_STEP_RATIO;
          if (!state.moved && Math.abs(dx) > 6) {
            state.moved = true;
            event.currentTarget.setPointerCapture?.(event.pointerId);
          }
          if (Math.abs(dx) >= step) {
            const { centre: at, count: n } = live.current;
            const target = clampIndex(at - Math.sign(dx), n);
            state.x = event.clientX;
            if (target !== at) onIndexChange(target);
          }
        }}
        onPointerUp={() => {
          suppressClick.current = Boolean(drag.current?.moved);
          drag.current = null;
        }}
        onPointerCancel={() => {
          drag.current = null;
        }}
        onClickCapture={(event) => {
          if (suppressClick.current) {
            suppressClick.current = false;
            event.stopPropagation();
            event.preventDefault();
          }
        }}
      >
        <div className="cf-floor" aria-hidden="true" style={{ top: Math.round(base * 1.6) }} />
        {Array.from({ length: end - start }, (_, i) => {
          const at = start + i;
          const offset = at - centre;
          const entry = item(at);
          const hidden = coverOpacity(offset, sides) === 0;
          return (
            <button
              key={entry.key}
              aria-hidden={hidden || undefined}
              aria-current={offset === 0 ? 'true' : undefined}
              aria-label={entry.label}
              className="cf-item"
              style={{
                width: base,
                height: base,
                top: Math.round(base * 0.6),
                marginLeft: -base / 2,
                opacity: coverOpacity(offset, sides),
                transform: coverTransform(offset, base, reduced),
                zIndex: 100 - Math.abs(offset),
                pointerEvents: hidden ? 'none' : undefined,
              }}
              tabIndex={-1}
              type="button"
              onClick={() => {
                // A click focuses this sleeve, which leaves the window a few steps on and would drop
                // focus to <body>; the stage keeps the arrow keys working.
                stageRef.current?.focus();
                if (offset === 0) onActivate(at);
                else onIndexChange(at);
              }}
            >
              <span className="cf-cover">
                {entry.art}
                {/* react-coverflow's enableHeading: the active title printed along the sleeve's foot. */}
                {offset === 0 ? <span aria-hidden="true" className="cf-heading">{entry.title}</span> : null}
              </span>
              {/* The sleeve mirrored on the shelf's gloss; decorative. */}
              <span aria-hidden="true" className="cf-reflection" inert>{entry.art}</span>
            </button>
          );
        })}
      </div>
      {/* react-coverflow's navigation arrows: step one cover; they do nothing else. */}
      <button
        aria-label="Previous"
        className="cf-nav is-prev"
        disabled={centre <= 0}
        type="button"
        onClick={() => step(clampIndex(centre - 1, count))}
      >
        <span aria-hidden="true">‹</span>
      </button>
      <button
        aria-label="Next"
        className="cf-nav is-next"
        disabled={centre >= count - 1}
        type="button"
        onClick={() => step(clampIndex(centre + 1, count))}
      >
        <span aria-hidden="true">›</span>
      </button>
      </div>

      <div className="cf-caption">
        <p className="cf-title">{current?.title ?? ''}</p>
        <p className="cf-subtitle">{current?.subtitle ?? ''}</p>
      </div>
      <p aria-live="polite" className="sr-only">{current?.label ?? ''}</p>

      {count > 1 ? (
        <input
          aria-label={`${label} position`}
          aria-valuetext={current?.label}
          className="cf-scrubber"
          max={count - 1}
          min={0}
          step={1}
          type="range"
          value={centre}
          onChange={(event) => onIndexChange(clampIndex(Number(event.target.value), count))}
        />
      ) : null}
    </div>
  );
}
