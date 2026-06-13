# Finance Assistant — Phase 3: Agentic Layer

**Goal:** A small, measurable analysis agent over the clean post-tagging DB — the thing that answers "how much did I spend on food in March?" the *same exact way every time*, without you writing code per question. Built so it becomes the foundation for a Streamlit UI, grounded advice, and (much later) a multi-agent setup. This phase is also your hands-on learning vehicle: **you hand-write the agent loop**; Claude Code builds the plumbing around it.

> Naming: the repo's current `claude.md` (tagging + reconciliation, Blocks 0–6) is **Phase 2**. When this doc is accepted, retitle that one to Phase 2 and this becomes Phase 3.

---

## How to use this doc

Each block has **Status**, **Role**, **What to do**, **Output**. After a block is done, set `Status:` and replace `Output:` with the real artifact (file path, table name, sample output, notes).

Status legend: ⬜ Not started · 🟡 In progress · ✅ Done · 🔁 Needs revisit

**Build ownership tag** on each block: `[CC]` = Claude Code builds it · `[YOU]` = you implement it by hand to learn · `[CURATE]` = you supply judgment/content, Claude Code wires mechanics.

---

## Design principles (the spine — read before building)

These are the decisions that keep this phase from sprawling. They are load-bearing.

1. **Claude Code is build-time; the agent is run-time.** Claude Code stays your dev/debug tool and keeps running ingestion (download → dry-run → verify CSV → commit). Do **not** agentify ingestion — you'd be building a worse Claude Code. The agent exists to be a *repeatable, embeddable, measurable* query surface, not to replace your terminal workflow.
2. **The LLM narrates; it never computes.** Money math is always done by deterministic tools (SQL `SUM`, Python). If the model ever produces a figure it didn't get from a tool, that's a bug. This single rule kills most of the risk.
3. **Privacy boundary.** Hosted model gets: the DB **schema** + the **question** + **aggregated tool results**. It never gets raw transaction rows. Tools execute locally; only the computed numbers go back for narration.
4. **Hosted for the loop, local for the cheap stuff.** The reasoning/tool-selection loop uses a hosted model (local llama3 is unreliable at structured tool-calling). Local llama3 stays for narrow, cheap tasks where a bad answer costs nothing (description cleaning, quick yes/no).
5. **One agent now.** "Budget / savings / investment" are *questions over the same data* → they are **tools and prompts, not agents**. You add a tool, not an agent. Second agent (Layer D) only earns its place because it has a genuinely different shape.
6. **Clean seam = reversible choices.** Layer A tools are stable and framework-agnostic. The agent loop is its own isolated module. If you ever adopt a framework (Layer E), only the loop module changes; every tool stays untouched. This is why starting on the raw Anthropic SDK is safe — the learning version *is* the production agent, and it's never a lock-in.
7. **Memory writes are guarded.** Long-term memory is only written on explicit user confirmation, never silently — this prevents memory poisoning, which becomes a real threat once Layer D feeds untrusted web text into the loop.

---

## Phase 3 "Done" Criteria (first deliverable = Layers A + B + C + Memory)

- **Layer A:** a handful of read-only DB tools, each unit-tested and exact.
- **Memory:** `agent_memory` (semantic) + `query_log` (episodic) stores exist; in-session short-term memory works (follow-ups like "and April?" resolve correctly).
- **Layer B:** a hand-written agent loop on the raw Anthropic SDK that answers factual questions by calling tools, **never fabricates a number**, and handles multi-turn.
- **Layer C:** an eval harness that runs a curated question set and reports accuracy; agent hits 100% on the *computable-factual* set (any fabricated figure = fail).
- **D and E explicitly deferred** — sketched here only so the next chat has continuity.

---

## Layer A — Deterministic Tool Library `[CC]`

**Status:** ✅ Done

**Role:** The agent's hands. Pure, read-only, exact functions over the post-tagging DB. This is also most of the unbuilt "insights/advice" layer — build it as plain tested functions with **zero LLM involvement**.

**What to do:**

- Write a small library of read-only query/analysis functions, e.g.: `spend_by_category(month)`, `monthly_trend(category, n_months)`, `top_merchants(month, n)`, `savings_rate(month)`, `reconciled_totals(month)` (uses Block 5 output so CC settlements don't double-count), `category_growth(window)`.
- Each function: deterministic, parameterized, returns a **compact structured result** (small dict/list of numbers), not raw rows.
- Reuse the Phase 2 date-handling lesson: dates are `DD/MM/YYYY` strings in the DB → filter in Python via `_parse_date()`, never by SQLite string comparison.
- Unit-test each function against a known month so the numbers are trusted independently of any agent.

**Why these double as the answer key:** because they're exact, `spend_by_category("2025-03")` *is* the ground truth for "food spend in March." Layer C reuses them to auto-generate evals for free.

**Output:**

- **Module:** `core/analytics.py`
- **Test file:** `tests/test_analytics.py` (30 tests, all passing)
- **Tools shipped:**
  - `spend_by_category(month)` → `dict[str, float]` — splitwise-aware category totals
  - `monthly_trend(category, n_months, as_of_month=None)` → `list[dict]` — per-month spend for a category
  - `top_merchants(month, n=10)` → `list[dict]` — top N merchants by debit spend
  - `savings_rate(month)` → `dict` — salary / SIP / genuine spend / savings rate %
  - `reconciled_totals(month)` → `dict` — genuine spend + CC reconciliation state
  - `category_growth(window=3, as_of_month=None)` → `list[dict]` — recent vs prior window avg per category
- **Bug fixed:** `cli/report.py` was querying `category = 'Salary'` for income; fixed to `category = 'Income'` in `savings_rate()`.
- **`cli/report.py`** updated to import from `core/analytics.py` (no duplicate logic).

---

## Memory Components `[CC]` for stores, `[YOU]` for wiring into the loop

**Status:** ⬜ Not started

**Role:** Give the agent the right recall without letting it invent or corrupt facts. Three kinds apply to this system; one already exists.

**1. Short-term / working memory `[YOU]`** — the agent's running message list during a single session (the dialogue + tool calls + tool results). This is what makes follow-ups work: "how much on food in March?" … "and April?" resolves because April's question is appended to a transcript that still holds the March context. Lives in RAM / a session object, owned by the Layer B loop. Add a *rolling summary* only if a session gets long (most finance Q&A sessions are short — don't over-build this).

**2. Long-term semantic memory `[CC]` store, `[YOU]` read/write** — a small `agent_memory` store of durable facts & preferences: e.g. "BOB joint account counts as Savings & Investment," "salary lands ~1st as `CMP` on SBI," "monthly food budget target = ₹X," "CRED Club = CC settlement." Some of this already lives in `accounts.yaml` / `categories.yaml` / the Phase 2 Working Notes — semantic memory is the **agent-readable, queryable** version. Retrieved by injecting relevant facts into the system prompt at session start (small enough to load all, or filter by keyword). **Written only on explicit user confirmation** (principle 7).

**3. Long-term episodic memory `[CC]`** — a `query_log` append-only table: `(timestamp, question, tools_called, final_answer)`. Powers Layer C eval, debugging, and later the monthly-report / "what you usually ask around month-end" features.

**4. Already exists — the corrections DB is long-term memory.** Merchant → category with growing confidence *is* the categorizer's long-term memory. Naming it as such keeps the system's memory story unified: corrections DB = tagging memory, `agent_memory` = analysis/preference memory, `query_log` = episodic history.

**Output:**

> *Fill in: `agent_memory` + `query_log` schema/paths, how facts get injected into the system prompt, the confirmation gate for writes.*

---

## Layer B — The Analysis Agent `[YOU — do not let Claude Code fill the loop body]`

**Status:** ⬜ Not started

**Role:** The learning centerpiece. One tool-using loop on the raw Anthropic SDK that takes a natural-language question, decides which Layer A tool(s) to call, executes them **locally**, and narrates the result.

> **For future Claude / Claude Code:** the loop body in this layer is the user's to write by hand. Provide review, design feedback, and debugging — but do **not** implement the loop for them.

### The loop, conceptually

A ReAct-style tool-use loop:

```
1. system prompt (rules + injected semantic memory) + tool schemas + user question
        │
        ▼
2. hosted model returns EITHER  →  final text answer  → done
                              OR  →  one or more tool_use blocks
        │
        ▼
3. for each tool_use: dispatch to the matching Layer A function, run it LOCALLY
        │
        ▼
4. append tool_result(s) to the message list  →  go back to step 2
```

Guard the loop with a **max-iterations** cap so a confused model can't spin forever.

### Tool-schema design (this is where most of the skill is)

- Each Layer A function is exposed as a tool: `name`, `description`, `input_schema`. The **description is the agent's API docs** — be precise; vague descriptions cause wrong tool choice.
- Constrain inputs hard: `category` as an enum of your canonical list, `month` as a regex'd string. Tight schemas prevent a class of errors before they happen.
- Tools are **read-only**. Return compact numbers, never raw rows (privacy boundary).

### Failure modes to watch (design against these up front)

- **Model invents a number instead of calling a tool.** Mitigate in the system prompt: "any figure must come from a tool call; never calculate." Treat a fabricated figure as a hard failure in Layer C.
- **Empty tool result → fabrication.** If a tool returns nothing, the agent must say "no data for that," not guess.
- **Local model can't tool-call cleanly.** Use the hosted model for the loop (principle 4).
- **Multi-turn reference resolution** ("and April?") depends on short-term memory being in the message list — verify it carries.
- **Runaway loops** — the max-iterations guard.

### Milestones (build in this order; each is a checkpoint)

1. Single hardcoded question → single tool → answer. (You see the whole mechanic once.)
2. Model chooses among multiple tools.
3. Multi-step: two tool calls to answer a comparison ("March vs April food").
4. Multi-turn session (short-term memory carries follow-ups).
5. Inject long-term semantic memory at session start.
6. Pass the Layer C eval bar.

### Skeleton (signatures + TODOs only — you implement)

```python
# agent/loop.py  — your file to write

def build_tool_schemas() -> list[dict]:
    """Map each Layer A function to an Anthropic tool schema.
    TODO: name, description (precise!), input_schema (enums/regex)."""
    ...

def dispatch_tool(name: str, tool_input: dict):
    """Route a tool_use to the matching Layer A function and run it LOCALLY.
    TODO: validate input, call the function, return a compact result."""
    ...

def run_session(question: str, session_messages: list, memory_facts: list[str]):
    """The loop: model -> (answer | tool_use) -> dispatch -> tool_result -> repeat.
    TODO: assemble system prompt (rules + memory_facts), call the hosted model,
          branch on stop_reason, append results, cap iterations, return final text.
    Append the final (question, tools_called, answer) to query_log."""
    ...
```

**Output:**

> *Fill in: `agent/` module path, which hosted model, max-iterations value, a transcript of a real multi-turn session, notes on what broke and how you fixed it.*

---

## Layer C — Eval Harness `[CC]` mechanics, `[CURATE]` question set

**Status:** ⬜ Not started

**Role:** The agent's answer key — a regression test so changes stop being vibes. Same ground-truth philosophy as Phase 2 Block 6 / financeEnv: **you can only grade what has a computable answer.**

**Conceptually how it works:**

- You curate a frozen list of factual questions whose answers are computable from the DB (the `[CURATE]` part — this is judgment, not mechanics).
- The **answer key writes itself**: the same Layer A function that the agent should call also *produces* the expected number. `spend_by_category("2025-03")` is both the tool and the truth.
- Run the agent on each question, take its final number, compare to the computed truth. Score = % correct. A fabricated or off figure = fail.
- It answers one question: "I changed the prompt / swapped the model / added a tool — did the agent get **better or worse**?"
- Covers **factual-computable** questions only. Open-ended advice can't be auto-graded — which is exactly why the agent stays grounded in computable tools. Anything ungradeable is a signal you've drifted into untrusted territory.
- Reuse `query_log` so eval runs are logged like real sessions.

**Output:**

> *Fill in: harness path, the curated question set, baseline accuracy, score history across changes.*

---

## Layer D — Web-Lookup Tagging Agent `[YOU, later]` (rough sketch)

**Status:** ⬜ Not started — deferred until Layer B passes its eval.

**Why it's a real second agent (not just a tool):** different *shape* from the analysis agent — external tool, untrusted input, a different success metric. Build it only after you've learned the guardrails from Layer B.

**Rough shape:**

- **Input:** a `canonical_merchant` that hit `Other` / low confidence in the Phase 2 categorizer.
- **Tools:** web search (merchant name → "what kind of business is this?"), then a constrained `propose_category` step restricted to the canonical category list.
- **Output:** proposed `{category, confidence, source_url}`, written to the corrections DB **only after human confirmation** — consistent with your existing dry-run discipline.
- **Eval (free answer key):** the corrections DB *is* its ground truth. Run it on merchants you've already categorized and measure agreement.
- **Privacy:** merchant strings leave the machine here — explicit, narrow boundary. Send only the merchant token; never amounts or account context.
- **Failure modes:** SEO/noise web results, prompt-injection from fetched text (→ memory poisoning, principle 7), over-confident wrong categories. Mitigate: constrain output to the category list, require confirmation, log sources, never let fetched text write memory directly.

---

## Layer E — Coordinator / Router `[later]` (rough sketch)

**Status:** ⬜ Not started — only after B and D both pass their evals and you actually feel the friction of running them separately.

**What it is:** a thin router that reads user intent and dispatches to the right agent (analysis vs tagging) or runs a tool directly.

**What it is NOT:** a chat free-for-all of agents "debating." Keep it a near-deterministic intent classifier → route, not an LLM "manager" that piles on latency and failure surface.

**The framework decision point:** this is where you *consider* (not automatically adopt) LangGraph / Pydantic AI, because now you have genuine multi-component orchestration. Evaluate it against the clean seam you preserved (principle 6) — by now you'll understand exactly what a framework abstracts, because you built the loop by hand first.

**Later attachments:** grounded advice specialists (budget / tax / investment) hang off here — each tool-backed and grounded (e.g. a real lookup tool for current Indian tax rules), never an ungrounded oracle. A local model giving tax advice from memory will be confidently wrong; that's a deliberate non-goal until grounding exists.

---

## The Big-Picture Flow

```
  Ingestion (STAYS in Claude Code — not agentified)
  download → dry-run → verify CSV → commit  ──►  Clean DB  ◄── corrections DB (tagging memory)
                                                   │
                                                   ▼
                                        [Layer A] read-only tools
                                                   ▲
                          ┌────────────────────────┘
                          │
   user question ──►  [Layer B] analysis agent loop  ◄──► short-term (session) memory
                          │   ├─ reads: agent_memory (semantic) + corrections DB
                          │   ├─ calls: Layer A tools (local, exact)
                          │   └─ writes: query_log (episodic)
                          ▼
                    narrated answer (numbers from tools only)
                          │
                   [Layer C] eval ── compares agent answer vs computed truth

   (later)  [Layer D] tagging agent ──► corrections DB
   (later)  [Layer E] router in front of B + D
```

---

## Working Notes  — decisions & reasoning carried in from the planning chat

> Continuity payload so a fresh chat (or future Claude Code) starts aligned.

- **Agent vs Claude Code:** ingestion + ad-hoc DB checks already work well in Claude Code; don't rebuild them. The agent's near-term value = learning vehicle + a repeatable/consistent query surface + the foundation the Streamlit UI sits on. Be clear-eyed: it won't speed up ingestion.
- **One agent, not six.** The six-agent vision collapses into one analysis agent + a tool library (+ maybe Layer D later). Advisory "agents" are tools/prompts.
- **Money is deterministic; the model only narrates.** Any model-produced figure not from a tool is a bug.
- **Privacy boundary:** schema + question + aggregated results to the hosted model; raw rows never leave the machine.
- **Hosted model for the loop; local llama3 for cheap narrow tasks.** llama3's tool-calling is too weak for the loop.
- **Raw Anthropic SDK for Layer B** — the loop is ~50–80 lines, not a maintenance burden, and the learning version *is* the production agent. Keep a clean seam (tools stable, loop swappable) so adopting a framework later is cheap and reversible. Revisit frameworks only at Layer E.
- **Memory write-guard:** long-term memory writes require explicit confirmation; critical once Layer D introduces untrusted web text.
- **Corrections DB = existing long-term (tagging) memory** — fold it into the system's memory story rather than treating memory as brand-new.
- **Eval ground-truth insight (from the RL-env thread):** only computable-factual questions are gradeable; design agent outputs to be checkable, keep advice grounded in computable tools.

---

## Next Phase (placeholder)

Phase 4 — candidate themes once Layer B+C are solid: Streamlit UI on top of the agent, grounded advice specialists (Layer E attachments), scheduled monthly reports off `query_log`. Not started until Phase 3 "Done" criteria are met.