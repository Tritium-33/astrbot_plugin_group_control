"""Authenticated AstrBot View APIs for the group speaking switches."""

import asyncio
import json
import re

from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request
from astrbot.core.star.session_llm_manager import SessionServiceManager

PLUGIN = "astrbot_plugin_group_control"


class GroupPanel:
    def __init__(self, plugin):
        self.plugin = plugin
        self.gate = plugin.group_gate
        self.catalogue = {}
        self.refreshed = False
        self.warnings = []
        self.refresh_lock = asyncio.Lock()
        self.routes = []

    def register(self):
        for endpoint, handler, method in (
            ("groups", self.list_groups, "GET"),
            ("groups/refresh", self.refresh_groups, "POST"),
            ("groups/switch", self.switch_groups, "POST"),
            ("groups/images", self.save_images, "POST"),
            ("groups/replies", self.save_replies, "POST"),
        ):
            route = f"/{PLUGIN}/{endpoint}"
            self.plugin.context.register_web_api(route, handler, [method], "群聊控制")
            self.routes.append(handler)

    def close(self):
        if not self.routes:
            return
        apis = self.plugin.context.registered_web_apis
        apis[:] = [row for row in apis if row[1] not in self.routes]
        self.routes.clear()

    async def refresh(self):
        async with self.refresh_lock:
            self.warnings = []

            async def read(platform):
                identity = platform.meta().id
                try:
                    async with asyncio.timeout(8):
                        groups = await platform.get_client().get_group_list()
                    rows = {
                        self.gate.key(identity, str(group["group_id"])): {
                            "platform_id": identity,
                            "group_id": str(group["group_id"]),
                            "name": str(group.get("group_name", "")),
                        }
                        for group in groups
                    }
                except Exception as error:  # noqa: BLE001 - OneBot boundary
                    self.warnings.append(f"{identity} 群列表暂不可用，仍可按群号控制。")
                    logger.warning(
                        "[群聊控制] %s 群列表读取失败：%s",
                        identity,
                        type(error).__name__,
                    )
                    return
                self.catalogue = {
                    key: row
                    for key, row in self.catalogue.items()
                    if row["platform_id"] != identity
                }
                self.catalogue.update(rows)

            await asyncio.gather(*(read(p) for p in self.gate.platforms()))
            self.refreshed = True

    async def snapshot(self):
        platforms = [{"id": p.meta().id} for p in self.gate.platforms()]
        available = {p["id"] for p in platforms}
        rows = {}
        for key, state in self.gate.states.items():
            platform, group = json.loads(key)
            rows[key] = {
                "platform_id": platform,
                "group_id": group,
                "name": state.get("name", ""),
            }
        rows.update(self.catalogue)
        output = []
        for key, row in sorted(rows.items()):
            umo = f"{row['platform_id']}:GroupMessage:{row['group_id']}"
            output.append(
                {
                    **row,
                    "enabled": self.gate.state(key)["enabled"],
                    "available": row["platform_id"] in available,
                    **(
                        {
                            "images": self.plugin.image_policy.settings(key),
                            "image_used": self.plugin.image_policy.used(key),
                        }
                        if hasattr(self.plugin, "image_policy")
                        else {}
                    ),
                    **(
                        {
                            "replies": self.plugin.reply_policy.settings(key),
                            "reply_used": self.plugin.reply_policy.usage(key),
                        }
                        if hasattr(self.plugin, "reply_policy")
                        else {}
                    ),
                    "native_enabled": await SessionServiceManager.is_session_enabled(
                        umo
                    ),
                }
            )
        conflict = self.plugin.legacy_conflict()
        warnings = list(self.warnings)
        if conflict:
            warnings.append("旧版路由插件仍包含群控，请更新至 v0.5.8 并重载后再操作。")
        if any(
            star.name == "astrbot_plugin_evidence_qa"
            and star.activated
            and hasattr(star.star_cls, "group_limit")
            for star in self.plugin.context.get_all_stars()
        ):
            warnings.append(
                "路由插件仍在执行旧限额，请先更新并重载至 v0.5.12，再使用本面板额度。"
            )
        return {
            "groups": output,
            "platforms": platforms,
            "warnings": warnings,
            "legacy_conflict": conflict,
            "image_ready": bool(
                getattr(getattr(self.plugin, "image_adapter", None), "instances", None)
            ),
        }

    async def save_images(self):
        payload = await request.json(default={})
        if (
            not isinstance(payload, dict)
            or payload.get("platform_id")
            not in {p.meta().id for p in self.gate.platforms()}
            or not isinstance(payload.get("group_id"), str)
            or not re.fullmatch(r"[1-9][0-9]{4,15}", payload["group_id"])
        ):
            return error_response("请选择有效平台和群号")
        key = self.gate.key(payload["platform_id"], payload["group_id"])
        try:
            self.plugin.image_policy.update(key, payload.get("images"))
        except ValueError as error:
            return error_response(str(error))
        except OSError:
            logger.exception("[群聊控制] 保存生图设置失败")
            return error_response("保存失败，原设置未改变", status_code=500)
        return json_response(await self.snapshot())

    async def save_replies(self):
        payload = await request.json(default={})
        if (
            not isinstance(payload, dict)
            or payload.get("platform_id")
            not in {p.meta().id for p in self.gate.platforms()}
            or not isinstance(payload.get("group_id"), str)
            or not re.fullmatch(r"[1-9][0-9]{4,15}", payload["group_id"])
        ):
            return error_response("请选择有效平台和群号")
        key = self.gate.key(payload["platform_id"], payload["group_id"])
        try:
            self.plugin.reply_policy.update(key, payload.get("replies"))
        except ValueError as error:
            return error_response(str(error))
        except OSError:
            logger.exception("[群聊控制] 保存回复额度失败")
            return error_response("保存失败，原设置未改变", status_code=500)
        return json_response(await self.snapshot())

    async def list_groups(self):
        if not self.refreshed:
            await self.refresh()
        return json_response(
            await self.snapshot(), headers={"Cache-Control": "no-store"}
        )

    async def refresh_groups(self):
        await self.refresh()
        return json_response(await self.snapshot())

    async def switch_groups(self):
        if self.plugin.legacy_conflict():
            return error_response(
                "请先更新并重载路由插件至 v0.5.8，避免两个开关冲突", status_code=409
            )
        payload = await request.json(default={})
        if not isinstance(payload, dict) or type(payload.get("enabled")) is not bool:
            return error_response("请选择开启或关闭")
        groups = payload.get("groups")
        if not isinstance(groups, list) or not 1 <= len(groups) <= 500:
            return error_response("请选择 1–500 个群")
        available = {p.meta().id for p in self.gate.platforms()}
        validated = []
        for row in groups:
            if (
                not isinstance(row, dict)
                or not isinstance(row.get("platform_id"), str)
                or row["platform_id"] not in available
                or not isinstance(row.get("group_id"), str)
                or not re.fullmatch(r"[1-9][0-9]{4,15}", row["group_id"])
            ):
                return error_response("请选择有效的 QQ 平台，并填写正确群号")
            key = self.gate.key(row["platform_id"], row["group_id"])
            validated.append(
                {
                    "platform_id": row["platform_id"],
                    "group_id": row["group_id"],
                    "name": self.catalogue.get(key, {}).get(
                        "name", self.gate.state(key).get("name", "")
                    ),
                }
            )
        try:
            self.gate.set_enabled(validated, payload["enabled"])
        except OSError:
            logger.exception("[群聊控制] 保存群发言开关失败")
            return error_response("保存失败，原开关状态未改变", status_code=500)
        return json_response(await self.snapshot())
