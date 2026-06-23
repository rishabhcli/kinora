import { useState } from "react";
import { useNavigate } from "react-router-dom";

import { BookWall } from "../components/BookWall";
import { setOnboarded } from "../lib/onboarding";

interface Slide {
  eyebrow: string;
  title: string;
  body: string;
}

const SLIDES: Slide[] = [
  {
    eyebrow: "Welcome to Kinora",
    title: "Watch the book.",
    body: "Kinora turns any book into a page-synced film that plays as you read — scene by scene, in step with the words.",
  },
  {
    eyebrow: "Always a step ahead",
    title: "A few seconds early.",
    body: "Six AI agents draft the script, render each shot, and share one evolving canon — so a long adaptation stays visually consistent.",
  },
  {
    eyebrow: "Your shelf, on film",
    title: "Bring your library.",
    body: "Drop in a PDF or EPUB. Kinora reads it, storyboards it, and starts the film a few seconds ahead of your page.",
  },
];

export default function OnboardingPage() {
  const navigate = useNavigate();
  const [index, setIndex] = useState(0);
  const slide = SLIDES[index];
  const last = index === SLIDES.length - 1;

  function finish() {
    setOnboarded();
    navigate("/login", { replace: true });
  }

  if (!slide) return null;

  return (
    <div className="relative h-screen w-screen overflow-hidden bg-walnut font-sans text-white">
      <div className="drag absolute inset-x-0 top-0 z-30 h-12" />
      <BookWall />

      <main className="relative z-20 flex h-full items-center justify-center px-6">
        <section className="glass no-drag w-full max-w-[460px] rounded-glass p-9">
          <p className="font-display text-sm tracking-tight text-ember-glow">Kinora</p>

          <div className="mt-7 min-h-[156px]">
            <p className="text-xs uppercase tracking-[0.22em] text-white/45">{slide.eyebrow}</p>
            <h1 className="mt-2 font-display text-[34px] font-semibold leading-tight">{slide.title}</h1>
            <p className="mt-3 text-[15px] leading-relaxed text-white/70">{slide.body}</p>
          </div>

          <div className="mt-7 flex items-center gap-2" aria-hidden>
            {SLIDES.map((_, n) => (
              <span
                key={n}
                className={`h-1.5 rounded-full transition-all duration-300 ${
                  n === index ? "w-6 bg-ember-glow" : "w-1.5 bg-white/25"
                }`}
              />
            ))}
          </div>

          <div className="mt-7 flex items-center justify-between">
            <button
              onClick={finish}
              className="rounded-lg px-2 py-1 text-sm text-white/55 transition hover:text-white"
            >
              Skip
            </button>
            <div className="flex items-center gap-2">
              {index > 0 && (
                <button
                  onClick={() => setIndex(index - 1)}
                  className="rounded-xl px-4 py-2.5 text-sm font-medium text-white/80 transition hover:text-white"
                >
                  Back
                </button>
              )}
              <button
                onClick={() => (last ? finish() : setIndex(index + 1))}
                className="rounded-2xl bg-gradient-to-b from-ember-glow to-ember-deep px-6 py-2.5 text-sm font-semibold text-walnut-deep shadow-[0_12px_34px_-8px_rgba(224,134,58,0.65)] transition hover:brightness-[1.06] active:scale-[0.99]"
              >
                {last ? "Get started" : "Next"}
              </button>
            </div>
          </div>
        </section>
      </main>
    </div>
  );
}
