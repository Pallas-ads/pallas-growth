"""Meta Model API access through Pydantic AI's Responses model (chosen by the Day 1 spike)."""

from __future__ import annotations

import os

from openai import AsyncOpenAI
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.providers.openai import OpenAIProvider

META_BASE_URL = "https://api.meta.ai/v1"
DEFAULT_MODEL = "muse-spark-1.3"

# USD per 1M tokens (input, output). Contributor models are cheaper; list price is an upper bound.
PRICES = {"muse-spark-1.3": (1.25, 4.25), "muse-wonder-1.0": (1.25, 4.25)}
SEARCH_PRICE = 2.50 / 1000


def meta_client() -> AsyncOpenAI:
    """Client for the live API. Reads the key from the environment and never prints it."""
    key = os.environ.get("MODEL_API_KEY")
    if not key:
        raise RuntimeError("MODEL_API_KEY is not set. Put it in .env (see .env.example).")
    return AsyncOpenAI(
        api_key=key, base_url=os.environ.get("META_MODEL_BASE_URL", META_BASE_URL), max_retries=2
    )


def responses_model(client: AsyncOpenAI, model: str = DEFAULT_MODEL) -> OpenAIResponsesModel:
    # Meta only accepts tool_choice="auto", so typed output must use JSON-schema mode.
    return OpenAIResponsesModel(
        model,
        provider=OpenAIProvider(openai_client=client),
        profile=OpenAIModelProfile(supports_forced_tool_choice=False),
    )


def estimate_cost(model: str, input_tokens: int, output_tokens: int, searches: int = 0) -> float:
    base = model.removesuffix("-contributor")
    price_in, price_out = PRICES.get(base, PRICES[DEFAULT_MODEL])
    return (input_tokens * price_in + output_tokens * price_out) / 1e6 + searches * SEARCH_PRICE
