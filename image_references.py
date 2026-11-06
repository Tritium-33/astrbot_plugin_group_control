"""Bind explicit image edits to original pixels, scoped to the requesting user."""

import base64
import copy
import hashlib
import json
import re
import time
from pathlib import Path

from astrbot.core.message.components import Image, Plain, Reply
from astrbot.core.utils.media_utils import file_uri_to_path

CONTEXT = "group_control.image_references"
PREVIOUS = re.compile(
    r"上[一1]张|上一幅|上张|前一张|(?:刚才|之前).{0,8}(?:图|照片)|(?:previous|last)\s+(?:image|picture|photo)",
    re.IGNORECASE,
)
REFERENCE = re.compile(
    r"(?:这|该|此|当前|原|参考|附|上传).{0,4}(?:图|照片)|(?:图|照片).{0,8}(?:改成|改为|改一下|重画|重绘)|(?:this|attached|current)\s+(?:image|picture|photo)",
    re.IGNORECASE,
)
NO_REFERENCE = re.compile(
    r"(?:不要|不用|无需|不需要)(?:再)?(?:参考|使用|基于|根据)(?:任何|这张|当前|上一张|原)?(?:图片|图|照片)|(?:不参考|忽略)(?:这些|这张|当前|上一张|参考)?(?:图|照片)|without\s+(?:any\s+)?reference",
    re.IGNORECASE,
)
RULE = "以随请求上传的原图为视觉依据。只执行用户明确要求的改动；未要求更改的人物外貌、服饰、物件及车辆结构沿用原图，不自行增补外形细节。不要用文字猜测替代原图。\n用户原话："


class ReferenceError(ValueError):
    pass


class ImageReferences:
    def __init__(self, gate, folder):
        self.gate, self.folder = gate, Path(folder)
        self.path = self.folder / "recent.json"
        self.recent = {}
        if self.path.exists():
            self.recent = json.loads(self.path.read_text(encoding="utf-8"))

    def key(self, event):
        group = self.gate.event_key(event)
        return json.dumps([group, str(event.get_sender_id())]) if group else None

    @staticmethod
    def item(image):
        return {
            key: str(getattr(image, key, "") or "") for key in ("file", "url", "path")
        }

    @staticmethod
    def source(item):
        for field in ("path", "file", "url"):
            ref = item.get(field, "")
            if ref and not ref.startswith(("http:", "https:", "base64:", "data:")):
                path = file_uri_to_path(ref) if ref.startswith("file:") else ref
                if Path(path).is_absolute() and Path(path).is_file():
                    return path
        return item.get("url") or item.get("file") or item.get("path") or ""

    def remember(self, event, items, origin):
        key = self.key(event)
        if not key or not items:
            return
        self.recent.pop(key, None)
        self.recent[key] = {"items": items[-6:], "origin": origin, "time": time.time()}
        self.recent = dict(list(self.recent.items())[-256:])
        self.folder.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.recent, ensure_ascii=False), encoding="utf-8")
        temp.replace(self.path)

    def observe(self, event):
        key = self.key(event)
        if not key or str(event.get_sender_id()) == str(event.get_self_id()):
            return None
        state = event.get_extra(CONTEXT)
        if state is None:
            state = {
                "previous": copy.deepcopy(self.recent.get(key)),
                "current": [],
                "bound": None,
            }
            event.set_extra(CONTEXT, state)
        chain = event.get_messages()
        current = [self.item(part) for part in chain if isinstance(part, Image)]
        # Keep the original descriptors before later plugins normalize or resize media.
        if current and not state["current"]:
            state["current"] = current
            self.remember(event, current, "input")
        state.setdefault(
            "text",
            "".join(part.text for part in chain if isinstance(part, Plain)).strip()
            or event.get_message_str().strip(),
        )
        return state

    def intent(self, event):
        state = self.observe(event)
        if not state or NO_REFERENCE.search(state["text"]):
            return ""
        if PREVIOUS.search(state["text"]):
            return "previous"
        if REFERENCE.search(state["text"]):
            return "current"
        return ""

    def hint(self, event):
        if not self.intent(event):
            return ""
        return (
            "\n\n用户明确提及参考图。若执行生图，必须使用原图；提示词只保留用户明确提出的要求，"
            "不要补写人物或装备外观。宿主会绑定当前/引用/上一张原图。普通识图问答照常回答，"
            "不因本提示自动调用生图。"
        )

    async def resolve(self, event, plugin, *, requested=False):
        state = self.observe(event)
        if state is None:
            return None
        if state["bound"] is not None:
            return state["bound"]
        intent = self.intent(event)
        if NO_REFERENCE.search(state["text"]):
            return None
        if not intent and not requested:
            return None
        quoted = []
        for part in event.get_messages():
            if isinstance(part, Reply):
                images = [
                    self.item(p) for p in (part.chain or []) if isinstance(p, Image)
                ]
                if not images:
                    from astrbot.core.utils.quoted_message_parser import (
                        extract_quoted_message_images,
                    )

                    images = [
                        {"file": ref}
                        for ref in await extract_quoted_message_images(event, part)
                    ]
                quoted.extend(images)
        if intent == "previous" and quoted:
            items, origin = quoted, "quoted"
        elif intent == "previous":
            previous = state["previous"] or {}
            items, origin = previous.get("items", [])[-1:], "previous"
        else:
            items, origin = (
                (state["current"], "current")
                if state["current"]
                else (quoted, "quoted")
            )
            if not items and intent:
                items, origin = (
                    (state["previous"] or {}).get("items", [])[-1:],
                    "previous",
                )
        if not items:
            raise ReferenceError(
                "[参考图缺失] 请重发或引用要修改的图片，本次未启动生图。"
            )
        images = []
        for item in items:
            ref = self.source(item)
            image = Image(
                file=ref, url=ref if ref.startswith(("http://", "https://")) else None
            )
            proxy = copy.copy(event)
            proxy.message_obj = copy.copy(event.message_obj)
            proxy.message_obj.message = [image]
            try:
                refs, _ = await plugin._fetch_images_from_event(
                    proxy, include_at_avatars=False
                )
            except (OSError, ValueError):
                refs = []
            if not refs and item.get("file") and hasattr(event, "bot"):
                data = await event.bot.call_action(
                    action="get_image", file=item["file"]
                )
                recovered = self.source(data)
                if recovered:
                    proxy.message_obj.message = [Image(file=recovered)]
                    refs, _ = await plugin._fetch_images_from_event(
                        proxy, include_at_avatars=False
                    )
            if not refs:
                raise ReferenceError(
                    "[参考图读取失败] 原图未取得，请重发或引用图片，本次未启动生图。"
                )
            images.extend(refs)
        # The same attachment may be offered as both QQ URL and a local cache path.
        unique = {}
        for ref in images:
            raw = base64.b64decode(
                ref.split(";base64,", 1)[-1].removeprefix("base64://"), validate=True
            )
            unique.setdefault(
                hashlib.sha256(raw).hexdigest(), (raw, base64.b64encode(raw).decode())
            )
        limit = getattr(plugin.image_generator, "max_reference_images", 6)
        if len(unique) > limit:
            raise ReferenceError(
                f"[参考图过多] 本次有 {len(unique)} 张原图，供应商上限为 {limit} 张。"
            )
        self.folder.mkdir(parents=True, exist_ok=True)
        cached = []
        for digest, (raw, _) in unique.items():
            path = self.folder / (digest + ".img")
            if not path.exists():
                path.write_bytes(raw)
            cached.append({"file": str(path)})
        self.remember(event, cached, origin)
        self.prune()
        state["bound"] = {
            "images": [value[1] for value in unique.values()],
            "origin": origin,
            "hashes": list(unique),
            "prompt": RULE + state["text"],
        }
        return state["bound"]

    def prune(self):
        files = sorted(
            self.folder.glob("*.img"), key=lambda p: p.stat().st_mtime, reverse=True
        )
        total = 0
        for path in files:
            total += path.stat().st_size
            if total > 256 * 1024 * 1024:
                path.unlink()

    def generated(self, event, urls, paths):
        items = [{"file": str(ref)} for ref in dict.fromkeys(paths or urls or [])]
        self.remember(event, items, "generated")
