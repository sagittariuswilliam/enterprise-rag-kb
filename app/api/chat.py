"""问答接口(RAG)

路由:
    POST /api/chat        同步问答(检索 + rerank + LLM + 存历史)
    POST /api/chat/stream 流式问答(SSE: citations -> content chunks -> done)

实现要点:
    - 全部依赖服务层已有的 async 接口: ``Retriever.search`` / ``LLMClient.achat`` / ``achat_stream``
    - 历史以 JSON 文件形式持久化到 ``app/data/history/{session_id}.json``
    - 任何不可恢复错误返回 500;无检索命中时返回 200 + 空 answer + 友好提示
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.config import settings
from app.core.deps import get_current_identity, require_gray_open
from app.models.schemas import ChatRequest, ChatResponse
from app.services.audit import record as audit_record
from app.services.hit_stats import record_hits
from app.services.llm import build_rag_prompt, get_llm
from app.services.retriever import Retriever

logger = logging.getLogger(__name__)
router = APIRouter()

HISTORY_DIR = Path(settings.data_dir) / "history"
HISTORY_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================================
# 工具函数
# ============================================================================
def _build_citations(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把检索命中转成前端友好的 citations 列表

    字段:
        source / page / chunk_id / doc_id / score / snippet
    """
    out: List[Dict[str, Any]] = []
    for r in results:
        text = r.get("text") or ""
        snippet = text[:200] + ("..." if len(text) > 200 else "")
        out.append(
            {
                "source": r.get("source", "unknown"),
                "page": r.get("page", ""),
                "chunk_id": r.get("chunk_id"),
                "doc_id": r.get("doc_id", ""),
                "score": round(float(r.get("score", 0.0) or 0.0), 4),
                "snippet": snippet,
            }
        )
    return out


def _save_history(
    session_id: str,
    question: str,
    answer: str,
    citations: List[Dict[str, Any]],
    owner_userid: str = "",
) -> None:
    """把一轮问答追加到 session 历史 JSON

    P1: 带 owner_userid — 历史按人隔离, list/get/delete 只认自己的。

    文件结构::

        {
            "session_id": "xxx",
            "created_at": "...",
            "updated_at": "...",
            "messages": [
                {"role": "user",      "content": "...", "ts": "..."},
                {"role": "assistant", "content": "...", "citations": [...], "ts": "..."}
            ]
        }
    """
    history_file = HISTORY_DIR / f"{session_id}.json"
    now = datetime.utcnow().isoformat() + "Z"

    if history_file.exists():
        try:
            history = json.loads(history_file.read_text(encoding="utf-8"))
            if not isinstance(history, dict):
                history = {}
        except (json.JSONDecodeError, OSError):
            history = {}
    else:
        history = {
            "session_id": session_id,
            "owner_userid": owner_userid,
            "created_at": now,
            "updated_at": now,
            "messages": [],
        }

    history.setdefault("session_id", session_id)
    history.setdefault("messages", [])
    if owner_userid:
        history["owner_userid"] = owner_userid

    history["messages"].append(
        {"role": "user", "content": question, "ts": now}
    )
    history["messages"].append(
        {
            "role": "assistant",
            "content": answer,
            "citations": citations,
            "ts": now,
        }
    )
    history["updated_at"] = now

    try:
        history_file.write_text(
            json.dumps(history, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:  # pragma: no cover - 写失败不阻塞主流程
        logger.warning("保存历史失败: session=%s err=%s", session_id, exc)


# ============================================================================
# #4 检索问答审计(单点 helper, chat 两条链路共用; audit.record 内部吞异常不影响主流程)
# ============================================================================
def _audit_chat(ident, session_id: str, question: str, results: List[Dict[str, Any]]) -> None:
    """记录一次员工检索问答: 谁问了什么、ACL 内命中了哪些文档

    同时是 #6 命中统计的单点入口 (record_hits 内部吞异常, 不阻断问答)。
    """
    record_hits(results, ident, question)
    docs = []
    for r in results:
        d = {
            "doc_id": r.get("doc_id", ""),
            "source": r.get("source", "unknown"),
            "page": r.get("page", ""),
            "score": round(float(r.get("score", 0.0) or 0.0), 4),
        }
        if d not in docs:
            docs.append(d)
    audit_record(
        actor=f"{ident.name}({ident.userid})",
        action="chat_search",
        target=session_id,
        detail={
            "question": question[:200],
            "hits": len(docs),
            "docs": docs[:10],
            "is_admin_view": bool(ident.admin or ident.boss),
        },
    )


# ============================================================================
# 同步问答
# ============================================================================

async def _package_hint(question: str) -> dict | None:
    """M3 意图路由卡 — '要文件/找资料' 意图时返回引导卡 payload, 否则 None。

    纪律: 卡只是入口, AI 永不自动提交组包; 任何异常静默降级为 None,
    绝不能影响问答链路。
    """
    from app.services.intent_router import detect_file_need
    try:
        if await detect_file_need(question):
            return {
                "title": "检测到您在找成体系的材料",
                "desc": ("问答只能看到片段。需要完整材料包？我可以跨部门组一份清单"
                         "（含其他部门文档），经各部门负责人审批后生成 7 天有效的受控链接。"),
                "prefill": (question or "")[:200],
            }
    except Exception:  # noqa: BLE001
        logger.debug("package_hint 失败(忽略)", exc_info=True)
    return None


@router.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, ident=Depends(get_current_identity)):
    """RAG 同步问答

    流程:retrieve(带用户ACL) -> build prompt -> LLM.achat -> save history
    """
    require_gray_open(ident)
    if not req or not req.question or not req.question.strip():
        raise HTTPException(status_code=400, detail="question 不能为空")

    session_id = req.session_id or uuid.uuid4().hex[:8]
    top_k = req.top_k or settings.top_k_rerank

    # ---- 1. 检索 (P1: 强制 ACL, 无权限文档不进候选) ----
    try:
        retriever = Retriever()
        results = await retriever.search(req.question, top_k=top_k, identity=ident)
    except Exception as exc:
        logger.exception("retriever.search 失败")
        raise HTTPException(status_code=500, detail=f"检索失败: {exc}")

    _audit_chat(ident, session_id, req.question, results)

    if not results:
        answer = "知识库里没有相关内容,无法回答。"
        _save_history(session_id, req.question, answer, [], owner_userid=ident.userid)
        return ChatResponse(answer=answer, citations=[], session_id=session_id)

    # ---- 2. 构造 prompt ----
    context_chunks = [r.get("text") or "" for r in results]
    top_score = max((float(r.get("score") or 0.0) for r in results), default=None)
    messages = build_rag_prompt(req.question, context_chunks, top_score=top_score)

    # ---- 3. 调 LLM(异步)----
    try:
        llm = get_llm()
        answer = await llm.achat(messages, temperature=0.3)
    except Exception as exc:
        logger.exception("LLM 调用失败")
        raise HTTPException(status_code=500, detail=f"LLM 调用失败: {exc}")

    # ---- 4. citations + 落盘 ----
    citations = _build_citations(results)
    _save_history(session_id, req.question, answer, citations, owner_userid=ident.userid)

    return ChatResponse(answer=answer, citations=citations, session_id=session_id,
                        package_hint=await _package_hint(req.question))


# ============================================================================
# 流式问答(SSE)
# ============================================================================
@router.post("/chat/stream")
async def chat_stream(req: ChatRequest, ident=Depends(get_current_identity)):
    """流式问答,SSE 协议

    事件顺序:
        data: {"type": "citations", "citations": [...], "session_id": "..."}
        data: {"type": "content",   "delta": "..."}        # 可能多条
        data: {"type": "done",      "answer": "完整文本"}
        data: {"type": "error",     "message": "..."}      # 异常时
    """
    require_gray_open(ident)
    if not req or not req.question or not req.question.strip():
        raise HTTPException(status_code=400, detail="question 不能为空")

    session_id = req.session_id or uuid.uuid4().hex[:8]
    top_k = req.top_k or settings.top_k_rerank
    question = req.question

    async def generate():
        # ---- 1. 检索 (P1: 强制 ACL) ----
        try:
            retriever = Retriever()
            results = await retriever.search(question, top_k=top_k, identity=ident)
        except Exception as exc:
            logger.exception("retriever.search 失败 (stream)")
            yield "data: " + json.dumps(
                {"type": "error", "message": f"检索失败: {exc}"},
                ensure_ascii=False,
            ) + "\n\n"
            return

        _audit_chat(ident, session_id, question, results)

        if not results:
            payload = {
                "type": "done",
                "answer": "知识库里没有相关内容,无法回答。",
                "session_id": session_id,
            }
            yield "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"
            _save_history(session_id, question, payload["answer"], [],
                          owner_userid=ident.userid)
            return

        citations = _build_citations(results)

        # 先发 citations 让前端能先渲染参考来源
        yield "data: " + json.dumps(
            {
                "type": "citations",
                "citations": citations,
                "session_id": session_id,
            },
            ensure_ascii=False,
        ) + "\n\n"

        # ---- 2. 构造 prompt ----
        context_chunks = [r.get("text") or "" for r in results]
        top_score = max((float(r.get("score") or 0.0) for r in results), default=None)
        messages = build_rag_prompt(question, context_chunks, top_score=top_score)

        # ---- 3. 异步流式调 LLM ----
        full_answer = ""
        try:
            llm = get_llm()
            async for delta in llm.achat_stream(messages, temperature=0.3):
                if not delta:
                    continue
                full_answer += delta
                yield "data: " + json.dumps(
                    {"type": "content", "delta": delta},
                    ensure_ascii=False,
                ) + "\n\n"
        except Exception as exc:
            logger.exception("LLM 流式调用失败")
            # 把已经收到的部分也告诉前端
            yield "data: " + json.dumps(
                {"type": "error", "message": f"LLM 流式中断: {exc}"},
                ensure_ascii=False,
            ) + "\n\n"
            # 仍然落盘已收到的内容
            _save_history(session_id, question, full_answer, citations,
                          owner_userid=ident.userid)
            return

        # ---- 4. done ----
        yield "data: " + json.dumps(
            {"type": "done", "answer": full_answer, "session_id": session_id},
            ensure_ascii=False,
        ) + "\n\n"

        # ---- 5. M3 意图路由卡: 检测到"要文件/找资料" → 附组包引导卡 ----
        # 纪律: 卡只是入口, AI 永不自动提交组包; 失败静默, 绝不影响问答。
        hint = await _package_hint(question)
        if hint:
            yield "data: " + json.dumps(
                {"type": "package_hint", **hint}, ensure_ascii=False) + "\n\n"

        _save_history(session_id, question, full_answer, citations,
                      owner_userid=ident.userid)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # 关闭 nginx 缓冲,实时推送
            "Connection": "keep-alive",
        },
    )