interface AudioVisualizerProps {
  level: number; // 0.0 to 1.0
  bars?: number;
  isActive?: boolean;
}

export function AudioVisualizer({
  level,
  bars = 20,
  isActive = false,
}: AudioVisualizerProps) {
  const activeBars = Math.round(level * bars);

  return (
    <div className="flex items-end justify-center gap-1 h-12">
      {Array.from({ length: bars }).map((_, i) => {
        const isBarActive = i < activeBars;
        // Create gradient from green to yellow to red
        let barColor = 'bg-gray-300';
        if (isBarActive && isActive) {
          if (i < bars * 0.5) {
            barColor = 'bg-green-500';
          } else if (i < bars * 0.75) {
            barColor = 'bg-yellow-500';
          } else {
            barColor = 'bg-red-500';
          }
        }

        // Vary bar heights for visual interest
        const baseHeight = 0.3 + (Math.sin((i / bars) * Math.PI) * 0.7);

        return (
          <div
            key={i}
            className={`w-1.5 rounded-sm transition-all duration-75 ${barColor}`}
            style={{
              height: isBarActive && isActive
                ? `${Math.max(8, baseHeight * 48)}px`
                : '8px',
            }}
          />
        );
      })}
    </div>
  );
}
