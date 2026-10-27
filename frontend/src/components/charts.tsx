/** ECharts wrappers.
 *
 *  Every chart in the app goes through `<EChart>` so the dark theme, the recessive grid, the
 *  crosshair-and-tooltip layer and the resize behaviour are defined once. Rules enforced here
 *  rather than left to each caller:
 *
 *  * one value axis, ever — there is no option to add a second y-scale;
 *  * colours come from the validated tokens in index.css (see WORKLOAD_ORDER in lib/status.ts),
 *    resolved to real hex because a canvas cannot read a CSS variable;
 *  * text wears text tokens, never the series colour;
 *  * two or more series always get a legend.
 */

import { BarChart, LineChart } from "echarts/charts";
import {
  GridComponent, LegendComponent, MarkLineComponent, TooltipComponent,
} from "echarts/components";
import { type EChartsType, init, use as registerEChartsModules } from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
import { useEffect, useMemo, useRef } from "react";

import type { EChartsOption, XAXisComponentOption, YAXisComponentOption } from "echarts";

// Only the chart types and components this app actually draws with are registered, so the
// bundle carries those and not the whole of ECharts.
registerEChartsModules([LineChart, BarChart, GridComponent, TooltipComponent, LegendComponent, MarkLineComponent,
     CanvasRenderer]);

import { orderWorkloads, workloadColor } from "@/lib/status";
import { DASH, num } from "@/lib/format";

// --- token resolution ----------------------------------------------------------------------

const varCache = new Map<string, string>();

/** Resolves a CSS custom property to the value a canvas can paint with. */
export function cssVar(name: string, fallback = "#888888"): string {
  const hit = varCache.get(name);
  if (hit) return hit;
  if (typeof window === "undefined") return fallback;
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const out = v || fallback;
  varCache.set(name, out);
  return out;
}

const ink = () => ({
  fg: cssVar("--color-fg", "#edeef2"),
  muted: cssVar("--color-fg-muted", "#a0a5ae"),
  subtle: cssVar("--color-fg-subtle", "#767a84"),
  grid: cssVar("--color-grid", "#2a2d34"),
  panel: cssVar("--color-panel", "#1b1e25"),
  border: cssVar("--color-border", "#2f333b"),
});

export const SERIES_1 = () => cssVar("--color-series-1", "#3987e5");
export const SERIES_2 = () => cssVar("--color-series-2", "#d95926");

// --- base component ------------------------------------------------------------------------

interface EChartProps {
  option: EChartsOption;
  /** Height in px. Charts are never taller than they are wide in this app. */
  height?: number;
  className?: string;
  /** Screen-reader description; the visible title lives outside the canvas. */
  label: string;
}

export function EChart({ option, height = 180, className, label }: EChartProps) {
  const host = useRef<HTMLDivElement | null>(null);
  const chart = useRef<EChartsType | null>(null);

  useEffect(() => {
    if (!host.current) return;
    const c = init(host.current, undefined, { renderer: "canvas" });
    chart.current = c;
    const ro = new ResizeObserver(() => c.resize());
    ro.observe(host.current);
    return () => { ro.disconnect(); c.dispose(); chart.current = null; };
  }, []);

  useEffect(() => {
    // `true` replaces the option wholesale: a series that disappears must not linger.
    chart.current?.setOption(option, true);
  }, [option]);

  return (
    <div ref={host} role="img" aria-label={label} className={className}
         style={{ height, width: "100%" }} />
  );
}

// --- shared option fragments ---------------------------------------------------------------

/** The tooltip chrome, as a concrete object so spreading it keeps a single type. */
function tooltipBase() {
  const c = ink();
  return {
    backgroundColor: c.panel,
    borderColor: c.border,
    borderWidth: 1,
    textStyle: { color: c.fg, fontSize: 11 },
    padding: [6, 9] as [number, number],
    extraCssText: "border-radius:6px;box-shadow:0 6px 24px rgba(0,0,0,.45)",
  };
}

function baseOption() {
  const c = ink();
  return {
    backgroundColor: "transparent",
    animationDuration: 200,
    textStyle: { fontFamily: "inherit", color: c.muted, fontSize: 11 },
    tooltip: tooltipBase(),
  };
}

/** Shared props for a numeric axis; cast to the x or y option type at the point of use, since
 *  ECharts types them separately even though the fields are identical. */
function valueAxisProps(unit: string, max?: number) {
  const c = ink();
  return {
    type: "value" as const,
    max,
    axisLabel: { color: c.subtle, fontSize: 10, formatter: (v: number) => `${num(v, 0)}${unit}` },
    axisLine: { show: false },
    axisTick: { show: false },
    splitLine: { lineStyle: { color: c.grid, width: 1 } },
  };
}

const yValue = (unit: string, max?: number): YAXisComponentOption => valueAxisProps(unit, max);

function timeAxis(): XAXisComponentOption {
  const c = ink();
  return {
    type: "time",
    axisLabel: { color: c.subtle, fontSize: 10, hideOverlap: true },
    axisLine: { lineStyle: { color: c.grid } },
    axisTick: { show: false },
    splitLine: { show: false },
  };
}

/** ECharts hands a label formatter a wide params object; we only ever read `value`. */
type LabelParams = { value: unknown };

function numberOf(v: unknown): number | null {
  return typeof v === "number" && !Number.isNaN(v) ? v : null;
}

// --- line chart (time series) --------------------------------------------------------------

export interface LineSeries {
  name: string;
  color: string;
  /** [epoch ms, value]; a gap is a null value, never a 0. */
  points: [number, number | null][];
}

interface TimeSeriesChartProps {
  series: LineSeries[];
  unit?: string;
  /** Pins the axis top, e.g. 100 for a percentage, so a flat line is not blown up. */
  max?: number;
  height?: number;
  label: string;
}

/** Multi-series line chart on ONE value axis. Crosshair + shared tooltip by default. */
export function TimeSeriesChart({ series, unit = "%", max, height, label }: TimeSeriesChartProps) {
  const option = useMemo<EChartsOption>(() => {
    const c = ink();
    return {
      ...baseOption(),
      grid: { left: 40, right: 12, top: series.length > 1 ? 28 : 10, bottom: 22 },
      legend: series.length > 1
        ? { show: true, top: 0, right: 0, itemWidth: 8, itemHeight: 8, itemGap: 14,
            icon: "roundRect", textStyle: { color: c.muted, fontSize: 11 } }
        : { show: false },
      tooltip: {
        ...tooltipBase(),
        trigger: "axis",
        axisPointer: { type: "line", lineStyle: { color: c.subtle, width: 1, type: "dashed" } },
      },
      xAxis: timeAxis(),
      yAxis: yValue(unit, max),
      series: series.map((s) => ({
        type: "line" as const,
        name: s.name,
        data: s.points,
        showSymbol: false,
        symbolSize: 8,
        lineStyle: { width: 2, color: s.color },
        itemStyle: { color: s.color },
        emphasis: { focus: "series" as const },
        connectNulls: false,
      })),
    };
  }, [series, unit, max]);

  return <EChart option={option} height={height} label={label} />;
}

/** Single-series sparkline: no axes, no legend, hover value only. Used for small multiples. */
export function Sparkline({ points, color, unit = "%", max, height = 44, label }: {
  points: [number, number | null][]; color: string; unit?: string; max?: number;
  height?: number; label: string;
}) {
  const option = useMemo<EChartsOption>(() => ({
    ...baseOption(),
    grid: { left: 2, right: 2, top: 6, bottom: 2 },
    tooltip: {
      ...tooltipBase(),
      trigger: "axis",
      axisPointer: { type: "line", lineStyle: { color: ink().subtle, width: 1, type: "dashed" } },
      valueFormatter: (v) => (v == null ? DASH : `${num(v as number, 1)}${unit}`),
    },
    xAxis: { ...timeAxis(), axisLabel: { show: false }, axisLine: { show: false } },
    yAxis: { ...valueAxisProps(unit, max), axisLabel: { show: false }, splitLine: { show: false } },
    series: [{
      type: "line", data: points, showSymbol: false, symbolSize: 8,
      lineStyle: { width: 2, color }, itemStyle: { color },
      areaStyle: { color, opacity: 0.12 }, connectNulls: false,
    }],
  }), [points, color, unit, max]);

  return <EChart option={option} height={height} label={label} />;
}

// --- categorical bars ----------------------------------------------------------------------

export interface CategoryBar { key: string; value: number | null; color?: string; label?: string }

/** Horizontal bars with direct value labels — identity never rests on colour alone.
 *  A 2px surface gap between bars comes from the category gap, not from a border. */
export function CategoryBars({ bars, unit = "%", max, label, height }: {
  bars: CategoryBar[]; unit?: string; max?: number; label: string; height?: number;
}) {
  const option = useMemo<EChartsOption>(() => {
    const c = ink();
    return {
      ...baseOption(),
      grid: { left: 4, right: 52, top: 4, bottom: 4, containLabel: true },
      tooltip: {
        ...tooltipBase(), trigger: "item",
        valueFormatter: (v) => (v == null ? DASH : `${num(v as number, 1)}${unit}`),
      },
      xAxis: { ...valueAxisProps(unit, max), splitLine: { show: false }, axisLabel: { show: false } },
      yAxis: {
        type: "category",
        inverse: true,
        data: bars.map((b) => b.label ?? b.key),
        axisLabel: { color: c.muted, fontSize: 11 },
        axisLine: { show: false },
        axisTick: { show: false },
      },
      series: [{
        type: "bar",
        barMaxWidth: 12,
        barCategoryGap: "35%",
        data: bars.map((b) => ({
          value: b.value,
          itemStyle: { color: b.color ?? SERIES_1(), borderRadius: [0, 4, 4, 0] },
        })),
        label: {
          show: true, position: "right", distance: 8, color: c.muted, fontSize: 11,
          formatter: (p: LabelParams) => {
            const v = numberOf(p.value);
            return v === null ? DASH : `${num(v, 1)}${unit}`;
          },
        },
      }],
    };
  }, [bars, unit, max]);

  return <EChart option={option} height={height ?? Math.max(60, bars.length * 26 + 16)} label={label} />;
}

/** Per-class activity, always in WORKLOAD_ORDER with the class's own colour. */
export function ClassActivityBars({ activity, label = "Activity by workload class" }: {
  activity: Record<string, number>; label?: string;
}) {
  const bars = useMemo<CategoryBar[]>(
    () => orderWorkloads(Object.keys(activity)).map((k) => ({
      key: k, value: activity[k], color: workloadColor(k), label: k.replace(/_/g, " "),
    })),
    [activity],
  );
  return <CategoryBars bars={bars} label={label} />;
}

// --- grouped bars (before vs after) --------------------------------------------------------

/** Two measures of the SAME unit side by side — the only grouped form used here. */
export function GroupedBars({ categories, groups, unit = "s", label, height = 200 }: {
  categories: string[];
  groups: { name: string; color: string; values: (number | null)[] }[];
  unit?: string; label: string; height?: number;
}) {
  const option = useMemo<EChartsOption>(() => {
    const c = ink();
    return {
      ...baseOption(),
      grid: { left: 44, right: 12, top: 28, bottom: 26 },
      legend: { show: true, top: 0, right: 0, itemWidth: 8, itemHeight: 8, itemGap: 14,
                icon: "roundRect", textStyle: { color: c.muted, fontSize: 11 } },
      tooltip: {
        ...tooltipBase(), trigger: "axis",
        axisPointer: { type: "shadow", shadowStyle: { color: "rgba(255,255,255,.03)" } },
        valueFormatter: (v) => (v == null ? DASH : `${num(v as number, 2)}${unit}`),
      },
      xAxis: {
        type: "category", data: categories,
        axisLabel: { color: c.subtle, fontSize: 10 },
        axisLine: { lineStyle: { color: c.grid } }, axisTick: { show: false },
      },
      yAxis: yValue(unit),
      series: groups.map((g) => ({
        type: "bar" as const, name: g.name, data: g.values,
        barMaxWidth: 14, barGap: "12%",
        itemStyle: { color: g.color, borderRadius: [4, 4, 0, 0] },
      })),
    };
  }, [categories, groups, unit]);

  return <EChart option={option} height={height} label={label} />;
}

/** Signed bars around a zero line. Colour carries polarity and is always paired with the
 *  signed number itself, so the sign is never colour-only. */
export function DivergingBars({ categories, values, unit = "%", label, height = 200, colorFor }: {
  categories: string[]; values: (number | null)[]; unit?: string; label: string;
  height?: number; colorFor?: (v: number, i: number) => string;
}) {
  const option = useMemo<EChartsOption>(() => {
    const c = ink();
    const pick = colorFor ?? ((v: number) =>
      v > 0 ? cssVar("--color-ok", "#55c975")
            : v < 0 ? cssVar("--color-bad", "#f75d59") : cssVar("--color-idle", "#7c808a"));
    return {
      ...baseOption(),
      grid: { left: 44, right: 12, top: 14, bottom: 26 },
      tooltip: {
        ...tooltipBase(), trigger: "item",
        valueFormatter: (v) => (v == null ? DASH : `${num(v as number, 1)}${unit}`),
      },
      xAxis: {
        type: "category", data: categories,
        axisLabel: { color: c.subtle, fontSize: 10 },
        axisLine: { lineStyle: { color: c.grid } }, axisTick: { show: false },
      },
      yAxis: yValue(unit),
      series: [{
        type: "bar", barMaxWidth: 16,
        data: values.map((v, i) => ({
          value: v,
          itemStyle: v == null ? {} : {
            color: pick(v, i),
            borderRadius: v >= 0 ? [4, 4, 0, 0] : [0, 0, 4, 4],
          },
        })),
        label: {
          show: true, position: "top", color: c.muted, fontSize: 10,
          formatter: (p: LabelParams) => {
            const v = numberOf(p.value);
            return v === null ? DASH : `${v > 0 ? "+" : ""}${num(v, 1)}${unit}`;
          },
        },
        markLine: {
          silent: true, symbol: "none",
          data: [{ yAxis: 0 }],
          lineStyle: { color: c.border, width: 1, type: "solid" },
          label: { show: false },
        },
      }],
    };
  }, [categories, values, unit, colorFor]);

  return <EChart option={option} height={height} label={label} />;
}

// --- workload timeline strip ---------------------------------------------------------------

/** A band of time coloured by the class the deterministic classifier reported.
 *  Rendered as plain divs, not a canvas: it is a legend-and-swatch list, not a plot. */
export function WorkloadStrip({ points, height = 18 }: {
  points: { t: number; workload: string }[]; height?: number;
}) {
  const classes = useMemo(
    () => orderWorkloads(new Set(points.map((p) => p.workload))), [points],
  );
  if (!points.length) return null;
  return (
    <div>
      <div className="flex w-full overflow-hidden rounded" style={{ height }}
           role="img" aria-label="Classified workload over time">
        {points.map((p, i) => (
          <div key={`${p.t}-${i}`} title={`${p.workload}`} className="flex-1"
               style={{ background: workloadColor(p.workload), minWidth: 1 }} />
        ))}
      </div>
      <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1">
        {classes.map((k) => (
          <span key={k} className="flex items-center gap-1.5 text-[11px] text-fg-muted">
            <span className="size-2 rounded-sm" style={{ background: workloadColor(k) }} />
            {k.replace(/_/g, " ")}
          </span>
        ))}
      </div>
    </div>
  );
}
