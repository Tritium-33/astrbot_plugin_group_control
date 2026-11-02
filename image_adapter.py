"""Optional integration with Gemini Image Generation 3.2 and AstrBot 4.29.3.

Quiet image turns deliver from the generator, without asking a second LLM to
describe or forward the image. Incoming-image questions never enter this path.
"""

import asyncio
import copy
import importlib
from contextvars import ContextVar
from functools import wraps

from astrbot.api import logger
from astrbot.core.provider.entities import LLMResponse
from mcp.types import CallToolResult, TextContent

from .image_policy import TURN

TOOL = "gemini_image_generation"
current_generation = ContextVar("group_control.generation", default=None)
error_delivery = ContextVar("group_control.image_error", default=False)


class ImageAdapter:
    def __init__(self, plugin, policy):
        self.plugin, self.policy = plugin, policy
        self.patches = []
        self.instances = set()
        self.active = True

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

        @wraps(original_core)
        async def core(event, *args, **kwargs):
            if not self.active or self.policy.gate.event_key(event) is None:
                return await original_core(event, *args, **kwargs)
            turn = self.policy.turn(event)
            count = kwargs.get("image_count", args[11] if len(args) > 11 else 1)
            ticket, error = self.policy.reserve(event, count)
            if error:
                turn["error"] = error
                return False, error
            try:
                success, result = await original_core(event, *args, **kwargs)
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
                return await original_result(*args, **kwargs)
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
                return await original_notify(image_plugin, event, payload, **kwargs)
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

    def close(self):
        self.active = False
        for owner, name, original, replacement in reversed(self.patches):
            if getattr(owner, name) is replacement:
                setattr(owner, name, original)
        self.patches.clear()
