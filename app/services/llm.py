"""LLM 调用 - OpenAI Chat Completions 兼容服务

兼容 OpenAI Chat Completions 协议（POST {base_url}/chat/completions）。

用法::

    from app.services.llm import LLMClient, get_llm, build_rag_prompt

    client = get_llm()
    messages = build_rag_prompt("什么是敏捷开发？", ["敏捷是一种...", "Scrum 是..."])
    answer = client.chat(messages, temperature=0.3, max_tokens=1024)

流式::

    for chunk in client.chat_stream(messages):
        print(chunk, end="", flush=True)
"""
from __future__ import annotations

import json
from typing import AsyncGenerator, Dict, Generator, List, Optional

import httpx

from app.config import settings
from app.core.logger import get_logger

logger = get_logger(__name__)


# ============================================================
# RAG Prompt 模板
# ============================================================

RAG_SYSTEM_PROMPT = """你是项目管理领域的智能客服助手。请严格基于用户提供的【参考资料】回答问题。

要求：
1. 回答必须基于参考资料，不能编造。
2. 如果参考资料里没有答案，直接说"根据已有资料无法回答这个问题"。
3. 回答末尾用 [1][2][3] 标注引用来源（对应参考资料的编号）。
4. 回答简洁，3-5 段话最佳。
5. 如果参考资料是若干份具体合同的零散条款（而不是针对该主题的专门制度/规则文档），严禁把它们归纳包装成"公司标准流程"或通用规定；应改为分别陈述"以下合同各自约定了……"，并提醒用户具体以相关合同条款为准。

{confidence_note}
参考资料：
{context}
"""

# 低置信降级阈值：top rerank 分数低于此值时注入免责指令
# （实证：权威文档命中通常 >0.9，零散合同条款兜底最高只有 ~0.78）
LOW_CONFIDENCE_THRESHOLD = 0.85

_LOW_CONFIDENCE_NOTE = (
    '【重要】本次检索没有命中直接回答该主题的权威制度文档，'
    '以下资料只是相关度较低的零散片段。回答必须以下面这句话开头，不得省略：'
    '"知识库中未找到该主题的专门制度文档，以下内容摘自若干相关资料，可能不适用于全部情况，请以公司正式制度或具体合同条款为准。"'
    '随后再基于资料谨慎归纳，不要给出确定性的流程结论。'
)


def _build_confidence_note(top_score: Optional[float]) -> str:
    """根据最高 rerank 分数决定是否注入低置信免责指令。"""
    if top_score is not None and top_score < LOW_CONFIDENCE_THRESHOLD:
        return _LOW_CONFIDENCE_NOTE
    return ""


def build_rag_prompt(
    question: str,
    context_chunks: List[str],
    system_prompt: str = RAG_SYSTEM_PROMPT,
    top_score: Optional[float] = None,
) -> List[Dict[str, str]]:
    """构造 RAG 对话 messages

    Parameters
    ----------
    question : str
        用户问题
    context_chunks : list[str]
        检索到的参考资料片段，按相关性顺序排列
    system_prompt : str
        系统提示模板，必须含 ``{context}`` 占位符
    top_score : float | None
        检索命中的最高 rerank 分数；低于阈值时自动注入低置信免责指令

    Returns
    -------
    list[dict]
        OpenAI 风格的 messages 列表（system + user）
    """
    context_str = ""
    for i, chunk in enumerate(context_chunks, 1):
        context_str += f"\n[{i}]\n{chunk}\n"

    return [
        {
            "role": "system",
            "content": system_prompt.format(
                context=context_str,
                confidence_note=_build_confidence_note(top_score),
            ),
        },
        {"role": "user", "content": question},
    ]


# ============================================================
# LLMClient
# ============================================================


class LLMClient:
    """OpenAI Chat Completions 兼容客户端

    - 同步 ``chat`` / 流式 ``chat_stream`` / 异步 ``achat``
    - 默认 timeout 60s，可在构造时覆盖
    """

    DEFAULT_TIMEOUT = 60.0

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.api_key = api_key or settings.llm_api_key
        self.base_url = (base_url or settings.llm_base_url).rstrip("/")
        self.model = model or settings.llm_model
        self.timeout = timeout

        if not self.api_key:
            logger.warning("LLM_API_KEY 未配置，调用 LLM 将失败")

    # --------------------------------------------------------
    # 内部工具
    # --------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _payload(
        self,
        messages: List[Dict[str, str]],
        temperature: float,
        max_tokens: int,
        stream: bool,
    ) -> Dict:
        return {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": stream,
            # 2026-10-16 修复回答腰斩: qwen3.8-flash 默认开思考模式,
            # reasoning token 计入 max_tokens 预算, 思考一长正文就被截断
            # (实测 usage: completion 482 中 382 是 reasoning_tokens)。
            # RAG 问答不需要深思考, 关掉: 截断消失 + 响应更快 + token 省 70%+。
            "enable_thinking": False,
        }

    # --------------------------------------------------------
    # 同步调用
    # --------------------------------------------------------

    def chat(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.3,
        max_tokens: int = 2048,
    ) -> str:
        """单次 chat 调用，返回完整回复文本

        Parameters
        ----------
        messages : list[dict]
            OpenAI 风格 messages（role: system/user/assistant）
        temperature : float
            采样温度，默认 0.3（RAG 场景偏低以保证稳定）
        max_tokens : int
            最大输出 token 数
        """
        url = f"{self.base_url}/chat/completions"
        payload = self._payload(messages, temperature, max_tokens, stream=False)

        logger.debug("LLM chat → %s model=%s", url, self.model)

        with httpx.Client(timeout=self.timeout) as client:
            r = client.post(url, json=payload, headers=self._headers())
            r.raise_for_status()
            data = r.json()

        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            logger.error("LLM 响应解析失败: %s | payload=%s", exc, data)
            raise RuntimeError(f"LLM 返回格式异常: {data}") from exc

    def chat_stream(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.3,
        max_tokens: int = 2048,
    ) -> Generator[str, None, None]:
        """流式 chat 调用，逐 chunk yield content 片段"""
        url = f"{self.base_url}/chat/completions"
        payload = self._payload(messages, temperature, max_tokens, stream=True)

        logger.debug("LLM chat_stream → %s model=%s", url, self.model)

        with httpx.Client(timeout=self.timeout) as client:
            with client.stream(
                "POST", url, json=payload, headers=self._headers()
            ) as r:
                r.raise_for_status()
                for line in r.iter_lines():
                    if not line:
                        continue
                    # SSE: "data: {...}"，以 "data: " 开头
                    if line.startswith("data:"):
                        line = line[len("data:"):].strip()
                    if not line or line == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(line)
                        delta = chunk["choices"][0].get("delta", {})
                        content = delta.get("content")
                        if content:
                            yield content
                    except (json.JSONDecodeError, KeyError, IndexError):
                        # 非 JSON 行（如 keep-alive）静默跳过
                        continue

    # --------------------------------------------------------
    # 异步调用（供 FastAPI 路由用）
    # --------------------------------------------------------

    async def achat(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.3,
        max_tokens: int = 2048,
    ) -> str:
        """异步版 chat"""
        url = f"{self.base_url}/chat/completions"
        payload = self._payload(messages, temperature, max_tokens, stream=False)

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(url, json=payload, headers=self._headers())
            r.raise_for_status()
            data = r.json()

        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            logger.error("LLM 响应解析失败: %s | payload=%s", exc, data)
            raise RuntimeError(f"LLM 返回格式异常: {data}") from exc

    async def achat_stream(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.3,
        max_tokens: int = 2048,
    ) -> AsyncGenerator[str, None]:
        """异步流式 chat"""
        url = f"{self.base_url}/chat/completions"
        payload = self._payload(messages, temperature, max_tokens, stream=True)

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            async with client.stream(
                "POST", url, json=payload, headers=self._headers()
            ) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    if not line:
                        continue
                    if line.startswith("data:"):
                        line = line[len("data:"):].strip()
                    if not line or line == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(line)
                        delta = chunk["choices"][0].get("delta", {})
                        content = delta.get("content")
                        if content:
                            yield content
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue


# ============================================================
# 单例（懒加载）
# ============================================================

_client: Optional[LLMClient] = None


def get_llm() -> LLMClient:
    """获取全局 LLMClient 单例"""
    global _client
    if _client is None:
        _client = LLMClient()
    return _client
