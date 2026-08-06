"""
AI 自动矫正自查机制 — 对应需求规格 §6

六类基础漏洞自动核查：
1. 法规条款漏匹配
2. BOM 零件漏拆解
3. 失效逻辑前后冲突
4. S/O/D 打分标准不统一
5. 混入制程/装配类失效
6. 高 AP 风险项无配套优化措施
"""
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class SelfCheckEngine:
    """AI 自动自查引擎

    全部 issue 携带 `requirement_ref` 字段（对应需求规格编号），
    便于审计追溯：
    - SC-1~SC-6：六类基础漏洞（§6.2）
    - V3-1~V3-4：阶段 3 功能分析校验（§4.3.4）
    - V4-1~V4-4：阶段 4 失效分析校验（§4.4.4）
    - V5-1~V5-4：阶段 5 风险分析校验（§4.5.4）
    - V6-1~V6-4：阶段 6 优化分析校验（§4.6.5）
    - QA-1.1~QA-6.2：质量验收标准（§5）
    - HC-1~HC-6：硬性执行约束（§8）
    """

    # issue_type → 默认需求编号（兜底映射）
    DEFAULT_REQUIREMENT_REF = {
        "regulation_gap": "SC-1",
        "bom_completeness": "SC-2",
        "failure_logic": "SC-3",
        "sod_consistency": "SC-4",
        "manufacturing_mixed": "SC-5",
        "optimization_missing": "SC-6",
        "high_risk_transmission_chain": "SC-3",
    }

    def __init__(self):
        self._issues: List[Dict[str, Any]] = []

    # ════════════════════════════════════════════════════════════
    # 主入口
    # ════════════════════════════════════════════════════════════

    def run_all_checks(
        self,
        stage_id: int,
        stage_name: str,
        data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """运行全部六类核查

        Args:
            stage_id: 当前阶段编号 (1-7)
            stage_name: 阶段名称
            data: 该阶段的完整输出数据

        Returns:
            自查报告 {"passed": bool, "issues": [...], "summary": str}
        """
        self._issues = []

        # 按阶段触发不同的检查组合
        if stage_id == 1:
            self._check_regulation_gap(data)
        elif stage_id == 2:
            self._check_bom_completeness(data)
        elif stage_id == 4:
            self._check_failure_logic(data)
            self._check_manufacturing_mixed(data)
            # §6.3 特殊专项复核：高风险传导链路完整性
            self._check_high_risk_transmission_chain(data)
        elif stage_id == 5:
            self._check_sod_consistency(data)                     # V5-1
            self._check_prevention_detection_classification(data) # V5-2
            self._check_ap_calculation(data)                      # V5-3
            self._check_sod_completeness(data)                    # V5-4
        elif stage_id == 6:
            self._check_optimization_missing(data)

        # 全阶段通用检查
        critical = [i for i in self._issues if i.get("severity") == "error"]
        passed = len(critical) == 0

        return {
            "stage_id": stage_id,
            "stage_name": stage_name,
            "passed": passed,
            "total_issues": len(self._issues),
            "critical_issues": len(critical),
            "issues": self._issues,
            "summary": self._generate_summary(passed),
        }

    # ════════════════════════════════════════════════════════════
    # 检查器 1：法规条款漏匹配
    # ════════════════════════════════════════════════════════════

    def _check_regulation_gap(self, data: Dict[str, Any]):
        """检查法规条款是否全部完成匹配映射（对应需求 SC-1 + QA-1.1）"""
        doc1 = data.get("document_1", {})
        mapping = doc1.get("mapping_table", [])

        for row in mapping:
            if not row.get("product_function"):
                self._add_issue(
                    "error",
                    f"法规条款 {row.get('clause_id', '?')} 未匹配到产品功能",
                    "regulation_gap",
                    {"clause": row},
                    requirement_ref="QA-1.1",
                )

        # 检查法规清单是否为空
        regulation_list = data.get("regulation_list", [])
        if not regulation_list:
            self._add_issue(
                "warning",
                "项目未配置法规清单",
                "regulation_gap",
                {},
                requirement_ref="QA-1.1",
            )

    # ════════════════════════════════════════════════════════════
    # 检查器 2：BOM 零件漏拆解
    # ════════════════════════════════════════════════════════════

    def _check_bom_completeness(self, data: Dict[str, Any]):
        """检查结构树是否完整覆盖 BOM（对应需求 SC-2 + V3-1 + QA-2.1）"""
        tree = data.get("structure_tree", {})
        nodes = tree.get("nodes", [])
        if isinstance(tree, dict) and "nodes" not in tree:
            nodes = tree.get("tree", {}).get("nodes", [])

        # V3-1 / QA-2.1 检查是否有零件级节点（防漏拆解）
        parts = [n for n in nodes if n.get("level") == "零件"]
        if not parts:
            self._add_issue(
                "warning",
                "结构树中未发现零件级节点",
                "bom_completeness",
                {"total_nodes": len(nodes)},
                requirement_ref="V3-1",
            )

        # 检查根节点唯一性
        roots = [n for n in nodes if not n.get("parentId") or n.get("parentId") == ""]
        if len(roots) != 1:
            self._add_issue(
                "error",
                f"根节点数量异常: {len(roots)} (应为1)",
                "bom_completeness",
                {"root_count": len(roots)},
                requirement_ref="QA-2.1",
            )

    # ════════════════════════════════════════════════════════════
    # 检查器 3：失效逻辑前后冲突
    # ════════════════════════════════════════════════════════════

    def _check_failure_logic(self, data: Dict[str, Any]):
        """检查失效逻辑是否自洽（对应需求 SC-3 + V4-1）"""
        failures = data.get("items", [])
        if not failures and isinstance(data, dict):
            failures = data.get("failure_analysis", {}).get("items", [])

        for i, f in enumerate(failures):
            # V4-1 检查三要素完整性
            if not f.get("failure_mode") or f.get("failure_mode") == "":
                self._add_issue(
                    "error",
                    f"失效条目 #{i}: 缺少失效模式",
                    "failure_logic",
                    {"index": i},
                    requirement_ref="V4-1",
                )
            if not f.get("failure_cause") or f.get("failure_cause") == "":
                self._add_issue(
                    "warning",
                    f"失效条目 #{i}: 缺少失效原因",
                    "failure_logic",
                    {"index": i},
                    requirement_ref="V4-1",
                )
            if not f.get("failure_effect") or f.get("failure_effect") == "":
                self._add_issue(
                    "warning",
                    f"失效条目 #{i}: 缺少失效影响",
                    "failure_logic",
                    {"index": i},
                    requirement_ref="V4-1",
                )

    # ════════════════════════════════════════════════════════════
    # 检查器 4：S/O/D 打分一致性
    # ════════════════════════════════════════════════════════════

    def _check_sod_consistency(self, data: Dict[str, Any]):
        """检查同类、同工况失效的 S/O/D 打分一致性（对应需求 V5-1）

        规则：
        - 按 (failure_mode, operating_condition) 复合 key 分组
        - S/O/D 三值都查（不再只查 S）
        - 同组内 S/O/D 任一值差 > 1 即报警
        - 无 condition 字段时按 (failure_mode,) 单独分组
        """
        items = data.get("items", [])
        if not items and isinstance(data, dict):
            items = data.get("risk_analysis", {}).get("items", [])

        def _extract_sod(item: Dict) -> tuple:
            """宽容提取 S/O/D 三个分值（兼容 severity/s, occurrence/o, detection/d）"""
            s = item.get("severity", item.get("s"))
            o = item.get("occurrence", item.get("o"))
            d = item.get("detection", item.get("d"))
            return s, o, d

        def _extract_condition(item: Dict) -> str:
            """提取工况字段（多种命名兼容）"""
            for k in ("operating_condition", "condition", "working_condition",
                      "scenario", "工况"):
                v = item.get(k)
                if v:
                    return str(v)
            return "_GLOBAL_"  # 全局兜底

        # 按 (failure_mode, condition) 分组
        groups: Dict[tuple, List[Dict]] = {}
        for item in items:
            mode = item.get("failure_mode", "unknown")
            cond = _extract_condition(item)
            key = (mode, cond)
            groups.setdefault(key, []).append(item)

        for (mode, cond), group in groups.items():
            if len(group) < 2:
                continue  # 单条不需对比

            # 收集 S/O/D 三值
            s_values = []
            o_values = []
            d_values = []
            for item in group:
                s, o, d = _extract_sod(item)
                if isinstance(s, (int, float)): s_values.append(int(s))
                if isinstance(o, (int, float)): o_values.append(int(o))
                if isinstance(d, (int, float)): d_values.append(int(d))

            label_suffix = f" @ {cond}" if cond != "_GLOBAL_" else ""

            # S 值一致性（差 > 1 报警）
            if s_values and (max(s_values) - min(s_values)) > 1:
                self._add_issue(
                    "warning",
                    f"失效模式 '{mode}'{label_suffix} 的 S 值不一致: {sorted(set(s_values))}（差值 > 1）",
                    "sod_consistency",
                    {"failure_mode": mode, "condition": cond, "s_values": sorted(set(s_values))},
                    requirement_ref="V5-1",
                )

            # O 值一致性
            if o_values and (max(o_values) - min(o_values)) > 1:
                self._add_issue(
                    "warning",
                    f"失效模式 '{mode}'{label_suffix} 的 O 值不一致: {sorted(set(o_values))}（差值 > 1）",
                    "sod_consistency",
                    {"failure_mode": mode, "condition": cond, "o_values": sorted(set(o_values))},
                    requirement_ref="V5-1",
                )

            # D 值一致性
            if d_values and (max(d_values) - min(d_values)) > 1:
                self._add_issue(
                    "warning",
                    f"失效模式 '{mode}'{label_suffix} 的 D 值不一致: {sorted(set(d_values))}（差值 > 1）",
                    "sod_consistency",
                    {"failure_mode": mode, "condition": cond, "d_values": sorted(set(d_values))},
                    requirement_ref="V5-1",
                )

    # ════════════════════════════════════════════════════════════
    # 检查器 5：混入制程/装配类失效
    # ════════════════════════════════════════════════════════════

    MANUFACTURING_KEYWORDS = [
        "装配", "焊接", "拧紧", "打胶", "涂胶", "注塑",
        "机加工", "冲压", "铸造", "来料", "批次", "供应",
        "产线", "工装", "夹具", "操作工", "培训",
    ]

    def _check_manufacturing_mixed(self, data: Dict[str, Any]):
        """检查是否混入了制程/装配类失效描述（对应需求 SC-5 + V4-2 + QA-1.3）"""
        items = data.get("items", [])
        if not items and isinstance(data, dict):
            items = data.get("failure_analysis", {}).get("items", [])

        for i, item in enumerate(items):
            cause = str(item.get("failure_cause", ""))
            for kw in self.MANUFACTURING_KEYWORDS:
                if kw in cause:
                    self._add_issue(
                        "error",
                        f"失效条目 #{i}: 失效原因含制程相关描述 '{kw}' → 建议删除",
                        "manufacturing_mixed",
                        {"index": i, "keyword": kw, "cause": cause[:80]},
                        requirement_ref="V4-2",
                    )
                    break

    def _check_optimization_missing(self, data: Dict[str, Any]):
        """检查高 AP 风险项是否都有优化措施"""
        risk_items = data.get("risk_items", [])
        opt_items = data.get("optimization_items", [])

        if not risk_items:
            risk_items = data.get("risk_analysis", {}).get("items", [])
        if not opt_items:
            opt_items = data.get("optimization_analysis", {}).get("items", [])

        # 检查高 AP 项 + 法规违规项（需求 V6-1: 高 AP、违规失效项均须配套优化方案）
        high_ap = [
            item for item in risk_items
            if item.get("ap") in ("H", "高") or
               (isinstance(item.get("rpn"), (int, float)) and item.get("rpn", 0) >= 80) or
               bool(item.get("regulation_violation"))
        ]

        opt_ids = {o.get("failure_id", o.get("id", "")) for o in opt_items}

        for item in high_ap:
            fid = item.get("failure_id", item.get("id", ""))
            if fid and fid not in opt_ids:
                tag = f"AP={item.get('ap', '?')}" + ("、法规违规" if item.get("regulation_violation") else "")
                self._add_issue(
                    "error",
                    f"高AP项 {item.get('failure_mode', '?')} ({tag}) 无优化措施",
                    "optimization_missing",
                    {"failure_id": fid, "ap": item.get("ap"), "sod": item.get("sod")},
                    requirement_ref="V6-1",
                )

    # ════════════════════════════════════════════════════════════
    # 检查器 6：V5-2 预防/探测措施分类抽查
    # ════════════════════════════════════════════════════════════

    def _check_prevention_detection_classification(self, data: Dict[str, Any]):
        """V5-2: 检查预防/探测控制措施分类是否清晰、有无混填

        规则:
        - 预防措施不应包含检测描述 (如 "测试"/"检查"/"验证")
        - 探测措施不应包含设计改善描述 (如 "冗余"/"降额"/"FTA")
        """
        items = data.get("items", [])
        if not items and isinstance(data, dict):
            items = data.get("risk_analysis", {}).get("items", [])

        detection_keywords = ("测试", "检查", "验证", "探测", "检测", "监控", "诊断",
                              "仿真", "试验", "test", "check", "verify", "detect", "monitor")
        prevention_keywords = ("冗余", "降额", "FTA", "分析", "计算", "裕量", "保护",
                               "隔离", "屏蔽", "冗余设计")

        for i, item in enumerate(items):
            pc = str(item.get("prevention_control", ""))
            dc = str(item.get("detection_control", ""))

            # 预防措施误填探测内容
            pc_has_detection = any(kw in pc for kw in detection_keywords)
            dc_has_prevention = any(kw in dc for kw in prevention_keywords)

            if pc_has_detection:
                self._add_issue(
                    "warning",
                    f"条目 #{i}: 预防措施 '{pc[:40]}' 含检测描述 → 疑似分类混淆",
                    "prevention_detection_mixed",
                    {"index": i, "field": "prevention_control"},
                    requirement_ref="V5-2",
                )
            if dc_has_prevention:
                self._add_issue(
                    "warning",
                    f"条目 #{i}: 探测措施 '{dc[:40]}' 含预防描述 → 疑似分类混淆",
                    "prevention_detection_mixed",
                    {"index": i, "field": "detection_control"},
                    requirement_ref="V5-2",
                )
            if not pc and not dc:
                self._add_issue(
                    "error",
                    f"条目 #{i}: 预防和探测措施均为空",
                    "empty_controls",
                    {"index": i},
                    requirement_ref="V5-2",
                )

    # ════════════════════════════════════════════════════════════
    # 检查器 7：V5-3 AP 计算逻辑重算校验
    # ════════════════════════════════════════════════════════════

    def _check_ap_calculation(self, data: Dict[str, Any]):
        """V5-3: 重新计算 AP 并与存储值比对，确认 AP 逻辑准确

        规则: 用 S/O/D 重算 AP，与记录值比对，不一致则告警
        """
        from engine.tools import calculate_ap as calc_ap

        items = data.get("items", [])
        if not items and isinstance(data, dict):
            items = data.get("risk_analysis", {}).get("items", [])

        mismatch_count = 0
        for i, item in enumerate(items):
            s = item.get("severity", item.get("s"))
            o = item.get("occurrence", item.get("o"))
            d = item.get("detection", item.get("d"))
            stored_ap = item.get("ap", "")

            if (isinstance(s, int) and isinstance(o, int) and isinstance(d, int)):
                recalc_ap = calc_ap(s, o, d)
                if recalc_ap != stored_ap and stored_ap:
                    mismatch_count += 1
                    if mismatch_count <= 5:  # 最多记录 5 条避免噪音
                        self._add_issue(
                            "error",
                            f"条目 #{i}: AP 计算不一致 (存储={stored_ap}, 重算={recalc_ap}, S={s}/O={o}/D={d})",
                            "ap_mismatch",
                            {"index": i, "stored": stored_ap, "recalculated": recalc_ap},
                            requirement_ref="V5-3",
                        )

            # 高 AP 项和法规违规项是否高亮
            if stored_ap in ("H", "高"):
                highlighted = item.get("highlighted", False)
                rpn_exceeds = isinstance(item.get("rpn"), (int, float)) and item.get("rpn", 0) >= 100
                if not highlighted and rpn_exceeds:
                    self._add_issue(
                        "warning",
                        f"条目 #{i}: AP={stored_ap} 但未高亮标记",
                        "ap_not_highlighted",
                        {"index": i, "ap": stored_ap},
                        requirement_ref="V5-3",
                    )

    # ════════════════════════════════════════════════════════════
    # 检查器 8：V5-4 空白打分 / 极端打分检查
    # ════════════════════════════════════════════════════════════

    def _check_sod_completeness(self, data: Dict[str, Any]):
        """V5-4: 检查无空白打分项、无随意极端打分

        规则:
        - S/O/D 任一为空 → error
        - S=10 or D=10 且无合理说明 → warning
        """
        items = data.get("items", [])
        if not items and isinstance(data, dict):
            items = data.get("risk_analysis", {}).get("items", [])

        for i, item in enumerate(items):
            s = item.get("severity", item.get("s"))
            o = item.get("occurrence", item.get("o"))
            d = item.get("detection", item.get("d"))

            # 空白检查
            if s is None or s == "" or s == 0:
                self._add_issue(
                    "error",
                    f"条目 #{i}: 严重度(S) 为空",
                    "sod_missing",
                    {"index": i, "field": "severity"},
                    requirement_ref="V5-4",
                )
            if o is None or o == "" or o == 0:
                self._add_issue(
                    "error",
                    f"条目 #{i}: 发生度(O) 为空",
                    "sod_missing",
                    {"index": i, "field": "occurrence"},
                    requirement_ref="V5-4",
                )
            if d is None or d == "" or d == 0:
                self._add_issue(
                    "error",
                    f"条目 #{i}: 探测度(D) 为空",
                    "sod_missing",
                    {"index": i, "field": "detection"},
                    requirement_ref="V5-4",
                )

            # 极端打分检查
            if isinstance(s, int) and s == 10:
                self._add_issue(
                    "warning",
                    f"条目 #{i}: 严重度 S=10 (最高) — 请确认有合理依据",
                    "extreme_score",
                    {"index": i, "field": "severity", "value": 10},
                    requirement_ref="V5-4",
                )
            if isinstance(d, int) and d == 10:
                self._add_issue(
                    "warning",
                    f"条目 #{i}: 探测度 D=10 (无法探测) — 请确认有合理依据",
                    "extreme_score",
                    {"index": i, "field": "detection", "value": 10},
                    requirement_ref="V5-4",
                )

    # ════════════════════════════════════════════════════════════
    # 检查器 9：高风险传导链路完整性（§6.3 特殊专项复核）
    # ════════════════════════════════════════════════════════════

    def _check_high_risk_transmission_chain(self, data: Dict[str, Any]):
        """§6.3 高风险传导链路专项复核

        按 high_risk_themes 主题（HEV 案例：热失控/高压绝缘/碰撞防护/BMS 异常等）
        逐个复核失效传导链路的完整性：
        1. 每个主题是否覆盖（关联的失效条目 > 0）
        2. 每条失效是否具备"上游根因 + 下游影响"完整链条
        3. 涉及专业清单是否齐全（与 high_risk_themes.involved_agents 对齐）

        Args:
            data: 阶段 4 失效分析的完整数据，应包含 high_risk_themes + failure items
        """
        # 提取高风险主题清单（可能在 baseline_doc 或 input_data 顶层）
        themes = (
            data.get("high_risk_themes")
            or (data.get("baseline_doc") or {}).get("high_risk_themes")
            or []
        )
        if not themes:
            return  # 未配置高风险主题则跳过

        # 提取失效条目
        failures = data.get("items", [])
        if not failures and isinstance(data, dict):
            failures = data.get("failure_analysis", {}).get("items", [])

        for theme in themes:
            theme_name = theme.get("name", theme.get("theme", "未命名主题"))
            expected_agents = set(theme.get("involved_agents", []) or [])

            # 找到该主题关联的失效条目
            # 关联依据：theme 字段、high_risk_theme 字段、关键词模糊匹配
            related = []
            for f in failures:
                f_theme = (f.get("theme") or f.get("high_risk_theme") or "").strip()
                f_agent = f.get("responsible_agent") or f.get("agent_name") or ""
                if f_theme == theme_name:
                    related.append(f)
                elif f_agent in expected_agents:
                    related.append(f)
                elif any(kw in str(f) for kw in [theme_name]):
                    related.append(f)

            # 规则 1：主题必须有关联失效条目
            if not related:
                self._add_issue(
                    "error",
                    f"高风险主题「{theme_name}」未发现任何关联失效条目（传导链起点缺失）",
                    "high_risk_transmission_chain",
                    {"theme": theme_name, "expected_agents": list(expected_agents)},
                    requirement_ref="QA-3.2",
                )
                continue

            # 规则 2：每条失效必须具备"上游根因 + 下游影响"完整链条
            broken_items = []
            for i, f in enumerate(related):
                cause = str(f.get("failure_cause", "")).strip()
                effect = str(f.get("failure_effect", "")).strip()
                if not cause:
                    broken_items.append({"index": i, "missing": "failure_cause",
                                        "mode": f.get("failure_mode", "?")})
                if not effect:
                    broken_items.append({"index": i, "missing": "failure_effect",
                                        "mode": f.get("failure_mode", "?")})

            for broken in broken_items:
                self._add_issue(
                    "error",
                    f"高风险主题「{theme_name}」传导链断裂: 失效 #{broken['index']} "
                    f"「{broken['mode']}」缺 {broken['missing']}",
                    "high_risk_transmission_chain",
                    {"theme": theme_name, **broken},
                    requirement_ref="V4-3",
                )

            # 规则 3：涉及专业清单覆盖度
            actual_agents = {
                f.get("responsible_agent") or f.get("agent_name")
                for f in related
                if f.get("responsible_agent") or f.get("agent_name")
            }
            missing_agents = expected_agents - actual_agents
            if missing_agents:
                self._add_issue(
                    "warning",
                    f"高风险主题「{theme_name}」涉及专业覆盖不全: 缺失 {sorted(missing_agents)}",
                    "high_risk_transmission_chain",
                    {"theme": theme_name, "expected": list(expected_agents),
                     "actual": list(actual_agents), "missing": list(missing_agents)},
                    requirement_ref="V4-3",
                )

    # ════════════════════════════════════════════════════════════
    # 辅助
    # ════════════════════════════════════════════════════════════

    def _add_issue(
        self,
        severity: str,
        description: str,
        issue_type: str,
        context: Dict[str, Any],
        requirement_ref: Optional[str] = None,
    ):
        """记录一条检查 issue

        Args:
            requirement_ref: 显式需求编号（如 "SC-1" / "V3-2" / "QA-1.1"），
                            缺省时从 issue_type 自动映射
        """
        if requirement_ref is None:
            requirement_ref = self.DEFAULT_REQUIREMENT_REF.get(issue_type, "")
        self._issues.append({
            "severity": severity,
            "type": issue_type,
            "requirement_ref": requirement_ref,
            "description": description,
            "suggestion": self._get_suggestion(issue_type, context),
            "context": context,
        })

    def _get_suggestion(self, issue_type: str, context: Dict) -> str:
        suggestions = {
            "regulation_gap": "请法规专家补充对应条款的产品功能映射",
            "bom_completeness": "请检查 BOM 文件完整性并重新生成结构树",
            "failure_logic": "请补充缺失的失效三要素",
            "sod_consistency": f"请统一 S 值标准（当前: {context.get('s_values', [])}）",
            "manufacturing_mixed": f"请将制程相关描述替换为设计根因",
            "optimization_missing": "强制提醒：请为高 AP 项补充设计优化方案",
        }
        return suggestions.get(issue_type, "请人工复核")

    def _generate_summary(self, passed: bool) -> str:
        if passed:
            return f"自查通过：{len(self._issues)} 个提示（0 严重错误）"
        errors = [i for i in self._issues if i["severity"] == "error"]
        return f"自查未通过：{len(errors)} 个严重错误，{len(self._issues) - len(errors)} 个警告"
