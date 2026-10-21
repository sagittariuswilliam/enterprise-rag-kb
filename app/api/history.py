"""会话历史接口

路由:
    GET    /api/history/list        列出所有 session(按 updated_at 倒序)
    GET    /api/history/{id}        获取单个 session 的全部消息
    DELETE /api/history/{id}        删除一个 session

存储: ``app/data/history/{session_id}.json``(由 chat.py 写入)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException

from app.config import settings
from app.core.deps import get_current_identity

logger = logging.getLogger(__name__)
router = APIRouter()

HISTORY_DIR = Path(settings.data_dir) / "history"
HISTORY_DIR.mkdir(parents=True, exist_ok=True)


def _session_owner(data: Dict[str, Any]) -> str:
    """P1: 会话归属人。老文件无 owner 字段 → '' 视为无主, 仅 admin/boss 可见"""
    return str(data.get("owner_userid", "") or "")


def _read_history_file(path: Path) -> Dict[str, Any] | None:
    """读取单个历史文件,失败返回 None(跳过损坏文件)"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("读取历史文件失败: path=%s err=%s", path, exc)
        return None


def _file_mtime_iso(path: Path) -> str:
    """把文件的 mtime 转成 ISO 字符串,便于前端展示"""
    try:
        ts = path.stat().st_mtime
        return datetime.utcfromtimestamp(ts).isoformat() + "Z"
    except OSError:  # pragma: no cover
        return ""


@router.get("/history/list")
async def list_sessions(ident=Depends(get_current_identity)):
    """列出会话(按 updated_at 倒序)

    P1: 只列自己的会话; admin/boss 全量 (老板要能查任何人的问答记录)。

    返回::

        {
            "sessions": [
                {
                    "session_id": "...",
                    "message_count": 6,
                    "title": "...",         # 第一条 user 问题前 30 字
                    "created_at": "...",
                    "updated_at": "...",
                    "file": "xxx.json"
                }
            ],
            "count": N
        }
    """
    sessions: List[Dict[str, Any]] = []

    for f in HISTORY_DIR.glob("*.json"):
        data = _read_history_file(f)
        if data is None:
            continue

        # P1: 按人隔离
        if not (ident.admin or ident.boss):
            if _session_owner(data) != ident.userid:
                continue

        messages = data.get("messages", []) or []
        # 取首条 user 消息做标题
        title = ""
        for m in messages:
            if m.get("role") == "user":
                title = (m.get("content") or "")[:30]
                break

        sessions.append(
            {
                "session_id": data.get("session_id", f.stem),
                "message_count": len(messages),
                "title": title,
                "created_at": data.get("created_at", ""),
                "updated_at": data.get("updated_at") or _file_mtime_iso(f),
                "file": f.name,
                # mtime 用于排序(纯数字)
                "_mtime": f.stat().st_mtime if f.exists() else 0,
            }
        )

    # 按 mtime 倒序
    sessions.sort(key=lambda x: x.get("_mtime", 0), reverse=True)

    # 清理掉内部排序字段
    for s in sessions:
        s.pop("_mtime", None)

    return {"sessions": sessions, "count": len(sessions)}


@router.get("/history/{session_id}")
async def get_session(session_id: str, ident=Depends(get_current_identity)):
    """获取单个 session 的完整消息列表 (P1: 非 admin/boss 只能取自己的)"""
    if not session_id or session_id.strip() in ("", "."):
        raise HTTPException(status_code=400, detail="session_id 不能为空")

    # 防止路径穿越
    if "/" in session_id or "\\" in session_id or ".." in session_id:
        raise HTTPException(status_code=400, detail="非法的 session_id")

    history_file = HISTORY_DIR / f"{session_id}.json"
    if not history_file.exists():
        raise HTTPException(status_code=404, detail="会话不存在")

    data = _read_history_file(history_file)
    if data is None:
        raise HTTPException(status_code=500, detail="历史文件损坏")

    if not (ident.admin or ident.boss) and _session_owner(data) != ident.userid:
        raise HTTPException(status_code=404, detail="会话不存在")

    return {
        "session_id": data.get("session_id", session_id),
        "created_at": data.get("created_at", ""),
        "updated_at": data.get("updated_at") or _file_mtime_iso(history_file),
        "messages": data.get("messages", []),
        "message_count": len(data.get("messages", []) or []),
    }


@router.delete("/history/{session_id}")
async def delete_session(session_id: str, ident=Depends(get_current_identity)):
    """删除一个 session 及其全部消息 (P1: 非 admin/boss 只能删自己的)"""
    if not session_id or session_id.strip() in ("", "."):
        raise HTTPException(status_code=400, detail="session_id 不能为空")

    if "/" in session_id or "\\" in session_id or ".." in session_id:
        raise HTTPException(status_code=400, detail="非法的 session_id")

    history_file = HISTORY_DIR / f"{session_id}.json"
    if not history_file.exists():
        raise HTTPException(status_code=404, detail="会话不存在")

    data = _read_history_file(history_file)
    if not (ident.admin or ident.boss) and _session_owner(data or {}) != ident.userid:
        raise HTTPException(status_code=404, detail="会话不存在")

    try:
        history_file.unlink()
    except OSError as exc:
        logger.exception("删除历史失败: %s", history_file)
        raise HTTPException(status_code=500, detail=f"删除失败: {exc}")

    logger.info("删除会话: session_id=%s", session_id)
    return {"status": "deleted", "session_id": session_id}