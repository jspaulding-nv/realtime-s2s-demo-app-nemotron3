import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ReferenceLine,
  ResponsiveContainer,
} from 'recharts';
import type { DriftDataPoint } from '../types/timing';

interface DriftChartProps {
  data: DriftDataPoint[];
}

export function DriftChart({ data }: DriftChartProps) {
  return (
    <ResponsiveContainer width="100%" height={300}>
      <LineChart data={data} margin={{ top: 5, right: 20, left: 10, bottom: 5 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#e5e7eb" />
        <XAxis
          dataKey="elapsedMinutes"
          label={{ value: 'Elapsed (min)', position: 'insideBottom', offset: -2 }}
          stroke="#6b7280"
          fontSize={12}
        />
        <YAxis
          label={{ value: 'Legacy Drift (s)', angle: -90, position: 'insideLeft' }}
          stroke="#6b7280"
          fontSize={12}
        />
        <Tooltip
          formatter={(value: number | undefined) => [
            `${(value ?? 0).toFixed(2)}s`,
            'Legacy Duration Drift',
          ]}
          labelFormatter={(label) => `${Number(label).toFixed(1)} min`}
        />
        <ReferenceLine y={20} stroke="#f59e0b" strokeDasharray="6 3" label="Warning (20s)" />
        <ReferenceLine y={30} stroke="#ef4444" strokeDasharray="6 3" label="Danger (30s)" />
        <Line
          type="monotone"
          dataKey="driftSeconds"
          stroke="#3b82f6"
          dot={false}
          isAnimationActive={false}
          name="Legacy Duration Drift"
          strokeWidth={2}
        />
      </LineChart>
    </ResponsiveContainer>
  );
}
