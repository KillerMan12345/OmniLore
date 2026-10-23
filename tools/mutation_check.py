#!/usr/bin/env python
"""变异测试自检：逐个把被测实现故意改坏，确认对应测试会 FAIL。

团队要求：test_w0_launch.py 里不能有「测了个寂寞」的用例 —— 一个名字承诺了行为、
却在实现被改坏后依然 PASS 的用例，比没有这条用例更危险。

做法：对每个 mutation，把源码改成坏版本 → 跑指定的测试 → 记录结果。
  * 期望 FAIL：说明该测试真的守住了这个行为（有效断言）。
  * 若仍 PASS：说明是空转断言，需要重写或改名。

⚠️ 安全约束（曾经踩过的大坑，改这个脚本时务必保留）
================================================================
本脚本**曾经直接修改活工作区**，一次异常退出就把三处变异留在了仓库里（落盘被删、
授权步骤被禁用、字段正交性被抹掉），害得 QA 手工还原。**根因就是在活仓库上改。**

现在的做法：
  1. 先把仓库 `shutil.copytree` 到临时目录（排除 .git / 缓存 / 数据目录）；
  2. 所有变异**只改副本**，测试也在副本里跑；
  3. 副本用完即弃，**活仓库全程只读**；
  4. 另外还有两道保险：
     - 起飞前检查活仓库无变异残留（含 "mutated" 字样）；
     - 结束后再次确认活仓库干净并输出校验结果。

每个 mutation 还会**先跑基线**（未变异时必须绿），否则该变异的结果判为不可信 ——
避免「测试根本没跑起来（收集错误）却被计成有效」的假绿。

用法::

    python tools/mutation_check.py
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

LIVE_ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable

# 复制仓库时排除的目录：只影响速度/体积，与测试无关。
COPY_IGNORE = shutil.ignore_patterns(
    ".git",
    "__pycache__",
    ".ruff_cache",
    ".pytest_cache",
    ".mypy_cache",
    ".workbuddy",
    "deliverables",
    "OmniLore_Data",
    "*.egg-info",
    "dist",
    "build",
)

# 变异残留的特征串 —— 用于起飞前/结束后的活仓库体检。
RESIDUE_MARKERS = ("mutated", "MUTATED", "MUTATION")


class Mutation:
    """A single deliberate breakage plus the tests expected to catch it.

    `old` / `new` may be a single string or a list of paired strings, so one
    mutation can disable several safeguards at once (needed when the production
    code has layered defences, e.g. grant + verify + rollback).
    """

    def __init__(self, name: str, rel_path: str, old, new, tests: list[str], expect: str = "fail") -> None:
        self.name = name
        self.rel_path = rel_path
        self.old = old
        self.new = new
        self.tests = tests
        self.expect = expect  # "fail" | "skip"

    def apply(self, work_root: Path) -> None:
        """Rewrite the source **in the workspace copy** with the broken variant."""
        path = work_root / self.rel_path
        text = path.read_text(encoding="utf-8")
        olds = [self.old] if isinstance(self.old, str) else self.old
        news = [self.new] if isinstance(self.new, str) else self.new
        if len(olds) != len(news):
            raise RuntimeError(f"[{self.name}] old/new 数量不匹配")
        for old in olds:
            if old not in text:
                raise RuntimeError(f"[{self.name}] 未能在 {self.rel_path} 找到待变异片段: {old[:60]!r}")
        for old, new in zip(olds, news):
            text = text.replace(old, new, 1)
        path.write_text(text, encoding="utf-8")


MUTATIONS: list[Mutation] = [
    Mutation(
        "M1 删除 expanduser()（P1-1 原始触发行为）",
        "omnilore/pathutil.py",
        "    path = Path(value).expanduser()",
        "    path = Path(value)",
        ["test_w0_launch.TestDataDirResolution.test_tilde_is_really_expanded"],
    ),
    Mutation(
        "M2 resolve_data_dir 忽略 raw_value，永远走默认目录",
        "omnilore/pathutil.py",
        "    value = raw_value if raw_value is not None else source_env.get(DEFAULT_DATA_DIR_ENV_VAR, DEFAULT_DATA_DIR)",
        "    value = source_env.get(DEFAULT_DATA_DIR_ENV_VAR, DEFAULT_DATA_DIR)",
        [
            "test_w0_launch.TestCliEntryPoint.test_default_data_dir_uses_shared_resolver",
            "test_w0_launch.TestDataDirResolution.test_relative_paths_resolve_against_cwd",
        ],
    ),
    Mutation(
        "M3 write_text_canonical 退化成平台默认换行（CRLF 回归）",
        "omnilore/textio.py",
        "    path.write_bytes(canonical_text_bytes(text))",
        "    Path(path).write_text(text, encoding='utf-8')",
        [
            "test_w0_launch.TestCanonicalTextIO.test_write_text_canonical_emits_lf_only_bytes",
            "test_w0_launch.TestCanonicalTextIO.test_storage_consistency_after_ingest",
        ],
    ),
    Mutation(
        "M4 read_text_canonical 不再归一化 CRLF",
        "omnilore/textio.py",
        '    return canonicalize_newlines(Path(path).read_bytes().decode("utf-8"))',
        '    return Path(path).read_bytes().decode("utf-8")',
        ["test_w0_launch.TestCanonicalTextIO.test_read_and_write_roundtrip_is_stable"],
    ),
    Mutation(
        "M5 改坏 __version__（PEP 440 版本号）",
        "omnilore/version.py",
        '__version__ = "0.3.0a0"',
        '__version__ = "9.9.9"',
        ["test_w0_launch.TestVersionConsistency.test_pyproject_matches_package_version"],
    ),
    Mutation(
        "M6 改坏 __version_label__（MCP serverInfo 口径）",
        "omnilore/version.py",
        '__version_label__ = "0.3.0-alpha"',
        '__version_label__ = "9.9.9"',
        ["test_w0_launch.TestVersionConsistency.test_display_label_matches_mcp_handshake"],
    ),
    Mutation(
        "M7 检索降级失效（embed 失败不再兜底，直接抛）",
        "omnilore/engine.py",
        "            except Exception as exc:  # provider unreachable / auth / quota / timeout\n                degraded = True",
        "            except Exception as exc:  # mutated\n                raise",
        ["test_w0_launch.TestZeroKeyDegradation.test_invalid_or_unreachable_provider_degrades_gracefully"],
    ),
    Mutation(
        "M8 合成降级不再上报（synthesis_degraded 恒 False）",
        "omnilore/engine.py",
        '            "synthesis_degraded": bool(synth_note),',
        '            "synthesis_degraded": False,',
        [
            "test_w0_launch.TestZeroKeyDegradation.test_synthesis_degrade_is_reported_orthogonally",
            "test_w0_launch.TestZeroKeyDegradation.test_invalid_or_unreachable_provider_degrades_gracefully",
        ],
    ),
    Mutation(
        "M9 把合成失败并回 degraded_reason（字段不再正交）",
        "omnilore/engine.py",
        '            "degraded_reason": degraded_reason,',
        '            "degraded_reason": degraded_reason or synth_note,',
        ["test_w0_launch.TestZeroKeyDegradation.test_synthesis_degrade_is_reported_orthogonally"],
    ),
    Mutation(
        "M10 bootstrap 每次都签发新 token（幂等性失效）",
        "omnilore/bootstrap.py",
        "    raw_token = token\n    if raw_token is None and state is not None:",
        "    raw_token = token\n    if False and raw_token is None and state is not None:",
        [
            "test_w0_launch.TestBootstrapIdempotency.test_bootstrap_is_idempotent_and_reuses_token",
            "test_w0_launch.TestBootstrapIdempotency.test_explicit_token_is_honoured_and_survives",
        ],
    ),
    Mutation(
        "M11 bootstrap 不再落盘 state（跨进程失效，P1-1 的根因）",
        "omnilore/bootstrap.py",
        '    write_text_canonical(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\\n")\n'
        "    restrict_secret_file_permissions(path)\n    return path",
        "    return path  # mutated: never persist",
        ["test_w0_launch.TestBootstrapIdempotency.test_bootstrap_state_file_is_persisted_and_loadable"],
    ),
    # 新版实现有三层防护（授权 / 等待显式 ACE / 收尾不变量校验 + /reset 回滚）。
    # 只拆一层不够 —— 回滚会把不变量救回来（这正是期望行为）。要证明不变量用例有效，
    # 必须三层一起拆。
    Mutation(
        "M11b 权限收紧三层防护全拆（授权+显式ACE校验+收尾校验）→ 锁死 owner",
        "omnilore/bootstrap.py",
        [
            '    if not _run_icacls(["icacls", str(path), "/grant:r", f"{principal}:(F)"]):',
            "    if not _wait_for(lambda: _has_explicit_ace(path), attempts=6, delay=0.05):",
            "    if not _is_readable(path) or not _has_explicit_ace(path):",
        ],
        ["    if False:  # mutated"] * 3,
        [
            "test_w0_launch.TestBootstrapIdempotency.test_permissions_tightening_never_locks_out_owner",
            "test_w0_launch.TestBootstrapIdempotency.test_permissions_are_actually_tightened",
            "test_w0_launch.TestBootstrapIdempotency.test_bootstrap_state_file_is_tightened_end_to_end",
        ],
    ),
    # 刻意**不依赖具体数字**：往测试文件塞一个哨兵用例让实际数 +1，而 README 宣称不变
    # → 宣称与证据失配 → 必须红。（早先写成 mutate 固定数字，每次加用例片段就失效。）
    Mutation(
        "M12 实际用例数变化但 README 未同步（文档宣称守卫）",
        "tests/test_w0_launch.py",
        '\nif __name__ == "__main__":\n    unittest.main()',
        '\nclass _MutationSentinel(unittest.TestCase):\n    def test_sentinel(self) -> None:\n        pass\n\n\n'
        'if __name__ == "__main__":\n    unittest.main()',
        [
            "test_w0_launch.TestReadmeClaimAccuracy.test_total_case_count_claim",
            "test_w0_launch.TestReadmeClaimAccuracy.test_per_file_case_count_claims",
        ],
    ),
    Mutation(
        "M13 README 表数写错（14 vs 15）",
        "README.md",
        "15 张表 + 角色 + RLS",
        "14 张表 + 角色 + RLS",
        ["test_w0_launch.TestReadmeClaimAccuracy.test_sql_table_count_claim"],
    ),
    Mutation(
        "M14 cli.py 第三处内联数据目录解析（P2-E 同源重复）",
        "omnilore/cli.py",
        "    data_dir = resolve_data_dir(args.data_dir)\n    engine = OmniLoreEngine(data_dir)\n    result = ensure_bootstrapped(",
        "    data_dir = Path(args.data_dir).expanduser().resolve()\n    engine = OmniLoreEngine(data_dir)\n    result = ensure_bootstrapped(",
        ["test_w0_launch.TestDataDirResolution.test_no_duplicate_data_dir_resolution_in_source"],
    ),
    Mutation(
        "M15 retrieval_mode 恒为 hybrid（零 Key 不再如实上报）",
        "omnilore/engine.py",
        '        return "hybrid" if self.config.get_api_key() else "keyword_only"',
        '        return "hybrid"',
        [
            "test_w0_launch.TestZeroKeyDegradation.test_model_adapter_reports_mode_and_reason",
            "test_w0_launch.TestZeroKeyDegradation.test_zero_key_reports_keyword_only",
        ],
    ),
    Mutation(
        "M16 MCP 不再共用 resolver（漂移回 P1-1 的原点）",
        "omnilore/mcp_stdio_server.py",
        "from .pathutil import resolve_data_dir\n",
        "",
        ["test_w0_launch.TestDataDirResolution.test_cli_and_mcp_share_one_resolver"],
    ),
    # 端到端用例守「调用点」，直接调用函数的用例守「函数行为」。两者缺一不可：
    # 只留前者，删掉函数体仍绿；只留后者，删掉调用点仍绿。
    Mutation(
        "M18 权限收紧整段 no-op（QA P2-1 变异 A）",
        "omnilore/bootstrap.py",
        "    restrict_secret_file_permissions(path)\n    return path",
        "    return path  # mutated: tightening removed",
        [
            "test_w0_launch.TestBootstrapIdempotency.test_bootstrap_state_file_is_tightened_end_to_end",
            "test_w0_launch.TestBootstrapIdempotency.test_permissions_are_actually_tightened",
        ],
    ),
    Mutation(
        "M19 Windows 分支提前 return（QA P2-1 变异 B）",
        "omnilore/bootstrap.py",
        '        if os.name == "nt":\n            _restrict_windows(path)\n        else:\n            os.chmod(path, 0o600)',
        '        if os.name == "nt":\n            return  # mutated\n        else:\n            os.chmod(path, 0o600)',
        ["test_w0_launch.TestBootstrapIdempotency.test_permissions_are_actually_tightened"],
    ),
    Mutation(
        "M20 删掉断继承那一步（QA P2-1 变异 C）",
        "omnilore/bootstrap.py",
        '    if not _run_icacls(["icacls", str(path), "/inheritance:r"]):',
        "    if False:  # mutated: inheritance removal removed",
        ["test_w0_launch.TestBootstrapIdempotency.test_permissions_are_actually_tightened"],
    ),
    Mutation(
        "M21 归因分类器置空（QA P2-B 无守卫）",
        "omnilore/engine.py",
        "_RESPONSE_SHAPE_ERRORS = (\n    KeyError,\n    IndexError,\n    TypeError,\n    ValueError,  # includes json.JSONDecodeError\n    AttributeError,\n)",
        "_RESPONSE_SHAPE_ERRORS = ()",
        ["test_w0_launch.TestZeroKeyDegradation.test_response_shape_error_is_not_blamed_on_provider"],
    ),
    Mutation(
        "M23 AUTO_BOOTSTRAP=0 早退不再区分是否已持久化身份（QA P2-1 回归）",
        "omnilore/mcp_stdio_server.py",
        "    if load_bootstrap_state(data_root) is None and not auto_bootstrap and explicit_token is None:",
        "    if not auto_bootstrap and explicit_token is None:",
        ["test_w0_launch.TestMcpStdioEntrypoint.test_auto_bootstrap_zero_still_accepts_persisted_token"],
    ),
    Mutation(
        "M22 MCP 入口改回 async def（console script 再次变成死链）",
        "omnilore/mcp_stdio_server.py",
        "def main_stdio_sync() -> None:",
        "async def main_stdio_sync() -> None:",
        [
            "test_w0_launch.TestMcpStdioEntrypoint.test_console_script_target_is_not_a_coroutine",
            "test_w0_launch.TestMcpStdioEntrypoint.test_stdio_handshake_serves_jsonrpc_over_real_pipes",
        ],
    ),
]


def scan_residue(root: Path) -> list[str]:
    """Return suspicious mutation residue left in a tree (safety net).

    Args:
        root: Tree to scan.

    Returns:
        List of `path:line` strings that look like leftover mutations.
    """
    hits: list[str] = []
    for src in list(root.glob("omnilore/*.py")) + list(root.glob("tests/*.py")):
        try:
            text = src.read_text(encoding="utf-8")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if any(marker in line for marker in RESIDUE_MARKERS):
                hits.append(f"{src.relative_to(root).as_posix()}:{lineno} {line.strip()[:70]}")
    return hits


def run_tests(work_root: Path, test_ids: list[str]) -> dict:
    """Run the given test ids inside `work_root`.

    `tests/` is placed on PYTHONPATH so that direct module ids resolve; without
    this the tests fail to *collect*, and a collection error must never be
    mistaken for a caught mutation.

    Args:
        work_root: Copied workspace to run in.
        test_ids: Fully qualified unittest test ids.

    Returns:
        Dict with failures / errors / skipped / ran counts.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(work_root), str(work_root / "tests")])
    proc = subprocess.run(
        [PY, "-m", "unittest", "-v"] + test_ids,
        cwd=str(work_root),
        capture_output=True,
        text=True,
        env=env,
    )
    out = proc.stdout + proc.stderr
    ran = re.search(r"^Ran (\d+) tests?", out, re.M)
    return {
        "failures": len(re.findall(r"^FAIL: ", out, re.M)),
        "errors": len(re.findall(r"^ERROR: ", out, re.M)),
        "skipped": len(re.findall(r"\.\.\. skipped", out)),
        "ran": int(ran.group(1)) if ran else 0,
    }


def main() -> int:
    print("=" * 78)
    print("变异测试自检（在临时副本上运行，活仓库只读）")
    print("=" * 78)

    residue = scan_residue(LIVE_ROOT)
    if residue:
        print("⛔ 起飞前体检失败：活仓库存在变异残留，拒绝运行：")
        for hit in residue:
            print("   ", hit)
        return 2
    print("起飞前体检：活仓库无变异残留 ✅\n")

    results: list[tuple[str, str, str]] = []
    tmp_parent = tempfile.mkdtemp(prefix="ctxhub-mutation-")
    try:
        for index, mut in enumerate(MUTATIONS, start=1):
            work_root = Path(tmp_parent) / f"ws{index}"
            shutil.copytree(LIVE_ROOT, work_root, ignore=COPY_IGNORE)

            # Step 0: baseline — tests must be green before we break anything.
            base = run_tests(work_root, mut.tests)
            if base["ran"] == 0 or base["failures"] or base["errors"]:
                results.append((mut.name, "基线异常 ⚠️",
                                f"ran={base['ran']} f={base['failures']} e={base['errors']}"))
                print(f"⚠️  基线异常  {mut.name}: ran={base['ran']} "
                      f"failures={base['failures']} errors={base['errors']}（结果不可信，跳过）")
                shutil.rmtree(work_root, ignore_errors=True)
                continue

            try:
                mut.apply(work_root)
            except RuntimeError as exc:
                results.append((mut.name, "SKIP-MUT", str(exc)))
                print(f"⚠️  {mut.name}: 跳过（{exc}）")
                shutil.rmtree(work_root, ignore_errors=True)
                continue

            res = run_tests(work_root, mut.tests)
            caught = (res["failures"] + res["errors"]) > 0 or (mut.expect == "skip" and res["skipped"] > 0)
            status = "有效 ✅" if caught else "空转 ❌"
            detail = f"failures={res['failures']} errors={res['errors']} skipped={res['skipped']}"
            results.append((mut.name, status, detail))
            print(f"{status}  {mut.name}  ({detail})")
            shutil.rmtree(work_root, ignore_errors=True)
    finally:
        shutil.rmtree(tmp_parent, ignore_errors=True)

    effective = [r for r in results if r[1] == "有效 ✅"]
    noop = [r for r in results if r[1] == "空转 ❌"]
    other = [r for r in results if r[1] not in ("有效 ✅", "空转 ❌")]

    print("\n" + "=" * 78)
    print(f"变异总数 {len(results)}：有效 {len(effective)} ／ 空转 {len(noop)} ／ 其他 {len(other)}")
    for name, _s, detail in noop:
        print(f"  ❌ 空转：{name}  ({detail})")
    for name, status, detail in other:
        print(f"  ⚠️  {name}: {status} ({detail})")

    residue = scan_residue(LIVE_ROOT)
    if residue:
        print("\n⛔ 结束体检失败：活仓库被污染：")
        for hit in residue:
            print("   ", hit)
        return 2
    print("结束体检：活仓库未被改动 ✅（所有变异只发生在临时副本里）")
    print("=" * 78)
    return 0 if not noop else 1


if __name__ == "__main__":
    raise SystemExit(main())
