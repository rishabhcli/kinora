import { createMediaPref, type PrefOverride } from "./mediaPref";

// Two more OS-or-override display preferences that A11yProvider reflects onto
// <html> (so CSS in a11y.css can react) and ReadingControls exposes as toggles.

export type DisplayOverride = PrefOverride;

const highContrast = createMediaPref({
  media: "(prefers-contrast: more)",
  storageKey: "kinora.highContrast",
  onValue: "more",
  offValue: "no-preference",
});

const reducedTransparency = createMediaPref({
  media: "(prefers-reduced-transparency: reduce)",
  storageKey: "kinora.reduceTransparency",
  onValue: "reduce",
  offValue: "full",
});

// High contrast
export const useHighContrastPref = (): boolean => highContrast.use();
export const getHighContrastSnapshot = (): boolean => highContrast.getSnapshot();
export const getHighContrastOverride = (): DisplayOverride => highContrast.getOverride();
export const setHighContrastOverride = (v: DisplayOverride): void => highContrast.setOverride(v);

// Reduced transparency
export const useReducedTransparencyPref = (): boolean => reducedTransparency.use();
export const getReducedTransparencySnapshot = (): boolean => reducedTransparency.getSnapshot();
export const getReducedTransparencyOverride = (): DisplayOverride => reducedTransparency.getOverride();
export const setReducedTransparencyOverride = (v: DisplayOverride): void =>
  reducedTransparency.setOverride(v);
