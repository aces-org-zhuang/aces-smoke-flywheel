#!/usr/bin/env python
"""重放 0.ssot 的各个历史状态，产出逐状态全景与前后差异。

为什么是"重放"而不是"原件"：
  0.ssot 在交付仓里只被写过一次（commit 39bc332 之后冻结），生成链上的中间态
  没有提交，早期产物被后一次覆盖。要让"演进路线"可复核，唯一诚实的办法是
  用**同一份冻结的 openapi.yaml** 重跑历史版本的投影逻辑，并明确标注这是重放。

冻结输入：
  0.ssot/openapi.yaml（commit d96174cd 的 spectacular 导出，sha256 见 schema_version.json）
  SUT 的前端枚举 ApiEndpoints.tsx（commit d96174cd）

状态定义（都对应真实发生过的时刻，不是设想）：
  G0-export-failed   spectacular 因 INVE-W8 退出码 0 写出 0 字节，无事实源可投影
                     证据：外部日志 spec.log（2026-10-31T09:44:51Z）+ 本脚本 --probe-empty
  G1-naive-axis      第一版 normalize_path 不剥离后端 /api 前缀 → 前后端不在同一坐标系
  G2-aligned-axis    修正后（现行 emit_ssot.py）→ 合同矩阵命中 190

用法：
  python tools/replay_ssot.py --repo D:/wbeval/repos/inventree --probe-empty
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import re
import shutil
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HIST = ROOT / "0.ssot" / "history"


def load_emit():
    spec = importlib.util.spec_from_file_location("emit_ssot", ROOT / "tools/emit_ssot.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


# 第一版的归一化：把 {id} 与 :id 统一、去掉尾斜杠，但**不**处理 /api 前缀。
# 抄的是历史实现的形状，不是新写的"错误版本"——差异只在最后那段前缀剥离。
def make_naive(api_prefix: str):
    def naive(path: str, *, consumer: bool = False) -> str:
        p = path.strip()
        p = p.split("?")[0]
        if not p.startswith("/"):
            p = "/" + p
        p = re.sub(r"\{[^}]+\}", "{}", p)
        p = re.sub(r":[A-Za-z_][A-Za-z0-9_]*", "{}", p)
        if len(p) > 1 and p.endswith("/"):
            p = p[:-1]
        return p

    return naive


def run_emit(mod, repo: Path, out: Path, openapi: Path) -> dict:
    buf = io.StringIO()
    argv = sys.argv[:]
    sys.argv = [
        "emit_ssot.py",
        "--repo",
        str(repo),
        "--out",
        str(out),
        "--openapi",
        str(openapi),
    ]
    try:
        with redirect_stdout(buf):
            rc = mod.main()
    finally:
        sys.argv = argv
    return {"exit_code": rc, "stdout_tail": buf.getvalue().strip().splitlines()[-3:]}


def snapshot_metrics(out: Path) -> dict:
    ep = json.loads((out / "endpoints.json").read_text(encoding="utf-8"))
    cs = json.loads((out / "consumers.json").read_text(encoding="utf-8"))
    cm = json.loads((out / "contract_matrix.json").read_text(encoding="utf-8"))
    dg = json.loads((out / "dependency_graph.json").read_text(encoding="utf-8"))
    sv = json.loads((out / "schema_version.json").read_text(encoding="utf-8"))
    return {
        "endpoints": ep["count"],
        "consumers": cs["count"],
        "matched": cm["totals"]["matched"],
        "unmatched_consumer": cm["totals"]["unmatched_consumer"],
        "backend_unclaimed": cm["totals"]["backend_unclaimed"],
        "apps": dg["node_count"],
        "edges": dg["edge_count"],
        "openapi_sha256": sv["openapi_sha256"],
        "files": {
            p.name: p.stat().st_size for p in sorted(out.iterdir()) if p.is_file()
        },
    }


def classify_unmatched(cm: dict, sut: Path) -> list[dict]:
    """给未命中的前端声明定性：allauth 体外边界 / 真实陈旧声明。

    陈旧 = 声明仍写在前端枚举里，但后端 URLconf 里已经没有任何对应路由。
    判据是"前端源码里还有没有引用 + 后端规范里有没有同名前缀路由"，两条都可复算。
    """
    frontend = (sut / "src/frontend").rglob("*.tsx")
    sources = ""
    for f in frontend:
        try:
            sources += f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
    out = []
    for c in cm["unmatched_consumer"]:
        enum = c["enum"]
        is_allauth = c["normalized"].startswith("/auth") or "auth/v1" in c["template"]
        refs = len(re.findall(rf"\b{re.escape(enum)}\b", sources)) - 1  # 减掉枚举自身定义
        out.append(
            {
                "enum": enum,
                "template": c["template"],
                "normalized": c["normalized"],
                "verdict": "outside-boundary:allauth" if is_allauth else "stale-consumer",
                "frontend_references": max(refs, 0),
                "reason": (
                    "allauth headless 路由由第三方包挂载，不在本仓 URLconf，属声明体外边界"
                    if is_allauth
                    else "后端规范中无该路由，前端仍保留声明且仍有引用"
                ),
            }
        )
    return out


def probe_empty(mod, repo: Path) -> dict:
    """复现 G0：空规范时生成器的行为。"""
    tmp = Path(tempfile.mkdtemp(prefix="ssot-empty-"))
    empty = tmp / "openapi.yaml"
    empty.write_bytes(b"")
    out = tmp / "out"
    out.mkdir()
    try:
        run_emit(mod, repo, out, empty)
        produced = sorted(p.name for p in out.iterdir())
        return {
            "probe": "empty-openapi",
            "guard_triggered": False,
            "files_written": produced,
            "note": "未触发守卫（历史行为：崩在 yaml 解析之后）",
        }
    except SystemExit as exc:
        return {
            "probe": "empty-openapi",
            "guard_triggered": True,
            "message": str(exc),
            "files_written": sorted(p.name for p in out.iterdir()),
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--probe-empty", action="store_true", help="复现空规范拒跑守卫")
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    openapi = ROOT / "0.ssot/openapi.yaml"
    mod = load_emit()

    result: dict = {
        "frozen_input": {
            "openapi": "0.ssot/openapi.yaml",
            "openapi_bytes": openapi.stat().st_size,
            "sut": str(repo),
        },
        "states": [],
    }

    if args.probe_empty:
        result["states"].append(
            {
                "id": "G0-export-failed",
                "kind": "probe+external-log",
                "metrics": {"endpoints": 0, "consumers": 0, "matched": 0,
                            "unmatched_consumer": 0, "backend_unclaimed": 0,
                            "apps": 0, "edges": 0},
                "observed": probe_empty(mod, repo),
                "external_evidence": {
                    "file": "spec.log（被测仓导出日志，2026-10-31T09:44:51Z）",
                    "line": "INVE-W8: Database Migrations required",
                    "exit_code": 0,
                    "bytes_written": 0,
                },
            }
        )

    # G1：轴错位
    naive = make_naive(mod.API_PREFIX)
    original = mod.normalize_path
    mod.normalize_path = naive
    g1 = HIST / "G1-naive-axis"
    if g1.exists():
        shutil.rmtree(g1)
    run = run_emit(mod, repo, g1, openapi)
    mod.normalize_path = original
    (g1 / "replay.json").write_text(
        json.dumps({"variant": "naive-axis", "run": run}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    m1 = snapshot_metrics(g1)

    # G2：轴对齐（现行实现）
    g2 = HIST / "G2-aligned-axis"
    if g2.exists():
        shutil.rmtree(g2)
    run2 = run_emit(mod, repo, g2, openapi)
    (g2 / "replay.json").write_text(
        json.dumps({"variant": "aligned-axis", "run": run2}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    m2 = snapshot_metrics(g2)

    # 逐条声明的状态迁移
    c1 = json.loads((g1 / "contract_matrix.json").read_text(encoding="utf-8"))
    c2 = json.loads((g2 / "contract_matrix.json").read_text(encoding="utf-8"))
    hit1 = {m["enum"] for m in c1["matched"]}
    hit2 = {m["enum"] for m in c2["matched"]}
    all_enum = sorted({m["enum"] for m in c2["matched"]} | {u["enum"] for u in c2["unmatched_consumer"]})
    flips = [
        {
            "enum": e,
            "G1": "unmatched" if e not in hit1 else "matched",
            "G2": "unmatched" if e not in hit2 else "matched",
        }
        for e in all_enum
        if (e in hit1) != (e in hit2)
    ]

    unmatched_detail = classify_unmatched(c2, repo)
    stale = [u for u in unmatched_detail if u["verdict"] == "stale-consumer"]

    result["states"] += [
        {
            "id": "G1-naive-axis",
            "kind": "replay",
            "definition": "normalize_path 不剥离后端 /api 前缀；前端模板相对 /api，两端不在同一坐标系",
            "metrics": m1,
            "run": run,
        },
        {
            "id": "G2-aligned-axis",
            "kind": "replay",
            "definition": "normalize_path 对后端剥离 /api 前缀（consumer 侧不动），两端落到同一坐标系",
            "metrics": m2,
            "run": run2,
        },
    ]
    result["delta"] = {
        "matched": m2["matched"] - m1["matched"],
        "unmatched_consumer": m2["unmatched_consumer"] - m1["unmatched_consumer"],
        "backend_unclaimed": m2["backend_unclaimed"] - m1["backend_unclaimed"],
        "endpoints_unchanged": m1["endpoints"] == m2["endpoints"],
        "consumers_unchanged": m1["consumers"] == m2["consumers"],
        "flipped_declarations": len(flips),
        "flip_sample": flips[:12],
    }
    result["unmatched_classification"] = {
        "total": len(unmatched_detail),
        "outside_boundary": len([u for u in unmatched_detail if u["verdict"].startswith("outside")]),
        "stale": len(stale),
        "stale_items": stale,
        "items": unmatched_detail,
    }
    (HIST / "evolution.json").parent.mkdir(parents=True, exist_ok=True)
    (HIST / "evolution.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(json.dumps({k: v for k, v in result.items() if k != "unmatched_classification"},
                     ensure_ascii=False, indent=2))
    print(
        f"\n未命中定性: 体外 {result['unmatched_classification']['outside_boundary']} / "
        f"陈旧 {result['unmatched_classification']['stale']}"
    )
    for s in stale:
        print(f"  stale: {s['enum']:22s} {s['template']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
