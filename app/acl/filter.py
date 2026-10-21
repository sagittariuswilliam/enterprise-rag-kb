"""ACL 过滤构造 — 全系统唯一出口 (07 方案 §3.4)

所有召回路径 (问答/打包/预览/引用回显) 必须经过 build_acl_filter,
禁止任何模块绕开自拼查询。

密级语义 (07 方案 §1.1, chunk 继承文档级 ACL):
  L1 私密    -> 仅 owner_userid 本人
  L2 部门    -> dept_id ∈ 我的任一部门 (any 语义, 一人多部门)
  L3 公司    -> 全员可见
  L4 指定    -> extra_acl 命中我的 userid 或任一 dept_id

kb_chunks payload 字段: level / dept_id (int) / owner_userid (str) /
extra_acl (str 数组: userid 原样, 部门写成 "dept:<id>" — 统一字符串口径,
因为 Qdrant MatchAny 不接受 int/str 混装)
"""

from __future__ import annotations

from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue

from app.acl.identity import Identity


def build_acl_filter(user: Identity) -> Filter:
    """返回 Qdrant Filter — 无权限文档连标题都不会被召回."""
    extra_targets = [user.userid, *[f"dept:{d}" for d in user.dept_ids]]
    return Filter(
        should=[
            # L3 全员
            FieldCondition(key="level", match=MatchAny(any=["L3"])),
            # 自己的文档 (任何密级, 含 L1/L2/L4)
            FieldCondition(key="owner_userid", match=MatchValue(value=user.userid)),
            # L2: 部门命中 (payload dept_id 存 int)
            Filter(
                must=[
                    FieldCondition(key="level", match=MatchValue(value="L2")),
                    FieldCondition(key="dept_id", match=MatchAny(any=list(user.dept_ids))),
                ]
            ),
            # L4: 显式指定 (userid 或 "dept:<id>")
            Filter(
                must=[
                    FieldCondition(key="level", match=MatchValue(value="L4")),
                    FieldCondition(key="extra_acl", match=MatchAny(any=extra_targets)),
                ]
            ),
        ]
    )
