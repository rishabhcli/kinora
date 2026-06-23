/**
 * Backend base URL for the mobile app. A constant for now; a real build wires
 * this from an EXPO_PUBLIC_* env var (and a device can't reach `localhost` —
 * that's the dev-tunnel / LAN URL's job).
 */
export const API_BASE_URL = "http://localhost:8000";
