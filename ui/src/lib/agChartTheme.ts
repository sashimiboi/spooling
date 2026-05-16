/**
 * Shared AG Charts styling. Applies Knaflic-style declutter: kill borders,
 * tick marks, and axis lines; use very light horizontal gridlines only; one
 * hero color with gray for context; direct labels instead of legends where
 * possible.
 */

type Resolved = 'light' | 'dark';

export const PROVIDER_COLORS: Record<string, string> = {
  'claude-code': '#d97706',
  codex: '#10b981',
  copilot: '#6366f1',
  cursor: '#06b6d4',
  windsurf: '#ec4899',
  kiro: '#a855f7',
  antigravity: '#ef4444',
  gemini: '#4285f4',
  'cortex-code': '#29b5e8',
};

// One-hue categorical palette: violet hero, then desaturated steps so the
// eye focuses on the first (largest) slice/bar.
export const CATEGORICAL_PALETTE = [
  '#7c5cfc', '#a78bfa', '#c4b5fd', '#ddd6fe',
  '#94a3b8', '#cbd5e1', '#e2e8f0', '#f1f5f9',
  '#64748b', '#475569',
];

export function getChartTokens(resolved: Resolved) {
  const dark = resolved === 'dark';
  return {
    dark,
    text: dark ? '#8b8b9e' : '#6b6b80',
    textStrong: dark ? '#e4e4e7' : '#18181b',
    grid: dark ? 'rgba(255,255,255,0.06)' : 'rgba(0,0,0,0.06)',
    hero: dark ? '#8b7cf6' : '#7c5cfc',
    muted: dark ? '#3f3f50' : '#d4d4d8',
    markerStroke: dark ? '#0b0b0f' : '#ffffff',
  };
}

export function baseChartOptions(_resolved: Resolved) {
  return {
    background: { fill: 'transparent' },
    padding: { top: 12, right: 16, bottom: 8, left: 8 },
    theme: {
      overrides: {
        common: {
          title: { enabled: false },
          subtitle: { enabled: false },
        },
      },
    },
  };
}

// Axis preset: stripped lines/ticks, subtle dashed horizontal gridline on value axis.
export function valueAxis(resolved: Resolved, opts: { position?: 'left' | 'right' | 'top' | 'bottom'; formatter?: (p: any) => string } = {}) {
  const t = getChartTokens(resolved);
  return {
    type: 'number',
    position: opts.position ?? 'left',
    label: {
      fontSize: 10,
      color: t.text,
      ...(opts.formatter ? { formatter: opts.formatter } : {}),
    },
    tick: { stroke: 'transparent' },
    line: { stroke: 'transparent' },
    gridLine: { style: [{ stroke: t.grid, lineDash: [2, 4] }] },
  };
}

export function categoryAxis(resolved: Resolved, opts: { position?: 'bottom' | 'left' | 'top' | 'right'; rotation?: number } = {}) {
  const t = getChartTokens(resolved);
  return {
    type: 'category',
    position: opts.position ?? 'bottom',
    label: {
      fontSize: 10,
      color: t.text,
      ...(opts.rotation ? { rotation: opts.rotation } : {}),
    },
    tick: { stroke: 'transparent' },
    line: { stroke: t.grid },
    gridLine: { enabled: false },
  };
}

// Donut preset: thick ring, center label, direct callout labels, no legend.
export function donutSeries(opts: {
  angleKey: string;
  labelKey: string;
  fills: string[];
  centerTitle: string;
  centerValue: string;
  resolved: Resolved;
}) {
  const t = getChartTokens(opts.resolved);
  return {
    type: 'donut',
    angleKey: opts.angleKey,
    calloutLabelKey: opts.labelKey,
    sectorLabelKey: opts.angleKey,
    legendItemKey: opts.labelKey,
    innerRadiusRatio: 0.64,
    fills: opts.fills,
    strokeWidth: 0,
    calloutLabel: {
      enabled: true,
      color: t.text,
      fontSize: 11,
      minAngle: 18, // hide callouts for tiny slivers so they don't collide
    },
    calloutLine: { colors: [t.grid], length: 8, strokeWidth: 1 },
    sectorLabel: { enabled: false },
    innerLabels: [
      {
        text: opts.centerValue,
        color: t.textStrong,
        fontSize: 22,
        fontWeight: 600,
      },
      {
        text: opts.centerTitle,
        color: t.text,
        fontSize: 11,
        margin: 6,
      },
    ],
  };
}
