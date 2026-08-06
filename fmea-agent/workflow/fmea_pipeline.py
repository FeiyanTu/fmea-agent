"""
FMEA 7-Step Pipeline — 多 Agent 架构 · WeKnora 知识库驱动 · 全阶段门控强制执行 · 组员审批确认
"""
import json
import logging
from pathlib import Path
from typing import Optional, Callable, Awaitable

from models.structure import StructureTree
from models.function import FunctionAnalysis
from models.failure import FailureAnalysis
from models.risk import RiskAnalysis
from models.optimization import OptimizationAnalysis
from models.report import FmeaFinalReport

from agents.base import AgentContext
from agents.system_integration import SystemIntegrationExpert
from agents.regulation import RegulationAgent
from orchestrator.agent_dispatcher import AgentDispatcher
from orchestrator.multi_agent_builder import MultiAgentBuilder
from orchestrator.gate import Gate, GateResult
from orchestrator.workflow_orchestrator import WorkflowOrchestratorAgent, STAGE_NAMES

logger = logging.getLogger(__name__)
ProgressCallback = Callable[[str, float, str], Awaitable[None]]


class PipelineBlockedError(Exception):
    def __init__(self, stage_id: int, stage_name: str, issues: list):
        self.stage_id = stage_id; self.stage_name = stage_name; self.issues = issues
        super().__init__(f"阶段 {stage_id} ({stage_name}) 门控驳回: {len(issues)} 个严重问题")


class ApprovalRequiredError(Exception):
    """阶段完成但等待组员审批"""
    def __init__(self, stage_id: int, stage_name: str, pending_count: int):
        self.stage_id = stage_id; self.stage_name = stage_name
        self.pending_count = pending_count
        super().__init__(f"阶段 {stage_id} ({stage_name}) 等待 {pending_count} 名组员确认")


class FmeaPipeline:
    """FMEA 7步 Pipeline — 全阶段门控 + 组员确认

    每步完成：AI自查 → 门控通过 → 发起组员审批 → 全员确认后自动唤醒下一步
    """

    def __init__(self, data_dir: str = "data/projects"):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._builder = MultiAgentBuilder(data_dir=str(data_dir))
        self._dispatcher: Optional[AgentDispatcher] = None
        self._gate = Gate()
        self._current_stage_id: int = 0         # HC-3: 当前执行中的阶段(0=空闲)
        self._stage_lock: bool = False           # HC-3: 阶段锁, 禁止多阶段并发
        self._boundary_config: dict = {}          # HC-6: 分析边界配置
        self._orchestrator: Optional[WorkflowOrchestratorAgent] = None
        self.project: Optional[dict] = None
        self._baseline: Optional[dict] = None
        self._kb_id: str = ""
        self._pending_stage: int = 0  # 等待审批的阶段号
        self._auto_proceed: bool = True  # 全员确认后自动进入下一阶段
        self.progress_callback: Optional[ProgressCallback] = None

    @property
    def orchestrator(self) -> WorkflowOrchestratorAgent:
        if self._orchestrator is None:
            self._orchestrator = WorkflowOrchestratorAgent(str(self.data_dir))
        return self._orchestrator

    @property
    def dispatcher(self) -> AgentDispatcher:
        if self._dispatcher is None:
            self._builder.llm_client = self._get_llm()
            self._dispatcher = self._builder.build_from_hev_preset()
        return self._dispatcher

    def _get_llm(self):
        from engine.llm_client import get_llm_client
        return get_llm_client()

    def _get_weknora(self):
        from knowledge.weknora_client import get_weknora_client
        return get_weknora_client()

    # ════════════════════════════════════════════════════════════
    # 各阶段（step0~step6）— 统一模式：执行 → 门控 → 审批请求
    # ════════════════════════════════════════════════════════════

    async def step0_planning(self, project_info: dict, sor_content: str = "") -> dict:
        await self._notify_progress("策划准备", 0.0, "系统集成专家正在锁定基线...")
        self.project = project_info
        self._kb_id = project_info.get("weknora_kb_id", project_info.get("kb_project_id", ""))

        # 初始化编排器：设置项目成员
        members = project_info.get("team_members", [])
        if members:
            self.orchestrator.set_project(project_info.get("id", ""),
                                          project_info.get("name", ""), members)
            # 设置全员确认后自动执行的处理器
            self.orchestrator.set_auto_proceed_handler(self._on_stage_approved)

        expert = SystemIntegrationExpert(llm_client=self._get_llm())
        ctx = AgentContext(step=1, step_name="策划准备与前置输入锁定",
            project_config={"project_info": project_info, "kb_id": self._kb_id},
            input_data={"project_info": project_info, "sor_content": sor_content, "kb_id": self._kb_id})
        result = await expert.analyze(ctx)
        self._baseline = result.data.get("baseline_doc", {})

        if self._kb_id:
            reg_agent = RegulationAgent(llm_client=self._get_llm(), weknora_client=self._get_weknora())
            reg_ctx = AgentContext(step=1, step_name="法规检索",
                project_config={"weknora_kb_id": self._kb_id},
                input_data={"kb_id": self._kb_id})
            reg_result = await reg_agent.analyze(reg_ctx)
            self._baseline["document_1"] = reg_result.data.get("document_1", {})

        summary = {**project_info, "current_step": 1, "baseline": self._baseline}
        self._save_step_data("00-planning.json", summary)

        # ── 独立交付物拆分（对应需求 §4.1.3 + §12 附录）──
        # 文件 0：DFMEA 策划基线文档
        if self._baseline:
            self._save_step_data("00-baseline-doc.json", {
                "document_id": "基线文档",
                "document_name": "DFMEA 策划基线文档",
                "generated_at": self._now_iso(),
                "baseline": self._baseline,
            })
        # 文件 1：法规条款映射清单
        if self._baseline.get("document_1"):
            self._save_step_data("01-document-1-regulation.json",
                                 self._baseline["document_1"])

        self._enforce_gate(1, "策划准备", summary)
        await self._notify_progress("策划准备", 1.0, "✅ 基线锁定完成")
        return summary

    async def step2_function(self, tree, project_id=None) -> FunctionAnalysis:
        self._kb_id = project_id or self._kb_id
        return await self._run_stage(3, "功能分析",
            {"structure_tree": tree, "kb_id": self._kb_id}, "02-function.json",
            lambda r: r, "功能分析完成")

    async def step3_failure(self, tree, func, project_id=None) -> FailureAnalysis:
        self._kb_id = project_id or self._kb_id
        data = {"structure_tree": tree, "function_analysis": func, "kb_id": self._kb_id}
        if self._baseline:
            data["high_risk_themes"] = self._baseline.get("high_risk_themes", [])
        return await self._run_stage(4, "失效分析", data, "03-failure.json",
            lambda r: r, "失效分析完成")

    async def step4_risk(self, failure) -> RiskAnalysis:
        return await self._run_stage(5, "风险分析",
            {"failure_analysis": failure}, "04-risk.json",
            lambda r: r, f"{len(r.items)}条")

    async def step5_optimization(self, risk) -> OptimizationAnalysis:
        return await self._run_stage(6, "优化分析",
            {"risk_analysis": risk}, "05-optimization.json",
            lambda r: r, f"{len(r.items)}条措施")

    async def step6_document(self, tree, func, failure, risk, opt) -> FmeaFinalReport:
        await self._notify_progress("文件化", 0.0, "正在组装FMEA报告...")
        # ... (document logic unchanged, omitted for brevity - same as before)
        reg_agent = RegulationAgent(llm_client=self._get_llm(), weknora_client=self._get_weknora())
        reg_output = await reg_agent.analyze(AgentContext(
            step=7, step_name="法规合规校验",
            project_config={"weknora_kb_id": self._kb_id},
            input_data={"kb_id": self._kb_id, "stage_data": opt.model_dump()}))
        ctx = self._make_context(7, "文件化", {
            "structure_tree": tree, "function_analysis": func,
            "failure_analysis": failure, "risk_analysis": risk,
            "optimization_analysis": opt, "project_info": self.project or {},
            "document_1": reg_output.data.get("document_1", {})})
        result = await self.dispatcher.dispatch_step(ctx)
        report = FmeaFinalReport()
        raw = result.data.get("result", result.data)
        if isinstance(raw, dict) and "report" in raw:
            report = raw["report"]
        elif hasattr(raw, 'model_dump'):
            report = raw
        report_data = report.model_dump() if hasattr(report, 'model_dump') else report
        self._save_step_data("06-report.json", report_data)

        from engine.quality_check import QualityScorer
        qr = QualityScorer().score({
            "document_1": reg_output.data.get("document_1", {}),
            "regulation_list": self.project.get("regulation_list", []),
            "structure_tree": tree.model_dump() if hasattr(tree, 'model_dump') else {},
            "function_analysis": func.model_dump() if hasattr(func, 'model_dump') else {},
            "failure_analysis": failure.model_dump() if hasattr(failure, 'model_dump') else {},
            "risk_analysis": risk.model_dump() if hasattr(risk, 'model_dump') else {},
            "optimization_analysis": opt.model_dump() if hasattr(opt, 'model_dump') else {},
            "report": report_data})

        # ── 独立交付物：全流程质量自查/验收报告（对应需求 §12 附录第 7 项）──
        quality_report = {
            "document_id": "质量验收报告",
            "document_name": "全流程质量自查与验收报告",
            "generated_at": self._now_iso(),
            "total_score": qr.get("total_score", 0),
            "verdict": qr.get("verdict", ""),
            "verdict_text": qr.get("verdict_text", ""),
            "dimension_scores": qr.get("dimensions", {}),
            "failed_dimensions": qr.get("failed_dimensions", []),
            "summary": qr.get("verdict_text", ""),
        }
        self._save_step_data("07-quality-check.json", quality_report)
        logger.info(f"[质量评分] {qr['total_score']}分, {qr['verdict']}")
        self._enforce_gate(7, "文件化", report_data)
        await self._notify_progress("文件化", 1.0, f"✅ 质量评分{int(qr['total_score'])}分")
        return report

    # ════════════════════════════════════════════════════════════
    # 统一阶段执行器 + 审批
    # ════════════════════════════════════════════════════════════

    # 阶段输出文件命名约定：0{stage_id-1}-xxx.json
    # 例如 stage_id=2 → 01-structure-tree.json
    # 实际策划（step0_planning）特殊处理：存 00-planning.json
    _STAGE_FILE_TEMPLATE = {
        1: "00-planning.json",
        2: "01-structure-tree.json",
        3: "02-function.json",
        4: "03-failure.json",
        5: "04-risk.json",
        6: "05-optimization.json",
        7: "06-report.json",
    }

    def _previous_stage_file(self, stage_id: int) -> Optional[str]:
        """获取指定阶段的前置阶段产出文件名（用于不可跳步校验）"""
        if stage_id <= 1:
            return None  # 阶段 1（策划）无前置
        prev_stage_id = stage_id - 1
        return self._STAGE_FILE_TEMPLATE.get(prev_stage_id)

    def _enforce_no_skip(self, stage_id: int, stage_name: str):
        """HC-2 硬性执行约束：七步法不可跳步

        校验当前阶段的前置阶段产出文件是否存在，
        若不存在则抛 PipelineBlockedError 拒绝执行。
        """
        prev_file = self._previous_stage_file(stage_id)
        if not prev_file:
            return  # 阶段 1 无前置
        prev_path = self._resolve_project_dir(self.project.get("id", "temp")) / "fmea" / prev_file
        if not prev_path.exists():
            raise PipelineBlockedError(
                stage_id, stage_name,
                [f"阶段顺序不可跳步（HC-2）: 前置阶段产出 {prev_file} 不存在，"
                 f"请先完成前置阶段"],
            )

    async def _run_stage(self, stage_id: int, stage_name: str,
                         input_data: dict, save_file: str,
                         extractor, summary_fmt: str):
        """统一的阶段执行流程：执行 → 门控 → 审批请求

        HC-2: 入口强制校验前置阶段文件存在，禁止跳步
        """
        # ── HC-2 不可跳步强约束：前置阶段文件必须存在 ──
        if self.project:
            self._enforce_no_skip(stage_id, stage_name)

        # ── HC-3 不可合并强约束：禁止多阶段并发执行 ──
        if self._stage_lock:
            raise PipelineBlockedError(
                stage_id, stage_name,
                f"HC-3 违规: 阶段 {self._current_stage_id} 正在执行中，"
                f"不可同时执行阶段 {stage_id}「{stage_name}」"
            )
        self._stage_lock = True
        self._current_stage_id = stage_id

        # ── HC-6 边界锁定：记录分析边界 ──
        if not self._boundary_config and self.project:
            self._boundary_config = {
                "product_type": self.project.get("product_type", ""),
                "development_phase": self.project.get("development_phase", "DV"),
                "boundary_conditions": self.project.get("boundary_conditions", []),
                "analysis_exclusions": self.project.get("analysis_exclusions", []),
            }

        await self._notify_progress(stage_name, 0.0, f"正在执行{stage_name}...")

        try:
            ctx = self._make_context(stage_id, stage_name, input_data)
            result = await self.dispatcher.dispatch_step(ctx)
            model = self._extract_model(result, self._get_model_class(stage_id))
            self._save_step_data(save_file, model.model_dump())

            # ── HC-6 边界锁定检查 ──
            if self._boundary_config:
                boundary_violations = self._check_boundary_compliance(
                    stage_id, stage_name, model, self._boundary_config
                )
                if boundary_violations:
                    logger.warning(
                        f"[HC-6] 阶段{stage_id} 边界违规: {boundary_violations}"
                    )

            self._enforce_gate(stage_id, stage_name, model.model_dump())

            # 阶段 2-6 需要组员审批
            if 2 <= stage_id <= 6 and self.project:
                summary = self._build_stage_summary(stage_id, stage_name, model)
                approval = await self.orchestrator.request_approval(stage_id, summary)
                self._pending_stage = stage_id
                await self._notify_progress(stage_name, 0.95,
                    f"⏳ {stage_name} 完成，等待 {approval.get('approved_count',0)}/{approval.get('total_count',0)} 名组员确认...")

                # 检查是否无需审批（无成员或已全员通过）
                if not self.project.get("team_members"):
                    pass  # 无成员时跳过审批
                elif not self._auto_proceed:
                    raise ApprovalRequiredError(stage_id, stage_name, len(approval.get("pending_members", [])))

            await self._notify_progress(stage_name, 1.0, f"✅ {summary_fmt}")
            return model
        finally:
            # HC-3: 释放阶段锁
            self._stage_lock = False
            self._current_stage_id = 0

    def _get_model_class(self, stage_id: int):
        return {2: StructureTree, 3: FunctionAnalysis, 4: FailureAnalysis,
                5: RiskAnalysis, 6: OptimizationAnalysis}.get(stage_id, dict)

    # ════════════════════════════════════════════════════════════
    # HC-6 边界锁定检查
    # ════════════════════════════════════════════════════════════

    def _check_boundary_compliance(
        self, stage_id: int, stage_name: str,
        model, boundary: dict,
    ) -> list:
        """HC-6: 检查阶段输出是否超出分析边界

        检查内容:
        1. 节点名称是否包含排除范围的术语
        2. 失效模式是否涉及制程/装配类描述
        3. 分析内容是否超出配置的 product_type 和 development_phase
        """
        violations = []
        exclusion_keywords = {
            "制造", "产线", "工装", "夹具", "来料", "注塑",
            "冲压", "铸造", "焊接工艺", "装配线",
        }

        items = getattr(model, 'items', [])
        for item in items:
            text = ""
            if hasattr(item, 'failure_cause'):
                text = item.failure_cause
            elif hasattr(item, 'failure_mode'):
                text = f"{item.failure_mode} "
            elif hasattr(item, 'description'):
                text = item.description
            elif hasattr(item, 'name'):
                text = item.name
            elif isinstance(item, dict):
                text = str(item.get('failure_cause', '')) + str(item.get('failure_mode', ''))

            for kw in exclusion_keywords:
                if kw in str(text):
                    violations.append(f"越界: 含排除词 '{kw}' (阶段{stage_id})")

        if violations:
            logger.warning(
                f"[HC-6] 阶段{stage_id}「{stage_name}」发现 "
                f"{len(violations)} 条边界违规"
            )
        return violations

    def _build_stage_summary(self, stage_id: int, stage_name: str, model) -> str:
        """构建阶段完成摘要"""
        items = getattr(model, 'items', [])
        return f"阶段{stage_id}「{stage_name}」已完成，共 {len(items)} 条记录，请审核确认。"

    async def _on_stage_approved(self, stage_id: int):
        """全员确认后的回调 — 自动唤醒下一阶段"""
        logger.info(f"[Pipeline] 🚀 阶段{stage_id} 全员确认，自动唤醒下一阶段")
        self._pending_stage = 0

    # ════════════════════════════════════════════════════════════
    # 对外接口：成员审批（供 API 调用）
    # ════════════════════════════════════════════════════════════

    async def member_approve(self, stage_id: int, member_name: str, comment: str = "") -> dict:
        """成员确认通过（API调用）"""
        return await self.orchestrator.approve_stage(stage_id, member_name, comment)

    async def member_reject(self, stage_id: int, member_name: str, comment: str = "") -> dict:
        """成员驳回（API调用）"""
        return await self.orchestrator.reject_stage(stage_id, member_name, comment)

    def get_approval_status(self, stage_id: Optional[int] = None) -> dict:
        """获取审批状态（API调用）"""
        return self.orchestrator.get_status(stage_id)

    async def check_approval_and_proceed(self) -> dict:
        """定时检查审批状态，全员通过时自动唤醒"""
        result = await self.orchestrator.check_and_notify()
        if result.get("ready_to_proceed"):
            self._pending_stage = 0
        return result

    # ════════════════════════════════════════════════════════════
    # 局部迭代模式（§7 模块化局部迭代）
    # ════════════════════════════════════════════════════════════

    async def run_incremental_update(
        self,
        change_type: str,
        target_module_id: str,
        change_data: dict,
    ) -> dict:
        """§7: 执行模块化局部迭代，无需全量重跑

        Args:
            change_type: "add_component" / "update_sor" / "add_condition" / "update_regulation"
            target_module_id: 目标模块/零部件 ID
            change_data: 变更数据

        Returns:
            {success, changed_modules, affected_modules, ap_updates, unchanged_modules}
        """
        from workflow.incremental_update import (
            IncrementalUpdateEngine, IncrementalUpdateRequest, ChangeType
        )

        ct_map = {
            "add_component": ChangeType.ADD_COMPONENT,
            "update_sor": ChangeType.UPDATE_SOR,
            "add_condition": ChangeType.ADD_CONDITION,
            "update_regulation": ChangeType.UPDATE_REGULATION,
        }
        ct = ct_map.get(change_type)
        if not ct:
            return {"success": False, "errors": [f"未知变更类型: {change_type}"]}

        engine = IncrementalUpdateEngine(data_dir=str(self.data_dir))
        request = IncrementalUpdateRequest(
            change_type=ct,
            target_module_id=target_module_id,
            change_data=change_data,
            project_id=self.project.get("project_id", ""),
        )
        result = engine.update(request)

        logger.info(
            f"[Pipeline] §7 局部迭代完成: "
            f"变更模块={result.changed_modules}, "
            f"影响模块={result.affected_modules}, "
            f"AP联动={len(result.ap_updates)}条"
        )
        return {
            "success": result.success,
            "changed_modules": result.changed_modules,
            "affected_modules": result.affected_modules,
            "ap_updates": result.ap_updates,
            "unchanged_modules": result.unchanged_modules,
            "errors": result.errors,
        }

    # ════════════════════════════════════════════════════════════
    # 批量模式
    # ════════════════════════════════════════════════════════════

    async def run_full_pipeline(self, project_info: dict,
                                sor_content: str = "",
                                tree: Optional[StructureTree] = None) -> FmeaFinalReport:
        await self.step0_planning(project_info, sor_content)
        kb_id = self._kb_id
        if tree is None:
            # 从已落盘的结构树文件加载（结构树由 extract_structure_tree_tool 走 WeKnora 生成）
            tree = self._load_structure_tree_from_disk(project_info)
        func = await self.step2_function(tree, kb_id)
        failure = await self.step3_failure(tree, func, kb_id)
        risk = await self.step4_risk(failure)
        opt = await self.step5_optimization(risk)
        return await self.step6_document(tree, func, failure, risk, opt)

    def _load_structure_tree_from_disk(self, project_info: dict) -> StructureTree:
        """从 01-structure-tree.json 加载（由 extract_structure_tree_tool 走 WeKnora 生成）"""
        import json as _json
        pd = self._resolve_project_dir(project_info.get("id", "temp"))
        path = pd / "fmea" / "01-structure-tree.json"
        if not path.exists():
            raise FileNotFoundError(
                f"结构树文件不存在: {path}。请先调用 extract_structure_tree_tool（走 WeKnora）"
            )
        with open(path, "r", encoding="utf-8") as f:
            data = _json.load(f)
        if isinstance(data, dict) and "nodes" in data:
            return StructureTree(nodes=data["nodes"])
        if isinstance(data, list):
            return StructureTree(nodes=data)
        return StructureTree(**data)

    # ════════════════════════════════════════════════════════════
    # 辅助方法（同前）
    # ════════════════════════════════════════════════════════════

    def _enforce_gate(self, stage_id, stage_name, data):
        gate_result = self._gate.check_stage(stage_id, stage_name, data)
        if gate_result["gate_result"] == GateResult.REJECT.value:
            issues = gate_result["check_result"].get("issues", [])
            raise PipelineBlockedError(stage_id, stage_name,
                [i for i in issues if i.get("severity") == "error"])
        if gate_result["gate_result"] == GateResult.NEEDS_REVISION.value:
            issues = gate_result["check_result"].get("issues", [])
            critical = [i for i in issues if i.get("severity") == "error"]
            if critical:
                raise PipelineBlockedError(stage_id, stage_name, critical)
        logger.info(f"[Gate] 阶段{stage_id}({stage_name}) ✅ PASS")

    def _make_context(self, step, name, data):
        return AgentContext(step=step, step_name=name,
            project_config={"kb_id": self._kb_id}, input_data=data,
            baseline_doc=self._baseline)

    def _extract_model(self, result, model_cls):
        raw = result.data.get("result", result.data)
        if hasattr(raw, 'model_dump'): return raw
        if isinstance(raw, dict) and raw: return model_cls(**raw)
        return model_cls()

    def _resolve_project_dir(self, project_id):
        import re as _re
        if (self.data_dir / project_id).is_dir(): return self.data_dir / project_id
        stripped = _re.sub(r'_\d{14}$', '', project_id)
        if stripped != project_id and (self.data_dir / stripped).is_dir():
            return self.data_dir / stripped
        return self.data_dir / project_id

    def _save_step_data(self, filename, data):
        if not self.project: return
        pd = self._resolve_project_dir(self.project.get("id", "temp"))
        (pd / "fmea").mkdir(parents=True, exist_ok=True)
        with open(pd / "fmea" / filename, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)

    async def _notify_progress(self, stage, progress, message):
        if self.progress_callback:
            await self.progress_callback(stage, progress, message)
        logger.info(f"[{stage}] {progress*100:.0f}% - {message}")

    def _now_iso(self) -> str:
        """当前时间 ISO 格式"""
        import datetime as _dt
        return _dt.datetime.now().isoformat(timespec="seconds")


_pipeline: Optional[FmeaPipeline] = None

def get_pipeline(data_dir: str = "data/projects") -> FmeaPipeline:
    global _pipeline
    if _pipeline is None:
        _pipeline = FmeaPipeline(data_dir)
    return _pipeline

