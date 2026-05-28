# Diagnosis — Meridian Multi-Agent Support System (ticket T-4471 & the rate-limit ticket)

Worked with the **Diagnostic Loop**: Symptom → Hypothesis → Evidence → Recommendation. Every claim is tied to a specific artifact and line.

---

## Step 1 — Symptom (Priya's words)

1. A user reported an **SSO failure and a $1,200 billing overcharge in one ticket**. "We fixed the SSO, and then on the $1,200 he's owed we told him to go chase it with his account manager. It should have either handled the refund or escalated it to a human on our side."
2. A user asked why they were **rate limited**; the agent "told them their plan allows 10K req/min and to check if they're over it." They weren't close — their **retry logic had gone haywire on one endpoint, all right there in our metrics**. "We do have a tool to fetch metrics, `fetch_customer_v2_databricks`, it's just not getting called most times."
3. Separately: **caching shows no savings** — needs help placing cache pointers.

Setup: a coordinator classifies (billing / technical / account) and spawns **one** specialist subagent; specialist works it, coordinator writes the response. Sonnet 4.6, custom orchestration.

---

## Step 2 — Hypotheses (formed from the email alone)

- **H1 — Single-dispatch routing.** The coordinator picks one category and spawns one specialist, so a ticket that spans two categories (SSO=account + refund=billing) loses the second half.
- **H2 — Tool descriptions lack trigger language.** The metrics tool isn't called because its name/description doesn't tell the model *when* it applies (rate limiting / usage anomalies).
- **H3 — Sub-agent over-claims + missing internal escalation.** The specialist reports "handled" without resolving or escalating, and unresolved work is pushed back onto the customer instead of a human at Meridian.

All three held up. A fourth (caching) is a separate config bug, addressed below.

---

## Step 3 & 4 — Evidence + Recommendation per root cause

### Root cause 1 — Single-dispatch routing can't handle multi-issue tickets ✅ confirmed (H1)

**Evidence**
- `system-prompt-coordinator.txt:23` — *"If a ticket could fit two categories, pick the one the customer seems most blocked by."* This explicitly tells the model to **collapse a multi-category ticket to one**.
- Process step 5 (`:12`) — *"Call spawn_specialist with the right category"* (singular). `coordinator-tools.json:153-165` — `spawn_specialist` takes a single `category` (enum, one value), so the architecture only supports one dispatch.
- `trace-T-4471-coordinator.json` — the coordinator **correctly identified both issues** (assistant msg: *"This ticket has two distinct issues: 1. SSO/SAML failure … 2. Billing overcharge"*), then at `toolu_05` called `spawn_specialist` **once** with `category: "account"`, reasoning *"SSO failure … is a security concern. Routing to the account specialist."* The billing issue was **never dispatched** — exactly the behavior line 23 prescribes ("most blocked by" → the urgent SSO).

**Recommendation (scoped)**
- Rewrite `system-prompt-coordinator.txt:23` to: *"If a ticket spans multiple categories, dispatch a specialist for **each** distinct issue and combine their findings. Do not collapse to one."*
- Change the orchestration so `spawn_specialist` is **called once per distinct issue** (loop), or extend its schema to accept a list of `{category, issue_summary}`. Pass each specialist a one-line scope of *its* sub-issue so it doesn't try to handle the others.
- In SYNTHESIZE (`:13`), require the coordinator to confirm **every** identified issue has a specialist result before writing the response.

---

### Root cause 2 — Tool descriptions have no trigger language; tool surface is polluted ✅ confirmed (H2)

**Evidence**
- `coordinator-tools.json:26-27` — `fetch_customer_v2_databricks`: *"Queries the Databricks warehouse. v2 endpoint, use this not the old one."* Describes the **implementation** (which endpoint), never **what it returns** (usage/throughput metrics) or **when to call it** (rate limiting, 429s, usage anomalies). A model cannot map "user reports rate limiting" → this tool. This is Priya's "not getting called most times."
- `subagent-technical-tools.json` — same disease across the board: `get_rate_limit_status` → *"Rate limit status"*; `read_error_logs` → *"Reads error logs"*; `test_api_key` → *"Tests an API key"*. None say *when* to reach for them. The retry-logic-gone-haywire ticket needed `read_error_logs` / `get_rate_limit_status` / metrics — all present, none reliably triggered.
- **Tool-surface pollution** in `coordinator-tools.json`: ~5 overlapping customer-lookup tools (`get_customer`, `fetch_customer_details`, `lookup_customer_info`, `customer_data_retrieval`, `get_data`) plus junk (`helper` = "General helper function", `process` = "Process a request", `tool_3_v2` = "Updated version of tool_3", `validate` = "Validates"). The T-4471 trace shows the cost: the coordinator tried `get_customer` (→ "customer not found"), then `lookup_customer_info`, then `fetch_customer_details` — **three** tools to identify one customer.
- The coordinator's own "TOOL SELECTION GUIDANCE" (`:25-27`) is generic filler — *"When you need customer information, choose the appropriate lookup tool. When you need to check something, use a check tool."* — no symptom→tool mapping.

**Recommendation (scoped)**
- Rewrite every tool `description` as **what it returns + when to use it**. Example for the metrics tool, also rename for intent:
  `fetch_usage_metrics` — *"Returns per-endpoint request volume, throughput, and rate-limit consumption from the metrics warehouse. Use whenever a customer reports rate limiting, throttling, 429s, slow/blocked requests, or any usage/quota anomaly — before quoting their plan limits."*
- **Prune the surface**: delete `helper`, `process`, `tool_3_v2`, `validate`, `get_data`; collapse the 5 customer-lookup tools to **one** canonical `lookup_customer` (by id or email). Fewer, well-described tools beats many opaque ones.
- Replace the vague "TOOL SELECTION GUIDANCE" with a short symptom→tool table.

---

### Root cause 3 — Sub-agent over-claims resolution; no internal escalation for out-of-scope/unresolved work ✅ confirmed (H3)

**Evidence**
- `system-prompt-subagent-account.txt:1` scopes the account specialist to SSO/seats/permissions; its tools (`:7-14`) include **no billing/refund tool**. Yet in the trace (`toolu_05` result) it returned *"Both issues handled … Billing: The $1,200 overcharge is outside what I can action directly. Advised them to escalate to their account manager."* — it **answered an out-of-scope issue by instructing the customer**, instead of flagging it for a billing specialist / human.
- The subagent prompt only says (`:20`) *"If the ticket asks about something outside account scope, note it so the coordinator knows"* — no rule against advising the customer, no requirement to **only report resolved when a resolution tool actually succeeded**, and "WHAT TO RETURN" (`:16-18`) asks for free text ("write up what you found"), which invites *"Both issues handled."*
- The coordinator then wrote `write_response` with **`resolution_status: "resolved"`** (`toolu_06`) even though the refund was neither processed nor escalated. The customer-facing text told the user to *"reach out to your account manager."*
- The escalation path **exists and was simply not used**: `coordinator-tools.json:181-191` `escalate_to_human` (reasons include `customer_escalation`, `security`), and `write_response`'s `resolution_status` enum (`:175`) includes `escalated` and `needs_info`. Nothing in either prompt instructs the agents to use them.

**Recommendation (scoped)**
- **Subagent prompt** (`system-prompt-subagent-account.txt:16-20`, and the same block in the billing/technical subagents): require a **structured per-issue result** — `resolved` only if a resolution tool returned success; otherwise `needs_other_specialist` (with which category) or `needs_human` (with reason). Add: *"Never instruct the customer to pursue an issue you couldn't resolve — flag it for the coordinator instead."*
- **Coordinator prompt** (`:13`, SYNTHESIZE): before calling `write_response`, verify **every** distinct issue resolved. If any is unresolved or out of the handling specialist's scope, call `escalate_to_human` (reason `customer_escalation`) and/or set `resolution_status: "escalated"` — never `resolved`. For T-4471 specifically: SSO → resolved; refund → `escalate_to_human` to billing ops with the Feb-26 downgrade evidence.

---

## Separate issue — Caching shows no savings (cache-pointer placement)

**Evidence**
- `trace-T-4471-coordinator.json` `usage`: **`cache_creation_input_tokens: 0, cache_read_input_tokens: 0`** — the cache is doing nothing, even though the system block carries `"cache_control": {"type": "ephemeral"}` (`:9-11`).
- The **first two lines of the cached system text** are volatile: `Current timestamp: {{ current_timestamp }}` / `Request ID: {{ request_id }}` (`system-prompt-coordinator.txt:1-2`), rendered in the trace to per-request values (`2026-03-11T13:52:14.203Z`, `req_9c4e7a2b1f8d`). Prompt caching is a **prefix match** — a value that changes every request at the *front* of the cached region invalidates the entire prefix behind it, including the large stable payload (process guide + classification guide + 5 worked examples).

**Recommendation (scoped)**
- Move `current_timestamp` and `request_id` **out of the cached system prefix** — put them in the **user message** (e.g., the "New ticket: T-4471" turn), or at the very end after the breakpoint. Keep the stable content (role, process, classification guide, examples) first and place the `cache_control` breakpoint on the **last stable system block**.
- Ensure the **tool list is byte-stable** (tools render before system; a reordered/edited tool array also busts the cache) — freeze and sort it.
- Then `cache_read_input_tokens` will be non-zero from the 2nd ticket onward; the examples block (the bulk of the 24.6K input tokens here) is exactly what you want served at ~0.1× cost.

---

## Stretch 03 — 60-second infographic for Priya's stakeholders

A single landscape graphic, three stacked "pipeline" lanes showing where each failure occurs:

- **Header:** "Two escaped tickets — same pipeline, three fixable gaps."
- **Lane diagram:** `Ticket → [Coordinator: classify] → [Specialist] → [Write response]`, with three call-out pins:
  1. 🔀 **Classify** pin → "Only routed *one* of two problems. Fix: route every issue." (maps to the SSO+refund ticket)
  2. 🔧 **Specialist** pin → "Had the metrics tool, didn't know when to use it. Fix: tell tools when to fire." (maps to the rate-limit ticket)
  3. ✅ **Write response** pin → "Marked 'resolved' with the refund still open. Fix: verify + escalate, never punt to the customer."
- **Before/after strip** for the refund: *Before:* "Go chase your account manager." → *After:* "Refund escalated to billing ops, ref #…, you'll hear back in 1 business day."
- **Footer metric:** "2 escapes last week → target: 0. Each fix has an early-warning metric (see dashboard)." No jargon, no trace screenshots — color-coded pins and one before/after quote.

---

## Stretch 04 — Observability dashboard (early-warning spec)

Goal: catch these three failure modes *before* a customer's support lead does.

| Panel | Metric | Alert trigger |
|---|---|---|
| **Multi-issue coverage** | % of tickets where #issues-detected by the coordinator > #specialists-spawned | any ticket with detected-issues > specialists-spawned → page (the H1 signature) |
| **Tool-trigger health** | For each "should-have" symptom→tool pair, the call rate (e.g., tickets mentioning "rate limit / 429 / throttle" that did **not** call `fetch_usage_metrics`) | metrics-tool call rate on rate-limit tickets < 90% |
| **Resolution integrity** | `resolved` tickets where no resolution/escalation tool succeeded for ≥1 detected issue ("phantom resolved"); reopen rate; CSAT on auto-resolved | any phantom-resolved ticket; reopen rate > baseline |
| **Escalation usage** | `escalate_to_human` call rate vs. tickets containing refund/security/legal keywords | refund-mentioning ticket closed `resolved` with no billing action |
| **Cache efficiency** | `cache_read_input_tokens / total_input_tokens` per agent, $ saved | cache hit rate < 50% (catches the prefix-busting regression) |
| **Per-stage latency & cost** | tokens + $ per coordinator vs. subagent step | cost per ticket > threshold |

Layout: top row = three red/amber/green tiles (Routing, Tool-use, Resolution integrity); middle = trend lines (escapes/day, reopen rate, cache hit rate); bottom = a live table of `resolved`-but-unverified tickets for human spot-check. The first three tiles map 1:1 to the three root causes, so a regression in any fix lights up immediately.
