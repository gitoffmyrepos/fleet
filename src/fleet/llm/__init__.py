"""SP-F (2026-05-24) — work-LLM provider chain.

Distinct from `fleet.router` which classifies tasks into dispatch kinds
(swarm/phase/subagent/verify/ship). This package provides the actual
work-LLM completion API used by SP-E agents (Openclaw + Hermes) when
they need an LLM call to investigate or fix a GitHub issue.

Public API:
    from fleet.llm import LLMChain, LLMResult, LLMChainExhaustedError
    chain = LLMChain.from_settings(settings)
    result = await chain.complete(prompt="...", max_tokens=4000)
"""

from fleet.llm.provider_chain import (
    LLMChain,
    LLMChainExhaustedError,
    LLMResult,
    RungAttempt,
)

__all__ = [
    "LLMChain",
    "LLMChainExhaustedError",
    "LLMResult",
    "RungAttempt",
]
