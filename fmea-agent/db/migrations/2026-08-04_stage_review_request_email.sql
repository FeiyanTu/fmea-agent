-- ============================================================
-- 邮件通知开关 — v20.0 新增 stage_review_request 类型
-- 触发点: 工程师调用 /api/approval/approve 后, 当 is_all_approved=True 时
-- 收件人: 项目负责人（leader）
-- 发件人: 管理员（admin）
-- 生效范围: stage_id 1-5（结构分析/功能分析/失效分析/风险评估/优化措施）
-- ============================================================

-- 1. notification_type 字段已存在（VARCHAR(50)），不需要 ALTER
-- 2. 插入全局默认（默认全开；重复执行安全）
INSERT INTO email_notification_switch (project_id, stage_id, notification_type, enabled) VALUES
    (NULL, 1, 'stage_review_request', TRUE),   -- 结构分析全部工程师提交后通知负责人审核
    (NULL, 2, 'stage_review_request', TRUE),   -- 功能分析
    (NULL, 3, 'stage_review_request', TRUE),   -- 失效分析
    (NULL, 4, 'stage_review_request', TRUE),   -- 风险评估
    (NULL, 5, 'stage_review_request', TRUE)    -- 优化措施
ON CONFLICT (project_id, stage_id, notification_type) DO NOTHING;

-- ── 关闭某项目某阶段该通知的示例（不执行，仅参考）──
-- UPDATE email_notification_switch SET enabled = FALSE
-- WHERE project_id = 29 AND stage_id = 1 AND notification_type = 'stage_review_request';
