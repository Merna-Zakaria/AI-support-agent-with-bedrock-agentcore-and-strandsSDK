
"""
Customer Support AI Agent
=========================

Run locally:
  uv run main.py '{"prompt": "Hello", "customer_id": "CUST-123", "session_id": "s1"}'

Deploy to AgentCore:
  agentcore deploy

Invoke deployed agent:
  agentcore invoke '{"prompt": "Hello", "customer_id": "CUST-123", "session_id": "s1"}'
"""

# ── Imports ───────────────────────────────────────────────────────────────────
from strands import Agent, tool
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.memory import MemoryClient
from strands.models import BedrockModel
from strands.tools.mcp.mcp_client import MCPClient
from mcp.client.streamable_http import streamable_http_client
import argparse, json
import os, re, sys, time, threading, asyncio, boto3
from strands.hooks import (
    HookProvider, AfterInvocationEvent, HookRegistry, MessageAddedEvent,
)
import logging
import uuid
from typing import Dict, Optional
from bedrock_agentcore.tools.code_interpreter_client import code_session
from strands_tools.browser import AgentCoreBrowser
import nest_asyncio
import playwright

# NOTE: `strands_tools.browser` (AgentCoreBrowser) is imported lazily inside
# get_browser_tool() so its heavy dependencies don't slow down cold start.


# INFO level so the logger.info(...) diagnostics show up in CloudWatch.
# Set LOG_LEVEL=WARNING in the environment to quiet things down later.
logging.basicConfig(level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO))
logger = logging.getLogger("CSAI_Agent")

# ── App Initialisation ────────────────────────────────────────────────────────
app = BedrockAgentCoreApp()

# Suppress interactive tool-consent prompts (required in headless deployments).
os.environ["BYPASS_TOOL_CONSENT"] = "true"


# ── Configuration ─────────────────────────────────────────────────────────────
# GATEWAY_URL must be the AgentCore Gateway MCP endpoint:
#   https://<alias>.gateway.bedrock-agentcore.<region>.amazonaws.com/mcp
# KB_ID is the 10-character Bedrock Knowledge Base ID, copied exactly from the console.
# MEMORY_ID is copied exactly from the AgentCore Memory console (no extra spaces).
GATEWAY_URL = "https://customersupportgateway-3ehsyrs3aj.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
KB_ID = os.environ.get("KB_ID", "")  # set the KB_ID env var, or put your real 10-char ID as the default here
REGION = "us-east-1"
MEMORY_ID = "CustomerSupportMemory-17ZK5t7lpo"

# Feature switches (env vars, all optional).
#   REQUIRE_GATEWAY=true  -> do NOT fall back to local-only tools if the Gateway is down;
#                            return an explicit error instead (use this while testing so a
#                            broken MCP setup can't hide behind the fallback).
#   BROWSER_MODE=auto     -> (default) only load the browser tool for requests that look like
#                            they need the web, so order/KB/loyalty requests never pay the
#                            browser init cost.
#   BROWSER_MODE=always   -> always load it (original behaviour; use once timeouts are resolved).
#   BROWSER_MODE=never    -> never load it (isolates browser as a timeout cause).
REQUIRE_GATEWAY = os.environ.get("REQUIRE_GATEWAY", "false").lower() == "true"
BROWSER_MODE = os.environ.get("BROWSER_MODE", "auto").lower()
if BROWSER_MODE not in ("auto", "always", "never"):
    logger.warning("Unknown BROWSER_MODE %r, using 'auto'", BROWSER_MODE)
    BROWSER_MODE = "auto"

# Sanity checks: surface configuration mistakes in the logs instead of failing silently.
if not (GATEWAY_URL.startswith("https://") and ".gateway.bedrock-agentcore." in GATEWAY_URL and GATEWAY_URL.endswith("/mcp")):
    logger.warning("GATEWAY_URL does not look like an AgentCore Gateway MCP endpoint: %s", GATEWAY_URL)
if not KB_ID:
    logger.error("KB_ID is NOT set in this runtime - search_knowledge_base will not work. "
                 "Set the KB_ID env var in the deployment or hardcode the ID above.")
if MEMORY_ID != MEMORY_ID.strip() or not MEMORY_ID:
    logger.warning("MEMORY_ID is empty or contains leading/trailing whitespace: %r", MEMORY_ID)


# ── Model and Clients ─────────────────────────────────────────────────────────
model_id = "global.amazon.nova-2-lite-v1:0"
model = BedrockModel(model_id=model_id)
memory_client = MemoryClient(region_name=REGION)
_bedrock_runtime = boto3.client("bedrock-agent-runtime", region_name=REGION)


# ── System Prompt ─────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """
You are an AI customer support assistant for an e-commerce platform.

You help customers with:
- Order tracking and refunds
- Returns and policies
- Product information and recommendations
- Loyalty points and discounts
- Customer preferences and conversation history
- Current information requiring web browsing

CORE RULES
- Be helpful, concise, accurate, and professional.
- Never guess or invent orders, prices, policies, points, refunds, or product information.
- Always use the appropriate tool when authoritative or exact information is required.
- Never claim an action was completed unless the tool confirms success.
- Ask a concise clarification question when required information is missing.

KNOWLEDGE BASE
You have access to `search_knowledge_base`.

ALWAYS use it before answering questions about:
- Product specifications/features
- Return, refund, or warranty policies
- Loyalty program rules
- Order status definitions
- Any company policy or policy-related number

Base answers on the retrieved content. Preserve exact figures and conditions.
If the knowledge base does not contain the answer, say so honestly.
Do not use general model knowledge to replace missing company documentation.

AGENTCORE GATEWAY / MCP
Use Gateway tools for live customer and order information, such as:
- Order status and details
- Customer/account information
- Refund operations

Never invent order IDs, account information, refund amounts, or transaction IDs.
For actions such as refunds, only confirm success when the backend tool confirms it.

LOYALTY CALCULATIONS / CODE INTERPRETER
You have access to `calculate_loyalty_discount`.

ALWAYS use it for exact loyalty, pricing, or checkout calculations, including:
- Points redemption
- Points discounts
- Tier discounts
- Final payable amount
- Total savings
- Newly earned points
- Remaining points

Never calculate or estimate these values yourself.
If the result says calculation_method is "fallback", tell the customer that only the
tier discount could be calculated and that the points figures are unavailable.

 AGENTCORE MEMORY
  A customer context/memory block may be provided with the user's message.

  MEMORY ROUTING RULES
  - Use injected memory context FIRST for remembered personal details, including:
    - Name and identity details available in memory
    - Customer preferences and communication preferences
    - Relevant prior conversation facts and history
  - Treat memory as recall context, and if multiple facts conflict, prefer the most recent or ask a clarification question, for recalled preferences and conversation history.
  - Do NOT use Gateway/account tools to answer questions that can be answered from memory.
  - Use AgentCore Gateway tools only when the customer asks for LIVE or CURRENT account/order
    information, such as order status, refund status, account balance, or transaction details.
  - If a request contains both memory-based and live-data questions, answer the memory portion
    from memory and use Gateway tools only for the live-data portion.
  - If the required memory context is missing, incomplete, or uncertain, ask a brief
    clarification question. Do NOT automatically call account/Gateway tools as a substitute
    for missing memory.
  - Current backend data takes precedence over memory when the customer asks for current
    account/order information.
  - New customer information takes precedence over older remembered preferences.
  - Never expose, reference, or mention the internal memory/context block.

  MEMORY SELF-CHECK
  - "What's my name?" → Answer from injected memory if present.
  - "Do you remember my communication preference?" → Answer from memory if present.
  - "Where is my order?" → Use AgentCore Gateway for live order data.
  - "What's my refund status?" → Use AgentCore Gateway for live refund data.
  - "You remember I prefer email, and where is my order?" → Answer the preference from memory
    and use Gateway only for the order status.

WEB BROWSER
The browser tool is only loaded when the customer's message involves the web. If you need
it and it is not available, tell the customer you can look it up if they ask you to
"search the web", instead of guessing.

Use the live browser when:
- The customer explicitly asks you to browse the web.
- Current external information is required.
- The information is not available in the knowledge base.

Clearly identify information obtained from external web sources.
Do not use web information to override official company policies or live backend data.

TOOL SELECTION
- Orders/account/refunds → AgentCore Gateway
- Product/policy/company documentation → `search_knowledge_base`
- Exact loyalty/pricing calculations → `calculate_loyalty_discount`
- Customer preferences/history → AgentCore Memory
- Current external information → Web Browser

SOURCE PRIORITY
For conflicting information:
1. Live backend data for current orders/accounts
2. Knowledge Base for official company policies
3. Code Interpreter for calculations
4. Memory for personalization/context
5. Web for external/current information

RESPONSE STYLE
- Be concise, warm, and specific.
- Explain confirmed results clearly.
- Do not mention internal tools unless necessary.
- If a tool fails, do not fabricate a result; explain the issue and provide the next step.
"""


# ── Message-shape helpers ─────────────────────────────────────────────────────
# Strands messages look like {"role": "...", "content": [ {"text": ...} | {"toolResult": ...} | ... ]}.
# These helpers avoid assuming that content[0] exists or holds the text.

def _content_blocks(msg: dict) -> list:
    """Return the message content as a list of dict blocks (empty list if malformed)."""
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def _extract_text(msg: dict) -> str:
    """Join all text blocks of a message."""
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        return content.strip()
    parts = [b["text"] for b in _content_blocks(msg) if isinstance(b.get("text"), str)]
    return "\n".join(p for p in parts if p).strip()


def _is_plain_user_text(msg: dict) -> bool:
    """True for a user message that carries text and is not a tool result."""
    if not isinstance(msg, dict) or msg.get("role") != "user":
        return False
    blocks = _content_blocks(msg)
    if not blocks or any("toolResult" in b for b in blocks):
        return False
    return any(isinstance(b.get("text"), str) for b in blocks)


# # ── Namespace Helper ──────────────────────────────────────────────────────────
# def get_namespaces(mem_client: MemoryClient, memory_id: str) -> Dict:
#     """
#     Fetch the namespace template for each memory strategy.

#     Returns e.g.:
#       { "SEMANTIC": "cs_agent/{actorId}/facts",
#         "USER_PREFERENCE": "cs_agent/{actorId}/preferences" }
#     """
#     strategies = mem_client.get_memory_strategies(memory_id)
#     return {s["type"]: s["namespaces"][0] for s in strategies}


# # Namespace templates never change per request, so look them up once per container.
# _NAMESPACE_CACHE: Dict[str, Dict] = {}


# def get_cached_namespaces(mem_client: MemoryClient, memory_id: str) -> Dict:
#     """Return namespace templates, fetching them from AgentCore Memory only on first use."""
#     if memory_id not in _NAMESPACE_CACHE:
#         _NAMESPACE_CACHE[memory_id] = get_namespaces(mem_client, memory_id)
#         logger.info("Namespaces loaded: %s", _NAMESPACE_CACHE[memory_id])
#     return _NAMESPACE_CACHE[memory_id]


# # ── Memory Hook ───────────────────────────────────────────────────────────────
# class MemoryHook(HookProvider):
#     """
#     Long-term memory hook for the customer support agent.

#     - MessageAddedEvent    → retrieve memories from each strategy namespace
#                              and prepend them as "Customer Context"
#     - AfterInvocationEvent → save the (USER, ASSISTANT) pair via create_event()
#                              so the memory service can extract facts/preferences
#     """

#     def __init__(
#         self,
#         actor_id: str,
#         session_id: str,
#         memory_client: MemoryClient,
#         memory_id: str,
#     ):
#         self.memory_client = memory_client
#         self.memory_id = memory_id
#         self.actor_id = actor_id
#         self.session_id = session_id
#         # The clean customer query, captured BEFORE memory context is prepended.
#         self._original_query: Optional[str] = None
#         # Namespaces are fetched lazily (see retrieve_customer_context) so no
#         # network call happens during request startup.

#     def retrieve_customer_context(self, event: MessageAddedEvent):
#         """Search each memory namespace and prepend results to the user's message."""
#         actor_id = event.agent.state.get("actor_id")
#         if not actor_id:
#             return

#         messages = event.agent.messages
#         if not messages or not _is_plain_user_text(messages[-1]):
#             return

#         blocks = _content_blocks(messages[-1])
#         text_block = next(b for b in blocks if isinstance(b.get("text"), str))
#         original_text = text_block["text"]

#         # Remember the clean query so it — not the augmented text — is saved to memory.
#         self._original_query = original_text

#         try:
#             namespaces = get_cached_namespaces(self.memory_client, self.memory_id)

#             all_context = []
#             for strategy_type, namespace in namespaces.items():
#                 resolved_namespace = namespace.format(actorId=actor_id)
#                 memories = self.memory_client.retrieve_memories(
#                     memory_id=self.memory_id,
#                     namespace=resolved_namespace,
#                     query=original_text,
#                     top_k=2,
#                 )
#                 for memory in memories:
#                     if isinstance(memory, dict):
#                         text = memory.get("content", {}).get("text", "").strip()
#                         if text:
#                             all_context.append(f"[{strategy_type}] {text}")

#             if all_context:
#                 context_block = "\n".join(all_context)
#                 text_block["text"] = f"Customer Context:\n{context_block}\n\n{original_text}"
#                 logger.info("Retrieved %d memory items for actor %s", len(all_context), actor_id)

#         except Exception as exc:
#             logger.error("Failed to retrieve customer context: %s", exc)

#     def save_support_interaction(self, event: AfterInvocationEvent):
#         """Persist the most recent (USER, ASSISTANT) pair so memories can be extracted."""
#         actor_id = event.agent.state.get("actor_id")
#         session_id = event.agent.state.get("session_id")
#         if not actor_id or not session_id:
#             return

#         try:
#             messages = event.agent.messages
#             user_text = self._original_query
#             agent_text = None

#             for msg in reversed(messages):
#                 if agent_text is None and msg.get("role") == "assistant":
#                     # Last assistant message that actually contains text
#                     # (skips tool-use-only messages).
#                     text = _extract_text(msg)
#                     if text:
#                         agent_text = text
#                 elif user_text is None and _is_plain_user_text(msg):
#                     # Fallback only: normally the clean query was captured earlier.
#                     user_text = _extract_text(msg)
#                 if agent_text and user_text:
#                     break

#             if user_text and agent_text:
#                 self.memory_client.create_event(
#                     memory_id=self.memory_id,
#                     actor_id=actor_id,
#                     session_id=session_id,
#                     messages=[
#                         (user_text, "USER"),
#                         (agent_text, "ASSISTANT"),
#                     ],
#                 )
#                 logger.info("Saved interaction to memory for actor %s", actor_id)

#         except Exception as exc:
#             logger.error("Failed to save interaction: %s", exc)

#     def register_hooks(self, registry: HookRegistry) -> None:  # type: ignore
#         """Register both memory callbacks."""
#         registry.add_callback(MessageAddedEvent, self.retrieve_customer_context)
#         registry.add_callback(AfterInvocationEvent, self.save_support_interaction)




























import logging
import re
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from botocore.exceptions import BotoCoreError, ClientError

# Assumes these already exist in your project, as in the original code:
# MemoryClient, HookProvider, HookRegistry, MessageAddedEvent, AfterInvocationEvent,
# _is_plain_user_text, _content_blocks, _extract_text

logger = logging.getLogger(__name__)

# ── Tunables ──────────────────────────────────────────────────────────────────
TOP_K_PER_NAMESPACE = 2        # was 5; keep small, raise only after retrieval quality is verified
MAX_TOTAL_MEMORIES = 4         # hard cap on what is injected into the prompt
MIN_SCORE = None               # e.g. 0.5 if your retrieval results include a relevance score
MAX_AGE_DAYS = None            # e.g. 180 to drop very old memories
LOG_MEMORY_TEXT = False        # set True while debugging (may log PII)
SAVE_ASSISTANT_TURNS = True    # set False during debugging to avoid reinforcing bad output

STATE_ORIGINAL_QUERY = "_memory_original_query"
STATE_MEMORY_TAINTED = "_memory_tainted"   # set when retrieved memory had conflicts

MEMORY_PREAMBLE = (
    "Unverified memory notes about this customer. They may be stale, incomplete or wrong. "
    "Use them only as hints. If they conflict with the current conversation or tool results, "
    "trust the latter. Never present them as confirmed facts, and confirm identity details "
    "(name, account) with the customer or a lookup tool before relying on them."
)

# Identity-style claims, where two different values for one actor means a conflict.
_IDENTITY_PATTERN = re.compile(
    r"\b(?:customer(?:'s)?\s+name\s+is|name\s+is|is\s+called|is\s+named)\s+([A-Za-z][\w'\-]*)",
    re.IGNORECASE,
)

# Assistant outputs we don't want to write back into long-term memory.
_BAD_OUTPUT_PATTERNS = re.compile(
    r"(i (?:can't|cannot|am unable to)|something went wrong|an error occurred|"
    r"i don't have access|i'm sorry, but)",
    re.IGNORECASE,
)
_MAX_ASSISTANT_CHARS = 4000


# ── Namespace Helper ──────────────────────────────────────────────────────────
def get_namespaces(mem_client: "MemoryClient", memory_id: str) -> Dict[str, List[str]]:
    """
    Fetch ALL namespace templates for each memory strategy.

    Returns e.g.:
      { "SEMANTIC": ["cs_agent/{actorId}/facts"],
        "USER_PREFERENCE": ["cs_agent/{actorId}/preferences"] }

    Strategies with no namespaces are skipped (and logged) instead of crashing.
    """
    strategies = mem_client.get_memory_strategies(memory_id)
    result: Dict[str, List[str]] = {}

    for s in strategies:
        strategy_type = s.get("type")
        namespaces = [ns for ns in (s.get("namespaces") or []) if isinstance(ns, str) and ns.strip()]

        if not strategy_type:
            logger.warning("Memory %s: strategy without a type skipped: %s", memory_id, s)
            continue
        if not namespaces:
            logger.warning("Memory %s: strategy %s has no namespaces, skipping", memory_id, strategy_type)
            continue
        if len(namespaces) > 1:
            logger.info(
                "Memory %s: strategy %s has %d namespaces, all will be searched",
                memory_id, strategy_type, len(namespaces),
            )
        result[strategy_type] = namespaces

    if not result:
        logger.error("Memory %s: no usable strategy namespaces found", memory_id)
    return result


# Namespace templates never change per request, so look them up once per container.
_NAMESPACE_CACHE: Dict[str, Dict[str, List[str]]] = {}
_NAMESPACE_LOCK = threading.Lock()


def get_cached_namespaces(mem_client: "MemoryClient", memory_id: str) -> Dict[str, List[str]]:
    """Return namespace templates, fetching them from AgentCore Memory only on first use."""
    with _NAMESPACE_LOCK:
        if memory_id not in _NAMESPACE_CACHE:
            namespaces = get_namespaces(mem_client, memory_id)
            if namespaces:  # don't cache an empty/failed result
                _NAMESPACE_CACHE[memory_id] = namespaces
                logger.info("Namespaces loaded for memory %s: %s", memory_id, namespaces)
            return namespaces
        return _NAMESPACE_CACHE[memory_id]


# ── Memory post-processing helpers ────────────────────────────────────────────
def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _created_ts(memory: Dict[str, Any]) -> float:
    """Best-effort creation timestamp (epoch seconds); 0 if unknown."""
    raw = memory.get("createdAt") or memory.get("created_at") or memory.get("timestamp")
    if isinstance(raw, datetime):
        return raw.timestamp()
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def _select_memories(candidates: List[Dict[str, Any]]) -> tuple[List[Dict[str, Any]], bool]:
    """
    Filter, dedupe, recency-sort and resolve identity conflicts.
    Returns (selected_memories, had_conflict).
    """
    now = datetime.now(timezone.utc).timestamp()

    # 1. Filter by relevance score and age.
    kept = []
    for c in candidates:
        if MIN_SCORE is not None and c["score"] is not None and c["score"] < MIN_SCORE:
            continue
        if MAX_AGE_DAYS is not None and c["ts"] and (now - c["ts"]) > MAX_AGE_DAYS * 86400:
            continue
        kept.append(c)

    # 2. Dedupe on normalized text (keep the newest copy).
    kept.sort(key=lambda c: c["ts"], reverse=True)
    seen, deduped = set(), []
    for c in kept:
        key = _normalize(c["text"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(c)

    # 3. Identity conflict handling: if different names appear, keep only the newest claim.
    identity_items = []
    for c in deduped:
        m = _IDENTITY_PATTERN.search(c["text"])
        if m:
            identity_items.append((c, m.group(1).lower()))

    had_conflict = len({name for _, name in identity_items}) > 1
    if had_conflict:
        newest = identity_items[0][0]  # deduped is already newest-first
        dropped = [c for c, _ in identity_items if c is not newest]
        deduped = [c for c in deduped if not any(c is d for d in dropped)]
        logger.warning(
            "Conflicting identity memories detected; keeping newest only. kept=%r dropped=%d",
            newest["text"] if LOG_MEMORY_TEXT else "<hidden>", len(dropped),
        )

    # 4. Cap the total (already newest-first; break ties by score).
    deduped.sort(key=lambda c: (c["ts"], c["score"] or 0), reverse=True)
    return deduped[:MAX_TOTAL_MEMORIES], had_conflict


def _is_safe_to_save(agent_text: str) -> bool:
    if not agent_text or len(agent_text) > _MAX_ASSISTANT_CHARS:
        return False
    return not _BAD_OUTPUT_PATTERNS.search(agent_text)


# ── Memory Hook ───────────────────────────────────────────────────────────────
class MemoryHook(HookProvider):
    """
    Long-term memory hook for the customer support agent.

    - MessageAddedEvent    → retrieve memories from each strategy namespace,
                             filter/dedupe/resolve conflicts, and prepend them as
                             clearly-labelled UNVERIFIED context
    - AfterInvocationEvent → save the (USER, ASSISTANT) pair via create_event(),
                             but skip assistant output that looks unreliable

    Request-specific data (the clean query, conflict flag) lives in agent state,
    NOT on this shared instance, so overlapping requests can't overwrite each other.
    """

    def __init__(
        self,
        actor_id: str,
        session_id: str,
        memory_client: "MemoryClient",
        memory_id: str,
    ):
        self.memory_client = memory_client
        self.memory_id = memory_id
        self.actor_id = actor_id
        self.session_id = session_id

    # ── Retrieval ─────────────────────────────────────────────────────────────
    def retrieve_customer_context(self, event: "MessageAddedEvent"):
        """Search each memory namespace and prepend cleaned results to the user's message."""
        actor_id = event.agent.state.get("actor_id")
        if not actor_id:
            return

        messages = event.agent.messages
        if not messages or not _is_plain_user_text(messages[-1]):
            return

        blocks = _content_blocks(messages[-1])
        text_block = next((b for b in blocks if isinstance(b.get("text"), str)), None)
        if text_block is None:
            return
        original_text = text_block["text"]

        # Per-request state lives on the agent, not on self.
        event.agent.state.set(STATE_ORIGINAL_QUERY, original_text)
        event.agent.state.set(STATE_MEMORY_TAINTED, False)

        current_namespace: Optional[str] = None
        try:
            namespaces = get_cached_namespaces(self.memory_client, self.memory_id)
            if not namespaces:
                return

            candidates: List[Dict[str, Any]] = []
            for strategy_type, templates in namespaces.items():
                for template in templates:
                    try:
                        current_namespace = template.format(actorId=actor_id)
                    except (KeyError, IndexError) as exc:
                        logger.error(
                            "Bad namespace template %r for strategy %s (memory %s): %r",
                            template, strategy_type, self.memory_id, exc,
                        )
                        continue

                    memories = self.memory_client.retrieve_memories(
                        memory_id=self.memory_id,
                        namespace=current_namespace,
                        query=original_text,
                        top_k=TOP_K_PER_NAMESPACE,
                    )
                    logger.info(
                        "Retrieved %d raw items | actor=%s memory=%s namespace=%s",
                        len(memories or []), actor_id, self.memory_id, current_namespace,
                    )

                    for memory in memories or []:
                        if not isinstance(memory, dict):
                            logger.warning("Unexpected memory type %s in %s", type(memory), current_namespace)
                            continue
                        text = ((memory.get("content") or {}).get("text") or "").strip()
                        if not text:
                            continue
                        if LOG_MEMORY_TEXT:
                            logger.debug("Memory [%s] %s: %s", strategy_type, current_namespace, text)
                        candidates.append(
                            {
                                "strategy": strategy_type,
                                "text": text,
                                "score": memory.get("score"),
                                "ts": _created_ts(memory),
                            }
                        )

            selected, had_conflict = _select_memories(candidates)
            event.agent.state.set(STATE_MEMORY_TAINTED, had_conflict)

            if selected:
                context_block = "\n".join(f"- [{c['strategy']}] {c['text']}" for c in selected)
                text_block["text"] = (
                    f"{MEMORY_PREAMBLE}\n\nMemory notes:\n{context_block}\n\n"
                    f"Customer message:\n{original_text}"
                )
                logger.info(
                    "Injected %d/%d memory items for actor %s (conflict=%s)",
                    len(selected), len(candidates), actor_id, had_conflict,
                )

        except ClientError as exc:
            err = exc.response.get("Error", {})
            logger.error(
                "AWS error retrieving memory | code=%s msg=%s actor=%s memory=%s namespace=%s",
                err.get("Code"), err.get("Message"), actor_id, self.memory_id, current_namespace,
            )
        except BotoCoreError as exc:
            logger.error(
                "AWS client/network error retrieving memory | %s: %s actor=%s memory=%s",
                type(exc).__name__, exc, actor_id, self.memory_id,
            )
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            logger.exception(
                "Malformed memory response | %s actor=%s memory=%s namespace=%s",
                type(exc).__name__, actor_id, self.memory_id, current_namespace,
            )
        except Exception:
            # Last-resort guard: memory must never break the customer conversation,
            # but log the full traceback so the real failure type is visible.
            logger.exception(
                "Unexpected error retrieving memory | actor=%s memory=%s namespace=%s",
                actor_id, self.memory_id, current_namespace,
            )

    # ── Saving ────────────────────────────────────────────────────────────────
    def save_support_interaction(self, event: "AfterInvocationEvent"):
        """Persist the latest turn, guarding against writing bad output back to memory."""
        actor_id = event.agent.state.get("actor_id")
        session_id = event.agent.state.get("session_id")
        if not actor_id or not session_id:
            return

        try:
            messages = event.agent.messages
            user_text = event.agent.state.get(STATE_ORIGINAL_QUERY)
            tainted = bool(event.agent.state.get(STATE_MEMORY_TAINTED))
            agent_text = None

            for msg in reversed(messages):
                if agent_text is None and msg.get("role") == "assistant":
                    text = _extract_text(msg)
                    if text:
                        agent_text = text
                elif user_text is None and _is_plain_user_text(msg):
                    user_text = _extract_text(msg)
                if agent_text and user_text:
                    break

            if not user_text:
                return

            # Decide whether the assistant's answer is trustworthy enough to store.
            save_assistant = (
                SAVE_ASSISTANT_TURNS
                and not tainted                      # retrieval had conflicting memory
                and agent_text is not None
                and _is_safe_to_save(agent_text)
            )

            turn = [(user_text, "USER")]
            if save_assistant:
                turn.append((agent_text, "ASSISTANT"))
            else:
                logger.info(
                    "Saving USER message only for actor %s (assistant output withheld: "
                    "tainted=%s, enabled=%s)", actor_id, tainted, SAVE_ASSISTANT_TURNS,
                )

            self.memory_client.create_event(
                memory_id=self.memory_id,
                actor_id=actor_id,
                session_id=session_id,
                messages=turn,
            )
            logger.info(
                "Saved %d message(s) to memory | actor=%s session=%s memory=%s",
                len(turn), actor_id, session_id, self.memory_id,
            )

        except ClientError as exc:
            err = exc.response.get("Error", {})
            logger.error(
                "AWS error saving interaction | code=%s msg=%s actor=%s session=%s memory=%s",
                err.get("Code"), err.get("Message"), actor_id, session_id, self.memory_id,
            )
        except BotoCoreError as exc:
            logger.error(
                "AWS client/network error saving interaction | %s: %s actor=%s memory=%s",
                type(exc).__name__, exc, actor_id, self.memory_id,
            )
        except Exception:
            logger.exception(
                "Unexpected error saving interaction | actor=%s session=%s memory=%s",
                actor_id, session_id, self.memory_id,
            )
        finally:
            # Clear per-request state so it can't leak into the next invocation.
            event.agent.state.set(STATE_ORIGINAL_QUERY, None)
            event.agent.state.set(STATE_MEMORY_TAINTED, False)

    def register_hooks(self, registry: "HookRegistry") -> None:  # type: ignore
        """Register both memory callbacks."""
        registry.add_callback(MessageAddedEvent, self.retrieve_customer_context)
        registry.add_callback(AfterInvocationEvent, self.save_support_interaction)
# ── Knowledge Base Tool ──────────────────────────────────────────────────────
@tool
def search_knowledge_base(query: str) -> str:
    """
    Search the Amazon product catalog and support knowledge base.
    Use this for product specifications, return policies, warranty
    information, loyalty program details, and order status definitions.

    Args:
        query: The question or topic to search for

    Returns:
        Relevant information retrieved from the knowledge base
    """
    if not KB_ID or KB_ID.startswith("<"):
        logger.error("search_knowledge_base called but KB_ID is not configured")
        return "Knowledge base not configured (KB_ID is missing from the runtime configuration)."

    try:
        resp = _bedrock_runtime.retrieve(
            knowledgeBaseId=KB_ID,
            retrievalQuery={"text": query},
        )
    except Exception as exc:
        logger.error("Knowledge base retrieve failed: %s", exc)
        return f"Knowledge base lookup failed: {exc}"

    results = resp.get("retrievalResults", [])
    if not results:
        return f"No information found for: {query}"

    chunks = [r["content"]["text"] for r in results if r.get("content", {}).get("text")]
    if not chunks:
        return f"No information found for: {query}"
    return "\n---\n".join(chunks)


# ── Loyalty Discount Tool (Code Interpreter) ─────────────────────────────────
def _parse_code_result(result: dict) -> Optional[str]:
    """
    The Code Interpreter wraps the script's stdout inside the result object
    (content text blocks and/or structuredContent.stdout). Pull the printed JSON
    out of that wrapper and return it, or None if this event has no loyalty payload.
    """
    candidates = []
    for block in result.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
            candidates.append(block["text"])
    structured = result.get("structuredContent")
    if isinstance(structured, dict) and structured.get("stdout"):
        candidates.append(structured["stdout"])

    for text in candidates:
        text = text.strip()
        parsed = None
        try:
            parsed = json.loads(text)
        except ValueError:
            # stdout may carry extra lines; try the outermost {...} span
            start, end = text.find("{"), text.rfind("}")
            if start != -1 and end > start:
                try:
                    parsed = json.loads(text[start:end + 1])
                except ValueError:
                    parsed = None
        if isinstance(parsed, dict) and "final_total" in parsed:
            return json.dumps(parsed, indent=2)
    return None


@tool
def calculate_loyalty_discount(
    loyalty_points: int,
    tier: str,
    order_total: float,
    product_category: str = "standard",
) -> str:
    """
    Calculate the loyalty discount for a customer order using the
    AgentCore Code Interpreter. Runs exact arithmetic in a secure sandbox.

    Args:
        loyalty_points:   Customer's current points balance
        tier:             Customer tier — Silver, Gold, or Platinum
        order_total:      Order total in USD
        product_category: standard, device, or fresh

    Returns:
        Full discount breakdown and final price
    """
    code = f"""
import json

POINT_VALUE = 0.01          # 100 points = $1.00
REDEEM_BLOCK = 500          # points are redeemed in 500-point blocks
MAX_POINT_SHARE = 0.50      # points cover at most 50% of the order

earn_rates = {{"standard": 1, "device": 2, "fresh": 5}}
tier_rates = {{"Silver": 0.00, "Gold": 0.10, "Platinum": 0.15}}

loyalty_points = {loyalty_points}
tier = "{tier}"
order_total = {order_total}
product_category = "{product_category}"

earn_rate = earn_rates.get(product_category, 1)
tier_rate = tier_rates.get(tier, 0.00)

# --- points redemption -------------------------------------------------
max_points_by_order = int((order_total * MAX_POINT_SHARE) / POINT_VALUE)
redeemable = min(loyalty_points, max_points_by_order)
points_redeemed = (redeemable // REDEEM_BLOCK) * REDEEM_BLOCK
points_value = round(points_redeemed * POINT_VALUE, 2)

# --- tier discount on the post-points subtotal -------------------------
subtotal = round(order_total - points_value, 2)
tier_discount = round(subtotal * tier_rate, 2)

# --- totals ------------------------------------------------------------
final_total = round(subtotal - tier_discount, 2)
total_savings = round(points_value + tier_discount, 2)
points_earned = int(final_total * earn_rate)
remaining_points = loyalty_points - points_redeemed + points_earned

result = {{
    "order_total": round(order_total, 2),
    "tier": tier,
    "tier_rate": tier_rate,
    "product_category": product_category,
    "earn_rate": earn_rate,
    "points_balance": loyalty_points,
    "points_redeemed": points_redeemed,
    "points_value": points_value,
    "subtotal_after_points": subtotal,
    "tier_discount": tier_discount,
    "final_total": final_total,
    "total_savings": total_savings,
    "points_earned": points_earned,
    "remaining_points": remaining_points,
    "calculation_method": "code_interpreter",
}}

print(json.dumps(result, indent=2))
"""

    try:
        with code_session(REGION) as code_client:
            response = code_client.invoke(
                "executeCode",
                {
                    "code": code,
                    "language": "python",
                    "clearContext": True,
                },
            )
            logger.info("calculate_loyalty_discount response received")

            # Consume the stream INSIDE the session, and only return once an
            # event actually carries the execution result (skip logs/status events).
            for event in response["stream"]:
                logger.info("calculate_loyalty_discount event: %s", event)
                result = event.get("result") if isinstance(event, dict) else None
                if not result:
                    continue
                if result.get("isError"):
                    raise RuntimeError(f"Code Interpreter execution error: {json.dumps(result)[:500]}")
                parsed = _parse_code_result(result)
                if parsed:
                    return parsed
                logger.warning("Result event had no parsable loyalty JSON: %s", json.dumps(result)[:300])

        raise RuntimeError("Code Interpreter stream contained no parsable loyalty result")

    except Exception as e:
        logger.error("Code Interpreter failed, using degraded fallback: %s", e)
        # DEGRADED fallback: tier discount only. Points redemption / earned points are
        # NOT calculated, and the payload says so explicitly.
        tier_rates = {"Silver": 0.00, "Gold": 0.10, "Platinum": 0.15}
        tier_rate = tier_rates.get(tier, 0.00)
        tier_discount = round(order_total * tier_rate, 2)
        final_total = round(order_total - tier_discount, 2)

        return json.dumps(
            {
                "order_total": round(order_total, 2),
                "tier": tier,
                "tier_rate": tier_rate,
                "tier_discount": tier_discount,
                "final_total_before_points": final_total,
                "points_redeemed": None,
                "points_value": None,
                "total_savings": None,
                "points_earned": None,
                "remaining_points": None,
                "calculation_method": "fallback",
                "degraded": True,
                "warning": (
                    "Code Interpreter unavailable, so ONLY the tier discount was calculated. "
                    "Points redemption, earned points, remaining points and total savings "
                    f"are NOT included and must not be estimated. Cause: {e}"
                ),
            },
            indent=2,
        )


# ── Browser (lazy) ────────────────────────────────────────────────────────────
_browser = None
_browser_lock = threading.Lock()

_BROWSER_INTENT = re.compile(
    r"\b(?:brows\w*|web|website|webpage|internet|google|news|competitors?)\b"
    r"|search\s+(?:the\s+)?(?:web|internet|online)"
    r"|look(?:ing)?\s+(?:it\s+|that\s+|this\s+)?up"
    r"|https?://|www\.",
    re.IGNORECASE,
)


def _needs_browser(user_input: str) -> bool:
    """Decide whether this request should get the browser tool (see BROWSER_MODE)."""
    if BROWSER_MODE == "always":
        return True
    if BROWSER_MODE == "never":
        return False
    return bool(_BROWSER_INTENT.search(user_input or ""))


# def get_browser_tool():
#     """
#     Import and create the AgentCore browser on first use, then reuse it.
#     The one-time cost is logged so it can be read straight from CloudWatch.
#     """
#     global _browser
#     with _browser_lock:
#         if _browser is None:
#             t0 = time.perf_counter()
#             from strands_tools.browser import AgentCoreBrowser  # heavy import, kept off the cold-start path
#             _browser = AgentCoreBrowser(region=REGION)
#             logger.info("[timing] browser import+init took %.2fs (first request only)", time.perf_counter() - t0)
#     return _browser.browser


# ── Agent Entrypoint ──────────────────────────────────────────────────────────
@app.entrypoint
async def invoke(payload, context=None):
    """
    Main handler called by AgentCore for every incoming request.

    Expected payload keys:
      prompt      (str, required) — the customer's message
      customer_id (str, optional) — unique customer identifier
      session_id  (str, optional) — session identifier; generated if absent
    """
    user_input = (payload or {}).get("prompt", "Hello!")
    actor_id = (payload or {}).get("customer_id", "anonymous_customer")
    session_id = (payload or {}).get("session_id") or str(uuid.uuid4())
    agent_core_browser = AgentCoreBrowser(region=REGION)

    if not user_input:
        return "Please provide a 'prompt' in the request payload."

    logger.info("Invoke — actor=%s session=%s | User: %s", actor_id, session_id, user_input[:80])

    t_start = time.perf_counter()
    try:
        memory_hook = MemoryHook(
            actor_id=actor_id,
            session_id=session_id,
            memory_client=memory_client,
            memory_id=MEMORY_ID,
        )

        tools = [
            agent_core_browser.browser,
            search_knowledge_base,
            calculate_loyalty_discount,
        ]
        # if _needs_browser(user_input):
        #     tools.append(get_browser_tool())
        #     logger.info("Browser tool loaded for this request (BROWSER_MODE=%s)", BROWSER_MODE)
        # logger.info("[timing] local setup done at %.2fs", time.perf_counter() - t_start)

        agent_state = {"actor_id": actor_id, "session_id": session_id}

        # The agent must be created AND invoked inside the MCPClient context,
        # because the gateway tools require the live MCP session.
        gateway_connected = False
        try:
            gateway_client = MCPClient(
                lambda: streamable_http_client(GATEWAY_URL)
            )

            with gateway_client:
                t_gw = time.perf_counter()
                gateway_tools = gateway_client.list_tools_sync()
                gateway_connected = True
                tools.extend(gateway_tools)
                logger.info(
                    "Loaded %d gateway tools in %.2fs: %s",
                    len(gateway_tools),
                    time.perf_counter() - t_gw,
                    [getattr(t, "tool_name", str(t)) for t in gateway_tools],
                )
                if not gateway_tools:
                    logger.error("Gateway connected but returned ZERO tools - check gateway targets/permissions")

                agent = Agent(
                    model=model,
                    tools=tools,
                    hooks=[memory_hook],
                    state=agent_state,
                    system_prompt=SYSTEM_PROMPT,
                )
                response = await agent.invoke_async(user_input)

        except Exception as gw_exc:
            if gateway_connected:
                # The gateway was fine; the agent run itself failed. Don't re-run it.
                raise

            if REQUIRE_GATEWAY:
                logger.exception("Gateway unavailable and REQUIRE_GATEWAY=true: %s", gw_exc)
                return (
                    "Sorry — the order and account system is unavailable right now, "
                    f"so I can't process that request. (gateway error: {gw_exc})"
                )

            # Gateway unavailable — degrade to local tools only, but make it loud in the logs.
            logger.exception(
                "GATEWAY UNAVAILABLE (order/refund/account tools will be missing). "
                "Check GATEWAY_URL (%s) and gateway auth/permissions: %s",
                GATEWAY_URL, gw_exc,
            )

            agent = Agent(
                model=model,
                tools=tools,
                hooks=[memory_hook],
                state=agent_state,
                system_prompt=SYSTEM_PROMPT,
            )
            response = await agent.invoke_async(user_input)

        logger.info("[timing] total invoke %.2fs", time.perf_counter() - t_start)
        message = getattr(response, "message", None) or {}
        text = _extract_text(message)
        return text if text else str(response)

    except Exception as exc:
        logger.exception("Agent invocation failed")
        return (
            "Sorry — I ran into a problem handling that request. "
            f"Please try again. (error: {exc})"
        )


# ── Diagnostics ───────────────────────────────────────────────────────────────
def run_diagnostics() -> dict:
    """
    Verify the real resources this agent depends on. Run locally with:
        uv run main.py --check
    Checks KB access, the exact MEMORY_ID, that Gateway tools actually load,
    and that the Code Interpreter returns a parsable result.
    """
    report: Dict[str, str] = {}

    if not KB_ID:
        report["knowledge_base"] = "FAILED: KB_ID is not set"
    else:
        try:
            resp = _bedrock_runtime.retrieve(knowledgeBaseId=KB_ID, retrievalQuery={"text": "return policy"})
            report["knowledge_base"] = f"OK ({len(resp.get('retrievalResults', []))} results)"
        except Exception as exc:
            report["knowledge_base"] = f"FAILED: {exc}"

    try:
        report["memory"] = f"OK, namespaces: {get_namespaces(memory_client, MEMORY_ID)}"
    except Exception as exc:
        report["memory"] = f"FAILED (check MEMORY_ID {MEMORY_ID!r}): {exc}"

    try:
        with MCPClient(lambda: streamable_http_client(GATEWAY_URL)) as client:
            names = [getattr(t, "tool_name", str(t)) for t in client.list_tools_sync()]
        report["gateway"] = f"OK, {len(names)} tools: {names}" if names else "FAILED: connected but 0 tools"
    except Exception as exc:
        report["gateway"] = f"FAILED: {exc}"

    try:
        out = json.loads(calculate_loyalty_discount(
            loyalty_points=1000, tier="Gold", order_total=100.0, product_category="standard"))
        report["code_interpreter"] = (
            f"OK: {out.get('calculation_method')}, final_total={out.get('final_total')}"
            if out.get("calculation_method") == "code_interpreter"
            else f"DEGRADED (fallback used): {out.get('warning')}"
        )
    except Exception as exc:
        report["code_interpreter"] = f"FAILED: {exc}"

    return report


# ── CLI entry point ───────────────────────────────────────────────────────────
def main():
    """Run one invocation (or the resource check) from the command line for local testing."""
    parser = argparse.ArgumentParser()
    parser.add_argument("payload", type=str, nargs="?")
    parser.add_argument("--check", action="store_true", help="verify KB, memory, gateway and code interpreter")
    args = parser.parse_args()
    if args.check:
        print(json.dumps(run_diagnostics(), indent=2))
        return
    if not args.payload:
        parser.error("payload JSON is required unless --check is used")
    response = asyncio.run(invoke(json.loads(args.payload)))
    print(response)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        # Local CLI testing:  uv run main.py '{"prompt": "Hello", ...}'
        main()
    else:
        # Deployed runtime (AgentCore starts the app with no CLI arguments).
        app.run()