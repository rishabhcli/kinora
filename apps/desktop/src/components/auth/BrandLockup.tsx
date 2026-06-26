// The Kinora brand lockup: mark + Fraunces wordmark. Reused in the brand rail and
// (compact) above the card on narrow widths. The cold-launch entrance animation is
// applied by the parent (login-intro classes); this just lays out the lockup.
import logoUrl from "../../assets/logo.svg";

export default function BrandLockup({
  size = "md",
  className,
}: {
  size?: "sm" | "md";
  className?: string;
}) {
  const px = size === "sm" ? 26 : 34;
  return (
    <div className={`auth-lockup auth-lockup--${size}${className ? ` ${className}` : ""}`}>
      <img src={logoUrl} alt="" width={px} height={px} className="auth-lockup-mark" />
      <span className="auth-lockup-word">Kinora</span>
    </div>
  );
}
