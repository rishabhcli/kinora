export function BrandMark({ className = "h-8 w-8" }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 32 32"
      className={className}
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      aria-hidden="true"
    >
      <defs>
        <linearGradient id="brand-bg" x1="4" y1="3" x2="28" y2="29" gradientUnits="userSpaceOnUse">
          <stop stopColor="#8b6dff" />
          <stop offset="1" stopColor="#4c1d95" />
        </linearGradient>
      </defs>
      <rect x="2" y="2" width="28" height="28" rx="8" fill="url(#brand-bg)" />
      <path
        d="M6.5 9.5C6.5 8.4 7.4 7.6 8.5 7.8L11 8.3V24.2L8.5 24.7C7.4 24.9 6.5 24.1 6.5 23V9.5Z"
        fill="#ffffff"
        fillOpacity="0.3"
      />
      <path
        d="M13.2 11.1C13.2 10 14.4 9.3 15.4 9.8L22.8 13.9C23.8 14.5 23.8 15.9 22.8 16.5L15.4 20.6C14.4 21.1 13.2 20.4 13.2 19.3V11.1Z"
        fill="#ffffff"
      />
    </svg>
  );
}

export function Wordmark({ className = "" }: { className?: string }) {
  return (
    <span className={`inline-flex items-center gap-2.5 ${className}`}>
      <BrandMark className="h-7 w-7" />
      <span className="text-base font-semibold tracking-tight text-kinora-mist">Kinora</span>
    </span>
  );
}
