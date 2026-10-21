"""P1 ACL 工具 — 文档侧元数据 (level/dept_id/owner_userid/extra_acl) 的统一构造与校验

字段契约与 acl/filter.py (读侧) 完全一致:
  level         "L1" | "L2" | "L3" | "L4"
  dept_id       list[int]  文档归属部门; 单部门也存数组 —
                Qdrant 对数组 payload 按"元素包含"匹配, 与 MatchAny 兼容
  owner_userid  str        上传者 (钉钉 userid)
  extra_acl     list[str]  L4 指定对象: userid 原样, 部门写 "dept:<id>"

纪律: 写侧只许走 build_doc_acl, 读侧只许走 build_acl_filter,
两侧同口径, 禁止任何模块自拼 payload。
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

LEVELS = ("L1", "L2", "L3", "L4")


class AclError(ValueError):
    """上传侧密级参数错误 → API 层转 400"""


def _to_int_list(raw: Any) -> list[int]:
    if raw is None or raw == "":
        return []
    if isinstance(raw, (str, int)):
        raw = [raw]
    out: list[int] = []
    for item in raw:
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            raise AclError(f"部门 id 必须是数字, 收到: {item!r}")
    # 去重保序
    seen: set[int] = set()
    uniq = []
    for d in out:
        if d not in seen:
            seen.add(d)
            uniq.append(d)
    return uniq


def normalize_extra_acl(userids: Any, dept_ids: Any) -> list[str]:
    """L4 指定对象 → 统一字符串口径 (userid 原样, 部门 "dept:<id>")

    防注入: userid 里出现 "dept:" 前缀拒绝 (否则能伪装部门条目)。
    """
    if userids is None or userids == "":
        userids = []
    if isinstance(userids, str):
        userids = [userids]
    cleaned: list[str] = []
    for u in userids:
        s = str(u).strip()
        if not s:
            continue
        if s.startswith("dept:"):
            raise AclError(f"非法 userid: {s!r}")
        cleaned.append(s)
    for d in _to_int_list(dept_ids):
        tag = f"dept:{d}"
        if tag not in cleaned:
            cleaned.append(tag)
    # 去重保序
    seen: set[str] = set()
    uniq = []
    for c in cleaned:
        if c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq


def build_doc_acl(
    level: str,
    *,
    owner_userid: str,
    dept_ids: Any = None,
    l4_users: Any = None,
    l4_depts: Any = None,
) -> dict[str, Any]:
    """上传入口唯一构造器 — 校验 + 归一化, 返回可直接入 payload 的 dict

    语义 (07 方案 §1.1):
      L1 私密 → 仅 owner 本人, 不需要部门
      L2 部门 → dept_ids 必填 (前端默认勾上传者主部门, 可改可多选)
      L3 公司 → 全员可见, 不需要部门
      L4 指定 → l4_users / l4_depts 至少一个 (选人组件产出, 禁手填)
    """
    level = (level or "").strip().upper()
    if level not in LEVELS:
        raise AclError(f"密级必须是 {list(LEVELS)} 之一, 收到: {level!r}")
    if not owner_userid:
        raise AclError("缺少上传者身份")

    depts = _to_int_list(dept_ids)
    extra = normalize_extra_acl(l4_users, l4_depts)

    if level == "L2" and not depts:
        raise AclError("L2 部门文档必须指定归属部门")
    if level == "L4" and not extra:
        raise AclError("L4 指定文档必须至少选择一个可见的人或部门")

    acl: dict[str, Any] = {
        "level": level,
        "dept_id": depts,
        "owner_userid": str(owner_userid),
        "extra_acl": extra,
    }
    return acl


def acl_applies(identity) -> bool:
    """admin / boss 不受 ACL 过滤 (全量视野, 用于管理界面与老板驾驶舱入口)"""
    return not (getattr(identity, "admin", False) or getattr(identity, "boss", False))


def can_modify(identity, doc_acl: Optional[dict]) -> bool:
    """文档删/改权限: owner 本人, 或 admin/boss"""
    if doc_acl is None:
        return bool(getattr(identity, "admin", False) or getattr(identity, "boss", False))
    userid = getattr(identity, "userid", "")
    if str(doc_acl.get("owner_userid", "")) == str(userid):
        return True
    return bool(getattr(identity, "admin", False) or getattr(identity, "boss", False))
