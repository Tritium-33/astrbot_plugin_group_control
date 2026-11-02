"""Per-group image permissions and persistent rolling image counts."""

import json
import time
from uuid import uuid4

DEFAULTS = {
    "enabled": True,
    "caption": False,
    "per_request": 0,
    "window_minutes": 60,
    "window_limit": 0,
}
TURN = "group_control.images"


class ImagePolicy:
    def __init__(self, gate, path, clock=time.time):
        self.gate, self.path, self.clock = gate, path, clock
        self.usage = []

    def load(self):
        if self.path.exists():
            self.usage = json.loads(self.path.read_text(encoding="utf-8"))
            # An interrupted process cannot establish whether its API calls succeeded.
            for row in self.usage:
                if row.pop("pending", False):
                    row["time"] = self.clock()

    def settings(self, key):
        return {**DEFAULTS, **self.gate.state(key).get("images", {})}

    @staticmethod
    def validate(value):
        if not isinstance(value, dict) or set(value) != set(DEFAULTS):
            raise ValueError("请填写完整的生图设置")
        for field in ("enabled", "caption"):
            if type(value[field]) is not bool:
                raise ValueError("生图和配文开关必须为开或关")
        for field, low, high in (
            ("per_request", 0, 100),
            ("window_minutes", 1, 43200),
            ("window_limit", 0, 100000),
        ):
            if type(value[field]) is not int or not low <= value[field] <= high:
                raise ValueError(f"{field} 必须为 {low}–{high} 的整数")
        return dict(value)

    def update(self, key, value):
        settings = self.validate(value)
        self.gate.save(
            {**self.gate.states, key: {**self.gate.state(key), "images": settings}}
        )

    def used(self, key):
        cutoff = self.clock() - self.settings(key)["window_minutes"] * 60
        return sum(
            row["count"]
            for row in self.usage
            if row["key"] == key and (row.get("pending") or row["time"] > cutoff)
        )

    def save(self, rows):
        # Keep enough history to support changing a window to the largest allowed one.
        cutoff = self.clock() - 43200 * 60
        rows = [row for row in rows if row.get("pending") or row["time"] > cutoff]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        temp.replace(self.path)
        self.usage = rows

    def turn(self, event):
        value = event.get_extra(TURN)
        if value is None:
            value = {"count": 0, "finished": False, "error": "", "delivered": False}
            event.set_extra(TURN, value)
        return value

    def check(self, event, count):
        key = self.gate.event_key(event)
        if key is None:
            return ""
        settings = self.settings(key)
        if self.gate.blocked(event) or not settings["enabled"]:
            return "[生图已关闭] 本群暂不允许生图。"
        if type(count) is not int or count < 1:
            return "[生图参数错误] 张数必须为正整数。"
        if (
            settings["per_request"]
            and self.turn(event)["count"] + count > settings["per_request"]
        ):
            return f"[生图限额] 单条消息最多生成 {settings['per_request']} 张。"
        if (
            settings["window_limit"]
            and self.used(key) + count > settings["window_limit"]
        ):
            return (
                f"[生图限额] 本群 {settings['window_minutes']} 分钟内最多 "
                f"{settings['window_limit']} 张，当前已用 {self.used(key)} 张（含进行中）。"
            )
        return ""

    def reserve(self, event, count):
        error = self.check(event, count)
        if error:
            return None, error
        key = self.gate.event_key(event)
        if key is None:
            return None, ""
        ticket = {
            "id": uuid4().hex,
            "key": key,
            "time": self.clock(),
            "count": count,
            "pending": True,
        }
        self.save([*self.usage, ticket])
        self.turn(event)["count"] += count
        return ticket, ""

    def settle(self, event, ticket, actual):
        if ticket is None:
            return
        rows = [row for row in self.usage if row["id"] != ticket["id"]]
        if actual:
            rows.append(
                {**ticket, "count": actual, "time": self.clock(), "pending": False}
            )
        self.save(rows)
        self.turn(event)["count"] += actual - ticket["count"]

    def quiet(self, event):
        key = self.gate.event_key(event)
        return bool(key and not self.settings(key)["caption"])
