"""Structured-output runner.

Sends the output model's JSON schema to the provider, validates the response with Pydantic,
and performs exactly one repair round that feeds the validation error back to the model.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from app.ports.interfaces import LLMProvider, Message, ProviderError, Usage

log = logging.getLogger("factory.agents.runner")

T = TypeVar("T", bound=BaseModel)


class StructuredOutputError(ProviderError):
    """The provider answered, but not with a usable JSON object.

    Carries ``usage`` when tokens were spent anyway, so a failed attempt is still billed and the
    operator can see how much a bad scene actually cost.
    """

    def __init__(self, message: str, *, usage: Usage | None = None) -> None:
        super().__init__(message)
        self.usage = usage


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def extract_json(text: str) -> str:
    cleaned = _FENCE.sub("", text.strip())
    if cleaned.startswith("{") or cleaned.startswith("["):
        return cleaned
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end > start:
        return cleaned[start : end + 1]
    return cleaned


class StructuredRunner:
    def __init__(self, provider: LLMProvider) -> None:
        self.provider = provider

    async def run(
        self,
        *,
        model: str,
        system: str,
        user: str,
        output_model: type[T],
        temperature: float = 0.7,
        max_tokens: int = 4096,
        timeout_s: float = 180.0,
        repair: bool = True,
    ) -> tuple[T, Usage]:
        schema = output_model.model_json_schema()
        messages = [
            Message("system", system),
            Message("user", user),
        ]
        started = time.perf_counter()
        result = await self.provider.complete(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            json_schema=schema,
            timeout_s=timeout_s,
        )
        try:
            return output_model.model_validate_json(extract_json(result.content)), result.usage
        except (ValidationError, ValueError) as exc:
            if not result.content.strip() and not repair:
                # A blank body is not a schema problem: the provider spent its whole output budget on
                # reasoning, or returned an empty block. Say so, because "Invalid JSON: EOF" sends
                # whoever reads the log looking in the wrong place.
                raise StructuredOutputError(
                    f"{output_model.__name__} got an empty response from "
                    f"{result.usage.provider or 'the provider'} "
                    f"({result.usage.output_tokens} output tokens). The model likely exhausted "
                    f"max_tokens={max_tokens} before emitting any text.",
                    usage=result.usage,
                ) from exc
            if not repair:
                raise StructuredOutputError(
                    f"{output_model.__name__} validation failed: {exc}", usage=result.usage
                ) from exc
            log.warning("structured repair round for %s", output_model.__name__)
            messages.append(Message("assistant", result.content))
            messages.append(
                Message(
                    "user",
                    "The JSON above failed schema validation. Fix it and return ONLY the "
                    f"corrected JSON object. Validation error:\n{exc}\n\nSchema:\n"
                    f"{json.dumps(schema, separators=(',', ':'))[:6000]}",
                )
            )
            repaired = await self.provider.complete(
                model=model,
                messages=messages,
                temperature=0.1,
                max_tokens=max_tokens,
                json_schema=schema,
                timeout_s=timeout_s,
            )
            try:
                parsed = output_model.model_validate_json(extract_json(repaired.content))
            except (ValidationError, ValueError) as exc2:
                raise StructuredOutputError(
                    f"{output_model.__name__} was invalid after repair: {exc2}"
                ) from exc2
            usage = Usage(
                provider=repaired.usage.provider or result.usage.provider,
                model=repaired.usage.model or result.usage.model,
                operation="llm_chat",
                input_tokens=result.usage.input_tokens + repaired.usage.input_tokens,
                output_tokens=result.usage.output_tokens + repaired.usage.output_tokens,
                units=result.usage.units + repaired.usage.units,
                unit_label="tokens",
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
            return parsed, usage


def dumps(model: BaseModel | dict[str, Any]) -> str:
    data = model.model_dump() if isinstance(model, BaseModel) else model
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)
