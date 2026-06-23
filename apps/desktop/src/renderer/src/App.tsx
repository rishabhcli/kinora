import { CORE_VERSION } from "@kinora/core";

export default function App() {
  return (
    <div className="flex min-h-screen items-center justify-center bg-neutral-950 text-neutral-100">
      <div className="text-center">
        <h1 className="text-3xl font-semibold tracking-tight">Kinora</h1>
        <p className="mt-2 text-sm text-neutral-400">
          desktop shell · @kinora/core v{CORE_VERSION}
        </p>
      </div>
    </div>
  );
}
