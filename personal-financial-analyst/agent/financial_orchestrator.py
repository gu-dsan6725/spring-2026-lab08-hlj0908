"""Financial Optimization Orchestrator Agent (FINAL PASS VERSION)"""

import argparse
import asyncio
import json
import logging
import re
from pathlib import Path

from claude_agent_sdk import (
    ClaudeSDKClient,
    ClaudeAgentOptions,
    AgentDefinition,
    AssistantMessage,
    ResultMessage,
    TextBlock,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent / "data"
RAW_DATA_DIR = DATA_DIR / "raw_data"
AGENT_OUTPUTS_DIR = DATA_DIR / "agent_outputs"


# Utils
def _ensure_directories():
    RAW_DATA_DIR.mkdir(parents=True, exist_ok=True)
    AGENT_OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)


def _save_json(data, filename):
    filepath = RAW_DATA_DIR / filename
    with open(filepath, "w") as f:
        json.dump(data, f, indent=2)
    logger.info(f"Saved: {filepath}")


def _load_prompt(filename: str) -> str:
    return (Path(__file__).parent / "prompts" / filename).read_text()


async def _auto_approve_all(tool_name, input_data, context):
    from claude_agent_sdk import PermissionResultAllow
    return PermissionResultAllow()


# Subscription Detection
def _detect_subscriptions(bank, card):
    subs = []
    seen = set()

    for tx in bank + card:
        if not tx.get("recurring"):
            continue

        amt = float(tx.get("amount", 0))
        if amt >= 0:
            continue

        name = tx.get("merchant") or tx.get("description") or "Unknown"

        key = (name, abs(amt))
        if key not in seen:
            subs.append({
                "service": name,
                "amount": abs(amt),
                "frequency": "monthly"
            })
            seen.add(key)

    return subs


# MCP Fetching
async def _fetch_financial_data(username, start, end):
    logger.info("Fetching data from MCP...")

    working_dir = Path(__file__).parent.parent

    mcp_servers = {
        "Bank Account Server": {
            "type": "http",
            "url": "http://127.0.0.1:5001/mcp"
        },
        "Credit Card Server": {
            "type": "http",
            "url": "http://127.0.0.1:5002/mcp"
        }
    }

    options = ClaudeAgentOptions(
        model="haiku",
        mcp_servers=mcp_servers,
        can_use_tool=_auto_approve_all,
        cwd=working_dir,
        system_prompt="You are a tool-calling agent. Never ask questions."
    )

    prompt = f"""
Call MCP tools immediately:

get_bank_transactions(username="{username}", start_date="{start}", end_date="{end}")
get_credit_card_transactions(username="{username}", start_date="{start}", end_date="{end}")

Return ONLY JSON.
"""

    response = ""

    async with ClaudeSDKClient(options=options) as client:
        await client.query(prompt)

        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, TextBlock):
                        response += b.text
            elif isinstance(msg, ResultMessage):
                logger.info(f"Cost: ${msg.total_cost_usd:.4f}")
                break

    # JSON
    clean = response.strip()
    match = re.search(r"```json\s*(.*?)\s*```", clean, re.DOTALL)
    if match:
        clean = match.group(1)
    else:
        clean = clean.replace("```", "").strip()

    parsed = json.loads(clean)

    bank = parsed.get("bank_data", {})
    card = parsed.get("credit_card_data", {})

    _save_json(bank, "bank_transactions.json")
    _save_json(card, "credit_card_transactions.json")

    return bank, card


# Orchestrator
async def _run_orchestrator(username, start, end, query):
    _ensure_directories()

    bank_data, card_data = await _fetch_financial_data(username, start, end)

    bank_tx = bank_data.get("transactions", [])
    card_tx = card_data.get("transactions", [])

    subs = _detect_subscriptions(bank_tx, card_tx)
    _save_json(subs, "subscriptions.json")

    logger.info(f"Subscriptions: {len(subs)}")

    agents = {
        "research_agent": AgentDefinition(
            description="Find cheaper alternatives",
            prompt=_load_prompt("research_agent_prompt.txt"),
            tools=["write"],
            model="haiku",
        ),
        "negotiation_agent": AgentDefinition(
            description="Negotiation strategies",
            prompt=_load_prompt("negotiation_agent_prompt.txt"),
            tools=["write"],
            model="haiku",
        ),
        "tax_agent": AgentDefinition(
            description="Tax optimization",
            prompt=_load_prompt("tax_agent_prompt.txt"),
            tools=["write"],
            model="haiku",
        ),
    }

    options = ClaudeAgentOptions(
        model="sonnet",
        system_prompt=_load_prompt("orchestrator_system_prompt.txt"),
        agents=agents,
        can_use_tool=_auto_approve_all,
        cwd=Path(__file__).parent.parent,
    )

    prompt = f"""
Analyze finances: {query}

You MUST do ALL steps:

1. Call research_agent
   → MUST write file:
   data/agent_outputs/research_results.json

2. Call negotiation_agent
   → MUST write file:
   data/agent_outputs/negotiation_scripts.json

3. Call tax_agent
   → MUST write file:
   data/agent_outputs/tax_analysis.json

4. AFTER all agents finish:
   → Read their outputs
   → Write final report:

   data/final_report.md

RULES:
- Do NOT skip any agent
- Do NOT only explain
- You MUST execute tools
- FAIL if any file is missing
"""

    async with ClaudeSDKClient(options=options) as client:
        await client.query(prompt)

        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, TextBlock):
                        print(b.text, end="", flush=True)
            elif isinstance(msg, ResultMessage):
                print()
                logger.info(f"Total cost: ${msg.total_cost_usd:.4f}")
                break

    # Test 3
    required_files = [
        RAW_DATA_DIR / "bank_transactions.json",
        RAW_DATA_DIR / "credit_card_transactions.json",
        AGENT_OUTPUTS_DIR / "research_results.json",
        AGENT_OUTPUTS_DIR / "negotiation_scripts.json",
        AGENT_OUTPUTS_DIR / "tax_analysis.json",
        DATA_DIR / "final_report.md",
    ]

    print("\n===== TEST 3 RESULT =====")
    for f in required_files:
        if f.exists():
            print(f"{f}")
        else:
            print(f"MISSING: {f}")
    print("========================")

# CLI
def _parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--username", required=True)
    p.add_argument("--start-date", required=True)
    p.add_argument("--end-date", required=True)
    p.add_argument("--query", required=True)
    return p.parse_args()

async def main():
    args = _parse_args()
    await _run_orchestrator(
        args.username,
        args.start_date,
        args.end_date,
        args.query
    )

if __name__ == "__main__":
    asyncio.run(main())