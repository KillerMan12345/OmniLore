"""
W6：EmbeddingProvider 抽象（R-04b）与平台一致性（R-11b）回归测试
==================================================================
本文件两组守卫，全部离线（不打真实网络、不连数据库、不执行 docker）：

**R-04b — 向量来源可替换**
  * `TestProviderAbstraction`：默认仍是 DashScope、OpenAI 兼容端点（含 Ollama 风格本地地址）
    能被正确调用、`none` 能显式关闭、provider 名写错会响亮失败。
  * `TestModelEnvWiring`：`OMNILORE_PRIMARY_MODEL` / `OMNILORE_ESCALATED_MODEL` /
    `OMNILORE_EMBEDDING_MODEL` / `OMNILORE_EMBEDDING_DIM` 这 4 个**曾经只是预留**的变量
    现在真的被读取 —— 断言方式不是「读过环境变量」，而是「**出现在真实请求体里**」。
  * `TestDimensionGuard`：配的维度与 provider 返回不一致时**抛错**，绝不截断/补零。
  * `TestDegradationRegression`：provider 不可用时仍降级 BM25（`keyword_only` + 非空原因 + 有结果）。
  * `TestDefaultBehaviourInvariance`：不设任何新变量时，配置默认值与**本地向量的数值**
    逐位不变（用一个钉住的摘要证明），升级不会悄悄改变既有用户的检索结果。

**R-11b — 统一数据目录 + ARM64 声明**
  * `TestUnifiedDataDir`：数据目录只有一个解析入口（`resolve_data_dir`），
    `ops/index`、引导状态、证据包输出全部挂在同一根下。
  * `TestArm64Ci`：CI 有一个**离线** arm64 job（不动 `integration-rls`），README 声明支持架构。

为什么这些也要写成测试：本项目的头号风险是「宣称与证据不匹配」。provider 抽象最容易
滑向「配置项很多、实际没接通」，arm64 最容易滑向「README 说支持、CI 里根本没跑」。
"""

from __future__ import annotations

import ast
import hashlib
import io
import json
import os
from pathlib import Path
import sys
from typing import Any, Dict, List
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from omnilore import OmniLoreEngine, OmniLoreMCPGateway  # noqa: E402
from omnilore.bootstrap import (  # noqa: E402
    bootstrap_state_path,
    ensure_bootstrapped,
    unknown_omnilore_env_names,
)
from omnilore.cli import main as cli_main  # noqa: E402
from omnilore.dashscope_client import (  # noqa: E402
    DashScopeHTTPTransport,
    ModelAdapterConfig,
    ModelConfigError,
)
from omnilore.embedding_provider import (  # noqa: E402
    EMBEDDING_BASE_URL_ENV,
    EMBEDDING_PROVIDER_ENV,
    DashScopeEmbeddingProvider,
    DisabledEmbeddingProvider,
    EmbeddingConfigurationError,
    EmbeddingProvider,
    LocalDeterministicEmbeddingProvider,
    OpenAICompatibleEmbeddingProvider,
    build_embedding_provider,
    is_loopback_url,
    validate_embedding_dimensions,
)
from omnilore.engine import ModelAdapter  # noqa: E402
from omnilore.pathutil import resolve_data_dir  # noqa: E402
from omnilore.runtime_index import INDEX_DIR_REL, index_dir  # noqa: E402

CI_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"
README_PATH = REPO_ROOT / "README.md"

#: 抽象之前 `ModelAdapter.degraded_reason` 在「DashScope 无 key」时给出的**原文**。
#: README 与既有测试都引用它；R-04b 之后必须逐字不变。
LEGACY_DEGRADED_REASON = (
    "未配置 DASHSCOPE_API_KEY，向量召回不可用，已自动降级为本地 BM25 / 关键词（keyword_only）检索"
)

#: 本地确定性向量的钉死摘要：证明 R-04b 的搬移**没有改变数值**。
#: 若这条变了，说明既有用户的检索排序会跟着变 —— 那必须是**有意的**改动。
PINNED_LOCAL_VECTOR_DIGEST = "903cb03701a10b51"

ALL_NEW_ENV_NAMES = (
    "OMNILORE_PRIMARY_MODEL",
    "OMNILORE_ESCALATED_MODEL",
    "OMNILORE_EMBEDDING_MODEL",
    "OMNILORE_EMBEDDING_DIM",
    EMBEDDING_PROVIDER_ENV,
    EMBEDDING_BASE_URL_ENV,
    "OMNILORE_EMBEDDING_API_KEY",
    "OPENAI_API_KEY",
)


def _clean_model_env() -> Dict[str, str]:
    """返回一份「不含任何模型/向量相关变量」的环境字典副本（供 patch.dict 使用）。"""
    return {k: v for k, v in os.environ.items() if k not in ALL_NEW_ENV_NAMES and k != "DASHSCOPE_API_KEY"}


class _RecordingTransport:
    """记录请求体并返回预设响应的 transport 替身（不打网络）。

    刻意继承 `DashScopeHTTPTransport` 的 `post_json` 签名，这样「所有 provider 都走同一个
    transport」这件事在测试里也是真的 —— 换 provider 不会绕过它。
    """

    def __init__(self, dimension: int = 1024, chat_content: str = '{"ok": true}'):
        self.calls: List[Dict[str, Any]] = []
        self._dimension = dimension
        self._chat_content = chat_content

    def post_json(self, url: str, body: Dict[str, Any], api_key: str) -> Dict[str, Any]:
        self.calls.append({"url": url, "body": body, "api_key": api_key})
        if url.endswith("/embeddings"):
            return {
                "data": [
                    {"index": idx, "embedding": [0.5] * self._dimension}
                    for idx, _ in enumerate(body.get("input") or [])
                ]
            }
        return {"choices": [{"message": {"content": self._chat_content}}]}


class _FailingProvider(EmbeddingProvider):
    """「可用但一调就炸」的 provider：模拟 401 / 超时 / 断网，用来验证降级链。"""

    name = "openai"
    model_id = "failing-embedding-model"

    def __init__(self, dimension: int = 1024):
        self._dimension = dimension

    @property
    def dimension(self) -> int:
        return self._dimension

    def is_available(self) -> bool:
        return True

    def unavailable_reason(self) -> str:
        return ""

    def embed(self, texts: List[str]) -> List[List[float]]:
        raise ConnectionError("connection refused (simulated provider outage)")


class TestProviderAbstraction(unittest.TestCase):
    """R-04b：provider 选择、默认不变、写错要响。"""

    def test_default_provider_is_dashscope_with_legacy_degraded_text(self) -> None:
        with mock.patch.dict(os.environ, _clean_model_env(), clear=True):
            config = ModelAdapterConfig()
            adapter = ModelAdapter(config, telemetry_sink=[])
            # 属性是实时计算的：必须在 patch 内读，否则会被宿主机残留的 key 影响
            mode, reason = adapter.retrieval_mode, adapter.degraded_reason
            provider_type = type(adapter.embedding_provider)
        self.assertEqual(config.embedding_provider, "dashscope")
        self.assertIs(provider_type, DashScopeEmbeddingProvider)
        self.assertEqual(mode, "keyword_only")
        self.assertEqual(reason, LEGACY_DEGRADED_REASON)

    def test_openai_compatible_provider_calls_embeddings_endpoint(self) -> None:
        """配了 OpenAI 兼容端点后：请求打到 `/embeddings`，模型/输入来自配置。"""
        env = _clean_model_env()
        env.update({
            EMBEDDING_PROVIDER_ENV: "openai",
            EMBEDDING_BASE_URL_ENV: "http://127.0.0.1:11434/v1",
            "OMNILORE_EMBEDDING_MODEL": "nomic-embed-text",
            "OMNILORE_EMBEDDING_DIM": "768",
        })
        with mock.patch.dict(os.environ, env, clear=True):
            adapter = ModelAdapter(ModelAdapterConfig(), telemetry_sink=[])
            self.assertIsInstance(adapter.embedding_provider, OpenAICompatibleEmbeddingProvider)
            self.assertEqual(adapter.retrieval_mode, "hybrid", "本地端点可用，不应被判为降级")
            self.assertEqual(adapter.degraded_reason, "")

            transport = _RecordingTransport(dimension=768)
            adapter._http = transport  # type: ignore[assignment]
            adapter.embedding_provider._http = transport  # type: ignore[attr-defined]
            vectors = adapter.embed_texts(["分布式锁与租约续期规范"])

        self.assertEqual(len(vectors), 1)
        self.assertEqual(len(vectors[0]), 768)
        self.assertEqual(len(transport.calls), 1)
        call = transport.calls[0]
        self.assertTrue(call["url"].endswith("/embeddings"), call["url"])
        self.assertEqual(call["body"]["model"], "nomic-embed-text")
        self.assertEqual(call["body"]["input"], ["分布式锁与租约续期规范"])
        self.assertEqual(call["body"]["dimensions"], 768, "显式配了维度就应当发给端点")
        self.assertEqual(call["api_key"], "", "本地回环端点不需要鉴权，不应带 Bearer")

    def test_loopback_detection_covers_ollama_and_lm_studio(self) -> None:
        for url in ("http://localhost:11434/v1", "http://127.0.0.1:1234/v1", "http://host.docker.internal:11434/v1"):
            self.assertTrue(is_loopback_url(url), f"{url} 应视为本地端点")
        for url in ("https://api.openai.com/v1", "https://api.siliconflow.cn/v1"):
            self.assertFalse(is_loopback_url(url), f"{url} 不应视为本地端点")

    def test_remote_openai_endpoint_without_key_degrades_with_actionable_reason(self) -> None:
        env = _clean_model_env()
        env.update({EMBEDDING_PROVIDER_ENV: "openai", EMBEDDING_BASE_URL_ENV: "https://api.example.com/v1"})
        with mock.patch.dict(os.environ, env, clear=True):
            adapter = ModelAdapter(ModelAdapterConfig(), telemetry_sink=[])
            mode, reason = adapter.retrieval_mode, adapter.degraded_reason
            self.assertIsNone(adapter.embedding_provider.api_key(), "测试前提：两个 key 变量都不存在")
        self.assertEqual(mode, "keyword_only")
        self.assertIn("OPENAI_API_KEY", reason, "原因必须点名该配哪个变量")
        self.assertIn("OMNILORE_EMBEDDING_API_KEY", reason)

    def test_provider_none_disables_vectors_even_with_dashscope_key(self) -> None:
        env = _clean_model_env()
        env.update({EMBEDDING_PROVIDER_ENV: "none", "DASHSCOPE_API_KEY": "sk-should-be-ignored"})
        with mock.patch.dict(os.environ, env, clear=True):
            adapter = ModelAdapter(ModelAdapterConfig(), telemetry_sink=[])
            provider_type = type(adapter.embedding_provider)
            mode, reason = adapter.retrieval_mode, adapter.degraded_reason
        self.assertIs(provider_type, DisabledEmbeddingProvider)
        self.assertEqual(mode, "keyword_only")
        self.assertIn("none", reason, "要说明这是配置选择，不是故障")

    def test_unknown_provider_name_fails_loudly_with_valid_values(self) -> None:
        """拼错 provider 名必须响亮失败 —— 静默回落会让用户以为「我已经切到 Ollama 了」。"""
        env = _clean_model_env()
        env[EMBEDDING_PROVIDER_ENV] = "dashsope"
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(EmbeddingConfigurationError) as ctx:
                ModelAdapter(ModelAdapterConfig(), telemetry_sink=[])
        message = str(ctx.exception)
        self.assertIn("dashsope", message)
        for valid in ("dashscope", "openai", "none"):
            self.assertIn(valid, message, f"错误信息必须列出合法取值（缺 {valid}）")

    def test_build_provider_returns_the_configured_implementation(self) -> None:
        with mock.patch.dict(os.environ, _clean_model_env(), clear=True):
            config = ModelAdapterConfig()
            http = DashScopeHTTPTransport(config)
            self.assertIsInstance(build_embedding_provider(config, http), DashScopeEmbeddingProvider)
            config.embedding_provider = "openai"
            self.assertIsInstance(build_embedding_provider(config, http), OpenAICompatibleEmbeddingProvider)


class TestModelEnvWiring(unittest.TestCase):
    """R-04b：四个「曾经只是预留」的变量现在真的生效 —— 断言它们出现在**请求体**里。"""

    def test_primary_and_escalated_model_env_reach_the_chat_request(self) -> None:
        env = _clean_model_env()
        env.update({
            "DASHSCOPE_API_KEY": "sk-chat-test",
            "OMNILORE_PRIMARY_MODEL": "my-primary-chat",
            "OMNILORE_ESCALATED_MODEL": "my-escalated-chat",
        })
        with mock.patch.dict(os.environ, env, clear=True):
            adapter = ModelAdapter(ModelAdapterConfig(), telemetry_sink=[])
            transport = _RecordingTransport(chat_content='{"answer": "ok"}')
            adapter._http = transport  # type: ignore[assignment]
            space = self._space()

            adapter.generate_structured(
                task_type="SYNTHESIZE_ANSWER",
                space=space,
                category="01_Work",
                payload={"chunks": []},
                required_schema={"answer": str},
            )
            adapter.generate_structured(
                task_type="SYNTHESIZE_ANSWER",
                space=space,
                category="01_Work",
                payload={"chunks": []},
                required_schema={"answer": str},
                escalate=True,
            )

        models = [call["body"]["model"] for call in transport.calls if call["url"].endswith("/chat/completions")]
        self.assertEqual(models, ["my-primary-chat", "my-escalated-chat"],
                         "OMNILORE_PRIMARY_MODEL / OMNILORE_ESCALATED_MODEL 没有进入 chat 请求体")

    def test_embedding_model_and_dim_env_reach_the_embeddings_request(self) -> None:
        env = _clean_model_env()
        env.update({
            "DASHSCOPE_API_KEY": "sk-embed-test",
            "OMNILORE_EMBEDDING_MODEL": "text-embedding-v3",
            "OMNILORE_EMBEDDING_DIM": "1536",
        })
        with mock.patch.dict(os.environ, env, clear=True):
            adapter = ModelAdapter(ModelAdapterConfig(), telemetry_sink=[])
            transport = _RecordingTransport(dimension=1536)
            adapter._http = transport  # type: ignore[assignment]
            adapter.embedding_provider._http = transport  # type: ignore[attr-defined]
            vectors = adapter.embed_texts(["维度必须跟着配置走"])

        self.assertEqual(len(vectors[0]), 1536)
        body = transport.calls[0]["body"]
        self.assertEqual(body["model"], "text-embedding-v3")
        self.assertEqual(body["dimensions"], 1536)

    def test_invalid_embedding_dim_is_a_config_error_with_next_step(self) -> None:
        env = _clean_model_env()
        env["OMNILORE_EMBEDDING_DIM"] = "1024O"  # 字母 O，典型手滑
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ModelConfigError) as ctx:
                ModelAdapterConfig()
        self.assertIn("OMNILORE_EMBEDDING_DIM", str(ctx.exception))
        self.assertIn("正整数", str(ctx.exception), "错误信息必须告诉用户该填什么")

    def test_cli_reports_config_errors_as_config_errors(self) -> None:
        """配置错不能被归成「未预期的错误」—— 否则用户会去翻代码而不是改环境变量。"""
        env = _clean_model_env()
        env[EMBEDDING_PROVIDER_ENV] = "not-a-provider"
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp).resolve() / "OmniLore_Data"
            with mock.patch.dict(os.environ, env, clear=True):
                err = io.StringIO()
                with redirect_stdout(io.StringIO()), redirect_stderr(err):
                    rc = cli_main(["bootstrap", "--data-dir", str(data_dir), "--quiet"])
        self.assertEqual(rc, 1, err.getvalue())
        self.assertIn("CONFIG_ERROR", err.getvalue())
        self.assertNotIn("未预期的错误", err.getvalue())

    def test_new_env_names_are_known_to_the_typo_guard(self) -> None:
        """新变量必须进 KNOWN 清单，否则启动回显会把它当拼写错误报出来。"""
        env = _clean_model_env()
        env.update({
            "OMNILORE_PRIMARY_MODEL": "a",
            "OMNILORE_ESCALATED_MODEL": "b",
            "OMNILORE_EMBEDDING_MODEL": "c",
            "OMNILORE_EMBEDDING_DIM": "64",
            EMBEDDING_PROVIDER_ENV: "none",
            EMBEDDING_BASE_URL_ENV: "http://localhost:11434/v1",
            "OMNILORE_EMBEDDING_API_KEY": "d",
        })
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(unknown_omnilore_env_names(), [], "新变量没有被登记进 KNOWN 清单")
            os.environ["OMNILORE_EMBEDDING_PROVIDR"] = "openai"  # 少一个 E
            self.assertEqual(unknown_omnilore_env_names(), ["OMNILORE_EMBEDDING_PROVIDR"],
                             "拼错的新变量必须被点名")

    @staticmethod
    def _space() -> Any:
        """构造一个最小空间（只用于 chat 请求体断言，不落库）。"""
        from omnilore import SpaceRecord, SpaceType

        return SpaceRecord(
            space_id="sp_env_wiring",
            space_type=SpaceType.PERSONAL,
            parent_space_id=None,
            vault_rel_path="spaces/sp_env_wiring",
        )


class TestDimensionGuard(unittest.TestCase):
    """R-04b：维度不一致必须响亮失败，绝不截断/补零。"""

    def test_validate_rejects_mismatch_and_says_what_to_do(self) -> None:
        vectors = [[0.1] * 1536]
        with self.assertRaises(EmbeddingConfigurationError) as ctx:
            validate_embedding_dimensions(vectors, 1024, provider="openai", model_id="text-embedding-3-small")
        message = str(ctx.exception)
        self.assertIn("1536", message, "必须报出 provider 实际返回的维度")
        self.assertIn("1024", message, "必须报出当前配置的维度")
        self.assertIn("OMNILORE_EMBEDDING_DIM", message, "必须指出该改哪个变量")
        self.assertIn("不会", message, "必须说明不会自动截断/补零")

    def test_validate_rejects_empty_and_ragged_vectors(self) -> None:
        with self.assertRaises(EmbeddingConfigurationError):
            validate_embedding_dimensions([], 1024, provider="openai", model_id="m")
        with self.assertRaises(EmbeddingConfigurationError):
            validate_embedding_dimensions([[0.1] * 4, [0.1] * 5], 4, provider="openai", model_id="m")

    def test_embed_texts_raises_instead_of_silently_truncating(self) -> None:
        env = _clean_model_env()
        env.update({
            EMBEDDING_PROVIDER_ENV: "openai",
            EMBEDDING_BASE_URL_ENV: "http://127.0.0.1:11434/v1",
            "OMNILORE_EMBEDDING_DIM": "1024",
        })
        with mock.patch.dict(os.environ, env, clear=True):
            adapter = ModelAdapter(ModelAdapterConfig(), telemetry_sink=[])
            transport = _RecordingTransport(dimension=1536)  # 端点实际返回 1536
            adapter._http = transport  # type: ignore[assignment]
            adapter.embedding_provider._http = transport  # type: ignore[attr-defined]
            with self.assertRaises(EmbeddingConfigurationError):
                adapter.embed_texts(["维度不匹配的输入"])


class TestDegradationRegression(unittest.IsolatedAsyncioTestCase):
    """R-04b：抽象化**不允许**弄丢 W0/A-6 的降级链。"""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve() / "OmniLore_Data"
        env = _clean_model_env()
        with mock.patch.dict(os.environ, env, clear=True):
            self.engine = OmniLoreEngine(self.root)
            self.boot = ensure_bootstrapped(self.engine, self.root)
            gateway = OmniLoreMCPGateway(self.engine)
            result = await gateway.ingest_document_or_file(
                self.boot.raw_token,
                content_bytes="# 分布式锁与租约续期规范\n\n租约续期失败时必须回退到重新抢锁。".encode("utf-8"),
                filename="provider_note.md",
                target_space_id=self.boot.space_id,
                target_category="01_Work",
                version_key="provider_series",
            )
            self.doc_id = result["task"]["result_doc_id"]

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_provider_outage_degrades_to_keyword_only_with_results(self) -> None:
        """provider 一调就炸（401/超时/断网）→ 仍是 keyword_only + 有结果，不是整体失败。"""
        self.engine.model_adapter.embedding_provider = _FailingProvider(self.engine.model_config.embedding_dimension)
        self.assertEqual(self.engine.model_adapter.retrieval_mode, "hybrid", "provider 表面上可用")

        result = await self.engine.search_knowledge(self.boot.raw_token, "租约续期")

        self.assertEqual(result["retrieval_mode"], "keyword_only", "故障必须降级，而不是让请求失败")
        self.assertNotEqual(result["degraded_reason"], "")
        self.assertIn("向量召回不可用", result["degraded_reason"])
        self.assertTrue(result["results"], "降级后仍必须靠 BM25 给出结果")
        self.assertEqual({hit["doc_id"] for hit in result["results"]}, {self.doc_id})

    async def test_provider_disabled_by_config_also_returns_bm25_results(self) -> None:
        """显式 `none` 与「没配 key」走同一条 BM25 路径，但原因文案不同。"""
        self.engine.model_adapter.embedding_provider = DisabledEmbeddingProvider(self.engine.model_config)
        result = await self.engine.search_knowledge(self.boot.raw_token, "租约续期")
        self.assertEqual(result["retrieval_mode"], "keyword_only")
        self.assertIn("none", result["degraded_reason"])
        self.assertTrue(result["results"])


class TestDefaultBehaviourInvariance(unittest.TestCase):
    """R-04b：不设新变量时**零行为变化** —— 配置默认值与本地向量数值都钉死。"""

    def test_config_defaults_are_unchanged(self) -> None:
        with mock.patch.dict(os.environ, _clean_model_env(), clear=True):
            config = ModelAdapterConfig()
        self.assertEqual(config.primary_chat_model, "qwen3.8-flash")
        self.assertEqual(config.escalated_chat_model, "qwen3.8-max")
        self.assertEqual(config.embedding_model, "text-embedding-v4")
        self.assertEqual(config.embedding_dimension, 1024)
        self.assertEqual(config.embedding_provider, "dashscope")
        self.assertEqual(config.embedding_base_url, "")
        self.assertFalse(config.embedding_dim_explicit, "未显式配置维度时不应向端点发送 dimensions")

    def test_local_embeddings_are_bit_identical_to_before_the_refactor(self) -> None:
        """本地确定性向量的数值必须逐位不变（搬代码不能改数学）。"""
        provider = LocalDeterministicEmbeddingProvider(1024)
        vector = provider.embed(["分布式锁与租约续期规范 alpha beta"])[0]
        self.assertEqual(len(vector), 1024)
        digest = hashlib.sha256(json.dumps(vector).encode("utf-8")).hexdigest()[:16]
        self.assertEqual(
            digest, PINNED_LOCAL_VECTOR_DIGEST,
            "本地确定性向量的数值变了：升级后既有用户的检索排序会跟着变。"
            "若这是有意的，请同步更新 PINNED_LOCAL_VECTOR_DIGEST 并在 CHANGELOG 说明。",
        )


class TestUnifiedDataDir(unittest.TestCase):
    """R-11b：数据目录只有一个解析入口，`ops/` 下的派生物都挂在同一根下。"""

    #: 允许出现 `OMNILORE_DATA_DIR` 字面量的模块：resolver 本身 + 引导（清单与配置片段）。
    _DATA_DIR_LITERAL_ALLOWED = {"pathutil.py", "bootstrap.py"}

    def test_expanduser_only_lives_in_the_shared_resolver(self) -> None:
        """任何模块自己**调用** `.expanduser()` 都是漂移的开始（QA P1-1 的成因）。

        用 AST 而不是 grep：注释和文档里提到 `.expanduser()`（例如提醒「不要在这里再写一份」）
        是合法的，字符串扫描会制造假阳性 —— 这类误报最后总会被人用 `# noqa` 糊掉。
        """
        offenders = []
        for path in sorted((REPO_ROOT / "omnilore").glob("*.py")):
            if path.name == "pathutil.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "expanduser"):
                    offenders.append(f"{path.name}:{node.lineno}")
        self.assertEqual(offenders, [], f"以下位置自己解析了 `~`，请改用 resolve_data_dir：{offenders}")

    def test_env_var_is_read_only_inside_the_shared_resolver(self) -> None:
        """除了 resolver（与 bootstrap 的「已知变量清单 / 配置片段」），没人直接读这个变量。"""
        patterns = (
            'os.environ.get("OMNILORE_DATA_DIR"',
            "os.environ.get('OMNILORE_DATA_DIR'",
            'os.environ["OMNILORE_DATA_DIR"',
            "os.environ['OMNILORE_DATA_DIR'",
        )
        offenders = []
        for path in sorted((REPO_ROOT / "omnilore").glob("*.py")):
            if path.name in self._DATA_DIR_LITERAL_ALLOWED:
                continue
            text = path.read_text(encoding="utf-8")
            if any(pattern in text for pattern in patterns):
                offenders.append(path.name)
        self.assertEqual(offenders, [], f"以下模块直接读了环境变量，请改用 resolve_data_dir：{offenders}")

    def test_entry_points_import_the_shared_resolver(self) -> None:
        for module in ("cli.py", "mcp_stdio_server.py"):
            text = (REPO_ROOT / "omnilore" / module).read_text(encoding="utf-8")
            self.assertIn("from .pathutil import resolve_data_dir", text,
                          f"{module} 没有走共享 resolver（数据目录会漂移）")
            self.assertIn("resolve_data_dir(", text, f"{module} 导入了 resolver 却没有使用")

    def test_resolver_normalizes_relative_and_home_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            self.assertEqual(resolve_data_dir("./OmniLore_Data", base=base), base / "OmniLore_Data")
            self.assertEqual(resolve_data_dir("~/x", base=base), (Path.home() / "x").resolve())
            env_path = resolve_data_dir(env={"OMNILORE_DATA_DIR": "./D"}, base=base)
            self.assertEqual(env_path, base / "D")

    def test_derived_state_hangs_off_the_same_root(self) -> None:
        """`ops/index`、引导状态、证据包输出都必须落在同一个数据目录下。"""
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp).resolve() / "OmniLore_Data"
            engine = OmniLoreEngine(data_dir)
            self.assertEqual(Path(engine.storage.base_dir), data_dir.resolve())
            self.assertEqual(index_dir(engine.storage.base_dir), data_dir / INDEX_DIR_REL)
            self.assertEqual(bootstrap_state_path(data_dir).parent.parent, data_dir)
            self.assertEqual((data_dir / "ops" / "audit_bundles").parent, data_dir / "ops",
                             "证据包默认输出目录必须挂在数据目录的 ops/ 下")


class TestArm64Ci(unittest.TestCase):
    """R-11b：ARM64 要有**离线** CI 证据通道，且不动既有 job。"""

    def setUp(self) -> None:
        self.raw = CI_WORKFLOW_PATH.read_text(encoding="utf-8")
        self.workflow = yaml.safe_load(self.raw)
        self.assertIsInstance(self.workflow, dict, "ci.yml 无法解析为映射（YAML 语法已坏）")
        self.jobs: Dict[str, Any] = self.workflow.get("jobs") or {}

    def _arm_job(self) -> Dict[str, Any]:
        matches = [
            (name, job) for name, job in self.jobs.items()
            if isinstance(job, dict) and "arm" in str(job.get("runs-on", "")).lower()
        ]
        self.assertEqual(len(matches), 1, f"应当恰有 1 个 arm64 job，实际 {len(matches)}：{matches}")
        return matches[0][1]

    def test_there_is_an_arm64_job_on_the_arm_runner_label(self) -> None:
        job = self._arm_job()
        self.assertIn("ubuntu-24.04-arm", str(job["runs-on"]),
                      "arm64 job 必须用 GitHub 的 arm64 runner 标签（ubuntu-24.04-arm）")

    def test_arm64_job_runs_the_offline_suite_and_lint(self) -> None:
        runs = "\n".join(str(step.get("run", "")) for step in self._arm_job().get("steps") or [])
        self.assertIn("unittest discover", runs, "arm64 job 没有跑测试套件，那它证明不了什么")
        self.assertIn("ruff check", runs, "arm64 job 没有跑 ruff")

    def test_arm64_job_has_no_database_service(self) -> None:
        """刻意只跑离线：同时引入「新架构 + 新服务容器」两个变量，出问题分不清是谁的锅。"""
        self.assertNotIn("services", self._arm_job(), "arm64 job 不应挂 postgres service container")
        runs = "\n".join(str(step.get("run", "")) for step in self._arm_job().get("steps") or [])
        self.assertNotIn("verify_rls_pg", runs)

    def test_integration_rls_job_stays_on_x86(self) -> None:
        job = self.jobs.get("integration-rls") or {}
        self.assertEqual(str(job.get("runs-on")), "ubuntu-latest",
                         "integration-rls 不应改到 arm64（避免引入新变量）")

    def test_offline_matrix_jobs_are_not_displaced(self) -> None:
        test_job = self.jobs.get("test") or {}
        matrix = (test_job.get("strategy") or {}).get("matrix") or {}
        self.assertEqual(sorted(matrix.get("os") or []), ["ubuntu-latest", "windows-latest"])
        for name in ("test", "lint", "dco"):
            self.assertIn(name, self.jobs, f"{name} job 不见了")
            self.assertNotIn("needs", self.jobs[name], f"{name} 不应依赖任何其他 job")

    def test_readme_declares_supported_architectures(self) -> None:
        text = README_PATH.read_text(encoding="utf-8")
        self.assertIn("x86_64", text, "README 没有声明 x86_64 支持")
        self.assertIn("arm64", text, "README 没有声明 arm64 支持")
        self.assertRegex(text, r"arm64[\s\S]{0,400}(ci\.yml|CI)", "README 声明了 arm64 却没写验证方式")


if __name__ == "__main__":  # pragma: no cover - 便于单文件运行
    unittest.main()
