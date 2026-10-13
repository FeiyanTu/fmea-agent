# -*- coding: utf-8 -*-
"""飞书机器人推送通道（FMEA Agent 通知专用，从 PQCP 验证版剥离，无业务依赖）

依赖：httpx（requirements.txt 已有）、python-dotenv（可选）

凭证：环境变量 FEISHU_APP_ID / FEISHU_APP_SECRET（不内置默认值）
（.env 由 main.py 启动加载；本模块兜底自行加载项目根目录 .env）

权限要求（飞书开放平台 → 权限管理）：
  - 邮箱直发（receive_id_type=email）：仅需 im:message，本应用已验证可用
  - 邮箱/手机号 → user_id：需 contact:user.id:readonly（未开通时自动降级邮箱直发）

实测结论（2026-09-14，PQCP 两应用对比）：
  - 发送失败优先排查「应用可用范围」，而不是先怀疑权限 scope
  - post 富文本不支持 style.bold（230001），标题截断 40 字
  - 所有调用失败静默返回 False + 记日志，推送失败不影响主流程
"""
import json
import logging
import os
import time
from pathlib import Path

import httpx

try:
    from dotenv import load_dotenv
    # 兜底加载项目根目录 .env（main.py 已加载时为 no-op）
    load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")
except ImportError:
    pass

logger = logging.getLogger("feishu_push")

BASE_URL = "https://open.feishu.cn"
TOKEN_URL = f"{BASE_URL}/open-apis/auth/v3/tenant_access_token/internal"
BATCH_GET_ID_URL = f"{BASE_URL}/open-apis/contact/v3/users/batch_get_id"
SEND_MSG_URL = f"{BASE_URL}/open-apis/im/v1/messages"

# 凭证一律从环境变量读取（见项目根目录 .env.example），不内置默认凭证，避免密钥入库。
_app_id = (os.environ.get("FEISHU_APP_ID") or "").strip()
_app_secret = (os.environ.get("FEISHU_APP_SECRET") or "").strip()

_token_cache = {"token": None, "expire_at": 0}
_uid_cache: dict = {}
_id_lookup_blocked = False


def is_configured() -> bool:
    return bool(_app_id and _app_secret)


def get_tenant_access_token() -> str | None:
    """获取 tenant_access_token，内存缓存，提前 60s 过期自动刷新"""
    now = time.time()
    if _token_cache["token"] and _token_cache["expire_at"] > now + 60:
        return _token_cache["token"]
    if not is_configured():
        logger.warning("飞书未配置 FEISHU_APP_ID/FEISHU_APP_SECRET，跳过")
        return None
    try:
        resp = httpx.post(TOKEN_URL, json={
            "app_id": _app_id,
            "app_secret": _app_secret,
        }, timeout=10)
        data = resp.json()
        if data.get("code") != 0:
            logger.error("获取飞书 token 失败: %s", data.get("msg"))
            return None
        _token_cache["token"] = data.get("tenant_access_token")
        _token_cache["expire_at"] = now + int(data.get("expire", 7200))
        return _token_cache["token"]
    except Exception as e:
        logger.error("获取飞书 token 异常: %s", e)
        return None


def resolve_user_id(email: str = None, phone: str = None) -> str | None:
    """按邮箱/手机号查询飞书 user_id（需 contact:user.id:readonly，未开通返回 None）"""
    global _id_lookup_blocked
    if _id_lookup_blocked:
        return None
    if (not email and not phone) or not is_configured():
        return None
    cache_key = email or phone
    if cache_key in _uid_cache:
        return _uid_cache[cache_key]
    token = get_tenant_access_token()
    if not token:
        return None
    try:
        payload = {}
        if email:
            payload["emails"] = [email]
        if phone:
            payload["mobiles"] = [phone]
        resp = httpx.post(BATCH_GET_ID_URL, headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }, json=payload, timeout=10)
        data = resp.json()
        if data.get("code") != 0:
            msg = data.get("msg") or ""
            if "contact:user.id:readonly" in msg or "Access denied" in msg:
                _id_lookup_blocked = True
                logger.info("未开通 contact:user.id:readonly，跳过邮箱→user_id 解析，直接走邮箱直发")
            else:
                logger.warning("飞书按邮箱/手机号查询 user_id 失败(%s/%s): %s", email, phone, msg)
            return None
        for u in data.get("data", {}).get("user_list", []):
            uid = u.get("user_id") or u.get("open_id")
            if uid:
                _uid_cache[cache_key] = uid
                return uid
        return None
    except Exception as e:
        logger.error("飞书查询 user_id 异常(%s/%s): %s", email, phone, e)
        return None


def send_feishu_card(target_id: str, title: str, content: str, link: str = None,
                     receive_id_type: str = "open_id") -> bool:
    """发送 post 富文本消息。返回 True/False，不抛异常"""
    token = get_tenant_access_token()
    if not token or not target_id:
        return False

    lines = [[{"tag": "text", "text": title}]]
    for line in (content or "").split("\n"):
        line = line.strip()
        if line:
            lines.append([{"tag": "text", "text": line}])
    if link:
        lines.append([{"tag": "a", "text": "点击查看详情", "href": link}])

    post_content = {"zh_cn": {"title": (title or "通知")[:40], "content": lines}}
    try:
        resp = httpx.post(
            SEND_MSG_URL,
            params={"receive_id_type": receive_id_type},
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json={
                "receive_id": target_id,
                "msg_type": "post",
                "content": json.dumps(post_content, ensure_ascii=False),
            },
            timeout=10,
        )
        data = resp.json()
        if data.get("code") == 0:
            logger.info("飞书消息已发送到 %s(%s): %s", target_id, receive_id_type, title)
            return True
        logger.warning("飞书发送失败(%s/%s): %s", target_id, receive_id_type, data.get("msg"))
        return False
    except Exception as e:
        logger.error("飞书发送异常: %s", e)
        return False


def send_card_to_contact(email: str = None, phone: str = None,
                         title: str = "", content: str = "", link: str = None) -> bool:
    """一步到位：给邮箱/手机号发消息（先解析 user_id，失败自动降级邮箱直发）"""
    uid = resolve_user_id(email, phone)
    if uid:
        return send_feishu_card(uid, title, content, link, receive_id_type="open_id")
    if email:
        return send_feishu_card(email, title, content, link, receive_id_type="email")
    logger.info("未匹配到飞书账号（无邮箱/手机号或查询失败）: %s/%s", email, phone)
    return False


def diagnose(probe_email: str = None, verbose: bool = True) -> dict:
    """只读探测当前凭证各能力是否可用（不发消息）"""
    result = {}
    result["获取 tenant_access_token"] = bool(get_tenant_access_token())
    result["邮箱/手机号 → user_id  [contact:user.id:readonly]"] = (
        bool(resolve_user_id(probe_email)) if probe_email else None)
    if verbose:
        for k, v in result.items():
            mark = "✅ 可用" if v is True else ("❌ 不可用" if v is False else "➖ 未探测")
            print(f"  {k:52s} {mark}")
    return result
