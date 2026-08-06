"""Step 6 文件化 Agent - DFMEA Excel 生成器"""
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from models.report import FmeaReportRow, FmeaFinalReport
from models.structure import StructureTree
from models.function import FunctionAnalysis
from models.failure import FailureAnalysis
from models.risk import RiskAnalysis
from models.optimization import OptimizationAnalysis

logger = logging.getLogger(__name__)


class DocumentAgent:
    """Step 6: 文件化 — 组装报告 + 生成 DFMEA Excel"""

    def __init__(self, output_dir: str = "data/projects"):
        self.output_dir = Path(output_dir)

    def assemble_report(
        self,
        project: dict,
        structure_tree: StructureTree,
        function_analysis: FunctionAnalysis,
        failure_analysis: FailureAnalysis,
        risk_analysis: RiskAnalysis,
        optimization_analysis: OptimizationAnalysis,
    ) -> FmeaFinalReport:
        """将所有步骤的数据组装为最终的 FMEA 报告

        核心逻辑：将各步骤的数据按关注要素对齐，生成报告行
        """
        report = FmeaFinalReport(
            project_id=project.get("id", ""),
            project_name=project.get("name", ""),
            fmea_type=project.get("fmea_type", "DFMEA"),
            generated_at=datetime.now().isoformat(),
            company_name=project.get("company_name", ""),
            product_model=project.get("product_model", ""),
            part_number=project.get("part_number", ""),
            customer_name=project.get("customer_name", ""),
            production_location=project.get("production_location", ""),
            model_year_program=project.get("model_year_program", ""),
            team_members=project.get("team_members", []),
            structure_tree=structure_tree,
            function_analysis=function_analysis,
            failure_analysis=failure_analysis,
            risk_analysis=risk_analysis,
            optimization_analysis=optimization_analysis,
        )

        # 建立各种索引
        # v3: func_map 按 node_id 索引，但一个节点有多个功能
        func_map = {item.node_id: item for item in function_analysis.items} if function_analysis else {}
        # v3: func_by_function_id 按 function_id 索引，快速查找功能详情
        func_by_function_id = {}
        if function_analysis:
            for item in function_analysis.items:
                for fn in item.functions:
                    func_by_function_id[fn.function_id] = (item, fn)

        node_map = {n.id: n for n in structure_tree.nodes} if structure_tree else {}

        # 按失效链生成报告行
        failure_items = failure_analysis.items if failure_analysis else []
        risk_map = {}
        if risk_analysis:
            for ra in risk_analysis.items:
                risk_map[ra.failure_id] = ra

        opt_map = {}
        if optimization_analysis:
            for oa in optimization_analysis.items:
                opt_map[oa.failure_id] = oa

        issue_number = 0
        for fm in failure_items:
            issue_number += 1
            ra = risk_map.get(fm.id)
            opt = opt_map.get(fm.id)
            node = node_map.get(fm.node_id)

            # v3: 优先通过 function_id 查找具体功能，兼容旧数据
            func_item = func_map.get(fm.node_id)
            specific_fn = None
            if fm.function_id and fm.function_id in func_by_function_id:
                _, specific_fn = func_by_function_id[fm.function_id]
            elif func_item and func_item.functions:
                # 兜底：用第一条功能
                specific_fn = func_item.functions[0]

            # 找父节点
            parent_node = node_map.get(node.parent_id) if node and node.parent_id else None

            # 找子节点
            children = [node_map[c.id] for c in structure_tree.nodes
                       if c.parent_id == fm.node_id] if structure_tree else []

            # v3: 关注要素功能 = 具体功能描述（而非合并字符串）
            focus_func = ""
            if specific_fn:
                focus_func = specific_fn.description
            elif func_item:
                focus_func = func_item.key_element_function

            row = FmeaReportRow(
                issue_number=f"# {issue_number}",
                # 结构分析
                next_higher_level=parent_node.name if parent_node else "整车",
                focus_element=fm.node_name,
                next_lower_level=", ".join([c.name for c in children[:3]]),
                # 功能分析
                higher_level_function=func_item.upper_level_function if func_item else "",
                focus_element_function=focus_func,
                lower_level_function=func_item.lower_level_function if func_item else "",
                # 失效分析
                failure_effect=fm.failure_effect,
                severity=fm.severity,
                failure_mode=fm.failure_mode,
                failure_cause=fm.failure_cause,
                # 风险分析
                prevention_control=ra.prevention_control if ra else "",
                occurrence=ra.occurrence if ra else 5,
                detection_control=ra.detection_control if ra else "",
                detection=ra.detection if ra else 5,
                rpn=ra.rpn if ra else 0,
                ap=ra.ap if ra else "",
                # 优化
                preventive_action=opt.preventive_action if opt else "",
                detective_action=opt.detective_action if opt else "",
                responsible_person=opt.responsible_person if opt else "",
                target_date=opt.target_date if opt else "",
                status=opt.status if opt else "",
                evidence_action=opt.evidence_action if opt else "",
                completion_date=opt.completion_date if opt else "",
                improved_s=opt.improved_s if opt else 0,
                improved_o=opt.improved_o if opt else 0,
                improved_d=opt.improved_d if opt else 0,
                improved_ap=opt.improved_ap if opt else "",
            )
            report.report_rows.append(row)

        logger.info(f"Report assembled: {len(report.report_rows)} rows")
        return report

    def generate_excel(self, report: FmeaFinalReport, output_path: Optional[str] = None) -> str:
        """生成 AIAG/VDA 标准 DFMEA Excel 文件 (46列)

        Args:
            report: 组装好的FMEA报告
            output_path: 输出路径，默认自动生成

        Returns:
            生成的文件路径
        """
        try:
            import openpyxl
            from openpyxl.styles import Font, Alignment, Border, Side, PatternFill
            from openpyxl.utils import get_column_letter
        except ImportError:
            raise ImportError("需要安装 openpyxl: pip install openpyxl")

        if not output_path:
            proj_id = report.project_id
            date_str = datetime.now().strftime("%Y-%m-%d")
            output_path = str(self.output_dir / proj_id / "output" / f"DFMEA_{report.project_name}_{date_str}.xlsx")

        # 确保输出目录存在
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "DFMEA（新版格式）"

        self._fill_header(ws, report)
        self._fill_data_rows(ws, report)
        self._apply_styles(ws, report)
        self._apply_merge_cells(ws, report)

        # ── §4.7 按专业分类 sheet ──
        self._add_professional_sheets(wb, report)

        wb.save(output_path)
        logger.info(f"DFMEA Excel saved to {output_path}")
        return output_path

    def _fill_header(self, ws, report: FmeaFinalReport):
        """填充规划信息头（Rows 1-10）"""
        # Row 1: 标题
        ws.merge_cells("A1:AT1")
        title_cell = ws.cell(1, 1, "Design Failure Mode and Effects Analysis (Design FMEA) 设计失效模式及影响分析(设计FMEA)")
        title_cell.font = Font(size=14, bold=True)

        # Row 2: 步骤标题
        ws.merge_cells("A2:AT2")
        ws.cell(2, 1, "Planning and Preparation (Step 1) 策划与准备 （步骤一）").font = Font(bold=True)

        # Row 3-8: 项目信息
        info_rows = [
            ("Company Name:\n公司名称:", report.company_name, "Product Type:\n产品型号:", report.product_model),
            ("Production Location:\n生产地址:", report.production_location, "PN:\n零件号:", report.part_number),
            ("Customer Name:\n顾客名称:", report.customer_name, "Model Year / Program(s):\n年型/项目:", report.model_year_program),
            ("Customer PN:\n顾客零件号:", "", "Confidentiality Level:\n保密级别:", report.confidentiality_level),
            ("Customer Drawing Revision No:\n顾客图纸版本号:", "", "Cross-Functional Team:\n跨职能团队:", ", ".join(report.team_members)),
        ]

        for i, (label1, val1, label2, val2) in enumerate(info_rows):
            r = 3 + i
            ws.cell(r, 1, label1).alignment = Alignment(wrap_text=True, vertical="top")
            ws.cell(r, 2, val1)
            ws.cell(r, 9, label2).alignment = Alignment(wrap_text=True, vertical="top")
            ws.cell(r, 10, val2)

        # Row 9: 列组标题
        col_groups = [
            (1, 2, "Continuous Improvement\n持续改善"),
            (3, 5, "Structure Analysis(Step2)\n结构分析（步骤二）"),
            (6, 8, "Function Analysis (Step 3)\n功能分析 （步骤三）"),
            (9, 12, "Failure Analysis (Step 4)\n失效分析 （步骤四）"),
            (13, 18, "DFMEA Risk Analysis (Step 5)\n设计 FMEA 风险分析 （步骤五）"),
            (19, 29, "DFMEA Optimization (Step 6)\n设计 FMEA 优化（步骤六）"),
        ]

        for col_start, col_end, title in col_groups:
            if col_end >= col_start:
                ws.merge_cells(start_row=9, start_column=col_start, end_row=9, end_column=col_end)
            ws.cell(9, col_start, title).font = Font(bold=True, size=10)
            ws.cell(9, col_start).alignment = Alignment(horizontal="center", wrap_text=True)

        # 跳过 Row 8（空行），从 Row 9 开始

        # Row 10: 列标题
        headers = [
            "Issues\n问题#",  # 1
            "History/change Authorization\n(as Applicable)\n历史/变更授权(适用时)",  # 2
            "1. Next Higher Level\n上一较高级别",  # 3
            "2. Focus Element\n关注要素",  # 4
            "3. Next Lower Level or Characteristic Type\n下一较低级别或特性类",  # 5
            "4. Next Higher Level Function and Requirement\n上一较高级别功能和要求",  # 6
            "5. Focus Element Function and Requirement\n关注要素功能和要求",  # 7
            "6. Next Lower Level Function and Requirement or Characteristic Type\n下一较低级别功能和要求",  # 8
            "1. Failure effects(FE) to the Next Higher Level Element and/or End User\n对于上一较高级别",  # 9
            "Severity of FE(S)\n失效影响的严重度",  # 10
            "2. Failure Mode(FM) of focus Element\n关注要素的失效模式 (FM)",  # 11
            "3. Failure Cause (FC) of the Next Lower Element or Characteristic\n下一较低级别要素或特性的失效",  # 12
            "Current Prevention Control(PC) of FC\n失效起因的当前预防控制(PC)",  # 13
            "Occurrence(O) of FC\n失效起因的频度(O)",  # 14
            "Current Detection Control(DC) of FC or FM\n失效起因或失效模式的当前探测控制(DC)",  # 15
            "Detection (D) of FC /FM\n失效起因/失效模式的探测度",  # 16
            "DFMEA AP\n设计FMEA措施优先级",  # 17
            "Special Characteristics\n特性类别",  # 18
            "Preventive Action\n预防措施",  # 19
            "Detection Action\n探测措施",  # 20
            "Responsible Person\n负责人姓名",  # 21
            "Target Completion Date\n目标完成日期",  # 22
            "Status\n状态",  # 23
            "Action Taken with Pointer to Evidence\n采取基于证据的措施",  # 24
            "Completion Date\n完成日期",  # 25
            "Severity(S)\n严重度",  # 26
            "Occurrence(O)\n发生度",  # 27
            "Detection (D)\n探测度",  # 28
            "DFMEA AP\n措施优先级",  # 29
            "Remark\n备注",  # 30
        ]

        for col, header in enumerate(headers, 1):
            cell = ws.cell(10, col, header)
            cell.font = Font(bold=True, size=9)
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    def _fill_data_rows(self, ws, report: FmeaFinalReport):
        """填充分析数据行（从 Row 11 开始）"""
        for i, row_data in enumerate(report.report_rows):
            r = 11 + i

            ws.cell(r, 1, row_data.issue_number)
            ws.cell(r, 2, row_data.history)
            ws.cell(r, 3, row_data.next_higher_level)
            ws.cell(r, 4, row_data.focus_element)
            ws.cell(r, 5, row_data.next_lower_level)
            ws.cell(r, 6, row_data.higher_level_function)
            ws.cell(r, 7, row_data.focus_element_function)
            ws.cell(r, 8, row_data.lower_level_function)
            ws.cell(r, 9, row_data.failure_effect)
            ws.cell(r, 10, row_data.severity)
            ws.cell(r, 11, row_data.failure_mode)
            ws.cell(r, 12, row_data.failure_cause)
            ws.cell(r, 13, row_data.prevention_control)
            ws.cell(r, 14, row_data.occurrence)
            ws.cell(r, 15, row_data.detection_control)
            ws.cell(r, 16, row_data.detection)
            ws.cell(r, 17, row_data.ap)
            ws.cell(r, 18, row_data.special_characteristics)
            ws.cell(r, 19, row_data.preventive_action)
            ws.cell(r, 20, row_data.detective_action)
            ws.cell(r, 21, row_data.responsible_person)
            ws.cell(r, 22, row_data.target_date)
            ws.cell(r, 23, row_data.status)
            ws.cell(r, 24, row_data.evidence_action)
            ws.cell(r, 25, row_data.completion_date)
            ws.cell(r, 26, row_data.improved_s or "")
            ws.cell(r, 27, row_data.improved_o or "")
            ws.cell(r, 28, row_data.improved_d or "")
            ws.cell(r, 29, row_data.improved_ap)
            ws.cell(r, 30, row_data.remark)

    def _apply_styles(self, ws, report: FmeaFinalReport):
        """应用样式：对齐、边框、条件格式"""
        from openpyxl.styles import Font, Alignment, Border, Side, PatternFill

        thin_border = Border(
            left=Side(style="thin"),
            right=Side(style="thin"),
            top=Side(style="thin"),
            bottom=Side(style="thin"),
        )

        # 数据行样式
        for r in range(11, 11 + len(report.report_rows)):
            for c in range(1, 31):
                cell = ws.cell(r, c)
                cell.alignment = Alignment(vertical="top", wrap_text=True)
                cell.font = Font(size=9)
                cell.border = thin_border

                # AP 高亮
                if c == 17:  # AP 列
                    ap_val = str(cell.value).strip()
                    if ap_val == "H":
                        cell.fill = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
                        cell.font = Font(size=9, color="9C0006", bold=True)
                    elif ap_val == "M":
                        cell.fill = PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid")
                    elif ap_val == "L":
                        cell.fill = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")

                # ── §4.5.2-4: AP=H + 法规违规 → 深红加粗特殊标记 ──
                if c == 17 and str(cell.value).strip() == "H":
                    if "reg" in str(cell.value).lower():
                        pass  # 待后续读取法规列后实现交叉匹配

                # RPN 高亮
                if c in (10, 14, 16):  # S/O/D 列
                    try:
                        val = int(cell.value) if cell.value else 0
                        if val >= 9:
                            cell.fill = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
                            cell.font = Font(size=9, bold=True, color="9C0006")
                    except (ValueError, TypeError):
                        pass

        # 列宽
        col_widths = {1: 5, 2: 15, 3: 12, 4: 15, 5: 12, 6: 18, 7: 18, 8: 18,
                      9: 25, 10: 6, 11: 20, 12: 20, 13: 22, 14: 6, 15: 22,
                      16: 6, 17: 6, 18: 10, 19: 20, 20: 20, 21: 12, 22: 12,
                      23: 8, 24: 20, 25: 12, 26: 6, 27: 6, 28: 6, 29: 6, 30: 15}

        from openpyxl.utils import get_column_letter as gcl
        for col, width in col_widths.items():
            ws.column_dimensions[gcl(col)] = gcl(col)  # width in characters

    def _apply_merge_cells(self, ws, report: FmeaFinalReport):
        """按关注要素合并单元格（Col 3-4 同一要素合并）"""
        if not report.report_rows:
            return

        current_element = None
        merge_start = 11

        for i, row in enumerate(report.report_rows):
            r = 11 + i
            if row.focus_element != current_element:
                # 结束上一组合并
                if current_element and merge_start < r - 1:
                    ws.merge_cells(start_row=merge_start, start_column=3,
                                    end_row=r - 1, end_column=3)
                    ws.merge_cells(start_row=merge_start, start_column=4,
                                    end_row=r - 1, end_column=4)
                # 开始新组
                current_element = row.focus_element
                merge_start = r

        # 最后一组合并
        last_row = 11 + len(report.report_rows) - 1
        if merge_start < last_row:
            ws.merge_cells(start_row=merge_start, start_column=3,
                           end_row=last_row, end_column=3)
            ws.merge_cells(start_row=merge_start, start_column=4,
                           end_row=last_row, end_column=4)

    def _add_professional_sheets(self, wb, report):
        """§4.7: 按专业分类 sheet（BMS需求/BMS软件/BMS硬件/电芯/结构/电气）"""
        agent_rows = {}
        for row in report.report_rows:
            agent = getattr(row, 'responsible_agent', '') or ''
            if not agent:
                agent = '未归类'
            agent_rows.setdefault(agent, []).append(row)

        if len(agent_rows) <= 1:
            return  # 分类不足，跳过

        for agent_name, rows in agent_rows.items():
            ws2 = wb.create_sheet(title=f"{agent_name}"[:31])  # sheet名限31字符
            ws2.cell(row=1, column=1, value=f"专业分类: {agent_name} ({len(rows)}条)").font = Font(size=11, bold=True)

            headers = ["失効", "失效影响(FE)", "失效模式(FM)", "失效原因(FC)", "S", "O", "D", "RPN", "AP", "优化措施"]
            for ci, h in enumerate(headers, 1):
                cell = ws2.cell(row=3, column=ci, value=h)
                cell.font = Font(size=9, bold=True, color="FFFFFF")
                cell.fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")

            for ri, row in enumerate(rows, 4):
                fm = getattr(row, 'failure_mode', '')[:60]
                fe = getattr(row, 'failure_effect', '')[:60]
                fc = getattr(row, 'failure_cause', '')[:60]
                s = getattr(row, 'severity', '')
                o = getattr(row, 'occurrence', '')
                d = getattr(row, 'detection', '')
                ap = getattr(row, 'ap', '')

                ws2.cell(row=ri, column=1, value=ri - 3)
                ws2.cell(row=ri, column=2, value=fe)
                ws2.cell(row=ri, column=3, value=fm)
                ws2.cell(row=ri, column=4, value=fc)
                ws2.cell(row=ri, column=5, value=s)
                ws2.cell(row=ri, column=6, value=o)
                ws2.cell(row=ri, column=7, value=d)
                ws2.cell(row=ri, column=8, value=s * o * d if isinstance(s, int) and isinstance(o, int) and isinstance(d, int) else '')
                ws2.cell(row=ri, column=9, value=ap)
                if ap == "H":
                    ws2.cell(row=ri, column=9).fill = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
