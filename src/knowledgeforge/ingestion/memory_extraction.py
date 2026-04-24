"""Prompting and JSON normalization for past-conversation memory extraction."""

from __future__ import annotations

import json
from typing import Any

from knowledgeforge.core.models import ConversationExchange, MemoryCard


PAST_CONVERSATION_EXTRACTION_PROMPT = """# Past Conversation Knowledge Extraction Prompt

You are extracting durable engineering knowledge from a past conversation.

Do not summarize casually. Extract only information that could help a future AI agent continue work, avoid mistakes, or understand architectural intent.

## Extract these memory types

- project_context
- objective
- definition_of_done
- decision
- constraint
- failed_attempt
- resolution
- blocker
- todo
- known_bug
- api_contract
- data_schema
- dependency
- environment
- command
- file_path
- security_rule
- user_preference
- handover_summary

## Rules

1. Do not treat old conversation content as current truth.
2. Mark old extracted items as `historical` or `active_unverified` unless the conversation clearly says it was verified.
3. Extract atomic memory cards, not one large summary.
4. Preserve the why behind decisions.
5. Extract failed attempts and rabbit holes aggressively.
6. Extract constraints and non-negotiables aggressively.
7. Do not create decisions from vague brainstorms.
8. If something is only an idea, mark it as `idea` or `historical`.
9. If something sounds superseded, mark it as `superseded_candidate`.
10. Include source date and conversation title when available.
11. Include confidence: high, medium, or low.
12. Include tags for retrieval.
13. Include whether repo confirmation is needed.
14. Prefer concise but complete records.

## Output JSON

Return this structure:

{
  "conversation_summary": {
    "title": "",
    "date": "",
    "projects_detected": [],
    "summary": "",
    "key_takeaways": []
  },
  "memory_cards": [
    {
      "type": "",
      "project": "",
      "title": "",
      "body": "",
      "why": "",
      "status": "",
      "confidence": "",
      "needs_repo_confirmation": true,
      "source": {
        "conversation_title": "",
        "conversation_date": "",
        "message_refs": []
      },
      "tags": []
    }
  ],
  "possible_conflicts": [
    {
      "new_item": "",
      "conflicts_with": "",
      "reason": "",
      "recommended_action": ""
    }
  ],
  "discarded_noise": [
    {
      "content_type": "",
      "reason": ""
    }
  ]
}
"""


def build_conversation_extraction_prompt(
    exchanges: list[ConversationExchange],
    title: str = "",
    max_chars: int = 60000,
) -> str:
    """Create a bounded extraction prompt from parsed conversation exchanges."""
    conversation_text = conversation_text_from_exchanges(exchanges, max_chars=max_chars)
    heading = f"Conversation title: {title}\n\n" if title else ""
    return (
        PAST_CONVERSATION_EXTRACTION_PROMPT
        + "\n\n## Conversation to extract\n\n"
        + heading
        + conversation_text
    )


def conversation_text_from_exchanges(
    exchanges: list[ConversationExchange],
    max_chars: int = 60000,
) -> str:
    """Render parsed exchanges with stable refs for extraction output."""
    parts: list[str] = []
    total = 0
    for exchange in exchanges:
        header = (
            f"[exchange:{exchange.exchange_id} "
            f"agent:{exchange.source_agent} "
            f"project_hint:{exchange.project} "
            f"date:{exchange.timestamp} "
            f"lines:{exchange.line_start}-{exchange.line_end}]"
        )
        block = "\n".join(
            [
                header,
                f"User: {exchange.user_message}",
                f"Assistant: {exchange.assistant_message}",
            ]
        )
        if exchange.tool_names:
            block += f"\nTools: {', '.join(exchange.tool_names)}"

        if total + len(block) > max_chars:
            remaining = max_chars - total
            if remaining > 500:
                parts.append(block[:remaining])
            break

        parts.append(block)
        total += len(block)
    return "\n\n".join(parts)


def parse_extraction_json(raw: str) -> dict[str, Any]:
    """Parse extraction JSON, tolerating fenced markdown around the object."""
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise


def memory_cards_from_extraction(
    payload: dict[str, Any],
    source_path: str = "",
    source_type: str = "conversation",
    include_summary_card: bool = True,
) -> list[MemoryCard]:
    """Normalize extraction JSON into MemoryCard instances."""
    summary = payload.get("conversation_summary") or {}
    conversation_title = str(summary.get("title") or "")
    conversation_date = str(summary.get("date") or "")
    projects_detected = summary.get("projects_detected") or []
    default_project = str(projects_detected[0]) if projects_detected else "unknown"

    cards: list[MemoryCard] = []
    if include_summary_card and summary.get("summary"):
        cards.append(
            MemoryCard(
                type="handover_summary",
                project=default_project,
                title=conversation_title or "Conversation summary",
                body=str(summary.get("summary") or ""),
                why="Provides orientation for a past conversation without treating it as current truth.",
                status="historical",
                confidence="medium",
                source_type=source_type,
                source_conversation=conversation_title,
                source_date=conversation_date,
                source_path=source_path,
                current_truth=False,
                needs_repo_confirmation=True,
                tags=["conversation-summary"],
            )
        )

    for item in payload.get("memory_cards") or []:
        if not isinstance(item, dict):
            continue
        source = item.get("source") or {}
        message_refs = source.get("message_refs") or []
        tags = item.get("tags") or []

        card = MemoryCard(
            type=str(item.get("type") or "project_context"),
            project=str(item.get("project") or default_project),
            title=str(item.get("title") or "")[:240],
            body=str(item.get("body") or ""),
            why=str(item.get("why") or ""),
            status=str(item.get("status") or "active_unverified"),
            confidence=str(item.get("confidence") or "medium"),
            source_type=source_type,
            source_conversation=str(source.get("conversation_title") or conversation_title),
            source_date=str(source.get("conversation_date") or conversation_date),
            source_path=source_path,
            source_lines=",".join(str(ref) for ref in message_refs),
            current_truth=bool(item.get("current_truth", False)),
            needs_repo_confirmation=bool(item.get("needs_repo_confirmation", True)),
            tags=[str(tag) for tag in tags],
        )
        if card.title and card.body:
            cards.append(card)

    return cards
