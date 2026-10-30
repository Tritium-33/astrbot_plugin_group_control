"""Independent QQ group speaking controls and an authenticated dashboard View."""

from aiocqhttp.api_impl import UnifiedApi
from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.pipeline.scheduler import PipelineScheduler
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from .group_gate import GroupGate
from .group_panel import GroupPanel

PLUGIN = "astrbot_plugin_group_control"


class GroupControlPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        folder = StarTools.get_data_dir(PLUGIN)
        self.group_gate = GroupGate(self, folder / "group_switches.json")
        self.group_panel = GroupPanel(self)

    def legacy_conflict(self):
        return any(
            star.name == "astrbot_plugin_evidence_qa"
            and star.activated
            and hasattr(star.star_cls, "group_gate")
            for star in self.context.get_all_stars()
        )

    async def initialize(self):
        if self.legacy_conflict():
            raise RuntimeError(
                "请先将证据问答与会话路由更新到 v0.5.8 并重载，再启用独立群聊控制。"
            )
        legacy = (
            self.group_gate.path.parent.parent
            / "astrbot_plugin_evidence_qa"
            / "group_switches.json"
        )
        migrated = self.group_gate.load(legacy)
        try:
            self.group_gate.install(
                PipelineScheduler, UnifiedApi, AiocqhttpMessageEvent
            )
            self.group_gate.attach_gcp()
            self.group_panel.register()
        except Exception:
            await self.terminate()
            raise
        logger.info(
            "[群聊控制] 独立面板已启用；%d个群关闭；旧状态迁移=%s。",
            sum(not state["enabled"] for state in self.group_gate.states.values()),
            migrated,
        )

    @filter.on_astrbot_loaded()
    async def loaded(self):
        self.group_gate.attach_gcp()

    async def terminate(self):
        self.group_panel.close()
        self.group_gate.close()
