"""
Alibaba Cloud Model Studio (DashScope) Production HTTP Client & Model Adapter
=============================================================================
Implements Section 6.4 of OmniLore PRD v2.3:
  - Supports live HTTP calls to DashScope OpenAI-compatible endpoints:
      * Chat/Vision: `qwen3.8-flash` (default) / `qwen3.8-max` (escalated)
      * Embeddings:  `text-embedding-v4` (dimension = 1024)
      * Reranker:    `gte-rerank-v2` (optional, disabled by default in Phase 1)
  - Exponential backoff retry on rate-limit (429) and transient 5xx errors
  - Outbound privacy policy check per (space_id, category)
  - Strict JSON schema & privilege-escalation field guard
  - Desensitized telemetry logging (`model_runs`) with token cost estimation
  - Automatic deterministic local fallback when `DASHSCOPE_API_KEY` is not set
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import time
from typing import Any, Dict, Optional, Tuple
import urllib.error
import urllib.request


# Pricing table per 1,000 tokens (USD equivalent for telemetry estimation)
MODEL_PRICING_PER_1K_TOKENS: Dict[str, Tuple[float, float]] = {
    "qwen3.8-flash": (0.00015, 0.00060),
    "qwen3.8-max": (0.00240, 0.00960),
    "text-embedding-v4": (0.00007, 0.0),
    "gte-rerank-v2": (0.00010, 0.0),
}

# -----------------------------------------------------------------------------
# Environment variables consumed by `ModelAdapterConfig`（R-04b）
# -----------------------------------------------------------------------------
# 这 4 个变量在 v0.3.0-alpha 里曾是「预留但代码不读」。R-04b 把它们真正接上了 ——
# 现在改它们**会生效**，`.env.example` 里那段「预留、未读取」的说明也已删除。
ENV_PRIMARY_MODEL: str = "OMNILORE_PRIMARY_MODEL"
ENV_ESCALATED_MODEL: str = "OMNILORE_ESCALATED_MODEL"
ENV_EMBEDDING_MODEL: str = "OMNILORE_EMBEDDING_MODEL"
ENV_EMBEDDING_DIM: str = "OMNILORE_EMBEDDING_DIM"
ENV_EMBEDDING_PROVIDER: str = "OMNILORE_EMBEDDING_PROVIDER"
ENV_EMBEDDING_BASE_URL: str = "OMNILORE_EMBEDDING_BASE_URL"

MODEL_ADAPTER_ENV_NAMES: Tuple[str, ...] = (
    ENV_PRIMARY_MODEL,
    ENV_ESCALATED_MODEL,
    ENV_EMBEDDING_MODEL,
    ENV_EMBEDDING_DIM,
    ENV_EMBEDDING_PROVIDER,
    ENV_EMBEDDING_BASE_URL,
)


class ModelConfigError(ValueError):
    """模型相关环境变量的取值非法（例如 `OMNILORE_EMBEDDING_DIM=abc`）。

    继承 `ValueError` 而不是 `OmniLoreError`，是为了不让 `dashscope_client` 反过来依赖
    `engine`（会成环）。CLI 与 MCP 入口都显式捕获它，按「配置错误」而非「未预期错误」上报。
    """


def _env_text(name: str, default: str) -> str:
    """读取环境变量；未设置或全空白时返回默认值。"""
    value = os.environ.get(name, "").strip()
    return value if value else default


@dataclass
class ModelAdapterConfig:
    provider: str = "aliyun_model_studio"
    region: str = "cn-beijing"
    api_endpoint: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    rerank_endpoint: str = "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"
    primary_chat_model: str = "qwen3.8-flash"
    escalated_chat_model: str = "qwen3.8-max"
    embedding_model: str = "text-embedding-v4"
    reranker_model: str = "gte-rerank-v2"
    embedding_dimension: int = 1024
    embedding_version: str = "v4-1024"
    index_version: int = 1
    #: 向量来源：`dashscope`（默认）/ `openai`（任意 OpenAI 兼容端点，含 Ollama）/ `none`。
    embedding_provider: str = "dashscope"
    #: `openai` provider 的 base_url；空串表示用该 provider 的内置默认值。
    embedding_base_url: str = ""
    #: 维度是否由用户**显式**配置（决定要不要向端点发送 `dimensions` 字段）。
    embedding_dim_explicit: bool = False
    # NOTE: `prompt_version` / `output_schema_version` identify the *prompt and output
    # schema generation* used by the internal v2.3 code line. They are deliberately
    # NOT the released package version (which is `0.3.0a0` / `v0.3.0-alpha`), because
    # bumping them invalidates cached model outputs. See docs/DECISIONS.md D2.
    prompt_version: str = "v2.3.0"
    output_schema_version: str = "v2.3.0"
    request_timeout_sec: float = 15.0
    max_retries: int = 2
    max_output_tokens: int = 2048
    enable_reranker: bool = False
    api_key_env_var: str = "DASHSCOPE_API_KEY"

    def __post_init__(self) -> None:
        """把环境变量覆盖到字段上（R-04b）。

        为什么在 `__post_init__` 里做：`ModelAdapterConfig()` 在 CLI / MCP / 测试里
        有十几处构造点，逐个传参必然漏；集中在一处才能保证「配了就生效」。
        未设置任何相关变量时所有字段保持数据类默认值 —— **默认行为零变化**。

        Raises:
            ModelConfigError: `OMNILORE_EMBEDDING_DIM` 不是正整数。
        """
        self.primary_chat_model = _env_text(ENV_PRIMARY_MODEL, self.primary_chat_model)
        self.escalated_chat_model = _env_text(ENV_ESCALATED_MODEL, self.escalated_chat_model)
        self.embedding_model = _env_text(ENV_EMBEDDING_MODEL, self.embedding_model)
        self.embedding_provider = _env_text(ENV_EMBEDDING_PROVIDER, self.embedding_provider).lower()
        self.embedding_base_url = _env_text(ENV_EMBEDDING_BASE_URL, self.embedding_base_url)

        raw_dim = os.environ.get(ENV_EMBEDDING_DIM, "").strip()
        if raw_dim:
            try:
                dimension = int(raw_dim)
            except ValueError as exc:
                raise ModelConfigError(
                    f"{ENV_EMBEDDING_DIM}={raw_dim!r} 不是整数；请填一个正整数（例如 1024 / 1536 / 768）"
                ) from exc
            if dimension <= 0:
                raise ModelConfigError(f"{ENV_EMBEDDING_DIM}={dimension} 必须大于 0")
            self.embedding_dimension = dimension
            self.embedding_dim_explicit = True

    def get_api_key(self) -> Optional[str]:
        val = os.environ.get(self.api_key_env_var, "").strip()
        return val if val else None


class DashScopeHTTPTransport:
    """
    Zero-external-dependency HTTP transport for OpenAI-compatible JSON endpoints.

    Started life as the DashScope-only client; since R-04b it also carries the
    OpenAI / Azure / Ollama / vLLM / LM Studio calls, because all of them speak the
    same `POST {base_url}/...  {"model": ...}` shape. Keeping **one** transport class
    is what makes the existing tests' `DashScopeHTTPTransport.post_json` monkeypatch
    keep working for every provider (no provider can quietly go around it).

    Features: exponential backoff retry, timeout enforcement, and the ability to send
    **no** `Authorization` header at all (`api_key=""`), which is how keyless local
    endpoints (Ollama / LM Studio) are addressed.
    """

    def __init__(self, config: ModelAdapterConfig):
        self.config = config

    def post_json(self, url: str, body: Dict[str, Any], api_key: str) -> Dict[str, Any]:
        payload_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": f"OmniLore/{self.config.prompt_version}",
        }
        # 空 key = 显式「这个端点不需要鉴权」（本地 Ollama / LM Studio）。
        # 发一个 `Bearer ` 出去会被部分代理当成 malformed token 直接 401。
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        last_err: Optional[Exception] = None
        for attempt in range(self.config.max_retries + 1):
            req = urllib.request.Request(url, data=payload_bytes, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.config.request_timeout_sec) as resp:
                    raw = resp.read().decode("utf-8")
                    return json.loads(raw)
            except urllib.error.HTTPError as exc:
                last_err = exc
                if exc.code in (429, 500, 502, 503, 504) and attempt < self.config.max_retries:
                    time.sleep(0.2 * (2 ** attempt))
                    continue
                break
            except Exception as exc:
                last_err = exc
                if attempt < self.config.max_retries:
                    time.sleep(0.2 * (2 ** attempt))
                    continue
                break
        raise RuntimeError(f"DashScope API call failed after retries: {type(last_err).__name__}")


def estimate_cost_usd(model_id: str, input_tokens: int, output_tokens: int) -> float:
    in_rate, out_rate = MODEL_PRICING_PER_1K_TOKENS.get(model_id, (0.0002, 0.0006))
    return round((input_tokens / 1000.0) * in_rate + (output_tokens / 1000.0) * out_rate, 6)
