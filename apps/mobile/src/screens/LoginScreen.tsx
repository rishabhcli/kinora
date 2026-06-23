import { useState } from "react";
import { ActivityIndicator, Pressable, StyleSheet, Text, TextInput, View } from "react-native";

import { api } from "../lib/api";
import { authStore } from "../lib/auth";

export function LoginScreen() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function signIn() {
    setBusy(true);
    setError(null);
    authStore.getState().setAuthenticating();
    const { data, error: loginError } = await api.POST("/api/auth/login", {
      body: { email, password },
    });
    if (loginError || !data) {
      setError("Invalid email or password.");
      authStore.getState().setAnonymous();
      setBusy(false);
      return;
    }
    const me = await api.GET("/api/auth/me");
    if (me.error || !me.data) {
      setError("Could not load your account.");
      authStore.getState().setAnonymous();
      setBusy(false);
      return;
    }
    authStore.getState().setSession(data.access_token, me.data);
  }

  return (
    <View style={styles.container}>
      <Text style={styles.title}>Kinora</Text>
      <Text style={styles.subtitle}>watch the book</Text>
      <TextInput
        value={email}
        onChangeText={setEmail}
        placeholder="email"
        placeholderTextColor="#666"
        autoCapitalize="none"
        keyboardType="email-address"
        style={styles.input}
      />
      <TextInput
        value={password}
        onChangeText={setPassword}
        placeholder="password"
        placeholderTextColor="#666"
        secureTextEntry
        style={styles.input}
      />
      {error ? <Text style={styles.error}>{error}</Text> : null}
      <Pressable onPress={signIn} disabled={busy} style={styles.button}>
        {busy ? <ActivityIndicator color="#fff" /> : <Text style={styles.buttonText}>Sign in</Text>}
      </Pressable>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, justifyContent: "center", padding: 24, backgroundColor: "#0a0a0a" },
  title: { color: "#fff", fontSize: 28, fontWeight: "600", textAlign: "center" },
  subtitle: { color: "#a3a3a3", textAlign: "center", marginBottom: 24 },
  input: {
    backgroundColor: "#171717",
    color: "#fff",
    borderRadius: 8,
    padding: 12,
    marginBottom: 12,
    borderWidth: 1,
    borderColor: "#262626",
  },
  error: { color: "#f87171", marginBottom: 12 },
  button: { backgroundColor: "#6366f1", padding: 14, borderRadius: 8, alignItems: "center" },
  buttonText: { color: "#fff", fontWeight: "600" },
});
