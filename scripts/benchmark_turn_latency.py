from __future__ import annotations

import argparse
import json
import pathlib
import statistics
import sys
import time
from unittest import mock

from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import backend.main as backend_main


FAST_INTENTS = [
    "what time is it",
    "what's the weather where i am",
    "pause music",
]

COMPLEX_INTENTS = [
    "show me nike running shoes size 8.5",
    "search web local-first operating system",
    "book a flight to new york next tuesday, add it to calendar, and message sarah",
]


def percentile(values: list[float], pct: int) -> float:
    if not values:
        return 0.0
    picked = sorted(values)
    idx = min(len(picked) - 1, max(0, int(len(picked) * (pct / 100.0)) - 1))
    return float(picked[idx])


def parse_ms(headers: dict[str, str], name: str) -> float:
    try:
        return float(headers.get(name.lower(), headers.get(name, "0")) or 0)
    except (TypeError, ValueError):
        return 0.0


def benchmark_group(client: TestClient, session_id: str, intents: list[str], runs: int, warmup: int) -> dict[str, object]:
    client.post("/api/session/init", json={"sessionId": session_id})
    samples: list[dict[str, object]] = []
    total_iterations = warmup + runs
    for idx in range(total_iterations):
        intent = intents[idx % len(intents)]
        started = time.perf_counter()
        resp = client.post("/api/turn", json={"sessionId": session_id, "intent": intent})
        wall_ms = (time.perf_counter() - started) * 1000.0
        if idx < warmup:
            continue
        headers = {str(key).lower(): str(value) for key, value in resp.headers.items()}
        body = resp.json()
        samples.append(
            {
                "intent": intent,
                "status": int(resp.status_code),
                "wallMs": round(wall_ms, 2),
                "classifyMs": parse_ms(headers, "X-Genome-Classify-Ms"),
                "nousMs": parse_ms(headers, "X-Genome-Nous-Ms"),
                "parseMs": parse_ms(headers, "X-Genome-Parse-Ms"),
                "totalMs": parse_ms(headers, "X-Genome-Total-Ms"),
                "nousParseMs": parse_ms(headers, "X-Nous-Parse-Ms"),
                "classifySource": str(headers.get("x-genome-classify-source", "")),
                "route": str((body.get("route") or {}).get("reason", "")),
            }
        )

    total_ms = [float(item["totalMs"]) for item in samples]
    wall_ms = [float(item["wallMs"]) for item in samples]
    classify_ms = [float(item["classifyMs"]) for item in samples]
    nous_ms = [float(item["nousMs"]) for item in samples]
    sources: dict[str, int] = {}
    for item in samples:
        key = str(item.get("classifySource", "") or "unknown")
        sources[key] = sources.get(key, 0) + 1

    return {
        "runs": runs,
        "warmup": warmup,
        "sources": sources,
        "statusCodes": sorted({int(item["status"]) for item in samples}),
        "totalMs": {
            "avg": round(statistics.fmean(total_ms), 2) if total_ms else 0.0,
            "p50": round(percentile(total_ms, 50), 2),
            "p95": round(percentile(total_ms, 95), 2),
            "max": round(max(total_ms) if total_ms else 0.0, 2),
        },
        "wallMs": {
            "avg": round(statistics.fmean(wall_ms), 2) if wall_ms else 0.0,
            "p50": round(percentile(wall_ms, 50), 2),
            "p95": round(percentile(wall_ms, 95), 2),
            "max": round(max(wall_ms) if wall_ms else 0.0, 2),
        },
        "classifyMs": {
            "avg": round(statistics.fmean(classify_ms), 2) if classify_ms else 0.0,
            "p95": round(percentile(classify_ms, 95), 2),
        },
        "nousMs": {
            "avg": round(statistics.fmean(nous_ms), 2) if nous_ms else 0.0,
            "p95": round(percentile(nous_ms, 95), 2),
        },
        "samples": samples,
    }


def print_summary(name: str, summary: dict[str, object]) -> None:
    total = summary.get("totalMs", {}) if isinstance(summary.get("totalMs"), dict) else {}
    wall = summary.get("wallMs", {}) if isinstance(summary.get("wallMs"), dict) else {}
    classify = summary.get("classifyMs", {}) if isinstance(summary.get("classifyMs"), dict) else {}
    nous = summary.get("nousMs", {}) if isinstance(summary.get("nousMs"), dict) else {}
    sources = summary.get("sources", {}) if isinstance(summary.get("sources"), dict) else {}
    print(f"{name}:")
    print(f"  total ms  avg/p50/p95/max: {total.get('avg', 0)}/{total.get('p50', 0)}/{total.get('p95', 0)}/{total.get('max', 0)}")
    print(f"  wall  ms  avg/p50/p95/max: {wall.get('avg', 0)}/{wall.get('p50', 0)}/{wall.get('p95', 0)}/{wall.get('max', 0)}")
    print(f"  classify avg/p95: {classify.get('avg', 0)}/{classify.get('p95', 0)}")
    print(f"  nous     avg/p95: {nous.get('avg', 0)}/{nous.get('p95', 0)}")
    print(f"  classify sources: {json.dumps(sources, sort_keys=True)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark /api/turn latency using in-process TestClient.")
    parser.add_argument("--runs", type=int, default=20, help="Measured requests per cohort.")
    parser.add_argument("--warmup", type=int, default=3, help="Warmup requests per cohort.")
    parser.add_argument("--out", type=pathlib.Path, default=None, help="Optional JSON output path.")
    args = parser.parse_args()

    with mock.patch.object(backend_main._auth, "session_valid", return_value=True):
        with TestClient(backend_main.app) as client:
            fast = benchmark_group(client, f"bench-fast-{int(time.time())}", FAST_INTENTS, args.runs, args.warmup)
            complex_ = benchmark_group(client, f"bench-complex-{int(time.time())}", COMPLEX_INTENTS, args.runs, args.warmup)

    report = {
        "generatedAt": int(time.time() * 1000),
        "runs": int(args.runs),
        "warmup": int(args.warmup),
        "targets": {
            "fastPathP95Ms": 300,
            "nousPathP95Ms": 500,
        },
        "fastPath": fast,
        "complexPath": complex_,
    }

    print_summary("fast-path intents", fast)
    print_summary("complex intents", complex_)
    print(
        "targets: "
        f"fast-path p95 <= {report['targets']['fastPathP95Ms']}ms | "
        f"complex p95 <= {report['targets']['nousPathP95Ms']}ms"
    )

    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
