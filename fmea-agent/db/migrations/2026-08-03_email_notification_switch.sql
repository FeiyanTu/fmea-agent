-- ============================================================
-- 邮件通知开关（v17.12）
-- 每个通知触发点一个开关，默认全开；project_id=NULL 为全局默认
-- 生效优先级：项目级 > 全局级（同名 project_id 设置覆盖全局）
-- 只通过 SQL 增删改控制，无页面
-- ============================================================

CREATE TABLE IF NOT EXISTS email_notification_switch (
    id                SERIAL PRIMARY KEY,
    project_id        INTEGER REFERENCES projects(id) ON DELETE CASCADE,  -- NULL = 全局默认
    stage_id          SMALLINT,              -- 0-6；NULL = 全部阶段
    notification_type VARCHAR(50) NOT NULL,  -- stage_complete / stage_push / optimize_assignee / stage_confirm_request / stage_confirm_remind
    enabled           BOOLEAN DEFAULT TRUE,
    created_at        TIMESTAMP DEFAULT NOW(),
    updated_at        TIMESTAMP DEFAULT NOW(),
    UNIQUE (project_id, stage_id, notification_type)
);

-- ── 全局默认（默认全开；重复执行安全）──
INSERT INTO email_notification_switch (project_id, stage_id, notification_type, enabled) VALUES
    (NULL, 0, 'stage_complete', TRUE),   -- 规划准备完成通知
    (NULL, 1, 'stage_complete', TRUE),   -- 结构分析完成通知
    (NULL, 2, 'stage_complete', TRUE),   -- 功能分析完成通知
    (NULL, 3, 'stage_complete', TRUE),   -- 失效分析完成通知
    (NULL, 4, 'stage_complete', TRUE),   -- 风险评估完成通知
    (NULL, 5, 'stage_complete', TRUE),   -- 优化措施完成通知
    (NULL, 6, 'stage_complete', TRUE),   -- 最终报告完成通知
    (NULL, NULL, 'stage_push', TRUE),            -- 阶段推送（负责人→工程师）
    (NULL, NULL, 'optimize_assignee', TRUE),     -- 优化措施变更通知
    (NULL, NULL, 'stage_confirm_request', TRUE), -- 工程师确认请求
    (NULL, NULL, 'stage_confirm_remind', TRUE)   -- 确认提醒
ON CONFLICT (project_id, stage_id, notification_type) DO NOTHING;

-- ── 关闭某个开关的示例（不执行，仅参考）──
-- UPDATE email_notification_switch SET enabled = FALSE
-- WHERE project_id IS NULL AND stage_id = 3 AND notification_type = 'stage_complete';
