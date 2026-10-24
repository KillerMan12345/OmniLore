"""
EmbeddingProvider 抽象 + DashScope / OpenAI 兼容（含 Ollama）实现（R-04b）
==========================================================================
为什么要有这层抽象
------------------
在这之前 embedding **硬绑阿里云百炼（DashScope）**：想用向量检索就必须注册阿里云、
开通百炼、创建 API Key、可能要充值。对自托管用户这是**软锁定** —— 他们手上大多是
OpenAI key、本地 Ollama，或者干脆一个 key 都没有。

本模块把「向量从哪来」变成可替换的一件事：

```
ModelAdapter
   └── EmbeddingProvider            <- 接口（embed / dimension / is_available / unavailable_reason）
         ├── DashScopeEmbeddingProvider          阿里云百炼（默认，行为与 v0.3.0 完全一致）
         ├── OpenAICompatibleEmbeddingProvider   OpenAI / Azure / Ollama / vLLM / LM Studio / 硅基流动 …
         ├── DisabledEmbeddingProvider           OMNILORE_EMBEDDING_PROVIDER=none（显式只用 BM25）
         └── LocalDeterministicEmbeddingProvider 零密钥降级路径（确定性哈希向量，非语义向量）
```

三条不可退化的约定
------------------
1. **默认行为零变化**：不设任何新环境变量时，仍是「DashScope + 无 key 降级 BM25」，
   连 `degraded_reason` 的中文文案都保持不变（README/测试都引用了它）。
2. **降级链不变**：provider 未配置 / 401 / 超时 / 网络错误 → 该次请求降级为
   `keyword_only`，`degraded_reason` 如实上报，检索结果仍然有内容（BM25 兜底）。
3. **维度不一致必须响亮失败**：配置的 `OMNILORE_EMBEDDING_DIM` 与 provider 实际返回的
   维度不符时**抛错**，绝不截断/补零 —— 那会静默产出**错误的相似度**，
   而错误的相似度比没有向量检索更危险（搜索结果看起来正常，但排序是垃圾）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
import hashlib
import math
import os
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from .dashscope_client import DashScopeHTTPTransport, ModelAdapterConfig

#: provider 选择开关。默认 `dashscope` —— 与抽象之前的唯一行为完全一致。
EMBEDDING_PROVIDER_ENV: str = "OMNILORE_EMBEDDING_PROVIDER"
#: OpenAI 兼容端点的 base_url（Ollama 用 `http://localhost:11434/v1`）。
EMBEDDING_BASE_URL_ENV: str = "OMNILORE_EMBEDDING_BASE_URL"
#: 显式 API Key（优先于 `OPENAI_API_KEY`；本地端点不需要）。
EMBEDDING_API_KEY_ENV: str = "OMNILORE_EMBEDDING_API_KEY"

PROVIDER_DASHSCOPE: str = "dashscope"
PROVIDER_OPENAI: str = "openai"
PROVIDER_NONE: str = "none"
SUPPORTED_PROVIDERS: tuple = (PROVIDER_DASHSCOPE, PROVIDER_OPENAI, PROVIDER_NONE)

#: OpenAI 官方默认端点（Azure / Ollama / vLLM / LM Studio 请显式配 base_url）。
DEFAULT_OPENAI_BASE_URL: str = "https://api.openai.com/v1"

#: 常见本地端点主机名 —— 命中即视为「不需要 API Key 的自托管端点」。
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal"})


class EmbeddingProviderError(RuntimeError):
    """embedding 相关错误的基类。"""


class EmbeddingResponseShapeError(EmbeddingProviderError, ValueError):
    """provider 返回了响应、但结构不符合 OpenAI 形状。

    同时继承 `ValueError`，是为了让它落进 `engine._RESPONSE_SHAPE_ERRORS` ——
    也就是说：「**我们的解析假设**错了」而不是「供应商挂了」。这条归因区分是 QA P2-B
    的修复成果，抽象化之后必须保留（否则内部 bug 又会被伪装成供应商故障）。
    """


class EmbeddingConfigurationError(EmbeddingProviderError):
    """**配置错误**：必须响亮失败，不允许被降级链吞掉。

    典型场景：`OMNILORE_EMBEDDING_DIM=1024` 但 provider 实际返回 1536 维。
    静默截断会产出错误相似度；静默降级会让用户永远不知道自己的配置没生效。
    """


def is_loopback_url(url: str) -> bool:
    """判断 URL 是否指向本机/本地端点（用于「本地端点不要求 API Key」）。"""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return host in _LOOPBACK_HOSTS


class EmbeddingProvider(ABC):
    """向量来源的统一接口。

    Attributes:
        name: provider 标识（`dashscope` / `openai` / `none` / `local`）。
    """

    name: str = "abstract"

    @property
    @abstractmethod
    def model_id(self) -> str:
        """当前使用的 embedding 模型名（用于遥测与 bundle/日志）。"""

    @property
    @abstractmethod
    def dimension(self) -> int:
        """期望的向量维度（配置值）。实际返回值必须与之相等，否则抛错。"""

    @abstractmethod
    def is_available(self) -> bool:
        """当前进程是否具备调用条件（有 key / 本地端点 / 显式启用）。"""

    @abstractmethod
    def unavailable_reason(self) -> str:
        """不可用时的中文说明（写入 `degraded_reason`，必须能指导用户下一步）。"""

    @abstractmethod
    def embed(self, texts: List[str]) -> List[List[float]]:
        """把文本批量转成向量（顺序与 `texts` 一一对应）。"""


class LocalDeterministicEmbeddingProvider(EmbeddingProvider):
    """零密钥降级路径：确定性哈希向量（词串 + CJK 二元组），**不是**语义向量。

    这不是「假的 provider」，而是刻意保留的降级实现：它让无 key 的安装仍能做
    「字面相近」的召回，并且完全可复现（同输入 → 同向量），便于测试与对账。
    真正的语义召回需要上面的远程 provider。
    """

    name = "local"

    def __init__(self, dimension: int):
        self._dimension = max(1, int(dimension))

    @property
    def model_id(self) -> str:
        return "local-deterministic-hash"

    @property
    def dimension(self) -> int:
        return self._dimension

    def is_available(self) -> bool:
        """永远可用 —— 它是最后一道兜底，不是可选 provider。"""
        return True

    def unavailable_reason(self) -> str:
        return ""

    def embed(self, texts: List[str]) -> List[List[float]]:
        dim = self._dimension
        vectors: List[List[float]] = []
        for text in texts:
            vec = [0.0] * dim
            tokens = re.findall(r"[\w\u4e00-\u9fa5]+", text.lower())
            if not tokens:
                vec[0] = 1.0
                vectors.append(vec)
                continue
            for token in tokens:
                subtokens = [token]
                if any("\u4e00" <= ch <= "\u9fa5" for ch in token) and len(token) > 1:
                    subtokens.extend(token[i : i + 2] for i in range(len(token) - 1))
                for sub in subtokens:
                    hashed = int(hashlib.md5(sub.encode("utf-8")).hexdigest(), 16)
                    idx = hashed % dim
                    sign = 1.0 if ((hashed >> 16) & 1) == 0 else -1.0
                    vec[idx] += sign
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            vectors.append([round(v / norm, 6) for v in vec])
        return vectors


class _RemoteEmbeddingProvider(EmbeddingProvider):
    """远程 provider 的共同部分：解析 OpenAI 形状的 `{"data": [{"embedding": [...]}]}`。"""

    def __init__(self, config: ModelAdapterConfig, http: DashScopeHTTPTransport):
        self._config = config
        self._http = http

    @property
    def dimension(self) -> int:
        return int(self._config.embedding_dimension)

    def _post_embeddings(self, url: str, body: Dict[str, Any], api_key: Optional[str]) -> List[List[float]]:
        """POST 到 `/embeddings` 并按 `index` 还原顺序。

        Raises:
            EmbeddingProviderError: 响应结构不符合 OpenAI 形状（我们的解析假设错了）。
        """
        response = self._http.post_json(url, body, api_key or "")
        try:
            data = response["data"]
            ordered = sorted(data, key=lambda item: item.get("index", 0))
            return [list(map(float, item["embedding"])) for item in ordered]
        except (KeyError, TypeError, ValueError) as exc:
            raise EmbeddingResponseShapeError(
                f"{self.name} 返回的 /embeddings 响应不符合 OpenAI 形状（{type(exc).__name__}）"
            ) from exc

    def embed(self, texts: List[str]) -> List[List[float]]:
        raise NotImplementedError


class DashScopeEmbeddingProvider(_RemoteEmbeddingProvider):
    """阿里云百炼（DashScope）OpenAI 兼容模式 —— 抽象之前的**唯一**行为。

    请求体与之前逐字节一致（含 `dimensions` 字段），这样升级到 R-04b 不会改变
    任何既有用户的检索结果。
    """

    name = PROVIDER_DASHSCOPE

    @property
    def model_id(self) -> str:
        return self._config.embedding_model

    def is_available(self) -> bool:
        return self._config.get_api_key() is not None

    def unavailable_reason(self) -> str:
        # 文案必须与抽象之前**逐字一致**：README 与测试都引用了这句话。
        return (
            f"未配置 {self._config.api_key_env_var}，向量召回不可用，"
            "已自动降级为本地 BM25 / 关键词（keyword_only）检索"
        )

    def embed(self, texts: List[str]) -> List[List[float]]:
        url = f"{self._config.api_endpoint.rstrip('/')}/embeddings"
        return self._post_embeddings(
            url,
            {
                "model": self._config.embedding_model,
                "input": texts,
                "dimensions": self._config.embedding_dimension,
            },
            self._config.get_api_key(),
        )


class OpenAICompatibleEmbeddingProvider(_RemoteEmbeddingProvider):
    """任何 OpenAI 兼容的 `/embeddings` 端点。

    覆盖面：OpenAI、Azure OpenAI、**Ollama**（`http://localhost:11434/v1`）、
    vLLM、LM Studio、硅基流动等。只要它接受
    `POST {base_url}/embeddings {"model": ..., "input": [...]}` 就行。

    API Key 解析顺序：`OMNILORE_EMBEDDING_API_KEY` → `OPENAI_API_KEY`；
    两者都没有但 `base_url` 指向本机时视为**无需鉴权**（Ollama / LM Studio 的常见用法）。
    """

    name = PROVIDER_OPENAI

    @property
    def model_id(self) -> str:
        return self._config.embedding_model

    @property
    def base_url(self) -> str:
        return (self._config.embedding_base_url or DEFAULT_OPENAI_BASE_URL).rstrip("/")

    def api_key(self) -> Optional[str]:
        """返回可用的 API Key（含本地端点放行规则）。"""
        explicit = os.environ.get(EMBEDDING_API_KEY_ENV, "").strip()
        if explicit:
            return explicit
        openai_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if openai_key:
            return openai_key
        return None

    def is_available(self) -> bool:
        if self.api_key() is not None:
            return True
        # 本地端点（Ollama / LM Studio）默认不校验鉴权：不因为「没 key」就降级。
        return is_loopback_url(self.base_url)

    def unavailable_reason(self) -> str:
        return (
            f"未配置 {EMBEDDING_API_KEY_ENV} / OPENAI_API_KEY，且 "
            f"{EMBEDDING_BASE_URL_ENV}={self.base_url} 不是本地地址，向量召回不可用，"
            "已自动降级为本地 BM25 / 关键词（keyword_only）检索"
        )

    def embed(self, texts: List[str]) -> List[List[float]]:
        body: Dict[str, Any] = {"model": self._config.embedding_model, "input": texts}
        # 只在用户**显式**配了维度时才发送 `dimensions`：部分自托管端点
        # （老版本 Ollama / 某些代理）不认识这个字段，默认不带更稳妥。
        if self._config.embedding_dim_explicit:
            body["dimensions"] = self._config.embedding_dimension
        return self._post_embeddings(f"{self.base_url}/embeddings", body, self.api_key())


class DisabledEmbeddingProvider(EmbeddingProvider):
    """`OMNILORE_EMBEDDING_PROVIDER=none`：显式只用 BM25。

    给「明确不想要向量」的用户一个诚实开关 —— 比让他们把 key 删掉更清楚，
    而且 `degraded_reason` 会说明这是**配置选择**而不是故障。
    """

    name = PROVIDER_NONE

    def __init__(self, config: ModelAdapterConfig):
        self._config = config

    @property
    def model_id(self) -> str:
        return "disabled"

    @property
    def dimension(self) -> int:
        return int(self._config.embedding_dimension)

    def is_available(self) -> bool:
        return False

    def unavailable_reason(self) -> str:
        return (
            f"{EMBEDDING_PROVIDER_ENV}=none：已按配置停用向量召回，"
            "使用本地 BM25 / 关键词（keyword_only）检索"
        )

    def embed(self, texts: List[str]) -> List[List[float]]:
        raise EmbeddingConfigurationError(
            f"{EMBEDDING_PROVIDER_ENV}=none 时不应调用 embed()：这是配置错误，不是运行时故障"
        )


def build_embedding_provider(
    config: ModelAdapterConfig,
    http: DashScopeHTTPTransport,
) -> EmbeddingProvider:
    """按配置构造 provider。

    Args:
        config: 模型配置（provider 字段已由环境变量解析）。
        http: 共享的 HTTP transport（DashScope 与 OpenAI 兼容端点都用它，
            测试里 monkeypatch `DashScopeHTTPTransport.post_json` 依然生效）。

    Returns:
        对应的 :class:`EmbeddingProvider` 实例。

    Raises:
        EmbeddingConfigurationError: `OMNILORE_EMBEDDING_PROVIDER` 取值无法识别。
            变量名/取值写错必须响亮失败 —— 静默回落到默认 provider 会让用户
            以为「我已经切到 Ollama 了」，实际还在打阿里云。
    """
    provider = (config.embedding_provider or PROVIDER_DASHSCOPE).strip().lower()
    if provider == PROVIDER_DASHSCOPE:
        return DashScopeEmbeddingProvider(config, http)
    if provider == PROVIDER_OPENAI:
        return OpenAICompatibleEmbeddingProvider(config, http)
    if provider == PROVIDER_NONE:
        return DisabledEmbeddingProvider(config)
    raise EmbeddingConfigurationError(
        f"{EMBEDDING_PROVIDER_ENV}={config.embedding_provider!r} 无法识别；"
        f"可选值：{', '.join(SUPPORTED_PROVIDERS)}"
    )


def validate_embedding_dimensions(
    vectors: List[List[float]],
    expected_dimension: int,
    *,
    provider: str,
    model_id: str,
) -> None:
    """校验 provider 返回的维度与配置一致。

    Args:
        vectors: provider 返回的向量列表。
        expected_dimension: 配置里声明的维度（`OMNILORE_EMBEDDING_DIM`）。
        provider: provider 名（写进错误信息，便于定位）。
        model_id: 模型名（写进错误信息）。

    Raises:
        EmbeddingConfigurationError: 维度不一致或向量为空。**绝不截断/补零**。
    """
    if not vectors:
        raise EmbeddingConfigurationError(
            f"{provider} 返回了 0 条向量（model={model_id}）：无法建立索引"
        )
    actual = len(vectors[0])
    if any(len(vec) != actual for vec in vectors):
        raise EmbeddingConfigurationError(
            f"{provider}（model={model_id}）返回的向量长度不一致："
            f"{sorted({len(v) for v in vectors})}；这会让相似度计算失去意义"
        )
    if actual != expected_dimension:
        raise EmbeddingConfigurationError(
            f"向量维度不一致：配置 OMNILORE_EMBEDDING_DIM={expected_dimension}，"
            f"但 {provider}（model={model_id}）实际返回 {actual} 维。"
            f"请把 OMNILORE_EMBEDDING_DIM 改成 {actual}（或换一个 {expected_dimension} 维的模型）。"
            "**不会**自动截断/补零 —— 那会产生错误的相似度。"
        )


def describe_effective_embedding(config: ModelAdapterConfig) -> str:
    """返回一行「当前真正生效的向量配置」，供启动回显使用。

    为什么需要它：环境变量在容器模式要经 compose 转发、在本地模式要自己 `export`。
    任何一环断了，用户都会看到「我明明配了 Ollama，怎么还在打阿里云」——
    把生效值打出来，这类静默失效就变成**可见**的。

    Args:
        config: 已应用环境变量覆盖的模型配置。

    Returns:
        单行中文描述（无换行）。

    Raises:
        EmbeddingConfigurationError: provider 取值无法识别（由调用方决定怎么呈现）。
    """
    provider = build_embedding_provider(config, DashScopeHTTPTransport(config))
    state = "可用 → hybrid" if provider.is_available() else "不可用 → keyword_only"
    return (
        f"  {EMBEDDING_PROVIDER_ENV} → {provider.name}"
        f"（模型 {provider.model_id}，维度 {provider.dimension}，状态：{state}）"
    )


__all__ = [
    "DEFAULT_OPENAI_BASE_URL",
    "EMBEDDING_API_KEY_ENV",
    "EMBEDDING_BASE_URL_ENV",
    "EMBEDDING_PROVIDER_ENV",
    "DashScopeEmbeddingProvider",
    "DisabledEmbeddingProvider",
    "EmbeddingConfigurationError",
    "EmbeddingProvider",
    "EmbeddingProviderError",
    "EmbeddingResponseShapeError",
    "LocalDeterministicEmbeddingProvider",
    "OpenAICompatibleEmbeddingProvider",
    "PROVIDER_DASHSCOPE",
    "PROVIDER_NONE",
    "PROVIDER_OPENAI",
    "SUPPORTED_PROVIDERS",
    "build_embedding_provider",
    "describe_effective_embedding",
    "is_loopback_url",
    "validate_embedding_dimensions",
]
