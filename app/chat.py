"""General text chat for testing the configured Ollama model."""

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, StringConstraints

from .extraction.service import ExtractionError, post_json

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/chat", tags=["chat"])
Content = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: Content


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1)


class ChatResponse(BaseModel):
    model: str
    message: ChatMessage
    done: Literal[True] = True


@router.post("", response_model=ChatResponse)
def chat(body: ChatRequest, request: Request):
    # A synchronous route runs blocking Ollama I/O in FastAPI's thread pool.
    settings = request.app.state.settings
    try:
        response = post_json(
            settings.host,
            "/api/chat",
            {
                "model": settings.model,
                "messages": [message.model_dump() for message in body.messages],
                "stream": False,
                "think": False,
            },
            settings.timeout,
        )
        message = response.get("message")
        if (
            response.get("done") is not True
            or response.get("done_reason") == "length"
            or not isinstance(message, dict)
            or message.get("role") != "assistant"
            or not isinstance(message.get("content"), str)
            or not message["content"].strip()
        ):
            raise ExtractionError("Ollama returned an incomplete or invalid chat response")
    except ExtractionError as exc:
        logger.exception("General chat failed")
        raise HTTPException(502, "Chat failed. Check the Ollama server and model.") from exc
    return ChatResponse(model=settings.model, message=ChatMessage(**message))
