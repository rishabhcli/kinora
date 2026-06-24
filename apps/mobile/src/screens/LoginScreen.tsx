import { AUTH_TIMEOUT_MS, CONNECTION_ERROR, withTimeout } from "@kinora/core";
import { useState } from "react";
import {
  KeyboardAvoidingView,
  Platform,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from "react-native";

import { GhostButton, GlassField, PrimaryButton, Surface, Wordmark } from "../components/ui";
import { api } from "../lib/api";
import { authStore, persistToken } from "../lib/auth";
import { alpha, BOTTOM_INSET, space, TOP_INSET, type } from "../theme/tokens";

/** The seeded demo reader (owns the bundled library) — one tap to explore. */
const DEMO = { email: "demo@kinora.local", password: "demo-password-123" } as const;

type Mode = "login" | "register";

/**
 * The welcome screen: a warm, branded hero over the ambient screening-room
 * backdrop, with a glass card carrying the sign-in form. The auth flow is the
 * existing one — token via expo-secure-store, validated through /api/auth/me.
 */
export function LoginScreen() {
  const [mode, setMode] = useState<Mode>("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  /** Exchange credentials for a session and load the account (shared by both modes). */
  async function loginAndLoad(currentEmail: string, currentPassword: string): Promise<string | null> {
    const login = await withTimeout(
      api.POST("/api/auth/login", { body: { email: currentEmail, password: currentPassword } }),
      AUTH_TIMEOUT_MS,
    );
    if (login === null) return CONNECTION_ERROR;
    const { data, error: loginError } = login;
    if (loginError || !data) return "That email and password didn't match.";
    authStore.getState().setToken(data.access_token);
    const me = await withTimeout(api.GET("/api/auth/me"), AUTH_TIMEOUT_MS);
    if (me === null) {
      authStore.getState().setAnonymous();
      return CONNECTION_ERROR;
    }
    if (me.error || !me.data) {
      authStore.getState().setAnonymous();
      return "Signed in, but couldn't load your account.";
    }
    persistToken(data.access_token);
    authStore.getState().setSession(data.access_token, me.data);
    return null;
  }

  async function run(currentEmail: string, currentPassword: string, asRegister: boolean) {
    if (busy) return;
    setBusy(true);
    setError(null);
    authStore.getState().setAuthenticating();
    let message: string | null;
    if (asRegister) {
      const reg = await withTimeout(
        api.POST("/api/auth/register", {
          body: { email: currentEmail, password: currentPassword },
        }),
        AUTH_TIMEOUT_MS,
      );
      message =
        reg === null
          ? CONNECTION_ERROR
          : reg.error || !reg.data
            ? "Couldn't create that account."
            : await loginAndLoad(currentEmail, currentPassword);
    } else {
      message = await loginAndLoad(currentEmail, currentPassword);
    }
    if (message) {
      setError(message);
      authStore.getState().setAnonymous();
      setBusy(false);
    }
    // On success the auth store flips to "authenticated" and the app swaps the
    // screen out from under us, so there's nothing more to do here.
  }

  const submitLabel = busy ? "One moment…" : mode === "login" ? "Sign in" : "Create account";

  return (
    <KeyboardAvoidingView
      style={styles.flex}
      behavior={Platform.OS === "ios" ? "padding" : undefined}
    >
      <ScrollView
        contentContainerStyle={styles.scroll}
        keyboardShouldPersistTaps="handled"
        showsVerticalScrollIndicator={false}
      >
        <View style={styles.hero}>
          <Wordmark size={type.wordmark.fontSize} withMark withTagline />
        </View>

        <Surface style={styles.card}>
          <Text style={styles.cardHeading}>
            {mode === "login" ? "Welcome back" : "Create your account"}
          </Text>
          <Text style={styles.cardSub}>
            {mode === "login"
              ? "Open a book and watch it come to life."
              : "Start turning the books you love into film."}
          </Text>

          <View style={styles.form}>
            <GlassField
              label="Email"
              value={email}
              onChangeText={setEmail}
              placeholder="you@example.com"
              autoCapitalize="none"
              autoComplete="email"
              keyboardType="email-address"
              textContentType="emailAddress"
            />
            <GlassField
              label="Password"
              value={password}
              onChangeText={setPassword}
              placeholder="Your password"
              secureTextEntry
              autoCapitalize="none"
              textContentType={mode === "login" ? "password" : "newPassword"}
              onSubmitEditing={() => void run(email, password, mode === "register")}
            />

            {error ? <Text style={styles.error}>{error}</Text> : null}

            <View style={styles.submit}>
              <PrimaryButton
                label={submitLabel}
                busy={busy}
                disabled={!email || !password}
                onPress={() => void run(email, password, mode === "register")}
              />
            </View>
          </View>

          <View style={styles.footer}>
            <GhostButton
              tone="ember"
              align="left"
              label="Explore the demo library →"
              onPress={() => void run(DEMO.email, DEMO.password, false)}
            />
            <GhostButton
              label={mode === "login" ? "Create account" : "Sign in"}
              onPress={() => {
                setError(null);
                setMode(mode === "login" ? "register" : "login");
              }}
            />
          </View>
        </Surface>
      </ScrollView>
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  flex: { flex: 1 },
  scroll: {
    flexGrow: 1,
    justifyContent: "center",
    paddingHorizontal: space.xxl,
    paddingTop: TOP_INSET + space.xxl,
    paddingBottom: BOTTOM_INSET + space.huge,
  },
  hero: { alignItems: "center", marginBottom: space.xxxl },
  card: { width: "100%", maxWidth: 440, alignSelf: "center", padding: space.xxl },
  cardHeading: {
    color: alpha.white95,
    fontSize: type.title.fontSize,
    lineHeight: type.title.lineHeight,
    fontWeight: "600",
  },
  cardSub: {
    color: alpha.white55,
    fontSize: type.label.fontSize,
    lineHeight: type.body.lineHeight,
    marginTop: 4,
  },
  form: { marginTop: space.xl, gap: space.lg },
  error: { color: "#f0a48a", fontSize: type.label.fontSize, marginTop: -2 },
  submit: { marginTop: space.xs },
  footer: {
    marginTop: space.xl,
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
  },
});
