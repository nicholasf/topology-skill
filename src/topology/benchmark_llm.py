#!/usr/bin/env python3
"""Benchmark an LLM node and record stats in topology.toml."""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

from .models import read_topology_data
from .sync import get_topology_path
from .toml_io import write_toml

BENCHMARK_PROMPT = "Explain the concept of recursion in programming in two sentences."


def run_single(hostname: str, port: int, model: str) -> tuple[float, float, float]:
    """Stream one completion request; return (ttft_ms, tok_s, total_ms)."""
    url = f"http://{hostname}:{port}/v1/chat/completions"
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": BENCHMARK_PROMPT}],
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_tokens": 200,
    }).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})

    t0 = time.perf_counter()
    ttft_ms = None
    completion_tokens = 0

    with urllib.request.urlopen(req, timeout=60) as resp:
        while True:
            raw = resp.readline()
            if not raw:
                break
            line = raw.decode().strip()
            if not line.startswith("data: "):
                continue
            payload = line[6:]
            if payload == "[DONE]":
                break
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                continue

            if ttft_ms is None:
                choices = obj.get("choices", [])
                if choices and choices[0].get("delta", {}).get("content"):
                    ttft_ms = (time.perf_counter() - t0) * 1000

            if obj.get("usage"):
                completion_tokens = obj["usage"].get("completion_tokens", 0)

    total_ms = (time.perf_counter() - t0) * 1000
    gen_ms = total_ms - (ttft_ms or 0)
    tok_s = round(completion_tokens / (gen_ms / 1000), 1) if completion_tokens and gen_ms > 0 else 0.0
    return round(ttft_ms or 0, 1), tok_s, round(total_ms, 1)


def run_benchmark(hostname: str, port: int, model: str, runs: int) -> tuple[float, float]:
    """Run N passes; return (avg_ttft_ms, avg_tok_s)."""
    ttfts, tok_ss = [], []
    for i in range(runs):
        print(f"  run {i + 1}/{runs}...", flush=True)
        ttft, tok_s, total = run_single(hostname, port, model)
        ttfts.append(ttft)
        tok_ss.append(tok_s)
        print(f"    ttft={ttft:.0f}ms  tok/s={tok_s:.1f}  total={total:.0f}ms")
    return round(sum(ttfts) / len(ttfts), 1), round(sum(tok_ss) / len(tok_ss), 1)


def record_result(
    topology_path: str, hostname: str, llm: str,
    ttft_ms: float, tok_s: float, runs: int,
) -> None:
    data = read_topology_data(topology_path)
    rows = data.get('benchmarks', [])

    new_row = {
        'hostname': hostname,
        'llm': llm,
        'timestamp': datetime.now().strftime('%Y-%m-%dT%H-%M-%S'),
        'ttft_ms': ttft_ms,
        'tok_s': tok_s,
        'runs': runs,
    }

    rows = [r for r in rows if not (r['hostname'] == hostname and r['llm'] == llm)]
    rows.append(new_row)
    rows.sort(key=lambda r: (r['hostname'], r['llm']))

    data['benchmarks'] = rows
    write_toml(data, topology_path)

    print(f"Recorded in {topology_path}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Benchmark an LLM node via OpenAI-compat API")
    parser.add_argument('hostname', help="target machine hostname or IP")
    parser.add_argument('model', help="model name as reported by the server")
    parser.add_argument('--port', type=int, default=9337)
    parser.add_argument('--runs', type=int, default=3)
    args = parser.parse_args(argv)

    print(f"Benchmarking {args.model} on {args.hostname}:{args.port} ({args.runs} runs)")
    print(f"Prompt: {BENCHMARK_PROMPT!r}\n")

    try:
        avg_ttft, avg_tok_s = run_benchmark(args.hostname, args.port, args.model, args.runs)
    except urllib.error.URLError as e:
        print(f"Connection error: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"\nAverage:  ttft={avg_ttft}ms  tok/s={avg_tok_s}")

    topology_path = get_topology_path()
    if os.path.exists(topology_path):
        record_result(topology_path, args.hostname, args.model, avg_ttft, avg_tok_s, args.runs)
    else:
        print(f"Warning: topology not found at {topology_path}, results not saved")


if __name__ == '__main__':
    main()
