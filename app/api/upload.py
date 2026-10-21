"""文档上传接口 — D4 异步化版(2026-09-20)

路由:
    POST   /api/upload                   上传文档 → 流式落盘 → L1路由/L4去重 → 立即返回 queued
    GET    /api/upload/status/{doc_id}   查询单个任务状态(状态机)
    GET    /api/upload/list              已入库文档 + 任务状态合并列表
    DELETE /api/upload/{source}          删除某个 source 的全部 chunks

D4 要点:
    - MAX_FILE_SIZE 50MB → 500MB(settings.max_file_size),**流式分块写盘**
      边写边计数,超限中止并删半成品,不再先 read 全文件(避免打爆内存)
    - 落盘 + L1 路由 + L4 哈希去重通过后立即返回 doc_id + status=queued,
      解析/OCR/切块/入库交后台线程池(upload_worker.py)
    - 状态机: queued → parsing(带 ocr_progress done/total) → ingested / failed(reason)
      持久化 app/data/upload_tasks.json,重启不丢已完成记录
    - 既有语义保留: 重复 409 / 空文件 400 / 类型不支持 400 / 路由不可用 400;
      解析后的质量门禁(空/短/乱码)、OcrQuotaError 等转入后台任务,
      以 failed+reason 呈现(OcrQuotaError 时文件保留)
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile

from app.acl.doc_acl import AclError, build_doc_acl, can_modify
from app.config import settings
from app.core.deps import get_current_identity, require_gray_open
from app.dingtalk.store import get_conn
from app.services import l4_notify
from app.services.gate import check_duplicate, sha256_of_file
from app.services.hash_registry import load_hash_registry, remove_hash, save_hash
from app.services.l4_approvals import create_pending, pick_approver, user_name
from app.services.router import route_file
from app.services.retriever import delete_document, list_documents
from app.services.upload_tasks import (
    STATUS_PENDING_APPROVAL,
    get_task_store,
    public_view,
)
from app.services.upload_worker import get_pool

logger = logging.getLogger(__name__)
router = APIRouter()

# 上传文件落地目录(由本路由独享,history 共用 data/ 但放不同子目录)
UPLOAD_DIR = Path(settings.data_dir) / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# 允许的扩展名 = 路由表全集（接入层 4 层改造: router 为唯一事实源）
from app.services.router import (  # noqa: E402
    PDF_EXTS, DOCX_EXTS, TEXT_EXTS, TABULAR_EXTS, PPTX_EXTS, IMAGE_EXTS,
    LEGACY_REJECT,
)
ALLOWED_EXTS = (
    PDF_EXTS | DOCX_EXTS | TEXT_EXTS | TABULAR_EXTS | PPTX_EXTS | IMAGE_EXTS
)

# 单文件上限 500MB(D4: 异步化后放开;流式写盘不占内存)
MAX_FILE_SIZE = settings.max_file_size

# 流式写盘分块大小
_WRITE_CHUNK = 1024 * 1024  # 1MB


def _split_csv(raw: str) -> list[str]:
    return [s.strip() for s in (raw or "").split(",") if s.strip()]


def _notify_approver(
    *, doc_id: str, approver_uid: str, owner_name: str,
    filename: str, size: int, acl: dict,
) -> None:
    """钉钉提醒审批人 (fire-and-forget, 送达情况回写 notify_ok)"""
    visible = len(acl.get("extra_acl") or [])
    text = (
        f"【知识库 L4 审批】{owner_name} 提交了文档《{filename}》"
        f"({max(1, round(size / 1024))}KB, 可见范围 {visible} 人/部门), "
        f"请前往管理控制台 → L4审批 处理\n\n"
        f"[管理控制台]({l4_notify.ADMIN_URL})"
    )
    l4_notify.send_async([approver_uid], "知识库 L4 审批", text, doc_id=doc_id)


async def _ingest_one(
    file: UploadFile,
    acl: dict,
    ident,
    *,
    batch_id: str = "",
    local_hashes: set[str] | None = None,
) -> dict:
    """单文件上传核心(P2b: 从 upload_document 抽出, 单传与批量共用同一状态机)

    流程: 扩展名 → 流式落盘 → 哈希去重(注册表 + 批内 local_hashes) →
          L1 路由 → 登记任务(L4 走审批分支) → 提交线程池。
    返回 dict 含 status=queued/pending_approval; 业务性拒绝抛 HTTPException
    (400/409/413/500), 调用方决定转成异常响应还是逐文件收集结果。
    """
    if not file or not file.filename:
        raise HTTPException(status_code=400, detail="未收到文件")

    # ---- 1. 扩展名校验 ----
    suffix = Path(file.filename).suffix.lower()
    if suffix in LEGACY_REJECT:
        raise HTTPException(status_code=400, detail=LEGACY_REJECT[suffix])
    if suffix not in ALLOWED_EXTS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件类型: {suffix},允许: {sorted(ALLOWED_EXTS)}",
        )

    doc_id = uuid.uuid4().hex[:8]
    # 文件名做一次净化,避免路径穿越
    safe_name = Path(file.filename).name
    file_path = UPLOAD_DIR / f"{doc_id}_{safe_name}"

    # ---- 2. 流式落盘(边写边计数,不再 read 全文件) ----
    total = 0
    try:
        with open(file_path, "wb") as out:
            while True:
                block = await file.read(_WRITE_CHUNK)
                if not block:
                    break
                total += len(block)
                if total > MAX_FILE_SIZE:
                    out.close()
                    file_path.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=413,
                        detail=f"文件过大: 超过上限 {MAX_FILE_SIZE} bytes"
                               f"(已接收 {total}+ bytes,半成品已删除)",
                    )
                out.write(block)
    except HTTPException:
        raise
    except OSError as exc:
        logger.error("写文件失败: path=%s err=%s", file_path, exc)
        file_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"写文件失败: {exc}")

    if total == 0:
        file_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="文件内容为空")

    # ---- L4 门禁 a: 哈希去重(解析前拦截,省一次 OCR/解析成本) ----
    # P2b: 批量导入时 local_hashes 承载"批内去重"(同一文件夹里两个相同文件)
    try:
        file_hash = sha256_of_file(file_path)
        known = load_hash_registry()
        dup = check_duplicate(file_hash, known)
        if not dup.ok:
            file_path.unlink(missing_ok=True)
            raise HTTPException(status_code=409, detail=f"重复文档: {dup.reason}")
        if local_hashes is not None and file_hash in local_hashes:
            file_path.unlink(missing_ok=True)
            raise HTTPException(status_code=409, detail="重复文档: 本批次内已有相同文件")
        if local_hashes is not None:
            local_hashes.add(file_hash)
    except HTTPException:
        raise
    except OSError as exc:
        logger.warning("哈希计算失败(跳过去重): %s", exc)
        file_hash = ""

    # ---- L1 路由 ----
    decision = route_file(file_path)
    if not decision.ok:
        file_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=400,
            detail=f"无法处理该文件 [route={decision.route}]: {decision.reason}",
        )

    # ---- 登记任务 + 提交后台线程池 → 立即返回 queued ----
    # L4 分支: 密级=指定人可见 → 必须审批通过后才允许入库。route 通过后不
    # submit 线程池, 而是落 l4_approvals 审批单 + 台账置 pending_approval,
    # 后台钉钉通知审批人(fire-and-forget)。批准动作在 app/api/l4.py 里提交。
    store = get_task_store()
    if acl["level"] == "L4":
        try:
            approver_uid, approver_name = pick_approver(ident.userid)
        except LookupError as exc:
            # 通讯录缺负责人且无 admin 兜底 — 不放行, 删文件, 不留半态
            file_path.unlink(missing_ok=True)
            logger.error("L4 审批人指派失败(通讯录缺负责人): owner=%s: %s",
                         ident.userid, exc)
            raise HTTPException(status_code=500, detail=str(exc))
        create_pending(
            doc_id,
            filename=safe_name,
            owner_userid=ident.userid,
            approver_userid=approver_uid,
            acl=acl,
            file_path=str(file_path),
            file_hash=file_hash,
            route=decision.route,
            size=total,
        )
        store.create(
            doc_id,
            filename=safe_name,
            size=total,
            route=decision.route,
            file_path=str(file_path),
            file_hash=file_hash,
            acl=acl,
            status=STATUS_PENDING_APPROVAL,
        )
        _notify_approver(
            doc_id=doc_id,
            approver_uid=approver_uid,
            owner_name=ident.name or user_name(ident.userid),
            filename=safe_name,
            size=total,
            acl=acl,
        )
        logger.info(
            "upload pending_approval: doc_id=%s filename=%s route=%s "
            "owner=%s approver=%s",
            doc_id, safe_name, decision.route, ident.userid, approver_uid,
        )
        return {
            "doc_id": doc_id,
            "filename": safe_name,
            "size": total,
            "route": decision.route,
            "status": "pending_approval",
            "level": acl["level"],
            "approver": approver_name,
        }

    store.create(
        doc_id,
        filename=safe_name,
        size=total,
        route=decision.route,
        file_path=str(file_path),
        file_hash=file_hash,
        acl=acl,
    )
    if batch_id:
        store.update(doc_id, batch_id=batch_id)
    get_pool().submit({
        "doc_id": doc_id,
        "file_path": str(file_path),
        "route": decision.route,
        "file_hash": file_hash,
        "filename": safe_name,
        "acl": acl,
    })

    logger.info(
        "upload queued: doc_id=%s filename=%s size=%d route=%s level=%s owner=%s batch=%s",
        doc_id, safe_name, total, decision.route, acl["level"], ident.userid,
        batch_id or "-",
    )
    return {
        "doc_id": doc_id,
        "filename": safe_name,
        "size": total,
        "route": decision.route,
        "status": "queued",
        "level": acl["level"],
    }


@router.post("/upload")
async def upload_document(
    file: UploadFile = File(...),
    level: str = Form(...),
    dept_ids: str = Form(""),
    l4_userids: str = Form(""),
    l4_dept_ids: str = Form(""),
    ident=Depends(get_current_identity),
):
    """上传文档 → 立即返回 queued,后台异步解析入库

    P1: 密级必选 (L1-L4), 上传者 = 当前登录人。
        L2 归属部门必选 (逗号分隔 dept_id); L4 指定人/部门必选 (选人组件产出)。

    P2b: 流程主体抽到 _ingest_one, 单传与批量共用同一状态机;
        本路由 = 灰度闸门 + ACL 参数校验 + 直接透传(单文件失败仍以 HTTP 状态码呈现)。
    """
    require_gray_open(ident)
    try:
        acl = build_doc_acl(
            level,
            owner_userid=ident.userid,
            dept_ids=_split_csv(dept_ids),
            l4_users=_split_csv(l4_userids),
            l4_depts=_split_csv(l4_dept_ids),
        )
    except AclError as exc:
        raise HTTPException(status_code=400, detail=f"密级参数错误: {exc}")

    return await _ingest_one(file, acl, ident)


# ---------------------------------------------------------------------------
# P2b · 批量导入(2026-10-19) — 共享文档机/存量档案, 管理员一次拖一批
#
# 语义(07 §存量资料策略 + 10 号决策版 P2 行):
#   - 批次默认密级(共享文档机 → L3 公司级; 部门批量 → 默认 L2+归属部门)
#   - 逐文件走与单传**完全相同**的状态机与门禁(route/hash 去重/quality gate/
#     L4 审批分支/低置信校对), 不做任何豁免
#   - 批内去重: 同一批里两个相同文件, 第二个 409 记入结果, 不入库
#   - 单文件失败不中断整批: 逐文件收集 status/reason, 前端渲染汇总表
#   - 权限: admin/boss(require_admin 口径)。部门负责人本部门批量导入留待
#     后续(10 号 §角色表), 本期先开管理员口
# ---------------------------------------------------------------------------

BATCH_MAX_FILES = 100  # 单请求文件数上限(500MB*100 由 nginx client_max_body_size 512m 再兜)


@router.post("/upload/batch")
async def upload_batch(
    files: list[UploadFile] = File(...),
    level: str = Form(...),
    dept_ids: str = Form(""),
    l4_userids: str = Form(""),
    l4_dept_ids: str = Form(""),
    ident=Depends(get_current_identity),
):
    """批量导入 — 一次一批, 整批共用默认密级/归属, 逐文件独立状态机"""
    from app.api.admin import require_admin  # 局部 import 避免循环依赖

    require_admin(ident)

    if not files:
        raise HTTPException(status_code=400, detail="未收到文件")
    if len(files) > BATCH_MAX_FILES:
        raise HTTPException(
            status_code=400,
            detail=f"单批最多 {BATCH_MAX_FILES} 个文件,当前 {len(files)} 个,请分批",
        )

    try:
        acl = build_doc_acl(
            level,
            owner_userid=ident.userid,
            dept_ids=_split_csv(dept_ids),
            l4_users=_split_csv(l4_userids),
            l4_depts=_split_csv(l4_dept_ids),
        )
    except AclError as exc:
        raise HTTPException(status_code=400, detail=f"密级参数错误: {exc}")

    batch_id = uuid.uuid4().hex[:8]
    local_hashes: set[str] = set()
    results: list[dict] = []
    for f in files:
        fname = Path(f.filename or "").name  # webkitRelativePath 会带路径, 只留文件名
        try:
            one = await _ingest_one(
                f, dict(acl), ident,
                batch_id=batch_id, local_hashes=local_hashes,
            )
            one["ok"] = True
        except HTTPException as exc:
            one = {
                "filename": fname, "ok": False,
                "http_status": exc.status_code,
                "reason": str(exc.detail), "status": "rejected",
            }
        except Exception as exc:  # 意外错误也不许断整批
            logger.exception("batch ingest unexpected: %s", fname)
            one = {"filename": fname, "ok": False,
                   "reason": f"内部错误: {exc}", "status": "failed"}
        results.append(one)

    accepted = sum(1 for r in results if r["ok"])
    logger.info(
        "batch import: batch_id=%s files=%d accepted=%d level=%s owner=%s",
        batch_id, len(files), accepted, acl["level"], ident.userid,
    )
    return {
        "batch_id": batch_id,
        "total": len(results),
        "accepted": accepted,
        "rejected": len(results) - accepted,
        "level": acl["level"],
        "results": results,
    }


@router.get("/upload/status/{doc_id}")
async def upload_status(doc_id: str):
    """查询单个上传任务的状态机(queued/parsing/ingested/failed + ocr_progress)"""
    if not doc_id or not doc_id.strip():
        raise HTTPException(status_code=400, detail="doc_id 不能为空")
    task = get_task_store().get(doc_id.strip())
    if task is None:
        raise HTTPException(status_code=404, detail=f"无此上传任务: {doc_id}")
    return public_view(task)


@router.get("/upload/list")
async def list_uploads(ident=Depends(get_current_identity)):
    """已入库文档(按 source, 只列有权可见的) + 全部任务状态合并列表

    P1: scroll 带 ACL filter — 无权限文档连标题都不出现 (07 §3.4)。
    任务列表只列自己的任务 (admin/boss 全量)。
    """
    try:
        docs = await list_documents(identity=ident)
    except Exception as exc:
        logger.exception("list_documents 失败")
        raise HTTPException(status_code=500, detail=f"列出文档失败: {exc}")

    all_tasks = get_task_store().list()
    if ident.admin or ident.boss:
        mine = all_tasks
    else:
        owner = (ident.userid,)
        mine = [t for t in all_tasks
                if (t.get("acl") or {}).get("owner_userid", "") in owner]
    tasks = [public_view(t) for t in mine]
    return {"documents": docs, "count": len(docs), "tasks": tasks}


@router.delete("/upload/{source}")
async def delete_upload(source: str, ident=Depends(get_current_identity)):
    """按文件名删除某个文档的全部 chunks

    Path 参数 ``source`` 即上传时的 filename,会原样作为 Qdrant filter 匹配。
    P1 权限: 只有 owner 本人或 admin/boss 可删 (先查 ACL 再删)。
    """
    if not source or source.strip() in ("", "."):
        raise HTTPException(status_code=400, detail="source 不能为空")

    # ---- 权限: 取该 source 的 acl (admin/boss 在 list_documents 内部自动免过滤) ----
    try:
        visible = await list_documents(identity=ident)
    except Exception as exc:
        logger.exception("list_documents 失败(删除前权限检查)")
        raise HTTPException(status_code=500, detail=f"删除失败: {exc}")
    doc = next((d for d in visible if d.get("source") == source), None)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"文档不存在或无权访问: {source}")
    if not can_modify(ident, doc):
        raise HTTPException(status_code=403, detail="只有文档上传者或管理员可删除")

    try:
        deleted = await delete_document(source)
    except Exception as exc:
        logger.exception("delete_document 失败: source=%s", source)
        raise HTTPException(status_code=500, detail=f"删除失败: {exc}")

    # 顺手清理本地 uploads 下的同名文件、media 留档与任务记录。
    # source 可能是 "{doc_id}_{filename}"(Qdrant payload 实际值)或裸 filename,两种都匹配。
    removed_docs: list[str] = []
    try:
        # 旧语义: "*_{source}" 前缀匹配
        for p in UPLOAD_DIR.glob(f"*_{source}"):
            removed_docs.append(p.name.split("_", 1)[0])
            p.unlink(missing_ok=True)
        # source 本身即完整落盘文件名(异步化后 payload source = 落盘名)
        exact = UPLOAD_DIR / source
        if exact.is_file():
            removed_docs.append(source.split("_", 1)[0])
            exact.unlink(missing_ok=True)
    except OSError:  # pragma: no cover
        pass

    import shutil

    store = get_task_store()
    media_root = Path(settings.data_dir) / "media"
    for t in store.list():
        fname = t.get("filename") or ""
        did = t.get("doc_id") or ""
        if fname == source or (did and f"{did}_{fname}" == source):
            fp = t.get("file_path")
            if fp:
                try:
                    Path(fp).unlink(missing_ok=True)
                except OSError:
                    pass
            if did:
                removed_docs.append(did)
                md = media_root / did
                if md.is_dir():
                    shutil.rmtree(md, ignore_errors=True)
            store.remove(did)

    return {
        "status": "deleted",
        "source": source,
        "deleted_chunks": deleted,
        "cleaned_doc_ids": sorted(set(removed_docs)),
    }
