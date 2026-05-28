!pip install -q anthropic

import os
from pathlib import Path
# Pull ANTHROPIC_API_KEY from the single root .env (walk up to find it),
# or Colab secrets, or an existing env var.
if not os.environ.get("ANTHROPIC_API_KEY"):
    try:
        from google.colab import userdata
        os.environ["ANTHROPIC_API_KEY"] = userdata.get("ANTHROPIC_API_KEY")
    except Exception:
        for _dir in [Path.cwd(), *Path.cwd().parents]:
            _env = _dir / ".env"
            if _env.exists():
                for _line in _env.read_text().splitlines():
                    if _line.strip().startswith("ANTHROPIC_API_KEY="):
                        os.environ["ANTHROPIC_API_KEY"] = _line.split("=", 1)[1].strip().strip('"').strip("'")
                        break
                if os.environ.get("ANTHROPIC_API_KEY"):
                    break

import math
from anthropic import Anthropic
from anthropic.types import ToolUseBlock, TextBlock

# ── Config ────────────────────────────────────────────────────────────────────

MODEL = "claude-haiku-4-5-20251001"
SYSTEM_PROMPT = "You are a helpful assistant."

client = Anthropic()

# ── Tool implementations ─────────────────────────────────────────────────────

def get_product(product: str):
    catalog = {
        "jeans": 49.99,
        "shirt": 29.99,
        "dress": 59.99,
        "jacket": 89.99,
        "sneakers": 74.99,
        "hat": 19.99,
        "socks": 9.99,
        "hoodie": 44.99,
        "shorts": 34.99,
        "t-shirt": 24.99,
        "sweater": 54.99,
        "belt": 24.99,
    }
    return catalog[product]


def calculate(op: str, input1: float, input2: float):
    match op:
        case "+": return input1 + input2
        case "-": return input1 - input2
        case "*": return input1 * input2
        case "/": return input1 / input2
        case "**": return input1 ** input2

TOOL_REGISTRY = {
    "get_product": get_product,
    "calculate": calculate,
}

# ── Tool specs (sent to Claude) ──────────────────────────────────────────────

GET_PRODUCT_SPEC = {
    "name": "get_product",
    "description": "get_product",
    "input_schema": {
        "type": "object",
        "properties": {
            "product": {
                "type": "string",
                "description": "product",
            },
        },
        "required": ["product"],
    },
}

CALCULATE_SPEC = {
    "name": "calculate",
    "description": "calculator",
    "input_schema": {
        "type": "object",
        "properties": {
            "op": {
                "type": "string",
                "description": "operator",
            },
            "input1": {
                "type": "number",
                "description": "input1",
            },
            "input2": {
                "type": "number",
                "description": "input2",
            },
        },
        "required": ["op", "input1", "input2"],
    },
}

ALL_TOOL_SPECS = [GET_PRODUCT_SPEC, CALCULATE_SPEC]

# ── Agent ─────────────────────────────────────────────────────────────────────

def call_claude(messages, tools, model=None):
    return client.messages.create(
        model=model or MODEL,
        system=SYSTEM_PROMPT,
        max_tokens = 1024,
        tools=tools,
        messages=messages,
    )


def execute_tool(name, inputs):
    try:
        return str(TOOL_REGISTRY[name](**inputs))
    except Exception as e:
        return f"Error: {e}"


def run_agent(prompt, eval_mode=False, model=None):
    messages = [{"role": "user", "content": prompt}]
    total_input_tokens = 0
    total_output_tokens = 0

    while True:
        response = call_claude(messages, tools=ALL_TOOL_SPECS, model=model)
        total_input_tokens += response.usage.input_tokens
        total_output_tokens += response.usage.output_tokens
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            break

        tool_calls = [block for block in response.content if isinstance(block, ToolUseBlock)]

        tool_results = []
        for tool_call in tool_calls:
            result = execute_tool(tool_call.name, tool_call.input)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tool_call.id,
                "content": result,
            })

        messages.append({"role": "user", "content": tool_results})

    if eval_mode:
        return {
            "messages": messages,
            "usage": {"input_tokens": total_input_tokens, "output_tokens": total_output_tokens},
        }

    return "\n".join(block.text for block in response.content if isinstance(block, TextBlock))


print("boutique agent ready.")

while True:
    query = input("\nYou: ")
    if not query.strip() or query.strip().lower() in ("quit", "exit", "q"):
        print("Session ended.")
        break
    print(f"\nBoutique: {run_agent(query)}")

# ── Graders (just run this cell) ──────────────────────────────────────────────

import re

def grade_response_contains(result, check, context=None):
    text = result["final_text"].lower()
    target = check.lower()
    if target in text:
        return {"score": 1.0, "reason": f"Found '{check}' in response"}
    return {"score": 0.0, "reason": f"'{check}' not found in response: {result['final_text'][:200]}"}


def grade_response_numeric(result, check, context=None):
    if isinstance(check, (int, float)):
        value, tolerance = float(check), 0.01
    else:
        value = float(check["value"])
        tolerance = float(check.get("tolerance", 0.01))

    numbers = re.findall(r"-?[\d,]+\.?\d*", result["final_text"])
    for num_str in numbers:
        try:
            num = float(num_str.replace(",", ""))
            if abs(num - value) <= tolerance:
                return {"score": 1.0, "reason": f"Found {num} (expected {value} +/- {tolerance})"}
        except ValueError:
            continue
    return {"score": 0.0, "reason": f"Expected {value} (+/- {tolerance}), found: {numbers[:10]}"}


def grade_tool_use(result, check, context=None):
    tool_name = check["tool_name"]
    expected_args = check.get("arguments", None)

    for call in result["tool_calls"]:
        if call["name"] != tool_name:
            continue
        if expected_args is None:
            return {"score": 1.0, "reason": f"Tool '{tool_name}' was called"}

        # Partial match: only check specified keys
        actual_args = call.get("arguments", {})
        match = all(
            (isinstance(v, str) and isinstance(actual_args.get(k), str) and v.lower() == actual_args[k].lower())
            or actual_args.get(k) == v
            for k, v in expected_args.items()
        )
        if match:
            return {"score": 1.0, "reason": f"Tool '{tool_name}' called with matching args: {expected_args}"}

    actual = [{"name": c["name"], "args": c.get("arguments", {})} for c in result["tool_calls"]]
    if expected_args:
        return {"score": 0.0, "reason": f"'{tool_name}' not called with {expected_args}. Actual: {actual}"}
    return {"score": 0.0, "reason": f"'{tool_name}' never called. Actual: {[c['name'] for c in result['tool_calls']]}"}


GRADER_REGISTRY = {
    "response_contains": grade_response_contains,
    "response_numeric": grade_response_numeric,
    "tool_use": grade_tool_use,
}

print(f"Graders loaded: {list(GRADER_REGISTRY.keys())}")

# ── Eval Runner (just run this cell) ──────────────────────────────────────────

import json, os, time, traceback
from concurrent.futures import ThreadPoolExecutor, as_completed


def parse_transcript(messages):
    """Extract final_text and tool_calls from raw agent transcript."""
    final_text, tool_calls = "", []
    for msg in messages:
        if msg["role"] != "assistant":
            continue
        for block in msg["content"]:
            if isinstance(block, TextBlock):
                final_text = block.text
            elif isinstance(block, ToolUseBlock):
                tool_calls.append({"name": block.name, "arguments": block.input, "id": block.id})
    # Match tool results back to calls
    for msg in messages:
        if msg["role"] != "user" or not isinstance(msg["content"], list):
            continue
        for item in msg["content"]:
            if isinstance(item, dict) and item.get("type") == "tool_result":
                for call in tool_calls:
                    if call["id"] == item["tool_use_id"]:
                        call["result"] = item.get("content", "")
                        break
    return {"final_text": final_text, "tool_calls": tool_calls, "messages": messages}


def run_single_task(agent_fn, task, model=None):
    """Run one task, apply graders, return result with grades + metrics."""
    start = time.time()
    try:
        raw = agent_fn(task["query"], eval_mode=True, model=model)
    except Exception:
        return {
            "task_id": task["id"], "task_description": task.get("description", ""),
            "query": task["query"], "category": task.get("category", ""),
            "error": traceback.format_exc(), "passed": False, "grades": [],
            "metrics": {"time": time.time() - start},
        }

    elapsed = time.time() - start
    result = parse_transcript(raw["messages"])
    usage = raw.get("usage", {})
    turns = sum(1 for m in raw["messages"] if m["role"] == "assistant")
    metrics = {
        "time": round(elapsed, 3), "tool_calls": len(result["tool_calls"]),
        "turns": turns, "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
    }

    grades = []
    context = {"query": task["query"], "task_id": task["id"], "model": model}
    for grader in task.get("graders", []):
        grader_fn = GRADER_REGISTRY.get(grader["type"])
        if grader_fn is None:
            grades.append({"type": grader["type"], "check": None, "score": 0.0, "reason": f"Unknown grader: {grader['type']}"})
            continue
        for check in grader.get("checks", []):
            grade = grader_fn(result, check, context)
            grades.append({"type": grader["type"], "check": check, "score": grade["score"], "reason": grade["reason"]})

    passed = all(g["score"] == 1.0 for g in grades) if grades else False

    return {
        "task_id": task["id"], "task_description": task.get("description", ""),
        "query": task["query"], "category": task.get("category", ""),
        "passed": passed, "grades": grades, "metrics": metrics,
        "final_text": result["final_text"],
        "transcript": [
            block.model_dump() if hasattr(block, "model_dump") else block
            for msg in raw["messages"]
            for block in (msg["content"] if isinstance(msg["content"], list) else [msg["content"]])
        ],
    }


def run_eval(agent_fn, tasks, model=None, num_runs=1, max_workers=5):
    """Run the full eval suite. Returns structured results."""
    all_runs = []
    for _ in range(num_runs):
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(run_single_task, agent_fn, t, model): t for t in tasks}
            run_results = [f.result() for f in as_completed(futures)]
        task_order = {t["id"]: i for i, t in enumerate(tasks)}
        run_results.sort(key=lambda r: task_order.get(r["task_id"], 999))
        all_runs.append(run_results)
    return {"runs": all_runs, "config": {"model": model, "num_runs": num_runs, "num_tasks": len(tasks)}}


def save_results(results, directory="eval_results"):
    """Save eval results to a JSON file."""
    os.makedirs(directory, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    model_name = results["config"].get("model") or "default"
    model_short = model_name.split("-")[1] if "-" in str(model_name) else model_name
    filename = f"{directory}/eval_{model_short}_{timestamp}.json"
    with open(filename, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"Results saved to {filename}")
    return filename


def print_summary(results):
    """Print formatted eval results."""
    config = results["config"]
    print(f"{'=' * 60}")
    print(f"EVAL RESULTS: {config['num_tasks']} tasks, {config['num_runs']} run(s)")
    if config.get("model"): print(f"Model: {config['model']}")
    print(f"{'=' * 60}\n")

    for run_idx, run in enumerate(results["runs"]):
        if config["num_runs"] > 1: print(f"--- Run {run_idx + 1} ---")
        passed = sum(1 for r in run if r["passed"])
        total = len(run)
        print(f"Overall: {passed}/{total} passed ({passed/total*100:.0f}%)\n")

        # Per-category breakdown
        categories = {}
        for r in run:
            cat = r.get("category", "uncategorized")
            categories.setdefault(cat, {"passed": 0, "total": 0})
            categories[cat]["total"] += 1
            if r["passed"]: categories[cat]["passed"] += 1
        if len(categories) > 1:
            print("By category:")
            for cat, c in sorted(categories.items()):
                print(f"  {cat}: {c['passed']}/{c['total']} ({c['passed']/c['total']*100:.0f}%)")
            print()

        # Per-task detail
        print("Tasks:")
        for r in run:
            mark = "PASS" if r["passed"] else "FAIL"
            print(f"  [{mark}] {r['task_id']}: {r['task_description']}")
            for g in r.get("grades", []):
                print(f"    {'+' if g['score'] == 1.0 else '-'} {g['type']}: {g['reason'][:120]}")
            if r.get("error"): print(f"    Error: {r['error'][:200]}")

        # Aggregate metrics
        ok = [r for r in run if not r.get("error")]
        if ok:
            print(f"\nMetrics (avg): {sum(r['metrics']['time'] for r in ok)/len(ok):.2f}s, "
                  f"{sum(r['metrics']['tool_calls'] for r in ok)/len(ok):.1f} tool calls, "
                  f"{sum(r['metrics']['turns'] for r in ok)/len(ok):.1f} turns")
            print(f"Tokens: {sum(r['metrics']['input_tokens'] for r in ok):,} in, "
                  f"{sum(r['metrics']['output_tokens'] for r in ok):,} out")
        print()


def inspect_task(results, task_id, run_index=0):
    """Print detailed results for a specific task including transcript."""
    run = results["runs"][run_index]
    r = next((r for r in run if r["task_id"] == task_id), None)
    if r is None:
        print(f"Task '{task_id}' not found"); return

    print(f"[{'PASS' if r['passed'] else 'FAIL'}] {r['task_id']}: {r['task_description']}")
    print(f"Query: {r['query']}")
    print(f"Response: {r.get('final_text', 'N/A')}\n")
    if r.get("error"): print(f"ERROR:\n{r['error']}"); return

    print("Grades:")
    for g in r["grades"]:
        print(f"  {'+' if g['score'] == 1.0 else '-'} {g['type']}: {g['reason']}")
    print(f"\nMetrics: {r['metrics']}")

    print("\nTranscript:")
    for item in r.get("transcript", []):
        if isinstance(item, dict):
            t = item.get("type", "?")
            if t == "text": print(f"  [text] {item.get('text', '')[:300]}")
            elif t == "tool_use": print(f"  [tool_use] {item.get('name', '?')}({item.get('input', {})})")
            elif t == "tool_result": print(f"  [tool_result] {str(item.get('content', ''))[:200]}")
            else: print(f"  [{t}] {str(item)[:200]}")
        else: print(f"  {str(item)[:200]}")


print("Eval framework ready.")

tasks = [
    # ── Reference task ─────────────────────────────────────────────────────
    {
        "id": "price_jeans",
        "description": "Direct price lookup for jeans",
        "query": "How much do jeans cost?",
        "category": "product_lookup",
        "graders": [
            {"type": "response_contains", "checks": ["49.99"]},
            {"type": "tool_use", "checks": [{"tool_name": "get_product", "arguments": {"product": "jeans"}}]},
        ],
    },

    # ── Build tasks for these queries ──────────────────────────────────────

    # 1. "Price of a t-shirt?"
    #    "t-shirt" is a hyphenated catalog key. We verify the agent passed the
    #    exact key (not "tshirt" / "t shirt", which would KeyError on the bad specs).
    {
        "id": "price_tshirt",
        "description": "Price lookup with hyphenated product name",
        "query": "Price of a t-shirt?",
        "category": "product_lookup",
        "graders": [
            {"type": "response_contains", "checks": ["24.99"]},
            {"type": "tool_use", "checks": [{"tool_name": "get_product", "arguments": {"product": "t-shirt"}}]},
        ],
    },

    # 2. "How much for shoes?"
    #    "shoes" is NOT in the catalog; "sneakers" (74.99) is. Designed to expose
    #    the unimproved agent: we want it to call the tool and surface "sneakers",
    #    not hallucinate a price for a product that doesn't exist.
    {
        "id": "price_shoes_synonym",
        "description": "Synonym query: 'shoes' is not in catalog ('sneakers' is)",
        "query": "How much for shoes?",
        "category": "product_lookup",
        "graders": [
            {"type": "tool_use", "checks": [{"tool_name": "get_product"}]},
            {"type": "response_contains", "checks": ["sneakers"]},
        ],
    },

    # 3. "3 shirts and 2 belts, what's my total?"
    #    shirt = 29.99, belt = 24.99 -> 3*29.99 + 2*24.99 = 139.95.
    #    Requires multiple lookups plus calculate (* then +).
    {
        "id": "total_shirts_belts",
        "description": "Multi-item total requiring product lookups + calculation",
        "query": "3 shirts and 2 belts, what's my total?",
        "category": "multi_tool",
        "graders": [
            {"type": "response_numeric", "checks": [{"value": 139.95, "tolerance": 0.10}]},
            {"type": "tool_use", "checks": [
                {"tool_name": "get_product"},
                {"tool_name": "calculate", "arguments": {"op": "*"}},
                {"tool_name": "calculate", "arguments": {"op": "+"}},
            ]},
        ],
    },

    # 4. "What's 20% off a jacket?"
    #    jacket = 89.99, 20% off -> 89.99 * 0.80 = 71.99 (the discounted price,
    #    which is what shoppers mean). Tolerance absorbs 71.99 vs 71.992 vs 72.00.
    {
        "id": "discount_jacket",
        "description": "Calculate 20% off a jacket (lookup + percentage math)",
        "query": "What's 20% off a jacket?",
        "category": "calculation",
        "graders": [
            {"type": "response_numeric", "checks": [{"value": 71.99, "tolerance": 0.10}]},
            {"type": "tool_use", "checks": [
                {"tool_name": "get_product"},
                {"tool_name": "calculate"},
            ]},
        ],
    },

    # 5. "What do you sell?"  -> open-ended, can't be graded deterministically.
    #    It's added in the LLM-as-judge cell below (Part 6), after that grader
    #    is defined and registered.
]

results = run_eval(run_agent, tasks)
print_summary(results)
save_results(results)

# Replace with a task ID you want to inspect
inspect_task(results, "price_jeans")

baseline = run_eval(run_agent, tasks, num_runs=5)
print_summary(baseline)

# Implement the LLM-as-judge grader
#
# Deterministic graders can't score open-ended answers ("What do you sell?").
# An LLM-as-judge reads the query + the agent's response + a criterion and
# returns PASS/FAIL. Keep each criterion narrow so the judge evaluates one thing.

JUDGE_SYSTEM_PROMPT = """You are an eval grader. You will receive:
- The original user query
- An AI agent's response to that query
- A criterion to evaluate

Judge whether the agent's response meets the criterion. Focus only on the
specific criterion provided, not on overall response quality.

Respond with exactly one of these on the first line:
PASS - if the criterion is clearly met
FAIL - if the criterion is not met or only partially met

Then on the next line, give a brief reason (one sentence)."""


def grade_llm_judge(result, check, context=None):
    query = context["query"] if context else "Unknown query"
    response_text = result["final_text"]

    judge_prompt = f"""Original query: {query}

Agent's response: {response_text}

Criterion: {check}"""

    try:
        judge_response = client.messages.create(
            model="claude-haiku-4-5",   # valid alias (the "-20241022" suffix is the retired Haiku 3.5 date)
            max_tokens=150,
            temperature=0.0,
            system=JUDGE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": judge_prompt}],
        )
        judge_text = judge_response.content[0].text.strip()
        first_line = judge_text.split("\n")[0].strip().upper()
        reason = judge_text.split("\n", 1)[1].strip() if "\n" in judge_text else judge_text

        if "PASS" in first_line:
            return {"score": 1.0, "reason": f"LLM judge: {reason}"}
        elif "FAIL" in first_line:
            return {"score": 0.0, "reason": f"LLM judge: {reason}"}
        else:
            return {"score": 0.0, "reason": f"LLM judge returned unparseable response: {judge_text[:200]}"}
    except Exception as e:
        return {"score": 0.0, "reason": f"LLM judge error: {e}"}


# Register it so the runner can use it
GRADER_REGISTRY["llm_judge"] = grade_llm_judge
print(f"Graders now available: {list(GRADER_REGISTRY.keys())}")

# Add tasks that use the LLM-as-judge grader

llm_judge_tasks = [
    {
        "id": "what_do_you_sell",
        "description": "Open-ended: agent describes available products",
        "query": "What do you sell?",
        "category": "capabilities",
        "graders": [
            {"type": "llm_judge", "checks": [
                "Response describes or lists some of the available products in the catalog",
                "Response is helpful and relevant to a shopping context (not dismissive or off-topic)",
            ]},
        ],
    },
]

# Run eval with both task sets (deterministic + LLM-judged)
all_tasks = tasks + llm_judge_tasks
results = run_eval(run_agent, all_tasks)
print_summary(results)

# ── FACILITATOR REFERENCE: Tasks (Part 3) ────────────────────────────────────
# Reference tasks for all five queries from Part 3, plus one LLM-as-judge task.

reference_tasks = [
    # ── 1. Direct lookup (given as the worked example) ─────────────────────
    {
        "id": "price_jeans",
        "description": "Direct price lookup for jeans",
        "query": "How much do jeans cost?",
        "category": "product_lookup",
        "graders": [
            {"type": "response_contains", "checks": ["49.99"]},
            {"type": "tool_use", "checks": [{"tool_name": "get_product", "arguments": {"product": "jeans"}}]},
        ],
    },

    # ── 2. Hyphen edge case ────────────────────────────────────────────────
    # "t-shirt" is in the catalog as a hyphenated key. The question is whether
    # the agent passes "t-shirt" (correct) or "tshirt" / "t shirt" (KeyError).
    # With the bad tool specs, the agent has no way to know the exact format.
    # The tool_use check with arguments verifies the agent got the key right.
    {
        "id": "price_tshirt",
        "description": "Price lookup with hyphenated product name",
        "query": "Price of a t-shirt?",
        "category": "product_lookup",
        "graders": [
            {"type": "response_contains", "checks": ["24.99"]},
            {"type": "tool_use", "checks": [{"tool_name": "get_product", "arguments": {"product": "t-shirt"}}]},
        ],
    },

    # ── 3. Synonym / not-in-catalog ────────────────────────────────────────
    # "shoes" is NOT in the catalog. "sneakers" is (74.99). This task is
    # designed to FAIL with the unimproved agent. Two valid grading approaches:
    #
    # Option A (pre-improvement): Just check the tool was called. The agent
    #   will get a KeyError, and we verify it at least tried the tool rather
    #   than hallucinating a price.
    #
    # Option B (post-improvement): After improving tool specs to list valid
    #   products, check the agent suggests "sneakers" as an alternative.
    #
    {
        "id": "price_shoes_synonym",
        "description": "Synonym query: 'shoes' is not in catalog ('sneakers' is)",
        "query": "How much for shoes?",
        "category": "product_lookup",
        "graders": [
            {"type": "tool_use", "checks": [{"tool_name": "get_product"}]},
            {"type": "response_contains", "checks": ["sneakers"]},
        ],
    },

    # ── 4. Multi-tool: lookups + calculation ───────────────────────────────
    # shirt = 29.99, belt = 24.99
    # 3 * 29.99 = 89.97, 2 * 24.99 = 49.98, total = 139.95
    #
    # We check the numeric result AND that tools were used (obviously this can be improved)
    {
        "id": "total_shirts_belts",
        "description": "Multi-item total requiring product lookups + calculation",
        "query": "3 shirts and 2 belts, what's my total?",
        "category": "multi_tool",
        "graders": [
            {"type": "response_numeric", "checks": [{"value": 139.95, "tolerance": 0.10}]},
            {"type": "tool_use", "checks": [
                {"tool_name": "get_product"},
                {"tool_name": "calculate", "arguments": {"op": "*"}},
                {"tool_name": "calculate", "arguments": {"op": "+"}},
            ]},
        ],
    },

    # ── 5. Calculation: percentage off ─────────────────────────────────────
    # jacket = 89.99, 20% off = 89.99 * 0.80 = 71.992
    #
    # Agents may interpret "20% off" as either:
    #   - The discounted price: 71.99 (what most users mean)
    #   - The discount amount: 18.00
    # We check for the discounted price. The tolerance handles rounding
    # differences (71.99 vs 71.992 vs 72.00).
    {
        "id": "discount_jacket",
        "description": "Calculate 20% off a jacket (lookup + percentage math)",
        "query": "What's 20% off a jacket?",
        "category": "calculation",
        "graders": [
            {"type": "response_numeric", "checks": [{"value": 71.99, "tolerance": 0.10}]},
            {"type": "tool_use", "checks": [
                {"tool_name": "get_product"},
                {"tool_name": "calculate"},
            ]},
        ],
    },

    # ── 6. Open-ended (requires LLM-as-judge) ─────────────────────────────
    # This can't be graded with deterministic checks. There are many valid
    # ways to describe the catalog. We use two separate criteria so each
    # gets its own isolated LLM judge call.
    {
        "id": "what_do_you_sell",
        "description": "Open-ended: agent describes available products",
        "query": "What do you sell?",
        "category": "capabilities",
        "graders": [
            {"type": "llm_judge", "checks": [
                "Response describes or lists some of the available products in the catalog",
                "Response is helpful and relevant to a shopping context (not dismissive or off-topic)",
            ]},
        ],
    },
]

print(f"Reference tasks loaded: {len(reference_tasks)} tasks")
for t in reference_tasks:
    grader_types = [g["type"] for g in t["graders"]]
    print(f"  {t['id']:25s} [{t['category']}] graders: {grader_types}")

# ── FACILITATOR REFERENCE: LLM-as-Judge Grader (Part 6) ──────────────────────

JUDGE_SYSTEM_PROMPT = """You are an eval grader. You will receive:
- The original user query
- An AI agent's response to that query
- A criterion to evaluate

Judge whether the agent's response meets the criterion. Focus only on the
specific criterion provided, not on overall response quality.

Respond with exactly one of these on the first line:
PASS - if the criterion is clearly met
FAIL - if the criterion is not met or only partially met

Then on the next line, give a brief reason (one sentence)."""


def grade_llm_judge(result, check, context=None):
    query = context["query"] if context else "Unknown query"
    response_text = result["final_text"]

    judge_prompt = f"""Original query: {query}

Agent's response: {response_text}

Criterion: {check}"""

    try:
        judge_response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=150,
            temperature=0.0,
            system=JUDGE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": judge_prompt}],
        )
        judge_text = judge_response.content[0].text.strip()
        first_line = judge_text.split("\n")[0].strip().upper()
        reason = judge_text.split("\n", 1)[1].strip() if "\n" in judge_text else judge_text

        if "PASS" in first_line:
            return {"score": 1.0, "reason": f"LLM judge: {reason}"}
        elif "FAIL" in first_line:
            return {"score": 0.0, "reason": f"LLM judge: {reason}"}
        else:
            return {"score": 0.0, "reason": f"LLM judge returned unparseable response: {judge_text[:200]}"}

    except Exception as e:
        return {"score": 0.0, "reason": f"LLM judge error: {e}"}


# Register it
GRADER_REGISTRY["llm_judge"] = grade_llm_judge
print(f"Graders now available: {list(GRADER_REGISTRY.keys())}")

# ── FACILITATOR REFERENCE: Improved Agent (Part 5) ───────────────────────────
# These are the three changes that should move the agent from ~50% to ~100% pass rate in this dummy eval.
# Audience should discover these through eval failures. Show selectively.

# ── Fix 1: Better system prompt ───────────────────────────────────────────────
SYSTEM_PROMPT = (
    "You are Boutique, a shopping assistant. You help customers find products, "
    "check prices, and calculate totals. Always use your tools to look up prices "
    "rather than guessing. If a product isn't found, suggest similar items from "
    "the catalog. Never do mental math, always use your calculate tool for any calculations."
)

# ── Fix 2: Better tool specs ─────────────────────────────────────────────────
# The original specs just say "get_product" and "product" with no context.
# Claude has no idea what products exist, what format to use, or what happens
# on error. These specs fix that.

GET_PRODUCT_SPEC = {
    "name": "get_product",
    "description": "Look up the price of a product from the store catalog. Returns the price as a number. Raises a KeyError if the product is not found.",
    "input_schema": {
        "type": "object",
        "properties": {
            "product": {
                "type": "string",
                "description": "Product name, lowercase. Available products: jeans, shirt, dress, jacket, sneakers, hat, socks, hoodie, shorts, t-shirt, sweater, belt",
            },
        },
        "required": ["product"],
    },
}

CALCULATE_SPEC = {
    "name": "calculate",
    "description": "Perform a math operation on two numbers. Use this for any arithmetic instead of doing mental math.",
    "input_schema": {
        "type": "object",
        "properties": {
            "op": {
                "type": "string",
                "description": "The math operator to apply.",
                "enum": ["+", "-", "*", "/", "**"],
            },
            "input1": {
                "type": "number",
                "description": "The first operand.",
            },
            "input2": {
                "type": "number",
                "description": "The second operand.",
            },
        },
        "required": ["op", "input1", "input2"],
    },
}

ALL_TOOL_SPECS = [GET_PRODUCT_SPEC, CALCULATE_SPEC]

# ── Fix 3: Better error handling in tool implementation ───────────────────────
# The original get_product just does catalog[product], which throws a raw
# KeyError. This version returns a helpful message instead.

def get_product(product: str):
    catalog = {
        "jeans": 49.99, "shirt": 29.99, "dress": 59.99, "jacket": 89.99,
        "sneakers": 74.99, "hat": 19.99, "socks": 9.99, "hoodie": 44.99,
        "shorts": 34.99, "t-shirt": 24.99, "sweater": 54.99, "belt": 24.99,
    }
    if product in catalog:
        return catalog[product]
    available = ", ".join(sorted(catalog.keys()))
    return f"Product '{product}' not found. Available products: {available}"

TOOL_REGISTRY["get_product"] = get_product

print("Improved agent loaded. Re-run the eval to see the difference.")

# ── Run the full eval with reference tasks ────────────────────────────────────
# results = run_eval(run_agent, reference_tasks)
# print_summary(results)
# save_results(results)


