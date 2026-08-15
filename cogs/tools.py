"""
/tool — OpenAI helpers for allowed users:
  ask / predict (chat), image (generate), edit (modify an attached image).
"""
from __future__ import annotations

import base64
import io
import json
import logging
from typing import Any, Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

import config
from checks import is_admin
from views import ToolShareView

log = logging.getLogger("ufc-bet-bot.tools")

_CHAT_URL = "https://api.openai.com/v1/chat/completions"
_IMAGE_GEN_URL = "https://api.openai.com/v1/images/generations"
_IMAGE_EDIT_URL = "https://api.openai.com/v1/images/edits"


def _chat_model() -> str:
    return (getattr(config, "OPENAI_CHAT_MODEL", None) or config.OPENAI_VISION_MODEL or "gpt-4o-mini").strip()


def _image_model() -> str:
    return (getattr(config, "OPENAI_IMAGE_MODEL", None) or "gpt-image-1-mini").strip()


async def _record_usage(db, usage: dict[str, Any] | None, *, source: str, user_id: int) -> None:
    if not usage or not db:
        return
    try:
        await db.record_openai_usage(
            model=usage.get("model"),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            total_tokens=int(usage.get("total_tokens") or 0),
            source=source,
            user_id=user_id,
        )
    except Exception:
        pass


async def _openai_chat(
    *,
    system: str,
    user: str,
    model: str | None = None,
    image_bytes: bytes | None = None,
    image_mime: str = "image/png",
) -> tuple[str, dict[str, Any]]:
    key = (config.OPENAI_API_KEY or "").strip()
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not set in .env")
    model = (model or _chat_model()).strip() or "gpt-4o-mini"
    if image_bytes:
        b64 = base64.b64encode(image_bytes).decode("ascii")
        mime = (image_mime or "image/png").split(";")[0].strip() or "image/png"
        user_content: Any = [
            {"type": "text", "text": user},
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{mime};base64,{b64}",
                    "detail": "high",
                },
            },
        ]
    else:
        user_content = user
    payload = {
        "model": model,
        "temperature": 0.7,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
    }
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(_CHAT_URL, headers=headers, json=payload) as resp:
            body = await resp.text()
            if resp.status >= 400:
                raise RuntimeError(f"OpenAI chat HTTP {resp.status}: {body[:300]}")
            data = json.loads(body)
    try:
        text = data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError, AttributeError) as e:
        raise RuntimeError(f"Bad chat response: {e}") from e
    usage = data.get("usage") or {}
    usage["model"] = data.get("model") or model
    return text, usage


async def _read_optional_image(
    image: discord.Attachment | None,
) -> tuple[bytes | None, str]:
    """Return (bytes, mime) or (None, '') if no attachment."""
    if image is None:
        return None, ""
    if not image.content_type or not image.content_type.startswith("image/"):
        raise ValueError("Attachment must be an image file.")
    if image.size and image.size > 8_000_000:
        raise ValueError("Image is too large (max ~8MB).")
    return await image.read(), image.content_type or "image/png"


def _image_payload(model: str, prompt: str, size: str) -> dict[str, Any]:
    """Build /images/generations body. gpt-image-* rejects response_format."""
    payload: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "n": 1,
        "size": size,
    }
    # DALL·E needs b64_json; GPT image models always return b64 and reject this param.
    if model.startswith("dall-e"):
        payload["response_format"] = "b64_json"
    return payload


async def _decode_image_response(data: dict[str, Any], *, timeout: aiohttp.ClientTimeout) -> bytes:
    item = (data.get("data") or [None])[0]
    if not item:
        raise RuntimeError("OpenAI image response had no data")
    if item.get("b64_json"):
        return base64.b64decode(item["b64_json"])
    url = item.get("url")
    if not url:
        raise RuntimeError("OpenAI image response missing b64/url")
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as resp:
            if resp.status >= 400:
                raise RuntimeError(f"Failed to download image: HTTP {resp.status}")
            return await resp.read()


async def _openai_image_generate(prompt: str, *, size: str = "1024x1024") -> bytes:
    key = (config.OPENAI_API_KEY or "").strip()
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not set in .env")
    model = _image_model()
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    timeout = aiohttp.ClientTimeout(total=180)

    # Prefer configured model, then fall back through known alternatives.
    candidates = [model]
    for alt in ("gpt-image-1-mini", "gpt-image-1", "dall-e-3"):
        if alt not in candidates:
            candidates.append(alt)

    last_err = "unknown error"
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for mdl in candidates:
            # DALL·E-3 only supports these sizes
            req_size = size
            if mdl.startswith("dall-e") and size not in ("1024x1024", "1024x1792", "1792x1024"):
                req_size = "1024x1024"
            payload = _image_payload(mdl, prompt, req_size)
            async with session.post(_IMAGE_GEN_URL, headers=headers, json=payload) as resp:
                body = await resp.text()
                if resp.status >= 400:
                    last_err = f"OpenAI image HTTP {resp.status}: {body[:300]}"
                    continue
                data = json.loads(body)
            return await _decode_image_response(data, timeout=timeout)

    raise RuntimeError(last_err)


async def _openai_image_edit(
    image_bytes: bytes,
    prompt: str,
    *,
    filename: str = "image.png",
) -> bytes:
    key = (config.OPENAI_API_KEY or "").strip()
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not set in .env")
    model = _image_model()
    # Normalize to PNG for edit endpoint compatibility
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(image_bytes))
        if img.mode not in ("RGBA", "RGB"):
            img = img.convert("RGBA")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        image_bytes = buf.getvalue()
        filename = "image.png"
    except Exception:
        pass

    headers = {"Authorization": f"Bearer {key}"}
    timeout = aiohttp.ClientTimeout(total=180)

    async def _post(mdl: str) -> dict[str, Any]:
        form2 = aiohttp.FormData()
        form2.add_field("model", mdl)
        form2.add_field("prompt", prompt)
        form2.add_field("n", "1")
        form2.add_field("size", "1024x1024")
        # response_format only for DALL·E — gpt-image rejects it
        if mdl.startswith("dall-e"):
            form2.add_field("response_format", "b64_json")
        form2.add_field(
            "image",
            image_bytes,
            filename=filename,
            content_type="image/png",
        )
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                _IMAGE_EDIT_URL, headers=headers, data=form2
            ) as resp:
                body = await resp.text()
                if resp.status >= 400:
                    raise RuntimeError(f"OpenAI edit HTTP {resp.status}: {body[:300]}")
                return json.loads(body)

    candidates = [model]
    for alt in ("gpt-image-1", "gpt-image-1-mini", "dall-e-2"):
        if alt not in candidates:
            candidates.append(alt)

    last_err: Exception | None = None
    data: dict[str, Any] | None = None
    for mdl in candidates:
        try:
            data = await _post(mdl)
            break
        except RuntimeError as e:
            last_err = e
            continue
    if data is None:
        raise last_err or RuntimeError("OpenAI edit failed")

    return await _decode_image_response(data, timeout=timeout)


class ToolCog(
    commands.GroupCog,
    name="tool",
    description="Ask GPT, get fight takes, or generate/edit images (OpenAI)",
):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        super().__init__()

    @app_commands.command(
        name="ask",
        description="Ask GPT a question (optional: attach a bet slip / image)",
    )
    @is_admin()
    @app_commands.describe(
        question="What do you want to ask?",
        image="Optional: bet slip or other image to look at",
    )
    async def ask(
        self,
        interaction: discord.Interaction,
        question: str,
        image: discord.Attachment | None = None,
    ):
        if not (config.OPENAI_API_KEY or "").strip():
            await interaction.response.send_message(
                "⚠️ `OPENAI_API_KEY` is not set.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        try:
            img_bytes, mime = await _read_optional_image(image)
            system = (
                "You are a helpful assistant inside a Discord UFC betting tracker bot. "
                "Be concise and practical unless the user asks for detail."
            )
            if img_bytes:
                system += (
                    " The user attached an image (often a sportsbook bet slip). "
                    "Read it carefully: list legs, picks, odds, stake, payout if visible. "
                    "Answer their question about the slip. This is NOT betting advice."
                )
            text, usage = await _openai_chat(
                system=system,
                user=question,
                image_bytes=img_bytes,
                image_mime=mime or "image/png",
            )
        except Exception as e:
            await interaction.followup.send(f"❌ `{e}`", ephemeral=True)
            return
        await _record_usage(
            self.bot.db, usage, source="tool-ask", user_id=interaction.user.id
        )
        if len(text) > 1900:
            text = text[:1900] + "…"
        content = f"**Ask**\n{text}"
        await interaction.followup.send(
            content,
            view=ToolShareView(
                invoker_id=interaction.user.id,
                content=content,
                label="GPT ask",
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="predict",
        description="Ask GPT for an MMA/UFC take (optional: attach a slip / image)",
    )
    @is_admin()
    @app_commands.describe(
        question="Fight / card / prop / slip to think about",
        image="Optional: bet slip or screenshot to analyze",
    )
    async def predict(
        self,
        interaction: discord.Interaction,
        question: str,
        image: discord.Attachment | None = None,
    ):
        if not (config.OPENAI_API_KEY or "").strip():
            await interaction.response.send_message(
                "⚠️ `OPENAI_API_KEY` is not set.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)

        card_bits = []
        for ev in (getattr(self.bot, "cached_events", None) or [])[:6]:
            name = ev.get("short_name") or ev.get("name")
            if name:
                card_bits.append(str(name))
        card_ctx = ", ".join(card_bits) if card_bits else "(none cached)"

        try:
            img_bytes, mime = await _read_optional_image(image)
            system = (
                "You are an MMA analyst assistant. Give reasoned fight takes and "
                "prop lean opinions. Be clear this is NOT betting advice / not a guarantee. "
                "Keep answers tight: lean, why, risk. "
                f"Upcoming cards known to the bot: {card_ctx}."
            )
            if img_bytes:
                system += (
                    " The user attached a bet slip or screenshot. Read the legs and odds, "
                    "then give a take on the ticket as a whole. Not financial advice."
                )
            text, usage = await _openai_chat(
                system=system,
                user=question,
                image_bytes=img_bytes,
                image_mime=mime or "image/png",
            )
        except Exception as e:
            await interaction.followup.send(f"❌ `{e}`", ephemeral=True)
            return
        await _record_usage(
            self.bot.db, usage, source="tool-predict", user_id=interaction.user.id
        )
        if len(text) > 1900:
            text = text[:1900] + "…"
        content = f"**Predict** _(opinion only — not betting advice)_\n{text}"
        await interaction.followup.send(
            content,
            view=ToolShareView(
                invoker_id=interaction.user.id,
                content=content,
                label="GPT predict",
            ),
            ephemeral=True,
        )

    @app_commands.command(name="image", description="Generate an image from a description")
    @is_admin()
    @app_commands.describe(
        prompt="What to generate",
        size="Square size (default 1024)",
    )
    @app_commands.choices(
        size=[
            app_commands.Choice(name="1024x1024", value="1024x1024"),
            app_commands.Choice(name="1024x1536 (portrait)", value="1024x1536"),
            app_commands.Choice(name="1536x1024 (landscape)", value="1536x1024"),
        ]
    )
    async def image(
        self,
        interaction: discord.Interaction,
        prompt: str,
        size: app_commands.Choice[str] | None = None,
    ):
        if not (config.OPENAI_API_KEY or "").strip():
            await interaction.response.send_message(
                "⚠️ `OPENAI_API_KEY` is not set.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        size_val = size.value if size else "1024x1024"
        try:
            raw = await _openai_image_generate(prompt, size=size_val)
        except Exception as e:
            await interaction.followup.send(
                f"❌ Image generation failed: `{e}`\n"
                "Image models can be pricey on Tier 1 — check OpenAI billing/model access.",
                ephemeral=True,
            )
            return
        content = f"🖼️ **Image** · `{_image_model()}`\n_{prompt[:200]}_"
        file = discord.File(io.BytesIO(raw), filename="tool_image.png")
        await interaction.followup.send(
            content=content,
            file=file,
            view=ToolShareView(
                invoker_id=interaction.user.id,
                content="",  # share image only — no prompt/model text
                file_bytes=raw,
                filename="tool_image.png",
                label="generated image",
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="edit",
        description="Modify an existing image using a text description",
    )
    @is_admin()
    @app_commands.describe(
        image="Image to edit",
        prompt="How to change it (e.g. 'make the background UFC octagon, add red lighting')",
    )
    async def edit(
        self,
        interaction: discord.Interaction,
        image: discord.Attachment,
        prompt: str,
    ):
        if not (config.OPENAI_API_KEY or "").strip():
            await interaction.response.send_message(
                "⚠️ `OPENAI_API_KEY` is not set.", ephemeral=True
            )
            return
        if not image.content_type or not image.content_type.startswith("image/"):
            await interaction.response.send_message(
                "⚠️ `image` must be an image file.", ephemeral=True
            )
            return
        if image.size and image.size > 8_000_000:
            await interaction.response.send_message(
                "⚠️ Image is too large (max ~8MB).", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        try:
            src = await image.read()
            raw = await _openai_image_edit(
                src, prompt, filename=image.filename or "image.png"
            )
        except Exception as e:
            await interaction.followup.send(
                f"❌ Image edit failed: `{e}`",
                ephemeral=True,
            )
            return
        content = f"✏️ **Edit** · `{_image_model()}`\n_{prompt[:200]}_"
        file = discord.File(io.BytesIO(raw), filename="tool_edit.png")
        await interaction.followup.send(
            content=content,
            file=file,
            view=ToolShareView(
                invoker_id=interaction.user.id,
                content="",  # share image only — no prompt/model text
                file_bytes=raw,
                filename="tool_edit.png",
                label="edited image",
            ),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(ToolCog(bot))
