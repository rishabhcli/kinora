# Agent 11 — Login Experience artifacts

Captured with Chromium (Playwright) against the Vite dev build at 1440×900 (retina),
downscaled to JPEG for the repo. The enter transition is a video.

| File | What it shows |
|---|---|
| `01-login-cold.jpg` | Cold launch → login: the moody screening-room backdrop (subdued book wall + warm projector beam), brand rail, frosted card. |
| `02-signup-strength.jpg` | Sign In → Sign Up morph; password **strength meter** visible. |
| `03-validation.jpg` | Real-time validation: invalid email + empty password, friendly inline errors. |
| `04-password-visible.jpg` | Password **show/hide** toggled on. |
| `05-home-after-enter.jpg` | After the enter transition — HomePage with its **fixed navbar intact** (the navbar-anchor invariant held). |
| `06-login-reduced-motion.jpg` | `prefers-reduced-motion: reduce` — calm static scene (no drift/dust/beam-sway), fully legible. |
| `07-login-narrow.jpg` | Responsive narrow (480px): brand rail collapses, card-carried brand, no double logo. |
| `08-enter-transition.webm` | The login→home hand-off: card recedes + wall blooms → warm flash → home cross-fades in. |
| `09-caps-lock.jpg` | Caps Lock hint on the password field (announced via `role="status"`). |
| `10-increased-contrast.jpg` | `prefers-contrast: more` — lifted secondary text + firmer borders (verified the media query applies). Also honours `prefers-reduced-transparency` (solid card, no frost). |
| `a11y-report.txt` | Accessibility probe (labels, ARIA, tab order, contrast, console). |

## A11y summary (from `a11y-report.txt`)
- Email + password fields have associated `<label>`s; `autocomplete=email` / `current-password`.
- Password toggle: `aria-label` + `aria-pressed`. Remember-me is a real labelled checkbox.
- Single polite `role="status"` live region announces progress/errors.
- Social buttons labelled ("Continue with Google/Apple/GitHub").
- Contrast vs card: title **16.4:1**, subtitle **8.1:1**, field label **8.1:1** (all ≫ AA 4.5:1).
- Keyboard tab order: email → password → show-password → remember → forgot → sign-in → socials.
- No broken page resources (the only console 404 is the browser's automatic favicon request).
