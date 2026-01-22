interface ControlButtonProps {
  isActive: boolean;
  onClick: () => void;
  disabled?: boolean;
}

export function ControlButton({
  isActive,
  onClick,
  disabled = false,
}: ControlButtonProps) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className={`
        relative w-20 h-20 rounded-full
        transition-all duration-200
        focus:outline-none focus:ring-4 focus:ring-blue-300
        disabled:opacity-50 disabled:cursor-not-allowed
        ${
          isActive
            ? 'bg-red-500 hover:bg-red-600 shadow-lg shadow-red-200'
            : 'bg-blue-500 hover:bg-blue-600 shadow-lg shadow-blue-200'
        }
      `}
      aria-label={isActive ? 'Stop translation' : 'Start translation'}
    >
      {isActive ? (
        // Stop icon (square)
        <div className="absolute inset-0 flex items-center justify-center">
          <div className="w-6 h-6 bg-white rounded-sm" />
        </div>
      ) : (
        // Microphone icon
        <div className="absolute inset-0 flex items-center justify-center">
          <svg
            className="w-8 h-8 text-white"
            fill="currentColor"
            viewBox="0 0 24 24"
          >
            <path d="M12 14c1.66 0 3-1.34 3-3V5c0-1.66-1.34-3-3-3S9 3.34 9 5v6c0 1.66 1.34 3 3 3z" />
            <path d="M17 11c0 2.76-2.24 5-5 5s-5-2.24-5-5H5c0 3.53 2.61 6.43 6 6.92V21h2v-3.08c3.39-.49 6-3.39 6-6.92h-2z" />
          </svg>
        </div>
      )}

      {/* Pulsing ring when active */}
      {isActive && (
        <div className="absolute inset-0 rounded-full animate-ping bg-red-400 opacity-20" />
      )}
    </button>
  );
}
