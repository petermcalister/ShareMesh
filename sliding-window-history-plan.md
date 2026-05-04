# Sliding Window Conversation History Manager — Plan

## Intent

Bound conversation history sent to the model by a **token budget of 150,000 tokens**, dropping oldest turns first when the budget is exceeded. The goal is to enforce a predictable upper limit on per-call token consumption (and therefore cost and latency) within a Streamlit chat interface powered by PydanticAI, while staying well below the model's context window so there is headroom for system prompt, tool definitions, and the response itself.

This replaces the earlier "12 pair LRU" design. The previous design was a fixed-turn-count FIFO — not LRU — and a turn count is a poor proxy for token consumption (a single pasted stack trace can dwarf a dozen short turns).

## Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Eviction policy | Sliding window (FIFO) | Chronological coherence matters more than access recency for chat; true LRU re-ordering would scramble the dialogue. |
| Budget unit | Tokens | Directly controls cost, latency, and context-window pressure. |
| Budget value | 150,000 tokens | Leaves comfortable headroom under Claude Sonnet/Opus 200K context for system prompt, tools, retrieved context, and response. |
| Implementation | PydanticAI `history_processors` hook | First-party extension point; avoids a parallel source of truth alongside the agent's own message history. |
| State shape | Stateless function over `list[ModelMessage]` | No shared mutable state; trivially testable; safe under Streamlit re-runs. |
| Token counting | Anthropic token counter (preferred) with `tiktoken` fallback | Accurate for the target model; fallback keeps the function usable in tests. |
| System messages | Always preserved | Dropping a system prompt silently changes agent behaviour. |
| Pair preservation | Drop user + assistant turns together | Avoids leaving an orphan assistant reply with no preceding user turn (model gets confused). |

## Constants

```python
INT_TOKEN_BUDGET_DEFAULT: int = 150_000
INT_TOKEN_BUDGET_HEADROOM: int = 8_000   # reserved for response + tool overhead
```

The effective trim target is `INT_TOKEN_BUDGET_DEFAULT - INT_TOKEN_BUDGET_HEADROOM` = 142,000 tokens of history. Headroom is conservative; tune after observing real usage.

## Architecture

```
┌──────────────────────┐      ┌──────────────────────────┐      ┌────────────────────┐
│  Streamlit chat UI   │ ───▶ │  PydanticAI Agent.run()  │ ───▶ │  Anthropic API     │
└──────────────────────┘      └──────────────────────────┘      └────────────────────┘
                                          │
                                          ▼
                              ┌──────────────────────────┐
                              │   history_processors:    │
                              │   fnTrimByTokenBudget    │
                              │   (this module)          │
                              └──────────────────────────┘
```

The processor runs immediately before each model call. It receives the full message history, returns a trimmed copy, and never mutates agent state directly.

## Algorithm

1. Compute token count for each message in the history (cache on a `_intTokenCount` attribute or external dict to avoid recounting on every call).
2. Partition messages into `lstSystem` (always kept) and `lstDialogue` (subject to trimming).
3. Walking from the **newest** dialogue message backwards, accumulate messages into `lstKept` until adding the next message would exceed the budget.
4. When dropping, drop in user/assistant **pairs** to avoid orphaned assistant replies.
5. Return `lstSystem + reversed(lstKept)`.

Edge cases:
- Single message larger than the budget → keep it anyway, log a warning. Truncating mid-message would corrupt JSON tool calls.
- Empty history → return as-is.
- All-system history (no dialogue yet) → return as-is.

## Python Implementation

```python
"""Sliding window history processor for PydanticAI agents.

Trims conversation history to fit within a token budget, dropping oldest
user/assistant pairs first. System messages are always preserved.
"""

import inspect
import logging
from typing import Callable

from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse

import common

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    filename=common.log_file,
    filemode='a',
)
logger = common.getLogger(__name__)

INT_TOKEN_BUDGET_DEFAULT: int = 150_000
INT_TOKEN_BUDGET_HEADROOM: int = 8_000


def fnCountTokensAnthropic(strText: str) -> int:
    """Count tokens using the Anthropic tokenizer.

    Falls back to a rough char-based estimate if the SDK is unavailable
    (e.g. in unit tests without network access).
    """
    try:
        import anthropic
        objClient = anthropic.Anthropic()
        objResult = objClient.messages.count_tokens(
            model="claude-sonnet-4-5",
            messages=[{"role": "user", "content": strText}],
        )
        return objResult.input_tokens
    except Exception as e:
        current_function = inspect.currentframe().f_code.co_name
        logger.warning(
            f"Token counter fallback in {current_function}: {e}"
        )
        # ~4 chars per token is a serviceable rough estimate for English
        return max(1, len(strText) // 4)


def fnExtractMessageText(objMessage: ModelMessage) -> str:
    """Concatenate text content from a PydanticAI message for token counting."""
    try:
        lstParts: list[str] = []
        for objPart in objMessage.parts:
            strContent = getattr(objPart, "content", None)
            if isinstance(strContent, str):
                lstParts.append(strContent)
            else:
                # tool calls / structured parts — fall back to repr for sizing
                lstParts.append(repr(objPart))
        return "\n".join(lstParts)
    except Exception as e:
        current_function = inspect.currentframe().f_code.co_name
        logger.warning(f"Failed to extract text in {current_function}: {e}")
        return repr(objMessage)


def fnTrimHistoryByTokenBudget(
    lstMessages: list[ModelMessage],
    intBudget: int = INT_TOKEN_BUDGET_DEFAULT - INT_TOKEN_BUDGET_HEADROOM,
    fnCounter: Callable[[str], int] = fnCountTokensAnthropic,
) -> list[ModelMessage]:
    """Trim message history to fit within a token budget.

    System messages (instructions) are always preserved at the head of the
    returned list. Dialogue messages are kept from newest backwards until
    the budget is exhausted; user/assistant pairs are evicted together to
    avoid orphaned assistant turns.

    Args:
        lstMessages: Full conversation history from the PydanticAI agent.
        intBudget: Maximum total tokens for the returned history.
        fnCounter: Injectable token counter for testability.

    Returns:
        New list of messages within budget. Input is not mutated.
    """
    try:
        if not lstMessages:
            return []

        lstSystem: list[ModelMessage] = []
        lstDialogue: list[ModelMessage] = []
        for objMessage in lstMessages:
            # PydanticAI represents system instructions inside ModelRequest
            # parts; treat any message with only system parts as system.
            blnIsSystemOnly = (
                isinstance(objMessage, ModelRequest)
                and all(
                    type(objPart).__name__ == "SystemPromptPart"
                    for objPart in objMessage.parts
                )
            )
            if blnIsSystemOnly:
                lstSystem.append(objMessage)
            else:
                lstDialogue.append(objMessage)

        intSystemTokens = sum(
            fnCounter(fnExtractMessageText(objMsg)) for objMsg in lstSystem
        )
        intRemaining = intBudget - intSystemTokens
        if intRemaining <= 0:
            logger.warning(
                f"System messages alone consume {intSystemTokens} tokens, "
                f"exceeding budget {intBudget}. Returning system only."
            )
            return list(lstSystem)

        # Walk dialogue from newest to oldest, keeping while we have budget.
        lstKeptReversed: list[ModelMessage] = []
        intUsed = 0
        for objMessage in reversed(lstDialogue):
            intCost = fnCounter(fnExtractMessageText(objMessage))
            if intUsed + intCost > intRemaining:
                break
            lstKeptReversed.append(objMessage)
            intUsed += intCost

        lstKept = list(reversed(lstKeptReversed))

        # Pair guard: if the oldest kept message is a ModelResponse (assistant)
        # without its preceding ModelRequest (user), drop it to avoid orphans.
        if lstKept and isinstance(lstKept[0], ModelResponse):
            logger.info(
                "Dropping orphaned assistant turn at head of trimmed history"
            )
            lstKept = lstKept[1:]

        intDropped = len(lstDialogue) - len(lstKept)
        if intDropped > 0:
            logger.info(
                f"Trimmed {intDropped} dialogue messages "
                f"(kept {len(lstKept)}, used {intUsed}/{intRemaining} tokens)"
            )

        return lstSystem + lstKept

    except Exception as e:
        current_function = inspect.currentframe().f_code.co_name
        print(f"An error occurred in {current_function}: {e}")
        logger.warning(f"An error occurred in {current_function}: {e}")
        raise e
```

## PydanticAI Wiring

```python
from pydantic_ai import Agent
from sliding_window_history import fnTrimHistoryByTokenBudget

objAgent = Agent(
    model="claude-sonnet-4-5",
    history_processors=[fnTrimHistoryByTokenBudget],
    system_prompt="...",
)
```

The processor runs on every `agent.run()` / `agent.run_sync()` call. The agent's own message history remains the source of truth — the processor just produces a trimmed view for the model.

## Streamlit Integration

```python
import streamlit as st
from pydantic_ai.messages import ModelMessage

if "lstHistory" not in st.session_state:
    st.session_state.lstHistory: list[ModelMessage] = []

strUserInput = st.chat_input("Ask…")
if strUserInput:
    objResult = objAgent.run_sync(
        strUserInput,
        message_history=st.session_state.lstHistory,
    )
    # Persist the agent's full history; the processor handles trimming
    # at send-time, not storage-time.
    st.session_state.lstHistory = objResult.all_messages()
```

Note: full history is retained in `st.session_state` so the user can scroll back through old turns in the UI even after they've been trimmed from the model's view.

## Testing Plan (behave)

Mock messages live in `files/temp/mock/data/sliding_window/`.

```gherkin
Feature: Sliding window history trimming

  Scenario: History under budget is returned unchanged
    Given a history of 5 short messages totalling 2000 tokens
    And a token budget of 150000
    When the history is trimmed
    Then all 5 messages are returned

  Scenario: Oldest dialogue is dropped when budget is exceeded
    Given a history of 50 messages totalling 200000 tokens
    And a token budget of 142000
    When the history is trimmed
    Then the system message is preserved
    And the most recent messages fit within 142000 tokens
    And the oldest user/assistant pair is dropped first

  Scenario: Orphan assistant turn at head is removed
    Given a trimmed history beginning with an assistant message
    When the pair guard runs
    Then the leading assistant message is dropped

  Scenario: Single oversized message is kept with warning
    Given a single user message of 200000 tokens
    And a token budget of 142000
    When the history is trimmed
    Then the message is returned
    And a warning is logged
```

Token counter is injected as a stub in step definitions so tests are deterministic and offline.

## Observability

- Log dropped count, kept count, and tokens used on every trim.
- Emit a counter/histogram if Langfuse or Phoenix is wired in — useful when tuning the budget.
- Log a warning when system prompt alone exceeds budget (indicates the budget is misconfigured).

## Future Extensions (not in scope now)

- **Summarisation of evicted turns** — pass dropped pairs to a cheaper model and prepend a synthetic "earlier in this conversation…" summary message. This is the natural next step if conversations routinely overflow 150K.
- **Semantic retrieval over evicted turns** — embed dropped messages and re-inject the top-k relevant ones at query time. Heavier infra; only worth it if summarisation proves insufficient.
- **Per-conversation budget overrides** — surface as a Streamlit sidebar control for debugging.

## Open Questions

1. Should tool-call/tool-result message pairs be treated atomically (drop both or neither)? Current design treats them as ordinary dialogue messages, which could orphan a tool result. Worth a follow-up if tool use is heavy.
2. Is 8K headroom enough? Depends on max response size and tool schema bulk. Measure once running.
3. Do we want the trim to be observable to the end user (e.g. a small "earlier turns hidden" badge in the Streamlit transcript)?
