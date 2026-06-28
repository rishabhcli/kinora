import { type ComponentType, useEffect, useId, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ParseKeys } from "i18next";
import { useSettings } from "../../lib/useSettings";
import { SUPPORTED_LANGUAGES, currentLanguage, setLanguage, type LanguageCode } from "../../i18n";
import {
  useReadingPrefs,
  READING_THEMES,
  READING_SPACINGS,
  READING_FONTS,
  READING_BOUNDS,
  clampPref,
  type ReadingTheme,
  type ReadingSpacing,
  type ReadingFontFamily,
} from "../../lib/readingPrefs";
import { api } from "../../lib/api";
import { diffFromDefaults, type SystemOverride } from "../../lib/settings";
import { settingsStore } from "../../lib/settings";
import { Icon, type IconName } from "../icons";
import { Row, RowButton, SectionTitle, Segmented, Select, SettingsGroup, Slider, Switch } from "./controls";
import ProfileEditor from "./ProfileEditor";

/** Override segmented options — labels are translated at render in each section. */
const OVERRIDE_VALUES: SystemOverride[] = ["system", "on", "off"];

function useVoices(): SpeechSynthesisVoice[] {
  const [voices, setVoices] = useState<SpeechSynthesisVoice[]>([]);
  useEffect(() => {
    if (typeof window === "undefined" || !window.speechSynthesis) return;
    const read = () => setVoices(window.speechSynthesis.getVoices());
    read();
    window.speechSynthesis.addEventListener?.("voiceschanged", read);
    return () => window.speechSynthesis.removeEventListener?.("voiceschanged", read);
  }, []);
  return voices;
}

/* ── General ────────────────────────────────────────────────────────────── */
function GeneralSection() {
  const { t } = useTranslation();
  const { settings, set, reset } = useSettings();
  // NOTE: launchView values are the English page-identity strings ("Home", …)
  // that HomePage's `pages` map keys off — only the displayed label is translated.
  const launchOpts: { value: typeof settings.launchView; label: string }[] = [
    { value: "Home", label: t("nav.home") },
    { value: "Library", label: t("nav.library") },
    { value: "Watch", label: t("nav.watch") },
    { value: "Favorites", label: t("nav.favorites") },
    { value: "Notes", label: t("nav.notes") },
  ];
  const langOpts = SUPPORTED_LANGUAGES.map((l) => ({ value: l.code, label: l.name }));
  return (
    <div>
      <SectionTitle icon="gearshape" title={t("settings.sections.general")} subtitle={t("settings.general.subtitle")} />
      <SettingsGroup>
        <Row icon="globe" label={t("language.name")} description={t("language.description")}>
          <Select
            value={currentLanguage()}
            options={langOpts}
            onChange={(v) => setLanguage(v as LanguageCode)}
            ariaLabel={t("language.name")}
          />
        </Row>
        <Row icon="house" label={t("settings.general.openTo")} description={t("settings.general.openToDesc")}>
          <Segmented value={settings.launchView} options={launchOpts} onChange={(v) => set({ launchView: v })} ariaLabel={t("settings.general.openTo")} />
        </Row>
        <Row icon="bell" label={t("settings.general.soundEffects")} description={t("settings.general.soundEffectsDesc")}>
          <Switch checked={settings.soundEffects} onChange={(v) => set({ soundEffects: v })} label={t("settings.general.soundEffects")} />
        </Row>
      </SettingsGroup>
      <SettingsGroup title={t("settings.general.reset")}>
        <Row icon="arrow.counterclockwise" label={t("settings.general.restoreDefaults")} description={t("settings.general.restoreDefaultsDesc")}>
          <RowButton
            tone="accent"
            icon="arrow.counterclockwise"
            onClick={() => {
              if (window.confirm(t("settings.general.restoreConfirm"))) reset();
            }}
          >
            {t("common.restore")}
          </RowButton>
        </Row>
      </SettingsGroup>
    </div>
  );
}

/* ── Appearance ─────────────────────────────────────────────────────────── */
function AppearanceSection() {
  const { t } = useTranslation();
  const { settings, set } = useSettings();
  const overrideOpts = OVERRIDE_VALUES.map((value) => ({
    value,
    label: t(`settings.override.${value}` as const),
  }));
  return (
    <div>
      <SectionTitle icon="paintbrush" title={t("settings.sections.appearance")} subtitle={t("settings.appearance.subtitle")} />
      <SettingsGroup>
        <Row icon="sparkles" label={t("settings.appearance.reduceMotion")} description={t("settings.appearance.reduceMotionDesc")}>
          <Segmented value={settings.reduceMotion} options={overrideOpts} onChange={(v) => set({ reduceMotion: v })} ariaLabel={t("settings.appearance.reduceMotion")} />
        </Row>
        <Row icon="circle.lefthalf.filled" label={t("settings.appearance.reduceTransparency")} description={t("settings.appearance.reduceTransparencyDesc")}>
          <Segmented value={settings.reduceTransparency} options={overrideOpts} onChange={(v) => set({ reduceTransparency: v })} ariaLabel={t("settings.appearance.reduceTransparency")} />
        </Row>
        <Row icon="eye" label={t("settings.appearance.increaseContrast")} description={t("settings.appearance.increaseContrastDesc")}>
          <Segmented value={settings.increaseContrast} options={overrideOpts} onChange={(v) => set({ increaseContrast: v })} ariaLabel={t("settings.appearance.increaseContrast")} />
        </Row>
      </SettingsGroup>
      <p className="text-[11.5px] text-kinora-subtle ml-1 -mt-2">
        {t("settings.appearance.themesHint")}
      </p>
    </div>
  );
}

/* ── Reading — composes Agent 6's useReadingPrefs (shared, not duplicated) ── */
function ReadingSection() {
  const { prefs, update } = useReadingPrefs();
  const voices = useVoices();
  const voiceId = useId();
  const bounds = READING_BOUNDS;
  return (
    <div>
      <SectionTitle icon="textformat" title="Reading" subtitle="Book text, comfort, and read-aloud preferences." />
      <SettingsGroup title="Theme">
        <div className="px-3.5 py-3 flex flex-wrap gap-2">
          {(Object.entries(READING_THEMES) as [ReadingTheme, (typeof READING_THEMES)[ReadingTheme]][]).map(
            ([key, t]) => {
              const active = prefs.theme === key;
              return (
                <button
                  key={key}
                  onClick={() => update({ theme: key })}
                  aria-pressed={active}
                  className="kn-set-focusable flex flex-col items-center gap-1.5"
                >
                  <span
                    className="rounded-xl"
                    style={{
                      width: 52,
                      height: 38,
                      background: t.swatch,
                      border: active ? "2px solid #d4a44e" : "1px solid rgba(255,255,255,0.14)",
                      boxShadow: active ? "0 0 0 3px rgba(212,164,78,0.2)" : "none",
                    }}
                  />
                  <span className={`text-[11px] ${active ? "text-kinora-text" : "text-kinora-muted"}`}>{t.label}</span>
                </button>
              );
            },
          )}
        </div>
      </SettingsGroup>
      <SettingsGroup title="Text">
        <Row icon="textformat" label="Reading font">
          <Segmented
            value={prefs.fontFamily}
            options={(Object.keys(READING_FONTS) as ReadingFontFamily[]).map((f) => ({
              value: f,
              label: READING_FONTS[f].label,
            }))}
            onChange={(v) => update({ fontFamily: v })}
            ariaLabel="Reading font"
          />
        </Row>
        <Row icon="textformat.size" label="Font size" htmlFor="rd-font">
          <Slider
            id="rd-font"
            label="Font size"
            min={bounds.fontScale.min}
            max={bounds.fontScale.max}
            step={bounds.fontScale.step}
            value={prefs.fontScale}
            onChange={(v) => update({ fontScale: clampPref(v, bounds.fontScale.min, bounds.fontScale.max) })}
            format={(v) => `${Math.round(v * 100)}%`}
          />
        </Row>
        <Row icon="text.justify" label="Line spacing" htmlFor="rd-lead">
          <Slider
            id="rd-lead"
            label="Line spacing"
            min={bounds.leading.min}
            max={bounds.leading.max}
            step={bounds.leading.step}
            value={prefs.leading}
            onChange={(v) => update({ leading: clampPref(v, bounds.leading.min, bounds.leading.max) })}
            format={(v) => v.toFixed(2)}
          />
        </Row>
        <Row icon="textformat.alt" label="Line width" htmlFor="rd-measure">
          <Slider
            id="rd-measure"
            label="Line width"
            min={bounds.measure.min}
            max={bounds.measure.max}
            step={bounds.measure.step}
            value={prefs.measure}
            onChange={(v) => update({ measure: clampPref(Math.round(v), bounds.measure.min, bounds.measure.max) })}
            format={(v) => `${Math.round(v)}ch`}
          />
        </Row>
        <Row icon="textformat" label="Letter spacing" description="The real dyslexia comfort lever.">
          <Segmented
            value={prefs.spacing}
            options={(Object.keys(READING_SPACINGS) as ReadingSpacing[]).map((s) => ({
              value: s,
              label: READING_SPACINGS[s].label,
            }))}
            onChange={(v) => update({ spacing: v })}
            ariaLabel="Letter spacing"
          />
        </Row>
      </SettingsGroup>
      <SettingsGroup title="Display">
        <Row icon="sun.max" label="Brightness" htmlFor="rd-brightness">
          <Slider
            id="rd-brightness"
            label="Brightness"
            min={bounds.brightness.min}
            max={bounds.brightness.max}
            step={bounds.brightness.step}
            value={prefs.brightness}
            onChange={(v) => update({ brightness: clampPref(v, bounds.brightness.min, bounds.brightness.max) })}
            format={(v) => `${Math.round(v * 100)}%`}
          />
        </Row>
        <Row icon="moon.stars" label="Auto Night" description="Switch to the Night theme between 7 PM and 7 AM.">
          <Switch checked={prefs.autoNight} onChange={(v) => update({ autoNight: v })} label="Auto Night" />
        </Row>
      </SettingsGroup>
      <SettingsGroup title="Read aloud">
        <Row icon="speaker.wave.2.fill" label="Voice" htmlFor={voiceId}>
          <select
            id={voiceId}
            value={prefs.ttsVoiceURI ?? ""}
            onChange={(e) => update({ ttsVoiceURI: e.target.value || null })}
            className="kn-set-focusable min-h-9 max-w-[220px] rounded-lg border border-white/10 bg-[#191511] px-2.5 py-1.5 text-sm text-kinora-text outline-none"
          >
            <option value="">System default</option>
            {voices.map((v) => (
              <option key={v.voiceURI} value={v.voiceURI}>
                {v.name}
              </option>
            ))}
          </select>
        </Row>
        <Row icon="speaker.wave.2.fill" label="Read-aloud speed" htmlFor="rd-tts-rate">
          <Slider
            id="rd-tts-rate"
            label="Read-aloud speed"
            min={bounds.ttsRate.min}
            max={bounds.ttsRate.max}
            step={bounds.ttsRate.step}
            value={prefs.ttsRate}
            onChange={(v) => update({ ttsRate: clampPref(v, bounds.ttsRate.min, bounds.ttsRate.max) })}
            format={(v) => `${v.toFixed(1)}×`}
          />
        </Row>
      </SettingsGroup>
    </div>
  );
}

/* ── Playback / Film ────────────────────────────────────────────────────── */
function PlaybackSection() {
  const { settings, set } = useSettings();
  return (
    <div>
      <SectionTitle icon="film" title="Playback" subtitle="How the page-synced film plays in the reading room." />
      <SettingsGroup>
        <Row icon="play.rectangle" label="Autoplay film" description="Start the film as soon as a book opens.">
          <Switch checked={settings.autoplayFilm} onChange={(v) => set({ autoplayFilm: v })} label="Autoplay film" />
        </Row>
        <Row icon="captions.bubble" label="Captions" description="Show the read-along text over the film.">
          <Switch checked={settings.captions} onChange={(v) => set({ captions: v })} label="Captions" />
        </Row>
        <Row icon="slider.horizontal.3" label="Scrub sensitivity" description="How fast scrolling moves the playhead." htmlFor="pb-scrub">
          <Slider
            id="pb-scrub"
            label="Scrub sensitivity"
            min={0.5}
            max={2}
            step={0.05}
            value={settings.scrubSensitivity}
            onChange={(v) => set({ scrubSensitivity: v })}
            format={(v) => `${v.toFixed(2)}×`}
          />
        </Row>
      </SettingsGroup>
      <p className="text-[11.5px] text-kinora-subtle ml-1 -mt-2">Applies to the reading-room film player.</p>
    </div>
  );
}

/* ── Notifications ──────────────────────────────────────────────────────── */
function NotificationsSection() {
  const { settings, set } = useSettings();
  const supported = typeof window !== "undefined" && "Notification" in window;
  const [perm, setPerm] = useState<NotificationPermission | "unsupported">(
    supported ? Notification.permission : "unsupported",
  );

  const enable = async (on: boolean) => {
    set({ notificationsEnabled: on });
    if (on && supported && Notification.permission === "default") {
      const p = await Notification.requestPermission();
      setPerm(p);
    }
  };
  const sendTest = () => {
    if (!supported) return;
    if (Notification.permission === "granted") {
      new Notification("Kinora", { body: "Notifications are working — enjoy your reading." });
    } else {
      Notification.requestPermission().then((p) => {
        setPerm(p);
        if (p === "granted") new Notification("Kinora", { body: "Notifications enabled." });
      });
    }
  };

  return (
    <div>
      <SectionTitle icon="bell" title="Notifications" subtitle="Reminders and updates from Kinora." />
      <SettingsGroup>
        <Row icon="bell.fill" label="Enable notifications" description={supported ? `Permission: ${perm}` : "Not supported on this platform."}>
          <Switch checked={settings.notificationsEnabled} onChange={enable} label="Enable notifications" />
        </Row>
        <Row icon="clock" label="Reading reminders" description="A nudge to keep your streak going.">
          <Switch checked={settings.readingReminders} onChange={(v) => set({ readingReminders: v })} label="Reading reminders" />
        </Row>
        <Row icon="envelope" label="Weekly digest" description="A summary of your reading each week.">
          <Switch checked={settings.weeklyDigest} onChange={(v) => set({ weeklyDigest: v })} label="Weekly digest" />
        </Row>
      </SettingsGroup>
      <SettingsGroup>
        <Row icon="sparkles" label="Test notification" description="Send a sample notification right now.">
          <RowButton icon="bell" onClick={sendTest}>
            Send test
          </RowButton>
        </Row>
      </SettingsGroup>
    </div>
  );
}

/* ── Privacy ────────────────────────────────────────────────────────────── */
function PrivacySection() {
  const { settings, set } = useSettings();
  const [cleared, setCleared] = useState(false);

  const clearLocal = () => {
    if (!window.confirm("Clear local reading data (preferences, profile, cached positions)? You'll stay signed in.")) {
      return;
    }
    try {
      const keep = new Set(["kinora.token", "kinora.settings"]);
      const toRemove: string[] = [];
      for (let i = 0; i < localStorage.length; i++) {
        const k = localStorage.key(i);
        if (k && k.startsWith("kinora.") && !keep.has(k)) toRemove.push(k);
      }
      toRemove.forEach((k) => localStorage.removeItem(k));
      setCleared(true);
      window.setTimeout(() => setCleared(false), 2200);
    } catch {
      /* storage blocked */
    }
  };

  return (
    <div>
      <SectionTitle icon="lock.shield" title="Privacy" subtitle="You're in control of your data." />
      <SettingsGroup>
        <Row icon="hand.raised" label="Usage analytics" description="Share anonymous usage to help improve Kinora.">
          <Switch checked={settings.analytics} onChange={(v) => set({ analytics: v })} label="Usage analytics" />
        </Row>
        <Row icon="exclamationmark.triangle" label="Crash reports" description="Automatically send diagnostics after a crash.">
          <Switch checked={settings.crashReports} onChange={(v) => set({ crashReports: v })} label="Crash reports" />
        </Row>
      </SettingsGroup>
      <SettingsGroup title="Local data">
        <Row icon="trash" label="Clear local reading data" description="Removes preferences, profile and cached positions from this device.">
          <RowButton tone="danger" icon="trash" onClick={clearLocal}>
            {cleared ? "Cleared" : "Clear"}
          </RowButton>
        </Row>
      </SettingsGroup>
    </div>
  );
}

/* ── Account ────────────────────────────────────────────────────────────── */
function AccountSection() {
  const signOut = () => {
    if (!window.confirm("Sign out of Kinora?")) return;
    api.logout();
    window.location.reload();
  };
  return (
    <div>
      <SectionTitle icon="person.crop.circle" title="Account" subtitle="Your profile and session." />
      <div
        className="rounded-2xl p-4 mb-6"
        style={{ background: "rgba(255,255,255,0.04)", border: "0.5px solid rgba(255,255,255,0.08)" }}
      >
        <ProfileEditor compact />
      </div>
      <SettingsGroup>
        <Row icon="rectangle.portrait.and.arrow.right" label="Sign out" description="End your session on this device.">
          <RowButton tone="danger" icon="rectangle.portrait.and.arrow.right" onClick={signOut}>
            Sign out
          </RowButton>
        </Row>
      </SettingsGroup>
    </div>
  );
}

/* ── About ──────────────────────────────────────────────────────────────── */
const APP_VERSION = "0.0.1"; // mirrors apps/desktop/package.json

function AboutSection() {
  const changed = Object.keys(diffFromDefaults(settingsStore.get())).length;
  const link = (url: string) => () => window.open(url, "_blank", "noopener,noreferrer");
  return (
    <div>
      <SectionTitle icon="info.circle" title="About" subtitle="Kinora — where stories come to life." />
      <div
        className="rounded-2xl p-5 mb-6 flex items-center gap-4"
        style={{ background: "rgba(255,255,255,0.04)", border: "0.5px solid rgba(255,255,255,0.08)" }}
      >
        <span className="grid place-items-center rounded-2xl" style={{ width: 56, height: 56, background: "rgba(212,164,78,0.14)", color: "#e8c878" }}>
          <Icon name="film.fill" size={30} />
        </span>
        <div>
          <p className="font-serif text-[18px] font-semibold text-kinora-text">Kinora</p>
          <p className="text-[12px] text-kinora-muted">Version {APP_VERSION} · Desktop</p>
          <p className="text-[11px] text-kinora-subtle mt-1">
            A book becomes a page-synced film that generates itself as you read.
          </p>
        </div>
      </div>
      <SettingsGroup title="Links">
        <Row icon="globe" label="Kinora on GitHub" description="Source, issues and releases.">
          <RowButton icon="arrow.right" onClick={link("https://github.com/rishabhcli/kinora")}>
            Open
          </RowButton>
        </Row>
        <Row icon="lock" label="Privacy Policy">
          <RowButton icon="arrow.right" onClick={link("https://github.com/rishabhcli/kinora")}>
            Open
          </RowButton>
        </Row>
        <Row icon="doc.text" label="Terms of Service">
          <RowButton icon="arrow.right" onClick={link("https://github.com/rishabhcli/kinora")}>
            Open
          </RowButton>
        </Row>
      </SettingsGroup>
      <p className="text-[11.5px] text-kinora-subtle ml-1">
        {changed === 0 ? "All settings are at their defaults." : `${changed} setting${changed === 1 ? "" : "s"} changed from default.`}
      </p>
    </div>
  );
}

/* ── Registry (drives the sidebar + search) ─────────────────────────────── */
export interface SettingsSectionDef {
  id: string;
  /** English label — kept stable for search keyword matching. */
  label: string;
  /** i18n key for the displayed sidebar label (translated at render). */
  labelKey: ParseKeys;
  icon: IconName;
  activeIcon: IconName;
  keywords: string;
  Component: ComponentType;
}

export const SETTINGS_SECTIONS: SettingsSectionDef[] = [
  { id: "general", label: "General", labelKey: "settings.sections.general", icon: "gearshape", activeIcon: "gearshape.fill", keywords: "launch startup sound effects reset defaults language locale", Component: GeneralSection },
  { id: "appearance", label: "Appearance", labelKey: "settings.sections.appearance", icon: "paintbrush", activeIcon: "paintbrush", keywords: "motion transparency contrast accessibility glass", Component: AppearanceSection },
  { id: "reading", label: "Reading", labelKey: "settings.sections.reading", icon: "textformat", activeIcon: "textformat", keywords: "theme dark sepia paper font size spacing line width night dyslexia brightness voice read aloud", Component: ReadingSection },
  { id: "playback", label: "Playback", labelKey: "settings.sections.playback", icon: "film", activeIcon: "film.fill", keywords: "film autoplay captions scrub sensitivity video player", Component: PlaybackSection },
  { id: "notifications", label: "Notifications", labelKey: "settings.sections.notifications", icon: "bell", activeIcon: "bell.fill", keywords: "reminders digest alerts push test", Component: NotificationsSection },
  { id: "privacy", label: "Privacy", labelKey: "settings.sections.privacy", icon: "lock.shield", activeIcon: "lock.shield", keywords: "analytics crash reports clear data tracking", Component: PrivacySection },
  { id: "account", label: "Account", labelKey: "settings.sections.account", icon: "person.crop.circle", activeIcon: "person.crop.circle.fill", keywords: "profile name email bio genre goal sign out logout avatar", Component: AccountSection },
  { id: "about", label: "About", labelKey: "settings.sections.about", icon: "info.circle", activeIcon: "info.circle.fill", keywords: "version credits github links terms privacy policy", Component: AboutSection },
];
