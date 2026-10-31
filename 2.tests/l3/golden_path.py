#!/usr/bin/env python
"""L3 黄金路径：一条不可失败的业务主线，全程走真实 HTTP。

路径（PCB 装配链的真实业务顺序）：
  建零件(装配件/元件) → 建供应商 → 供应商物料 → 采购订单 → 下单(issue) →
  收货入库 → 建 BOM → 建生产工单 → 分配库存 → 结单 →
  断言：产出库存增加、元件库存减少、订单与工单进入完成态

断言口径：response + db_delta（不接受"接口返回 200 就算过"）。
每个步骤都落证据：请求、状态码、耗时、关键返回字段。

用法:
  python 2.tests/l3/golden_path.py --base-url http://127.0.0.1:8000 --token <token> --out <run.json>
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

PO_COMPLETE = 30
BUILD_COMPLETE = 30


class StepError(RuntimeError):
    pass


class Client:
    def __init__(self, base: str, token: str):
        self.base = base.rstrip("/")
        self.token = token
        self.steps: list[dict] = []

    def call(self, method: str, path: str, payload: dict | None = None, expect=(200, 201)) -> dict:
        url = f"{self.base}{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Token {self.token}")
        req.add_header("Content-Type", "application/json")
        started = time.time()
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = resp.read().decode("utf-8", "replace")
                code = resp.status
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            code = exc.code
        except urllib.error.URLError as exc:
            raise StepError(f"{method} {path} 连接失败: {exc}") from exc
        elapsed = round(time.time() - started, 3)
        record = {
            "method": method,
            "path": path,
            "status": code,
            "elapsed_s": elapsed,
            "payload": payload,
            "response_head": body[:600],
        }
        self.steps.append(record)
        if code not in expect:
            raise StepError(f"{method} {path} -> {code}，期望 {expect}；响应: {body[:500]}")
        try:
            return json.loads(body) if body else {}
        except json.JSONDecodeError:
            return {"_raw": body}

    def get(self, path: str, **kw) -> dict:
        return self.call("GET", path, None, **kw)

    def post(self, path: str, payload: dict, **kw) -> dict:
        return self.call("POST", path, payload, **kw)


def stock_quantity(client: Client, part_id: int) -> float:
    data = client.get(f"/api/stock/?part={part_id}&limit=200")
    total = 0.0
    for item in data.get("results", []):
        total += float(item.get("quantity") or 0)
    return total


def run(base: str, token: str) -> dict:
    c = Client(base, token)
    stamp = datetime.now().strftime("%H%M%S")
    checks: list[dict] = []

    def check(name: str, actual, expected):
        ok = actual == expected
        checks.append({"check": name, "actual": actual, "expected": expected, "ok": ok})
        if not ok:
            raise StepError(f"断言失败 {name}: actual={actual} expected={expected}")

    # 0. 身份与基础数据
    me = c.get("/api/user/me/")
    locations = c.get("/api/stock/location/?limit=50")
    root = next(
        (loc for loc in locations.get("results", []) if not loc.get("structural")), None
    )
    if root is None:
        raise StepError("没有可用的非结构库位，无法收货")

    # 1. 零件
    category = c.post("/api/part/category/", {"name": f"GP-CAT-{stamp}", "description": "golden path"})
    assembly = c.post(
        "/api/part/",
        {
            "name": f"GP-ASSY-{stamp}",
            "category": category["pk"],
            "description": "golden path assembly",
            "assembly": True,
            "purchaseable": False,
            "trackable": False,
            "active": True,
        },
    )
    component = c.post(
        "/api/part/",
        {
            "name": f"GP-COMP-{stamp}",
            "category": category["pk"],
            "description": "golden path component",
            "assembly": False,
            "purchaseable": True,
            "trackable": False,
            "active": True,
        },
    )

    # 2. 供应商与供应商物料
    supplier = c.post(
        "/api/company/", {"name": f"GP-SUP-{stamp}", "is_supplier": True, "is_customer": False}
    )
    supplier_part = c.post(
        "/api/company/part/",
        {"part": component["pk"], "supplier": supplier["pk"], "SKU": f"GP-SKU-{stamp}"},
    )

    # 3. 采购订单 + 下单 + 收货
    po = c.post(
        "/api/order/po/",
        {"supplier": supplier["pk"], "reference": f"GP-PO-{stamp}", "description": "golden path"},
    )
    line = c.post(
        "/api/order/po-line/",
        {"order": po["pk"], "part": supplier_part["pk"], "quantity": 100},
    )
    c.post(f"/api/order/po/{po['pk']}/issue/", {})
    received = c.post(
        f"/api/order/po/{po['pk']}/receive/",
        {"items": [{"line_item": line["pk"], "quantity": 100}], "location": root["pk"]},
    )
    check("收货生成库存条目", len(received) if isinstance(received, list) else 1, 1)
    check("元件到货数量", stock_quantity(c, component["pk"]), 100.0)
    po_after = c.get(f"/api/order/po/{po['pk']}/")
    check("采购订单状态=完成", po_after["status"], PO_COMPLETE)

    # 4. BOM：一个装配件消耗 2 个元件
    c.post("/api/bom/", {"part": assembly["pk"], "sub_part": component["pk"], "quantity": 2})

    # 5. 生产工单：做 5 个装配件，消耗 10 个元件
    build = c.post(
        "/api/build/",
        {
            "part": assembly["pk"],
            "reference": f"GP-BO-{stamp}",
            "quantity": 5,
            "title": "golden path build",
        },
    )
    build_lines = c.get(f"/api/build-line/?build={build['pk']}")
    if not build_lines.get("results"):
        raise StepError("工单没有生成 build line，BOM 可能未生效")
    build_line_id = build_lines["results"][0]["pk"]

    stock_items = c.get(f"/api/stock/?part={component['pk']}&limit=10")["results"]
    stock_item_id = stock_items[0]["pk"]

    c.post(
        f"/api/build/{build['pk']}/issue/",
        {},
    )
    c.post(
        f"/api/build/{build['pk']}/allocate/",
        {"items": [{"build_line": build_line_id, "stock_item": stock_item_id, "quantity": 10}]},
    )
    c.post(f"/api/build/{build['pk']}/finish/", {})

    # 6. db_delta 断言
    build_after = c.get(f"/api/build/{build['pk']}/")
    check("工单状态=完成", build_after["status"], BUILD_COMPLETE)
    check("元件库存扣减", stock_quantity(c, component["pk"]), 90.0)
    check("产出库存增加", stock_quantity(c, assembly["pk"]), 5.0)

    return {
        "journey": "golden_path.pcb_assembly",
        "base_url": base,
        "actor": me.get("username"),
        "steps": c.steps,
        "checks": checks,
        "ids": {
            "category": category["pk"],
            "assembly_part": assembly["pk"],
            "component_part": component["pk"],
            "supplier": supplier["pk"],
            "supplier_part": supplier_part["pk"],
            "purchase_order": po["pk"],
            "build_order": build["pk"],
        },
        "result": "PASS" if all(x["ok"] for x in checks) else "FAIL",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--token", required=True)
    ap.add_argument("--out", default="5.evidence/runs/l3-golden/run.json")
    args = ap.parse_args()

    started = datetime.now(timezone.utc)
    try:
        result = run(args.base_url, args.token)
    except StepError as exc:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                {
                    "journey": "golden_path.pcb_assembly",
                    "result": "FAIL",
                    "error": str(exc),
                    "generated_at": started.isoformat(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"KS_L3\tFAIL\t{exc}")
        return 1

    result["generated_at"] = started.isoformat()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"KS_L3\t{result['result']}\tsteps={len(result['steps'])}\tchecks={len(result['checks'])}")
    for chk in result["checks"]:
        print(f"  {chk['check']}: {chk['actual']} (期望 {chk['expected']})")
    return 0 if result["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
