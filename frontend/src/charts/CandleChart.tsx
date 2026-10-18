import { useEffect, useRef } from 'react';
import { createChart, ColorType, type UTCTimestamp, type SeriesMarker, type Time } from 'lightweight-charts';
import type { Candles } from '../api/client';

export function CandleChart({ data }: { data: Candles }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!ref.current) return;
    const chart = createChart(ref.current, {
      layout: { background: { type: ColorType.Solid, color: 'transparent' }, textColor: '#8b95a9' },
      grid: { vertLines: { color: '#1a2030' }, horzLines: { color: '#1a2030' } },
      rightPriceScale: { borderColor: '#232b3d' }, timeScale: { borderColor: '#232b3d', timeVisible: true },
      autoSize: true,
    });
    const series = chart.addCandlestickSeries({ upColor: '#22c55e', downColor: '#ef4444', wickUpColor: '#22c55e', wickDownColor: '#ef4444', borderVisible: false, priceFormat: { type: 'custom', formatter: (p: number) => `₩${Math.round(p).toLocaleString()}` } });
    const candles = data.candles.map((c) => ({ time: c.time as UTCTimestamp, open: c.open, high: c.high, low: c.low, close: c.close }));
    series.setData(candles);
    const bucket = (t: number) => (candles.find((c) => c.time >= t)?.time ?? candles[candles.length - 1]?.time) as UTCTimestamp;
    const markers: SeriesMarker<Time>[] = data.markers.map((m) => ({
      time: bucket(m.time), position: m.side === 'BUY' ? 'belowBar' : 'aboveBar', color: m.side === 'BUY' ? '#22c55e' : '#ef4444',
      shape: m.side === 'BUY' ? 'arrowUp' : 'arrowDown', text: `${m.side} ${Math.round(m.price).toLocaleString()}`,
    }));
    if (markers.length) series.setMarkers(markers);
    if (data.open_position) series.createPriceLine({ price: data.open_position.entry_price, color: '#f59e0b', lineWidth: 1, lineStyle: 2, title: 'ENTRY' });
    chart.timeScale().fitContent();
    return () => chart.remove();
  }, [data]);
  return <div ref={ref} className="chart" />;
}
