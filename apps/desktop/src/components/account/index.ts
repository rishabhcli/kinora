// Barrel for the account-management surface. The page host imports AccountPage;
// the sections + primitives are exported for direct embedding or testing.
export { default as AccountPage } from "./AccountPage";
export { default as ProfileSection } from "./ProfileSection";
export { default as SecuritySection } from "./SecuritySection";
export { default as SessionsSection } from "./SessionsSection";
export { default as BillingSection } from "./BillingSection";
export { default as PreferencesSection } from "./PreferencesSection";
export { default as MfaEnrollDialog } from "./MfaEnrollDialog";
export { default as PasskeysCard } from "./PasskeysCard";
export { default as RecentActivityCard } from "./RecentActivityCard";
export { default as DangerZone } from "./DangerZone";
export { default as UsageCard } from "./UsageCard";
export { Avatar, Toggle, Segmented, Section } from "./primitives";
