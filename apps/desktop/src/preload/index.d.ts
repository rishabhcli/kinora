declare global {
  interface Window {
    kinora: {
      platform: string;
      secure: {
        getToken: () => Promise<string | null>;
        setToken: (token: string | null) => Promise<void>;
      };
    };
  }
}

export {};
