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

    def get_api_key(self) -> Optional[str]:
        val = os.environ.get(self.api_key_env_var, "").strip()
        return val if val else None


class DashScopeHTTPTransport:
    """
    Zero-external-dependency HTTP transport for Alibaba Cloud Model Studio APIs
    with exponential backoff retry, timeout enforcement, and secret redaction.
    """

    def __init__(self, config: ModelAdapterConfig):
        self.config = config

    def post_json(self, url: str, body: Dict[str, Any], api_key: str) -> Dict[str, Any]:
        payload_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": f"OmniLore/{self.config.prompt_version}",
        }
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
