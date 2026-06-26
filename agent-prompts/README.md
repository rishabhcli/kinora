# Kinora overnight agent prompts

## Start Agent 01 (zero paste)

```bash
cd ~/Documents/GitHub/kinora
bash agent-prompts/go 01
```

No copy/paste. Mission text loads from `agent-prompts/.missions/` automatically.

---

## Which file is which?

| You see this at the top | File | Paste? |
|---|---|---|
| `/ralph-loop:ralph-loop` | `agent-01-event-director-stitching.md` | ✅ one line only |
| `# MISSION` or `<!-- INTERNAL` | `.missions/agent-01-*.md` | ❌ **never** |

**You have the wrong file if it starts with `# MISSION`.**

---

## All agents

```bash
bash agent-prompts/go 01   # through go 12
```

Worktrees: `../kinora-a01` … `../kinora-a11`; Agent 12 uses main repo.
