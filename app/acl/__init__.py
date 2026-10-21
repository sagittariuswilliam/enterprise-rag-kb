"""ACL 包 — 读写两侧唯一出口

- filter.py   读侧: build_acl_filter(identity) -> Qdrant Filter
- doc_acl.py  写侧: build_doc_acl(...) -> payload dict (与读侧同口径)
"""

from app.acl.doc_acl import AclError, LEVELS, acl_applies, build_doc_acl, can_modify, normalize_extra_acl

__all__ = [
    "AclError", "LEVELS", "acl_applies", "build_doc_acl",
    "can_modify", "normalize_extra_acl",
]
