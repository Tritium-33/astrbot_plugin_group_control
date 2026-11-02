"""Per-platform QQ group switches, checked before processing and OneBot sends."""

import json
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

from aiocqhttp.exceptions import ActionFailed
from astrbot.api.platform import MessageType

STAMP = "group_control.epoch"
DENIED = "group_control.denied"
proactive_turn = ContextVar("group_control.proactive_turn", default=None)


class GroupMuted(ActionFailed):
    def __init__(self):
        super().__init__(
            {"retcode": 1403, "message": "群发言总开关已关闭，本条消息未发送"}
        )


class GroupGate:
    def __init__(self, plugin, path):
        self.plugin, self.path = plugin, path
        self.states = {}
        self.patches = []
        self.gcp_patches = []
        self.active = False

    @staticmethod
    def key(platform, group):
        return json.dumps([str(platform), str(group)], ensure_ascii=False)

    def load(self, legacy=None):
        source = self.path if self.path.exists() else legacy
        migrated = bool(source and source != self.path and source.exists())
        if source and source.exists():
            states = json.loads(source.read_text(encoding="utf-8"))
            for key, row in states.items():
                platform, group = json.loads(key)
                if (
                    not isinstance(platform, str)
                    or not isinstance(group, str)
                    or type(row["enabled"]) is not bool
                    or type(row["epoch"]) is not int
                ):
                    raise ValueError("群发言状态格式不正确")
            if migrated:
                self.save(states)
            else:
                self.states = states
        elif not self.path.exists():
            self.save({})
        return migrated

    def state(self, key):
        return self.states.get(key, {"enabled": True, "epoch": 0})

    def set_enabled(self, groups, enabled):
        """Commit a complete batch before changing live permissions; no await gap."""
        states = dict(self.states)
        for row in groups:
            key = self.key(row["platform_id"], row["group_id"])
            previous = self.state(key)
            states[key] = {
                **previous,
                "enabled": enabled,
                "epoch": previous["epoch"] + int(previous["enabled"] and not enabled),
                "name": row.get("name", previous.get("name", "")),
            }
        self.save(states)

    def save(self, states):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(
            json.dumps(states, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temp.replace(self.path)
        self.states = states

    @staticmethod
    def event_key(event):
        if (
            event.get_message_type() == MessageType.GROUP_MESSAGE
            and event.get_platform_name() == "aiocqhttp"
        ):
            return GroupGate.key(event.get_platform_id(), event.get_group_id())
        return None

    def blocked(self, event):
        key = self.event_key(event)
        if key is None:
            return False
        state = self.state(key)
        stamp = event.get_extra(STAMP)
        if stamp is None:
            stamp = state["epoch"]
            event.set_extra(STAMP, stamp)
        denied = (
            not state["enabled"]
            or stamp != state["epoch"]
            or event.get_extra(DENIED, False)
        )
        if denied:
            event.set_extra(DENIED, True)
        return denied

    @contextmanager
    def proactive(self, platform, group):
        key = self.key(platform, group)
        token = proactive_turn.set((key, self.state(key)["epoch"]))
        try:
            yield self.state(key)["enabled"]
        finally:
            proactive_turn.reset(token)

    def platforms(self):
        return [
            platform
            for platform in self.plugin.context.platform_manager.platform_insts
            if platform.meta().name == "aiocqhttp"
        ]

    @staticmethod
    def send_target(action, params):
        action = action.removesuffix("_async").removesuffix("_rate_limited")
        group = params.get("group_id")
        if not group:
            return None
        if action.startswith("send_group_") or action in (
            "upload_group_file",
            "group_poke",
            "send_poke",
        ):
            return str(group)
        if action in ("send_msg", "send_forward_msg"):
            kind = params.get("message_type")
            if kind != "private" and (kind == "group" or not params.get("user_id")):
                return str(group)
        return None

    def outbound_blocked(self, api, action, params):
        from astrbot.core.platform.astr_message_event import current_message_event

        group = self.send_target(action, params)
        if group is None:
            return False
        platform = next(
            (p for p in self.platforms() if p.get_client().api is api), None
        )
        if platform is None:
            return False
        key = self.key(platform.meta().id, group)
        state = self.state(key)
        if not state["enabled"]:
            return True
        event = current_message_event.get()
        if event is not None and self.event_key(event) == key and self.blocked(event):
            return True
        previous = proactive_turn.get()
        return bool(previous and previous[0] == key and previous[1] != state["epoch"])

    def install(self, scheduler, api_class, event_class=None):
        if self.active:
            return
        original_execute = scheduler.execute
        original_action = api_class.call_action

        @wraps(original_execute)
        async def execute(instance, event):
            if self.active:
                self.attach_gcp()
                images = getattr(self.plugin, "image_adapter", None)
                if images:
                    images.attach()
                if self.blocked(event):
                    event.stop_event()
                    return
            return await original_execute(instance, event)

        @wraps(original_action)
        async def call_action(api, action, **params):
            if self.active and self.outbound_blocked(api, action, params):
                raise GroupMuted()
            images = getattr(self.plugin, "image_adapter", None)
            if self.active and images:
                params = images.filter_outbound(api, action, params)
                if params is None:
                    return {"message_id": 0}
            return await original_action(api, action, **params)

        self.active = True
        for owner, name, replacement in (
            (scheduler, "execute", execute),
            (api_class, "call_action", call_action),
        ):
            self.patches.append((owner, name, getattr(owner, name), replacement))
            setattr(owner, name, replacement)

        if event_class is not None:
            original_send = event_class.send

            @wraps(original_send)
            async def send(event, message):
                if self.active and self.blocked(event):
                    event.clear_result()
                    event.stop_event()
                    # An interrupted send must not count as a successful quota use.
                    raise GroupMuted()
                images = getattr(self.plugin, "image_adapter", None)
                if self.active and images:
                    message = images.filter_chain(event, message)
                    if message is None:
                        return
                return await original_send(event, message)

            self.patches.append((event_class, "send", original_send, send))
            event_class.send = send

    def attach_gcp(self):
        """Optional adapter: GCP timers bypass the incoming-message pipeline."""
        if not self.active:
            return
        for name, module in list(sys.modules.items()):
            if "group_chat_plus" not in name or not name.endswith(
                ".utils.proactive_chat_manager"
            ):
                continue
            owner = getattr(module, "ProactiveChatManager", None)
            if owner is None or any(row[0] is owner for row in self.gcp_patches):
                continue
            descriptor = owner.__dict__.get("_process_proactive_chat_simplified")
            if not isinstance(descriptor, classmethod):
                continue

            def wrap(original):
                @wraps(original)
                async def process(
                    cls,
                    context,
                    config,
                    plugin_instance,
                    platform_id,
                    is_private,
                    chat_id,
                    chat_key,
                ):
                    args = (
                        cls,
                        context,
                        config,
                        plugin_instance,
                        platform_id,
                        is_private,
                        chat_id,
                        chat_key,
                    )
                    if not self.active or is_private:
                        return await original(*args)
                    with self.proactive(platform_id, chat_id) as allowed:
                        if not allowed:
                            return
                        return await original(*args)

                return classmethod(process)

            replacement = wrap(descriptor.__func__)
            self.gcp_patches.append((owner, descriptor, replacement))
            owner._process_proactive_chat_simplified = replacement

    def close(self):
        self.active = False
        for owner, original, replacement in reversed(self.gcp_patches):
            if owner.__dict__.get("_process_proactive_chat_simplified") is replacement:
                owner._process_proactive_chat_simplified = original
        self.gcp_patches.clear()
        for owner, name, original, replacement in reversed(self.patches):
            if getattr(owner, name) is replacement:
                setattr(owner, name, original)
        self.patches.clear()
