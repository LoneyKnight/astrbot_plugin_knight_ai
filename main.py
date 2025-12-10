from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.all import *
import httpx
import re
import mimetypes


@register("knight-ai", "绘图", "通过图像生成API生成图片", "1.0.0")
class KnightAIPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.api1 = {
            "base": str(config.get("API_BASE_1", "")).strip(),
            "keys": list(config.get("API_KEYS_1", []))
            or ([config.get("API_KEY_1")] if config.get("API_KEY_1") else []),
            "model": str(config.get("API_MODEL_1", "nano-banana")).strip(),
            "timeout": int(config.get("API_TIMEOUT_1", 240)),
        }
        self.api2 = {
            "base": str(config.get("API_BASE_2", "")).strip(),
            "keys": list(config.get("API_KEYS_2", []))
            or ([config.get("API_KEY_2")] if config.get("API_KEY_2") else []),
            "model": str(config.get("API_MODEL_2", "nano-banana")).strip(),
            "timeout": int(config.get("API_TIMEOUT_2", 240)),
        }

    async def _build_user_content(self, event: AstrMessageEvent):
        items = []
        desc = await self._extract_prompt(event)
        imgs = await self._extract_images(event)
        if desc:
            items.append({"type": "text", "text": desc})
        for img in imgs:
            items.append({"type": "image_url", "image_url": {"url": img}})
        urls = []
        if (
            hasattr(event, "message_obj")
            and event.message_obj
            and hasattr(event.message_obj, "message")
        ):
            for comp in event.message_obj.message:
                if isinstance(comp, Image):
                    if comp.file and str(comp.file).startswith("http"):
                        urls.append(comp.file)
                        continue
                    try:
                        url = await comp.register_to_file_service()
                        urls.append(url)
                    except Exception:
                        pass
                elif isinstance(comp, Reply) and comp.chain:
                    for r in comp.chain:
                        if isinstance(r, Image):
                            if r.file and str(r.file).startswith("http"):
                                urls.append(r.file)
                                continue
                            try:
                                url = await r.register_to_file_service()
                                urls.append(url)
                            except Exception:
                                pass

        for u in urls:
            items.append({"type": "image_url", "image_url": {"url": u}})

        if items and any(i.get("type") == "image_url" for i in items):
            return items
        return desc or ""

    def _extract_image(self, content: str) -> tuple[str | None, str | None]:
        b64m = re.search(
            r"data:image/(?:png|jpeg|jpg|webp);base64,([A-Za-z0-9+/=]+)", content
        )
        if b64m:
            return b64m.group(1), None
        urlm = re.search(r"!\[.*?\]\((https?://[^\s)]+)\)", content)
        if urlm:
            return None, urlm.group(1)
        anyurl = re.search(r"(https?://[^\s)]+)", content)
        if anyurl:
            return None, anyurl.group(1)
        return None, None

    async def _call_api(
        self, api_conf: dict, content
    ) -> tuple[Image | None, str | None]:
        base = api_conf.get("base")
        keys = [k for k in api_conf.get("keys", []) if k]
        model = api_conf.get("model")
        timeout = api_conf.get("timeout", 240)
        if not base or not keys or not model:
            return None, "API配置不完整：缺少基础地址、密钥或模型"

        url = base.rstrip("/") + "/v1/chat/completions"
        payload = {"model": model, "messages": [{"role": "user", "content": content}]}

        last_reason = None
        async with httpx.AsyncClient(timeout=timeout) as client:
            for _ in keys:
                try:
                    resp = await client.post(
                        str(url),
                        json=payload,
                        headers={"Authorization": f"Bearer {keys[0]}"},
                    )
                    if resp.status_code != 200:
                        text = resp.text[:200]
                        last_reason = f"HTTP {resp.status_code}：{text}"
                        keys.append(keys.pop(0))
                        continue
                    data = resp.json()
                    msg = (data.get("choices", [{}])[0] or {}).get("message", {})
                    md = msg.get("content", "")
                    b64, url = self._extract_image(md)
                    if b64:
                        return Image.fromBase64(b64), None
                    if url:
                        return Image.fromURL(url), None
                    last_reason = "响应中未找到图片数据"
                    keys.append(keys.pop(0))
                except Exception as e:
                    last_reason = f"网络或解析错误：{str(e)}"
                    logger.warning(f"knight-ai api error: {e}")
                    keys.append(keys.pop(0))
                    continue
        return None, (last_reason or "未知错误")

    def _parse_desc_from_message(self, event: AstrMessageEvent) -> str:
        s = (event.message_str or "").strip()
        s = re.sub(r"^[\\/]*画图\s*", "", s)
        return s.strip()

    @filter.command("画图")
    async def draw(self, event: AstrMessageEvent):
        event.should_call_llm(True)
        content = await self._build_user_content(event)

        img_comp, reason1 = await self._call_api(self.api1, content)
        reason2 = None
        if not img_comp:
            img_comp, reason2 = await self._call_api(self.api2, content)

        sender_id = (
            event.message_obj.sender.user_id
            if hasattr(event, "message_obj") and event.message_obj
            else ""
        )
        if img_comp:
            chain = [At(qq=sender_id), img_comp]
            yield event.chain_result(chain)
            return
        reason = reason1 if reason1 else reason2
        msg = (
            f"图像生成失败：{reason}"
            if reason
            else "图像生成失败，请检查配置或稍后重试"
        )
        yield event.chain_result([At(qq=sender_id), Plain(msg)])

    async def _extract_prompt(self, event: AstrMessageEvent) -> str:
        tokens = self.parse_commands(event.message_str)
        if tokens.len <= 1:
            return ""
        return " ".join(tokens.tokens[1:]).strip()

    async def _extract_images(self, event: AstrMessageEvent) -> list[str]:
        out: list[str] = []
        for comp in event.get_messages():
            if isinstance(comp, Image):
                url = comp.url or comp.file or ""
                if not url:
                    continue
                if url.startswith("http"):
                    out.append(url)
                elif url.startswith("file:///"):
                    path = url.replace("file:///", "")
                    b64 = await comp.convert_to_base64()
                    mime = mimetypes.guess_type(path)[0] or "image/png"
                    out.append(f"data:{mime};base64,{b64}")
                else:
                    b64 = await comp.convert_to_base64()
                    mime = mimetypes.guess_type(url)[0] or "image/png"
                    out.append(f"data:{mime};base64,{b64}")
        return out