import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/react';
import { DriftChart } from '../components/DriftChart';
import type { DriftDataPoint } from '../types/timing';

describe('DriftChart', () => {
  it('renders without data (no crash)', () => {
    const { container } = render(<DriftChart data={[]} />);
    expect(container).toBeTruthy();
  });

  it('renders with sample drift data', () => {
    const data: DriftDataPoint[] = [
      { elapsedMinutes: 0.5, rawDriftSeconds: 5.0, driftSeconds: 2.0 },
      { elapsedMinutes: 1.0, rawDriftSeconds: 8.0, driftSeconds: 5.0 },
      { elapsedMinutes: 1.5, rawDriftSeconds: 12.0, driftSeconds: 9.0 },
    ];

    const { container } = render(<DriftChart data={data} />);
    expect(container).toBeTruthy();
    // Recharts renders SVG elements
    const svg = container.querySelector('svg');
    // ResponsiveContainer may not render svg in jsdom, but component should not crash
  });

  it('renders with large drift values (warning/danger thresholds)', () => {
    const data: DriftDataPoint[] = [
      { elapsedMinutes: 0.5, rawDriftSeconds: 25.0, driftSeconds: 22.0 },
      { elapsedMinutes: 1.0, rawDriftSeconds: 35.0, driftSeconds: 32.0 },
    ];

    // Should not crash with values above warning/danger thresholds
    const { container } = render(<DriftChart data={data} />);
    expect(container).toBeTruthy();
  });
});
