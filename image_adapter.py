"""Optional integration with Gemini Image Generation 3.2 and AstrBot 4.29.3.

Quiet image turns deliver from the generator, without asking a second LLM to
describe or forward the image. Incoming-image questions never enter this path.
"""

import asyncio
import copy
import importlib
from contextvars import ContextVar
from dataclasses import replace
from functools import wraps

from astrbot.api import logger
from astrbot.core.provider.entities import LLMResponse
from mcp.types import CallToolResult, TextContent

from .image_policy import TURN
from .image_references import CONTEXT, NO_REFERENCE, ImageReferences, ReferenceError

TOOL = "gemini_image_generation"
current_generation = ContextVar("group_control.generation", default=None)
reference_prompt = ContextVar("group_control.reference_prompt", default=None)
error_delivery = ContextVar("group_control.image_error", default=False)


class ImageAdapter:
    def __init__(self, plugin, policy):
        self.plugin, self.policy = plugin, policy
        self.patches = []
        self.instances = set()
        self.active = True
        self.references = ImageReferences(
            policy.gate, policy.path.parent / "image_references"
        )

    def capability(self, event):
        key = self.policy.gate.event_key(event)
        return {
            "group_allows_generation": (
                self.policy.settings(key)["enabled"]
                and self.policy.gate.state(key)["enabled"]
            )
            if key
            else True,
            "reference_binding": bool(self.instances),
            "previous_image_available": self.references.key(event)
            in self.references.recent,
        }

    def observe(self, event):
        self.references.observe(event)

    def patch(self, owner, name, replacement):
        self.patches.append((owner, name, getattr(owner, name), replacement))
        setattr(owner, name, replacement)

    def quiet_turn(self, event):
        return bool(
            self.active
            and event
            and event.get_extra(TURN)
            and not event.get_extra(TURN).get("forum")
            and self.policy.quiet(event)
            and not error_delivery.get()
        )

    def filter_chain(self, event, chain):
        if self.quiet_turn(event):
            from astrbot.core.message.components import Plain

            text = "".join(part.text for part in chain.chain if isinstance(part, Plain))
            if self.error_text(event, text):
                return chain
            chain.chain = self.image_parts(chain.chain)
            if not chain.chain:
                return None
        return chain

    @classmethod
    def image_parts(cls, parts):
        from astrbot.core.message.components import File, Image, Node, Nodes, Video

        output = []
        for part in parts:
            if isinstance(part, (Image, Video, File)):
                output.append(part)
            elif isinstance(part, Node):
                node = copy.copy(part)
                node.content = cls.image_parts(part.content)
                if node.content:
                    output.append(node)
            elif isinstance(part, Nodes):
                nodes = copy.copy(part)
                nodes.nodes = cls.image_parts(part.nodes)
                if nodes.nodes:
                    output.append(nodes)
        return output

    @classmethod
    def image_segments(cls, parts):
        from aiocqhttp import Message

        output = []
        for part in Message(parts):
            if part["type"] in ("image", "video", "file"):
                output.append(part)
            elif part["type"] == "node":
                content = cls.image_segments(part["data"].get("content", []))
                if content:
                    output.append(
                        {**part, "data": {**part["data"], "content": content}}
                    )
        return output

    @staticmethod
    def error_text(event, text):
        turn = event.get_extra(TURN) or {}
        return bool(
            turn.get("error") and not turn.get("error_sent") and turn["error"] in text
        )

    def filter_outbound(self, api, action, params):
        from aiocqhttp import Message
        from astrbot.core.platform.astr_message_event import current_message_event

        generation = current_generation.get()
        event = current_message_event.get() or (generation[1] if generation else None)
        if not self.quiet_turn(event):
            return params
        gate = self.policy.gate
        group = gate.send_target(action, params)
        platform = next(
            (p for p in gate.platforms() if p.get_client().api is api), None
        )
        if (
            not platform
            or not group
            or gate.key(platform.meta().id, group) != gate.event_key(event)
        ):
            return params
        field = "message" if "message" in params else "messages"
        if field not in params:
            return params
        parts = Message(params[field])
        if self.error_text(
            event,
            "".join(p["data"].get("text", "") for p in parts if p["type"] == "text"),
        ):
            return params
        parts = self.image_segments(parts)
        if not parts:
            return None
        return {**params, field: parts}

    async def send_error(self, event, text):
        token = error_delivery.set(True)
        try:
            await event.send(event.plain_result(text))
        finally:
            error_delivery.reset(token)

    def install_runner(self, runner_class):
        original = runner_class._iter_llm_responses_with_fallback

        @wraps(original)
        async def responses(runner):
            event = getattr(runner.run_context.context, "event", None)
            if (
                not self.active
                or not self.instances
                or event is None
                or not self.policy.quiet(event)
            ):
                async for response in original(runner):
                    yield response
                return
            turn = event.get_extra(TURN)
            if turn and turn.get("forum"):
                async for response in original(runner):
                    yield response
                return
            if turn and turn["finished"]:
                # A real tool result exists. Do not pay for another caption-generation call.
                yield LLMResponse(
                    role="assistant",
                    completion_text=turn["error"]
                    or ("[图片已发送]" if turn["delivered"] else "[生图任务已提交]"),
                )
                return
            tools = getattr(runner.req, "func_tool", None)
            if tools and any(t.name == TOOL for t in tools.tools):
                runner.streaming = False
            async for response in original(runner):
                if TOOL in (response.tools_call_name or []):
                    self.policy.turn(event)
                    response.completion_text = ""
                    response.reasoning_content = ""
                    response.result_chain = None
                yield response

        self.patch(runner_class, "_iter_llm_responses_with_fallback", responses)

    def attach(self):
        if not self.active:
            return
        for star in self.plugin.context.get_all_stars():
            if (
                star.name != "astrbot_plugin_gemini_image_generation"
                or not star.activated
            ):
                continue
            plugin = star.star_cls
            if id(plugin) in self.instances or not getattr(
                plugin, "image_generator", None
            ):
                continue
            package = plugin.__class__.__module__.rsplit(".", 1)[0]
            try:
                module = importlib.import_module(package + ".tl.llm_tools")
                self.attach_plugin(plugin, module)
                self.instances.add(id(plugin))
                logger.info("[群聊控制] 生图权限、张数和纯图片交付已接入。")
            except (ImportError, AttributeError):
                logger.exception("[群聊控制] 生图插件接口不兼容，未完成接入")

    def attach_plugin(self, plugin, module):
        original_core = plugin.image_generator.generate_image_core
        original_fetch = plugin._fetch_images_from_event

        @wraps(original_fetch)
        async def fetch(event, *args, **kwargs):
            state = event.get_extra(CONTEXT) or {}
            if self.active and state.get("bound"):
                return list(state["bound"]["images"]), []
            return await original_fetch(event, *args, **kwargs)

        self.patch(plugin, "_fetch_images_from_event", fetch)

        @wraps(original_core)
        async def core(event, *args, **kwargs):
            if not self.active or self.policy.gate.event_key(event) is None:
                return await original_core(event, *args, **kwargs)
            turn = self.policy.turn(event)
            try:
                bound = await self.references.resolve(event, plugin)
            except (ReferenceError, OSError, ValueError) as error:
                turn["error"] = str(error)
                return False, turn["error"]
            if bound:
                # Core entry is also used by explicit /改图 commands.
                args = list(args)
                supplied = args[0] if args else kwargs.get("prompt")
                prompt = (
                    supplied
                    if supplied in bound.get("prompts", [])
                    else bound["prompt"]
                )
                for index, name, value in (
                    (0, "prompt", prompt),
                    (1, "reference_images", bound["images"]),
                    (2, "avatar_reference", []),
                    (8, "negative_prompt", None),
                ):
                    if len(args) > index:
                        args[index] = value
                    else:
                        kwargs[name] = value
            count = kwargs.get("image_count", args[11] if len(args) > 11 else 1)
            ticket, error = self.policy.reserve(event, count)
            if error:
                turn["error"] = error
                return False, error
            try:
                token = current_generation.set((plugin, event))
                prompt_token = reference_prompt.set(prompt if bound else None)
                try:
                    success, result = await original_core(event, *args, **kwargs)
                finally:
                    reference_prompt.reset(prompt_token)
                    current_generation.reset(token)
            except asyncio.CancelledError:
                self.policy.settle(event, ticket, count)
                raise
            except Exception:
                self.policy.settle(event, ticket, 0)
                raise
            actual = (
                len(plugin.message_sender.merge_available_images(result[0], result[1]))
                if success
                else 0
            )
            self.policy.settle(event, ticket, actual)
            if not success:
                turn["error"] = str(result)
            elif self.quiet_turn(event):
                result = (result[0], result[1], None, result[3])
            return success, result

        self.patch(plugin.image_generator, "generate_image_core", core)
        tool_class = module.GeminiImageGenerationTool
        # A reloaded plugin may reuse the same tool class: only patch a class once.
        if any(owner is tool_class for owner, *_ in self.patches):
            self.instances.add(id(plugin))
            return
        original_call = tool_class.call

        @wraps(original_call)
        async def call(tool, context, **kwargs):
            event = context.context.event
            if not self.active or self.policy.gate.event_key(event) is None:
                return await original_call(tool, context, **kwargs)
            turn = self.policy.turn(event)
            turn["forum"] = bool(kwargs.get("for_forum"))
            batch = kwargs.get("batch_tasks")
            try:
                count = (
                    sum(item.get("image_count", 1) for item in batch) if batch else 1
                )
            except (TypeError, AttributeError):
                count = 0
            error = self.policy.check(event, count)
            if error:
                turn.update(finished=True, error=error)
                if self.quiet_turn(event):
                    await self.send_error(event, error)
                    turn["error_sent"] = True
                return error
            token = current_generation.set((tool.plugin, event))
            try:
                explicit_none = NO_REFERENCE.search(
                    self.references.observe(event)["text"]
                )
                requested = bool(kwargs.get("use_reference_images")) or any(
                    item.get("use_reference_images")
                    for item in (batch or [])
                    if isinstance(item, dict)
                )
                bound = await self.references.resolve(
                    event, tool.plugin, requested=requested
                )
                if bound or explicit_none:

                    def bind(item, index=0):
                        selected = {
                            **item,
                            "use_reference_images": bool(bound),
                            "reference_image_paths": [],
                            "include_user_avatar": False,
                        }
                        if bound:
                            selected["prompt"] = bound["prompt"]
                            if batch and len(batch) > 1:
                                selected["prompt"] += (
                                    f"\n本请求对应用户所需的第 {index + 1}/{len(batch)} 项图片，按原话中相应的要求处理。"
                                )
                            bound.setdefault("prompts", []).append(selected["prompt"])
                            selected["negative_prompt"] = None
                        return selected

                    if batch:
                        kwargs["batch_tasks"] = [
                            bind(item, i) for i, item in enumerate(batch)
                        ]
                    else:
                        kwargs = bind(kwargs)
                if bound:
                    logger.info(
                        "[群聊控制] 参考图绑定 source=%s count=%d sha256=%s",
                        bound["origin"],
                        len(bound["images"]),
                        ",".join(h[:12] for h in bound["hashes"]),
                    )
                result = await original_call(tool, context, **kwargs)
                if (
                    isinstance(result, str)
                    and not result.lstrip().startswith("{")
                    and not turn["forum"]
                ):
                    turn["error"] = result
                if turn["error"] and self.quiet_turn(event):
                    await self.send_error(event, turn["error"])
                    turn["error_sent"] = True
                return result
            except Exception as error:  # noqa: BLE001 - third-party tool boundary
                message = f"[生图失败] {error}"
                turn["error"] = message
                if self.quiet_turn(event):
                    await self.send_error(event, message)
                    turn["error_sent"] = True
                return message
            finally:
                turn["finished"] = True
                current_generation.reset(token)

        self.patch(tool_class, "call", call)
        original_result = module._build_call_tool_result

        @wraps(original_result)
        async def build_result(*args, **kwargs):
            generation = current_generation.get()
            image_plugin, event = generation if generation else (None, None)
            if not self.quiet_turn(event):
                result = await original_result(*args, **kwargs)
                if event is not None and self.active:
                    self.references.generated(
                        event, kwargs.get("image_urls"), kwargs.get("image_paths")
                    )
                return result
            # v3.2 calls this with keyword arguments.
            await self.deliver(image_plugin, module, event, kwargs)
            return CallToolResult(
                content=[
                    TextContent(type="text", text="图片已直接发送，无需再转发或配文。")
                ]
            )

        self.patch(module, "_build_call_tool_result", build_result)
        original_notify = module.notify_llm_background_result

        @wraps(original_notify)
        async def notify(image_plugin, event, payload, **kwargs):
            if not self.quiet_turn(event):
                result = await original_notify(image_plugin, event, payload, **kwargs)
                if self.active:
                    for item in payload.get("items", [payload]):
                        self.references.generated(
                            event, item.get("image_urls"), item.get("image_paths")
                        )
                return result
            for item in payload.get("items", [payload]):
                if item.get("image_urls") or item.get("image_paths"):
                    await self.deliver(image_plugin, module, event, item)
                if (
                    item.get("error")
                    or item.get("status") == "failed"
                    or (
                        item.get("result")
                        and not item.get("image_urls")
                        and not item.get("image_paths")
                    )
                ):
                    await self.send_error(
                        event, item.get("error") or item.get("result") or "[生图失败]"
                    )
            return True

        base = module.__package__
        for target in (
            module,
            importlib.import_module(base + ".batch_generation"),
            importlib.import_module(base + ".background_notify"),
        ):
            self.patch(target, "notify_llm_background_result", notify)
        self.instances.add(id(plugin))
        self.attach_provider(importlib.import_module(base + ".api.openai_images"))

    def attach_provider(self, module):
        owner = module.OpenAIImagesProvider
        if any(target is owner for target, *_ in self.patches):
            return
        original = owner.build_request

        @wraps(original)
        async def build(instance, *, client, config):
            generation = current_generation.get()
            event = generation[1] if generation else None
            state = event.get_extra(CONTEXT) if event else None
            bound = state.get("bound") if state else None
            if not self.active or not bound:
                return await original(instance, client=client, config=config)
            settings = (
                config.provider_settings
                or getattr(client, "openai_images_settings", {})
                or {}
            )
            if settings.get("generations_only"):
                raise module.APIError(
                    "[参考图未提交] 供应商开启了仅文生图，请关闭该选项。",
                    retryable=False,
                )
            if len(bound["images"]) > min(
                max(int(settings.get("max_reference_images", 6)), 1), 16
            ):
                raise module.APIError(
                    "[参考图过多] 供应商上限不足以携带本次全部原图。", retryable=False
                )
            config = replace(
                config,
                reference_images=list(bound["images"]),
                prompt=reference_prompt.get() or bound["prompt"],
                negative_prompt=None,
            )
            request = await original(instance, client=client, config=config)
            logger.info(
                "[群聊控制] 原图请求已构建 endpoint=images/edits count=%d",
                len(bound["images"]),
            )
            return request

        self.patch(owner, "build_request", build)

    async def deliver(self, plugin, module, event, payload):
        sent = await module._dispatch_generation_result(
            plugin,
            event,
            True,
            (payload.get("image_urls", []), payload.get("image_paths", []), None, None),
            scene="群控纯图片",
            notify_llm=False,
        )
        if not sent:
            raise RuntimeError("图片已生成，但发送失败")
        self.policy.turn(event)["delivered"] = True
        self.references.generated(
            event, payload.get("image_urls"), payload.get("image_paths")
        )

    def close(self):
        self.active = False
        for owner, name, original, replacement in reversed(self.patches):
            if getattr(owner, name) is replacement:
                setattr(owner, name, original)
        self.patches.clear()
