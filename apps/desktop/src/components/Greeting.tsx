import { currentUser } from "../data/books";
import { useState, useEffect } from "react";
import type React from "react";

/* ===== Time-based sun/moon icons ===== */

const MorningSun = ({ size = 22 }: { size?: number }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none">
    <defs>
      <radialGradient id="morningCore" cx="35%" cy="40%" r="65%">
        <stop offset="0%" stopColor="#fff0d0" />
        <stop offset="50%" stopColor="#ffc97a" />
        <stop offset="100%" stopColor="#e8954a" />
      </radialGradient>
    </defs>
    <line x1="12" y1="20" x2="12" y2="22" stroke="#e8954a" strokeWidth="1.5" strokeLinecap="round" opacity="0.5" />
    <line x1="5" y1="20" x2="19" y2="20" stroke="#e8954a" strokeWidth="1.5" strokeLinecap="round" opacity="0.3" />
    {[200, 250, 290, 340].map((deg) => {
      const rad = (deg * Math.PI) / 180;
      const x1 = 12 + 7 * Math.cos(rad);
      const y1 = 12 + 7 * Math.sin(rad);
      const x2 = 12 + 10 * Math.cos(rad);
      const y2 = 12 + 10 * Math.sin(rad);
      return <line key={deg} x1={x1} y1={y1} x2={x2} y2={y2} stroke="#ffc97a" strokeWidth="1.4" strokeLinecap="round" opacity={0.7} />;
    })}
    <circle cx="12" cy="12" r="5" fill="url(#morningCore)" />
    <circle cx="10.5" cy="10.5" r="1.2" fill="#fff0d0" opacity={0.6} />
  </svg>
);

const NoonSun = ({ size = 22 }: { size?: number }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none">
    <defs>
      <radialGradient id="noonCore" cx="35%" cy="35%" r="65%">
        <stop offset="0%" stopColor="#fffae8" />
        <stop offset="40%" stopColor="#ffd966" />
        <stop offset="100%" stopColor="#f5a623" />
      </radialGradient>
    </defs>
    {[0, 45, 90, 135, 180, 225, 270, 315].map((deg) => {
      const rad = (deg * Math.PI) / 180;
      const x1 = 12 + 7 * Math.cos(rad);
      const y1 = 12 + 7 * Math.sin(rad);
      const x2 = 12 + 10.5 * Math.cos(rad);
      const y2 = 12 + 10.5 * Math.sin(rad);
      return <line key={deg} x1={x1} y1={y1} x2={x2} y2={y2} stroke="#ffd966" strokeWidth="1.5" strokeLinecap="round" opacity={0.85} />;
    })}
    <circle cx="12" cy="12" r="5.5" fill="url(#noonCore)" />
    <circle cx="10" cy="10" r="1.5" fill="#fffae8" opacity={0.7} />
  </svg>
);

const AfternoonSun = ({ size = 22 }: { size?: number }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none">
    <defs>
      <radialGradient id="afternoonCore" cx="40%" cy="40%" r="65%">
        <stop offset="0%" stopColor="#ffe8c0" />
        <stop offset="50%" stopColor="#ff9c5a" />
        <stop offset="100%" stopColor="#d96d2a" />
      </radialGradient>
    </defs>
    {[270, 300, 330, 0, 30, 60, 90].map((deg) => {
      const rad = (deg * Math.PI) / 180;
      const x1 = 12 + 7 * Math.cos(rad);
      const y1 = 12 + 7 * Math.sin(rad);
      const x2 = 12 + 10 * Math.cos(rad);
      const y2 = 12 + 10 * Math.sin(rad);
      return <line key={deg} x1={x1} y1={y1} x2={x2} y2={y2} stroke="#ff9c5a" strokeWidth="1.4" strokeLinecap="round" opacity={0.7} />;
    })}
    <circle cx="12" cy="12" r="5" fill="url(#afternoonCore)" />
    <circle cx="11" cy="11" r="1.2" fill="#ffe8c0" opacity={0.5} />
  </svg>
);

const NightMoon = ({ size = 22 }: { size?: number }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none">
    <defs>
      <radialGradient id="moonGrad" cx="35%" cy="35%" r="70%">
        <stop offset="0%" stopColor="#f5f0e8" />
        <stop offset="60%" stopColor="#d4cfc4" />
        <stop offset="100%" stopColor="#a89e94" />
      </radialGradient>
    </defs>
    <path d="M15.5 4.5a8 8 0 1 0 4 11.5 6.5 6.5 0 0 1-4-11.5z" fill="url(#moonGrad)" />
    <circle cx="13" cy="9" r="0.8" fill="#a89e94" opacity="0.4" />
    <circle cx="15.5" cy="12" r="0.6" fill="#a89e94" opacity="0.3" />
    <circle cx="12" cy="13.5" r="0.5" fill="#a89e94" opacity="0.3" />
    <circle cx="19" cy="6" r="0.4" fill="#f5f0e8" opacity="0.6" />
    <circle cx="5" cy="8" r="0.3" fill="#f5f0e8" opacity="0.5" />
  </svg>
);

const MidnightMoon = ({ size = 22 }: { size?: number }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none">
    <defs>
      <radialGradient id="midnightGrad" cx="35%" cy="35%" r="70%">
        <stop offset="0%" stopColor="#e8e2d8" />
        <stop offset="60%" stopColor="#9a9088" />
        <stop offset="100%" stopColor="#5a5248" />
      </radialGradient>
    </defs>
    <path d="M16 3.5a9 9 0 1 0 4.5 13 7 7 0 0 1-4.5-13z" fill="url(#midnightGrad)" />
    <circle cx="13.5" cy="8.5" r="0.7" fill="#5a5248" opacity="0.5" />
    <circle cx="16" cy="12" r="0.5" fill="#5a5248" opacity="0.4" />
    <circle cx="12" cy="14" r="0.4" fill="#5a5248" opacity="0.4" />
    <circle cx="20" cy="5" r="0.35" fill="#e8e2d8" opacity="0.5" />
    <circle cx="4" cy="7" r="0.3" fill="#e8e2d8" opacity="0.4" />
    <circle cx="6" cy="18" r="0.25" fill="#e8e2d8" opacity="0.3" />
  </svg>
);

function getSunIcon(hour: number) {
  if (hour >= 5 && hour < 11) return { Icon: MorningSun, greeting: "Good morning" };
  if (hour >= 11 && hour < 15) return { Icon: NoonSun, greeting: "Good afternoon" };
  if (hour >= 15 && hour < 18) return { Icon: AfternoonSun, greeting: "Good evening" };
  if (hour >= 18 && hour < 22) return { Icon: NightMoon, greeting: "Good night" };
  return { Icon: MidnightMoon, greeting: "Good night" };
}

function TextGenerateEffect({ text, delay = 0 }: { text: string; delay?: number }) {
  const words = text.split(" ");
  const [visibleCount, setVisibleCount] = useState(0);

  useEffect(() => {
  const timers: ReturnType<typeof setTimeout>[] = [];
  words.forEach((_, i) => {
    timers.push(setTimeout(() => setVisibleCount(i + 1), delay + i * 180));
  });
  return () => timers.forEach(clearTimeout);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <span>
      {words.map((word, i) => (
        <span
          key={i}
          style={{
            display: "inline-block",
            opacity: i < visibleCount ? 1 : 0,
            filter: i < visibleCount ? "blur(0px)" : "blur(8px)",
            transition: "opacity 0.4s ease, filter 0.4s ease",
            marginRight: "0.25em",
          }}
        >
          {word}
        </span>
      ))}
    </span>
  );
}

function AnimatedIcon({ Icon, size, delay }: { Icon: React.FC<{ size?: number }>; size: number; delay: number }) {
  const [show, setShow] = useState(false);

  useEffect(() => {
    const t = setTimeout(() => setShow(true), delay);
    return () => clearTimeout(t);
  }, [delay]);

  return (
    <span
      style={{
        display: "inline-flex",
        opacity: show ? 1 : 0,
        filter: show ? "blur(0px)" : "blur(8px)",
        transform: show ? "scale(1)" : "scale(0.8)",
        transition: "opacity 0.5s ease, filter 0.5s ease, transform 0.5s cubic-bezier(0.34, 1.56, 0.64, 1)",
      }}
    >
      <Icon size={size} />
    </span>
  );
}

export default function Greeting() {
  const hour = new Date().getHours();
  const { Icon, greeting } = getSunIcon(hour);
  const greetingWords = `${greeting}, ${currentUser.name}`.split(" ");
  const iconDelay = greetingWords.length * 180;

  return (
    <div className="animate-fade-in">
      <div className="flex items-center gap-2 mb-0.5">
        <h2 className="font-serif text-xl font-semibold text-kinora-text tracking-wide">
          <TextGenerateEffect text={`${greeting}, ${currentUser.name}`} />
        </h2>
        <AnimatedIcon Icon={Icon} size={22} delay={iconDelay} />
      </div>
      <p className="text-kinora-muted text-[12px]">
        <TextGenerateEffect text="Pick up where you left off or discover something new." delay={iconDelay + 200} />
      </p>
    </div>
  );
}
