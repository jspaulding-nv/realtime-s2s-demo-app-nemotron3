import type { SessionStatus } from '../types/messages';

interface StatusIndicatorProps {
  status: SessionStatus;
}

const statusConfig: Record<
  SessionStatus,
  { label: string; color: string; pulse: boolean }
> = {
  disconnected: { label: 'Disconnected', color: 'bg-gray-400', pulse: false },
  connected: { label: 'Connected', color: 'bg-blue-500', pulse: false },
  listening: { label: 'Listening', color: 'bg-green-500', pulse: true },
  processing: { label: 'Processing', color: 'bg-yellow-500', pulse: true },
  stopped: { label: 'Stopped', color: 'bg-gray-500', pulse: false },
  error: { label: 'Error', color: 'bg-red-500', pulse: false },
};

export function StatusIndicator({ status }: StatusIndicatorProps) {
  const config = statusConfig[status];

  return (
    <div className="flex items-center gap-2">
      <div className="relative">
        <div className={`w-3 h-3 rounded-full ${config.color}`} />
        {config.pulse && (
          <div
            className={`absolute inset-0 w-3 h-3 rounded-full ${config.color} animate-ping opacity-75`}
          />
        )}
      </div>
      <span className="text-sm font-medium text-gray-600">{config.label}</span>
    </div>
  );
}
