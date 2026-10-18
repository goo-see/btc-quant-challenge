import { useEffect, useRef } from 'react';
import { createChart, type IChartApi, type UTCTimestamp, ColorType } from 'lightweight-charts';
import type { EquityPoint } from '../api/client';

export function EquityChart({ points, initial }: { points: EquityPoint[]; initial: number }) {
  const ref = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  useEffect(() => {
    if (!ref.current) return;
    const chart = createChart(ref.current, {
      layout: { background: { type: ColorType.Solid, color: 'transparent' }, textColor: '#8b95a9' },
      grid: { vertLines: { color: '#1a2030' }, horzLines: { color: '#1a2030' } },
      rightPriceScale: { borderColor: '#232b3d' }, timeScale: { borderColor: '#232b3d', timeVisible: true },
      autoSize: true, handleScroll: false, handleScale: false,
    });
    chartRef.current = chart;
    const area = chart.addAreaSeries({ lineColor: '#60a5fa', topColor: 'rgba(96,165,250,.35)', bottomColor: 'rgba(96,165,250,.02)', lineWidth: 2, priceFormat: { type: 'custom', formatter: (p: number) => `₩${Math.round(p).toLocaleString()}` } });
    const base = chart.addLineSeries({ color: '#8b95a9', lineWidth: 1, lineStyle: 2, priceLineVisible: false, lastValueVisible: false });
    const data = points.map((p) => ({ time: p.time as UTCTimestamp, value: p.equity })).filter((d, i, a) => i === 0 || d.time > a[i - 1].time);
    area.setData(data);
    if (data.length) base.setData([{ time: data[0].time, value: initial }, { time: data[data.length - 1].time, value: initial }]);
    chart.timeScale().fitContent();
    return () => chart.remove();
  }, [points, initial]);
  return <div ref={ref} className="chart" />;
}
