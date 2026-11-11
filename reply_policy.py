"""Per-group total and ambient reply budgets, shared by all sending paths."""

from collections import defaultdict, deque
from contextvars import ContextVar
from dataclasses import dataclass
from time import monotonic

from astrbot.api.message_components import At
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.filter.command_group import CommandGroupFilter

TURN = "group_control.reply_turn"
MAX_SECONDS = 2592000
current_turn = ContextVar("group_control.reply_turn", default=None)


@dataclass
class ReplyTurn:
    key: str
    ambient: bool
    event: object = None
    reserved: bool = False
    sent: bool = False
    denied: bool = False


class ReplyPolicy:
    def __init__(self, plugin, clock=monotonic):
        self.plugin = plugin
        self.gate = plugin.group_gate
        self.clock = clock
        self.history = defaultdict(deque)
        self.pending = {}

    def settings(self, key):
        saved = self.gate.state(key).get("replies", {})
        return {
            "total": {
                "enabled": False,
                "seconds": 3600,
                "limit": 10,
                **saved.get("total", {}),
            },
            "ambient": {
                "enabled": False,
                "seconds": 3600,
                "limit": 1,
                **saved.get("ambient", {}),
            },
            "gcp_unlimited": saved.get("gcp_unlimited", False),
        }

    @staticmethod
    def validate(value):
        if not isinstance(value, dict) or set(value) != {
            "total",
            "ambient",
            "gcp_unlimited",
        }:
            raise ValueError("请填写完整的回复额度设置")
        if type(value["gcp_unlimited"]) is not bool:
            raise ValueError("Chat Plus 冷却开关必须为开或关")
        result = {"gcp_unlimited": value["gcp_unlimited"]}
        for name in ("total", "ambient"):
            row = value[name]
            if not isinstance(row, dict) or set(row) != {"enabled", "seconds", "limit"}:
                raise ValueError("每项额度都需要开关、统计秒数和回复轮数")
            if type(row["enabled"]) is not bool:
                raise ValueError("额度开关必须为开或关")
            if (
                type(row["seconds"]) is not int
                or not 1 <= row["seconds"] <= MAX_SECONDS
            ):
                raise ValueError("统计秒数须为1至2592000的整数")
            if type(row["limit"]) is not int or not 1 <= row["limit"] <= 100000:
                raise ValueError("回复上限须为1至100000的整数")
            result[name] = dict(row)
        return result

    def update(self, key, value):
        settings = self.validate(value)
        self.gate.save(
            {**self.gate.states, key: {**self.gate.state(key), "replies": settings}}
        )

    def migrate(self):
        """Import old settings once per group; the router no longer enforces them."""
        states = dict(self.gate.states)
        count = 0
        for star in self.plugin.context.get_all_stars():
            if star.name != "astrbot_plugin_evidence_qa" or not star.activated:
                continue
            cfg = star.star_cls.config
            limited = {str(group) for group in cfg.get("reply_limit_groups", [])}
            exempt = {str(group) for group in cfg.get("no_cooldown_groups", [])}
            for platform in self.gate.platforms():
                for group in limited | exempt:
                    key = self.gate.key(platform.meta().id, group)
                    old = states.get(key, self.gate.state(key))
                    if "replies" in old:
                        continue
                    settings = self.settings(key)
                    settings["gcp_unlimited"] = group in exempt
                    settings["ambient"].update(
                        enabled=group in limited and group not in exempt,
                        seconds=max(
                            1,
                            min(
                                MAX_SECONDS,
                                int(float(cfg.get("reply_limit_seconds", 300))),
                            ),
                        ),
                        limit=max(
                            1, min(100000, int(cfg.get("reply_limit_count", 10)))
                        ),
                    )
                    states[key] = {**old, "replies": settings}
                    count += 1
        if count:
            self.gate.save(states)
        return count

    def observe(self, event):
        key = self.gate.event_key(event)
        if key is None:
            return None
        turn = event.get_extra(TURN)
        if turn is None:
            direct = any(
                isinstance(part, At) and str(part.qq) == str(event.get_self_id())
                for part in event.get_messages()
            )
            turn = ReplyTurn(key, not direct, event)
            event.set_extra(TURN, turn)
        return turn

    def owner(self, event):
        for star in self.plugin.context.get_all_stars():
            if star.name == "astrbot_plugin_evidence_qa" and star.activated:
                return star.star_cls.group_control.owner(event)
        config = self.plugin.context.get_config(umo=event.unified_msg_origin)
        return str(event.get_sender_id()) in {
            str(item) for item in config.get("admins_id", [])
        }

    def management(self, turn):
        event = turn.event
        if event is None or not self.owner(event):
            return False
        return bool(event.get_extra("evidence_qa.group_management", False)) or any(
            isinstance(rule, (CommandFilter, CommandGroupFilter))
            for handler in event.get_extra("activated_handlers", [])
            for rule in handler.event_filters
        )

    def usage(self, key):
        now = self.clock()
        rows = self.history[key]
        while rows and now - rows[0][0] >= MAX_SECONDS:
            rows.popleft()
        settings = self.settings(key)
        result = {}
        for name in ("total", "ambient"):
            ambient = name == "ambient"
            window = settings[name]["seconds"]
            times = [
                when
                for when, auto in rows
                if now - when < window and (not ambient or auto)
            ]
            pending = sum(
                t.key == key and (not ambient or t.ambient)
                for t in self.pending.values()
            )
            limit = settings[name]["limit"]
            next_seconds = 0
            if settings[name]["enabled"] and len(times) + pending >= limit and times:
                index = max(0, min(len(times) - 1, len(times) + pending - limit))
                next_seconds = max(1, int(times[index] + window - now + 0.999))
            result[name] = {
                "used": len(times),
                "pending": pending,
                "wait_seconds": next_seconds,
            }
        return result

    def exhausted(self, turn):
        settings, usage = self.settings(turn.key), self.usage(turn.key)
        for name in ("total", "ambient"):
            if name == "ambient" and not turn.ambient:
                continue
            rule, used = settings[name], usage[name]
            own = int(turn.reserved)
            if (
                rule["enabled"]
                and used["used"] + used["pending"] - own >= rule["limit"]
            ):
                return True
        return False

    def enter(self, event):
        turn = self.observe(event)
        if turn is None or self.owner(event):
            # Natural-language owner controls are classified before reserving.
            return True
        return self.acquire(turn)

    def acquire(self, turn):
        if turn is None:
            return True
        if self.management(turn):
            self.release(turn)
            return True
        if turn.denied:
            return False
        if turn.sent:
            return True
        if self.exhausted(turn):
            turn.denied = True
            self.release(turn)
            if turn.event is not None:
                turn.event.stop_event()
                turn.event.clear_result()
            return False
        if not turn.reserved and any(
            self.settings(turn.key)[name]["enabled"] for name in ("total", "ambient")
        ):
            turn.reserved = True
            self.pending[id(turn)] = turn
        return True

    def sent(self, turn):
        if turn is None or turn.sent or self.management(turn):
            return
        if any(
            self.settings(turn.key)[name]["enabled"] for name in ("total", "ambient")
        ):
            self.history[turn.key].append((self.clock(), turn.ambient))
        turn.sent = True
        self.release(turn)

    def release(self, turn):
        if turn is not None:
            self.pending.pop(id(turn), None)
            turn.reserved = False

    def outbound(self, api, action, params):
        group = self.gate.send_target(action, params)
        if group is None or not action.startswith("send_"):
            return None
        platform = next(
            (p for p in self.gate.platforms() if p.get_client().api is api), None
        )
        if platform is None:
            return None
        key = self.gate.key(platform.meta().id, group)
        turn = current_turn.get()
        if turn is not None and turn.key == key:
            return turn
        from astrbot.core.platform.astr_message_event import current_message_event

        event = current_message_event.get()
        if event is not None and self.gate.event_key(event) == key:
            return self.observe(event)
        return ReplyTurn(key, True)

    def close(self):
        self.pending.clear()
