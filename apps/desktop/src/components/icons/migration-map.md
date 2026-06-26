# SF Symbols migration map (Agent 9 → all owners)

Swap your hand-rolled inline `<svg>`s for the unified `<Icon>` in **your own files**.
Import once: `import { Icon } from "@/components/icons";` (or relative `./icons`).

```tsx
<Icon name="house" size={17} />                       // decorative (aria-hidden)
<Icon name="magnifyingglass" size={15} title="Search" /> // labelled (role="img")
<Icon name="gearshape" weight="medium" mode="hierarchical" />
```

Colour comes from `currentColor`, so keep wrapping it in the element whose text
colour you want (no `stroke=`/`fill=` needed). **Icon-only buttons must still label
the button** (`aria-label`) — `title` on the glyph is for standalone meaning.

## Per-file mapping

### `Navbar.tsx` (Agent 4)
| Old inline component | `<Icon name=…>` |
|---|---|
| `HomeIcon` | `house` |
| `LibraryIcon` | `books.vertical` |
| `WatchIcon` (circle+play) | `play.rectangle` |
| `HeartIcon` | `heart` (use `heart.fill` for the active/favourited state) |
| `NotesIcon` | `note.text` |
| `SearchIcon` | `magnifyingglass` |
| profile-menu “Edit Profile” | `person.crop.circle` |
| profile-menu “Settings” gear | `gearshape` |
| profile-menu “Pricing” | `creditcard` |
| profile-menu “Log Out” | `rectangle.portrait.and.arrow.right` |
| `BookLogoIcon`, `GeometricAvatar` | **keep** — brand assets, not icons (exempt) |

### `Greeting.tsx` (Agent 4)
| Old | New |
|---|---|
| sun | `sun.max.fill` |
| moon | `moon.stars.fill` *(or `moon.fill`)* |

### `BookShelf.tsx` (Agent 5)
| Old | New |
|---|---|
| left arrow | `chevron.left` |
| right arrow | `chevron.right` |

### `GooeySearch.tsx` (Agent 5/4)
| Old | New |
|---|---|
| search glass | `magnifyingglass` |

### `BookCard.tsx` (Agent 5)
| Old | New |
|---|---|
| play overlay | `play.fill` (on a circular chip) |
| bookmark / save | `bookmark` / `bookmark.fill` |

### `LoginPage.tsx` (Agent 11)
| Old | New |
|---|---|
| Google / Apple / GitHub logos | **keep** — third-party brand marks (NOT SF Symbols) |
| email field affordance | `envelope` |
| password field affordance | `lock` |
| show/hide password | `eye` / `eye.slash` |

### Reading room (Agent 10) — suggested
`play.fill`, `pause.fill`, `backward.fill`, `forward.fill`, `speaker.wave.2.fill`,
`speaker.slash.fill`, `captions.bubble`, `gobackward`, `slider.horizontal.3`, `xmark`.

## Final sweep (via Agent 12)
Once owners have merged their adoptions, a coordinated pass removes any remaining
inline icon SVGs (except the brand logos above) and drops the now-unused
`lucide-react` dependency from `apps/desktop/package.json`.

## Adding a new symbol
Add the name to the `IconName` union in `types.ts` **and** draw it in `glyphs.ts`
(the registry is `Record<IconName, GlyphDef>`, so a missing one is a compile error).
Run `node --test src/components/icons/glyphs.test.ts` to validate. See the live
set + a copy-to-clipboard picker at `/icon-gallery.html` (`vite` dev).
