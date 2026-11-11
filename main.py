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
from .group_policy import GroupPolicy
from .image_adapter import ImageAdapter
from .image_policy import TURN, ImagePolicy
from .reply_policy import ReplyPolicy

PLUGIN = "astrbot_plugin_group_control"


class GroupControlPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        folder = StarTools.get_data_dir(PLUGIN)
        self.group_gate = GroupGate(self, folder / "group_switches.json")
        self.reply_policy = ReplyPolicy(self)
        self.gcp_policy = GroupPolicy(self)
        self.image_policy = ImagePolicy(self.group_gate, folder / "image_usage.json")
        self.image_adapter = ImageAdapter(self, self.image_policy)
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
        self.reply_policy.migrate()
        self.image_policy.load()
        try:
            from astrbot.core.agent.runners.tool_loop_agent_runner import (
                ToolLoopAgentRunner,
            )
            from astrbot.core.pipeline.process_stage.stage import ProcessStage

            self.image_adapter.install_runner(ToolLoopAgentRunner)
            self.image_adapter.attach()
            self.group_gate.install(
                PipelineScheduler, UnifiedApi, AiocqhttpMessageEvent, ProcessStage
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
        self.reply_policy.migrate()
        self.group_gate.attach_gcp()
        self.image_adapter.attach()

    @filter.on_llm_request(priority=100)
    async def image_request(self, event, request):
        if not self.reply_policy.acquire(self.reply_policy.observe(event)):
            return
        self.image_adapter.observe(event)
        request.system_prompt = (
            request.system_prompt or ""
        ) + self.image_adapter.references.hint(event)

    @filter.on_llm_response(priority=-10000)
    async def image_response(self, event, response):
        if response and self.image_adapter.quiet_turn(event):
            turn = event.get_extra(TURN)
            if response.role == "err" and not turn.get("error_sent"):
                await self.image_adapter.send_error(event, response.completion_text)
            response.completion_text = ""
            response.reasoning_content = ""
            response.result_chain = None

    @filter.on_decorating_result(priority=10000)
    async def image_result(self, event):
        result = event.get_result()
        if result and self.image_adapter.filter_chain(event, result) is None:
            event.clear_result()

    async def terminate(self):
        self.group_panel.close()
        self.image_adapter.close()
        self.group_gate.close()
        self.gcp_policy.close()
        self.reply_policy.close()
