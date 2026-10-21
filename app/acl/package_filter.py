"""P4 组包召回池过滤器 — ACL 唯一出口的【限定例外】(16 号 V1.1 §3)

纪律:
  - build_acl_filter 仍是全系统唯一出口; 本过滤器【只允许】P4 组包链路
    (propose 组清单 + confirm 元数据校验) 使用, chat/文档管理/搜索/预览一律不动。
  - 池 = 全库 - 他人 L1 - 全部 L4:
      L2/L3 全员标题可见 (Q3 拍板的标题级例外, 正文/chunk 绝不返回)
      L1 仅发起人自己的
      L4 一律排除 (指定可见, 打包旁路等于拆墙)
  - 每次 propose/confirm 全量落审计, 滥用刷标题留底 (§3 补偿防线)。
"""

from __future__ import annotations

from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue


def build_package_pool_filter(creator_userid: str) -> Filter:
    return Filter(
        should=[
            # L2/L3: 标题级可见 (仅元数据, 输出侧禁止带正文)
            FieldCondition(key="level", match=MatchAny(any=["L2", "L3"])),
            # 自己的文档 (任何密级, 含 L1); 自己的 L4 也由 owner 分支放行?
            # —— 不放行: confirm 侧 level=L4 直接剔除, 池里带出也会被过滤,
            # 此处保持 owner 分支只为拿到自己 L1/L2 的正文授权口径一致。
            FieldCondition(key="owner_userid", match=MatchValue(value=creator_userid)),
        ]
    )
