# KINORA — Hackathon Requirements & Submission Checklist

> Consolidated from the official rules (rules.md), hackathon background (HackathonBackground.md), and Devpost submission requirements.

---

## Hackathon Overview

| | |
|---|---|
| **Name** | Global AI Hackathon Series with Qwen Cloud |
| **Sponsor** | Alibaba Cloud |
| **Administrator** | Devpost, Inc. |
| **Total prizes** | $70,000+ in cash and cloud credits |
| **Submission period** | May 26, 2026 (8am PT) – Jul 9, 2026 (2pm PT) |
| **Judging period** | Jul 10 – Jul 31, 2026 |
| **Winners announced** | On or around Aug 7, 2026 |
| **Website** | qwencloud-hackathon.devpost.com |
| **API base URL** | `https://dashscope-intl.aliyuncs.com/compatible-mode/v1` |
| **API docs** | https://bit.ly/qwencloud-first-api |
| **Model selection** | https://bit.ly/qwencloud-modelselection |
| **Pricing** | https://bit.ly/qwencloud-pricing |
| **Get API key** | https://bit.ly/qwencloud-getapi |

---

## Tracks

| Track | Name | Prize | Kinora's coverage |
|---|---|---|---|
| Track 1 | MemoryAgent | $7K cash + $3K credits | **Secondary** — versioned canon graph, episodic store, forgetting, preference learning |
| Track 2 | AI Showrunner | $7K cash + $3K credits | **Primary** — full short drama pipeline, Wan/HappyHorse, multimodal orchestration |
| Track 3 | Agent Society | $7K cash + $3K credits | **Secondary** — 6-agent crew, negotiation protocol, conflict resolution |
| Track 4 | Autopilot Agent | $7K cash + $3K credits | **Secondary** — Production Autopilot: automated quality gates, error remediation, budget optimization, HITL checkpoints |
| Track 5 | EdgeAgent | $7K cash + $3K credits | Not applicable |

**Bonus prizes:**
- Top 10 Honorable Mentions: $500 cash + $500 credits each
- Top 10 Blog Post Awards: $500 cash + $500 credits each

**Important:** A project can only win **one grand prize** and up to **one blog post prize**.

---

## Judging Criteria

### Stage One (Pass/Fail)
Does the project reasonably fit the theme and reasonably apply the required APIs/SDKs?

### Stage Two (Weighted Scoring)

| Criterion | Weight | What judges look for | How Kinora wins |
|---|---|---|---|
| **Innovation & AI Creativity** | 30% | Sophisticated Qwen Cloud API use (custom skills, MCP); algorithmic/engineering innovation | MCP server shared by all agents; two novel architectural moves (film-as-attention, consistency-as-retrieval) |
| **Technical Depth & Engineering** | 30% | Architecture quality (modularity, scalability, error handling); clean code, non-trivial logic; tech stack sophistication | Multi-model orchestration, closed-loop VL critic, watermark scheduler, budget accounting, cancellable render queue |
| **Problem Value & Impact** | 25% | Real-world relevance; scalability/productization potential | Anti-brainrot literacy, accessibility (ADHD/dyslexia/ESL), manga/indie-author adaptation |
| **Presentation & Documentation** | 15% | Clear technical demo, key logic visualized; clear documentation with architecture docs | Architecture diagram, live agent feed, buffer sawtooth, metrics panel |

### Tie Breaking
Highest score in Innovation & AI Creativity first, then Technical Depth, then Problem Value, then Presentation.

---

## Submission Requirements Checklist

### Required (all must be complete)

- [ ] **Public, open-source code repository** — must contain all source code, assets, and instructions
  - [ ] Add `LICENSE` file (MIT or Apache-2.0) — must be detectable at top of repo page (About section)
  - [ ] Set repository description on GitHub
- [ ] **Proof of Alibaba Cloud deployment** — short recording (separate from demo) + link to code file using Alibaba Cloud services/APIs
  - [ ] `deploy/alibaba_render_worker.py` using `oss2` + `dashscope` + ECS/Function Compute
  - [ ] Record a short clip of it running end-to-end
- [ ] **Architecture diagram** — clear visual showing how Qwen Cloud connects to backend, database, frontend
  - [ ] Export from TECHNICAL_SPEC.md §2 as a clean PNG
- [ ] **Demo video** — less than 3 minutes, public on YouTube/Vimeo/Youku
  - [ ] Must show the project functioning
  - [ ] No third-party trademarks or copyrighted music
- [ ] **Text description** — explain features and functionality
- [ ] **Track identified** — Track 2: AI Showrunner
- [ ] **Working demo accessible** — link to website/functioning demo/test build
  - [ ] If private, include login credentials in testing instructions
  - [ ] Must be available free of charge through Judging Period (ends Jul 31)

### Optional (but recommended)

- [ ] **Blog/social post** — public post sharing build journey with QwenCloud → eligible for Blog Post Prize ($500 + credits)
  - [ ] Include link in submission
- [ ] **Testing instructions** — clear setup steps for judges to run the project

---

## Key Rules to Remember

1. **Must use Qwen Cloud API** — $40 voucher provided; you're responsible for any overage
2. **Must deploy on Alibaba Cloud** — proof required
3. **Open-source libraries allowed** as building blocks, but no direct copying of open-source projects
4. **Project must be original work** — solely owned, no IP violations
5. **New or significantly updated** — if project existed before hackathon, must have been significantly updated after May 26, 2026
6. **All materials in English** (or provide English translation)
7. **Multiple submissions allowed** — but each must be unique and substantially different
8. **API is OpenAI-compatible** — use OpenAI SDK in Python or Node.js with the DashScope base URL

---

## API Access Steps

1. Sign up at qwencloud.com
2. Check free quota at https://home.qwencloud.com/benefits
3. If not eligible for free trial, request $40 hackathon credits via coupon form
4. Generate API key at https://bit.ly/qwencloud-getapi
5. Use base URL: `https://dashscope-intl.aliyuncs.com/compatible-mode/v1`
6. API is OpenAI-compatible — use `openai` Python/Node.js SDK with custom `base_url`

```python
from openai import OpenAI
client = OpenAI(
    api_key="sk-xxx",
    base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
)
response = client.chat.completions.create(
    model="qwen3.6-plus",
    messages=[{"role": "user", "content": "Hello"}]
)
```

---

## Prize Details

| Prize | Cash | Cloud Credits | Other |
|---|---|---|---|
| Track winner (each track) | $7,000 | $3,000 | Blog feature + swag bag |
| Honorable mention (top 10) | $500 | $500 | — |
| Blog post award (top 10) | $500 | $500 | — |

- Prizes paid to individual, team representative, or organization
- Winners responsible for taxes in their jurisdiction
- US residents may need W-9; others may need W-8BEN
- Prizes delivered within 60 days of completed forms
