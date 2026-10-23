"""Optional, server-only OpenAI-compatible endpoint. No built-in credentials."""
import os

import httpx


def ai_configured():
    return bool(os.getenv("TRAINER_AI_API_KEY") and os.getenv("TRAINER_AI_MODEL"))


async def generate_ai_text(messages, temperature=.25, max_tokens=3500, timeout=240):
    base = os.getenv("TRAINER_AI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    if not base.startswith("https://") and not base.startswith(("http://127.0.0.1:", "http://localhost:")):
        raise ValueError("AI endpoint must use HTTPS or a loopback address")
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        response = await client.post(base + "/chat/completions", headers={
            "Authorization": "Bearer " + os.environ["TRAINER_AI_API_KEY"]}, json={
            "model": os.environ["TRAINER_AI_MODEL"], "messages": messages,
            "temperature": temperature, "max_tokens": max_tokens})
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]
