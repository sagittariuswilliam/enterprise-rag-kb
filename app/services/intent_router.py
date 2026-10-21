"""M3 意图路由 — 问答流里检测"要文件/找资料"意图 (16 号 V1.1 §5)

口径:
  - 普通问题不打扰: 先用关键词粗筛 (零成本), 粗筛命中才调一次 LLM 精判。
  - 只判断, 永不自动组包: 输出只驱动前端一张引导卡, 提交动作必须人发起。
  - LLM 失败/超时 → False (静默降级, 不影响问答链路)。
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# 粗筛词表 — 招投标/找资料场景高频词 (宁滥勿缺, 精判交给 LLM)
_TRIGGER_WORDS = (
    "投标", "招标", "标书", "资质", "业绩", "报价", "材料", "资料",
    "模板", "合同", "方案", "证明", "证书", "报告", "手册", "制度",
    "帮我找", "有没有", "打包", "全套", "清单", "文件",
)
_MIN_LEN = 6


def keyword_prehit(question: str) -> bool:
    q = (question or "").strip()
    if len(q) < _MIN_LEN:
        return False
    return any(w in q for w in _TRIGGER_WORDS)


_CLASSIFY_PROMPT = (
    "判断下面这条员工提问是否在寻找/索要成体系的文件资料"
    "（如投标材料、资质业绩证明、制度文件、报价模板等），"
    "而不是单纯问一个知识点。\n"
    "只输出 yes 或 no，不要其他文字。\n提问：{q}"
)


async def detect_file_need(question: str) -> bool:
    """是否附组包引导卡。粗筛未命中直接 False（省一次 LLM 调用）。"""
    if not keyword_prehit(question):
        return False
    try:
        from app.services.llm import get_llm
        raw = await get_llm().achat(
            _CLASSIFY_PROMPT.format(q=question.strip()[:300]),
            temperature=0.0,
        )
    except Exception as exc:  # noqa: BLE001 — 意图判断绝不能拖死问答
        logger.debug("intent LLM 失败(忽略): %s", exc)
        return False
    ans = (raw or "").strip().lower()
    return bool(re.match(r"^yes", ans)) or ans.startswith("是")
