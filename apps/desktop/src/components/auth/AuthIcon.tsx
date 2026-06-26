// Icon shim for the auth screen. UI glyphs route to lucide-react; the three
// social brand marks are inline SVG (brand colours / exact paths). This is the
// Agent 9 `<Icon>` seam — at integration it swaps to the shared icon component
// 1:1 (same names): google · apple · github · mail · lock · eye · eye-off ·
// check · arrow-right · loader · alert.
import {
  Mail,
  Lock,
  Eye,
  EyeOff,
  Check,
  ArrowRight,
  Loader2,
  AlertCircle,
  type LucideIcon,
} from "lucide-react";

export type AuthIconName =
  | "google"
  | "apple"
  | "github"
  | "mail"
  | "lock"
  | "eye"
  | "eye-off"
  | "check"
  | "arrow-right"
  | "loader"
  | "alert";

const LUCIDE: Partial<Record<AuthIconName, LucideIcon>> = {
  mail: Mail,
  lock: Lock,
  eye: Eye,
  "eye-off": EyeOff,
  check: Check,
  "arrow-right": ArrowRight,
  loader: Loader2,
  alert: AlertCircle,
};

interface Props {
  name: AuthIconName;
  size?: number;
  className?: string;
  /** brand marks render in their own colours; set false to force currentColor */
  brand?: boolean;
}

export default function AuthIcon({ name, size = 18, className, brand = true }: Props) {
  const Lucide = LUCIDE[name];
  if (Lucide) {
    return <Lucide size={size} strokeWidth={1.75} className={className} aria-hidden="true" />;
  }

  const common = {
    width: size,
    height: size,
    viewBox: "0 0 24 24",
    className,
    "aria-hidden": true as const,
    focusable: false as const,
  };

  if (name === "google") {
    return brand ? (
      <svg {...common} fill="none">
        <path d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z" fill="#4285F4" />
        <path d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z" fill="#34A853" />
        <path d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z" fill="#FBBC05" />
        <path d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z" fill="#EA4335" />
      </svg>
    ) : (
      <svg {...common} fill="currentColor"><path d="M12 11v3.3h4.7c-.2 1.2-1.5 3.6-4.7 3.6-2.8 0-5.1-2.3-5.1-5.2S9.2 6.5 12 6.5c1.6 0 2.7.7 3.3 1.3l2.3-2.2C16.1 4.2 14.2 3.4 12 3.4 7.3 3.4 3.5 7.2 3.5 12s3.8 8.6 8.5 8.6c4.9 0 8.1-3.4 8.1-8.3 0-.6 0-1-.1-1.3H12z" /></svg>
    );
  }
  if (name === "apple") {
    return (
      <svg {...common} fill="currentColor">
        <path d="M17.05 20.28c-.98.95-2.05.8-3.08.35-1.09-.46-2.09-.48-3.24 0-1.44.62-2.2.44-3.06-.35C2.79 15.25 3.51 7.59 9.05 7.31c1.35.07 2.29.74 3.08.8 1.18-.24 2.31-.93 3.57-.84 1.51.12 2.65.72 3.4 1.8-3.12 1.87-2.38 5.98.48 7.13-.57 1.5-1.31 2.99-2.54 4.09l.01-.01zM12.03 7.25c-.15-2.23 1.66-4.07 3.74-4.25.29 2.58-2.34 4.5-3.74 4.25z" />
      </svg>
    );
  }
  // github (fallback): lucide Github is a glyph, but the filled mark reads better here
  return (
    <svg {...common} fill="currentColor">
      <path d="M12 2C6.48 2 2 6.48 2 12c0 4.42 2.87 8.17 6.84 9.5.5.09.66-.22.66-.48v-1.7c-2.78.6-3.37-1.34-3.37-1.34-.45-1.15-1.11-1.46-1.11-1.46-.91-.62.07-.6.07-.6 1 .07 1.53 1.03 1.53 1.03.89 1.52 2.34 1.08 2.91.83.09-.65.35-1.09.63-1.34-2.22-.25-4.55-1.11-4.55-4.94 0-1.09.39-1.98 1.03-2.68-.1-.25-.45-1.27.1-2.64 0 0 .84-.27 2.75 1.02.8-.22 1.65-.33 2.5-.33.85 0 1.7.11 2.5.33 1.91-1.29 2.75-1.02 2.75-1.02.55 1.37.2 2.39.1 2.64.64.7 1.03 1.59 1.03 2.68 0 3.84-2.34 4.68-4.57 4.93.36.31.68.92.68 1.85V21c0 .27.16.58.67.48C19.13 20.17 22 16.42 22 12c0-5.52-4.48-10-10-10z" />
    </svg>
  );
}
