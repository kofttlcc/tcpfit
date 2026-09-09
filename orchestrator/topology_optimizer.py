#!/usr/bin/env python3
"""Bounded, topology-aware TCP configuration search for tcpfit.

The engine deliberately keeps host configuration and workload configuration
separate.  System interaction is behind small adapters so selection and
rollback behaviour can be tested without privileged or remote hosts.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import signal
import statistics
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable

@dataclass(frozen=True)
class Peer:
    name: str
    host: str
    role: str
    port: int = 5201


@dataclass(frozen=True)
class Path:
    name: str
    upstream: str
    downstream: str
    required: bool = True


@dataclass(frozen=True)
class Workload:
    seconds: int = 10
    warmup_seconds: int = 2
    parallel: int = 2
    repeats: int = 3
    validation_seconds: int = 20
    interval_seconds: float = 1.0


@dataclass(frozen=True)
class Policy:
    goodput_tolerance: float = 0.05
    path_regression: float = 0.10
    retrans_target: float = 0.01
    max_cv: float = 0.15
    min_improvement: float = 0.02
    max_candidates: int = 12
    baseline_every: int = 4


@dataclass(frozen=True)
class HostConfig:
    congestion: str
    qdisc: str
    buffer_bytes: int
    backlog: int

    @property
    def name(self) -> str:
        return f"{self.congestion}/{self.qdisc}/buf={self.buffer_bytes}/backlog={self.backlog}"


@dataclass
class Sample:
    path: str
    direction: str
    goodput_mbps: float | None
    retransmits: int | None
    packets: int | None
    latency_ms: float | None = None
    jitter_ms: float | None = None
    cpu_percent: float | None = None
    note: str = ""

    @property
    def retrans_rate(self) -> float | None:
        if self.retransmits is None or not self.packets:
            return None
        return self.retransmits / self.packets


@dataclass
class Trial:
    config: HostConfig
    samples: list[Sample]
    phase: str
    valid: bool = True
    reason: str = ""

    def path_goodput(self) -> dict[str, float]:
        groups: dict[str, list[float]] = {}
        for s in self.samples:
            if s.goodput_mbps is not None:
                groups.setdefault(s.path, []).append(s.goodput_mbps)
        return {p: statistics.median(v) for p, v in groups.items()}

    def goodput(self) -> float:
        values = list(self.path_goodput().values())
        return statistics.harmonic_mean(values) if values and all(values) else 0.0

    def retrans_rate(self) -> float | None:
        known = [(s.retransmits, s.packets) for s in self.samples
                 if s.retransmits is not None and s.packets]
        if not known:
            return None
        return sum(x for x, _ in known) / sum(n for _, n in known)

    def cv(self) -> float:
        values = [s.goodput_mbps for s in self.samples if s.goodput_mbps is not None]
        return statistics.pstdev(values) / statistics.mean(values) if len(values) > 1 and statistics.mean(values) else 0.0


def validate_topology(peers: list[Peer], paths: list[Path], node_mode: str) -> None:
    if node_mode not in {"router", "proxy", "tunnel", "terminator"}:
        raise ValueError("node_mode 必須是 router/proxy/tunnel/terminator")
    if len(peers) > 1 and any(p.role not in {"upstream", "downstream"} for p in peers):
        raise ValueError("多對端必須明確指定 upstream/downstream，不可推測")
    names = {p.name for p in peers}
    if len(peers) > 2 and not paths:
        raise ValueError("超過兩個對端必須提供 paths 配對關係")
    for path in paths:
        if path.upstream not in names or path.downstream not in names:
            raise ValueError(f"路徑 {path.name} 引用不存在的對端")
        roles = {p.name: p.role for p in peers}
        if roles[path.upstream] != "upstream" or roles[path.downstream] != "downstream":
            raise ValueError(f"路徑 {path.name} 方向與對端角色不符")


def choose(trials: Iterable[Trial], baseline: Trial, required_paths: set[str], policy: Policy) -> Trial:
    """Select near-best goodput first, then retransmission/stability/resource use."""
    base_paths = baseline.path_goodput()
    eligible: list[Trial] = []
    for trial in trials:
        paths = trial.path_goodput()
        missing = required_paths - paths.keys()
        if not trial.valid or missing or trial.cv() > policy.max_cv:
            continue
        if any(paths[p] < base_paths.get(p, paths[p]) * (1 - policy.path_regression) for p in required_paths):
            continue
        eligible.append(trial)
    if not eligible:
        return baseline
    best_goodput = max(t.goodput() for t in eligible)
    near = [t for t in eligible if t.goodput() >= best_goodput * (1 - policy.goodput_tolerance)]
    near.sort(key=lambda t: (t.retrans_rate() is None,
                             t.retrans_rate() if t.retrans_rate() is not None else math.inf,
                             t.cv(),
                             statistics.mean([s.cpu_percent for s in t.samples if s.cpu_percent is not None])
                             if any(s.cpu_percent is not None for s in t.samples) else math.inf))
    winner = near[0]
    # Measurement noise wins over tiny apparent changes. Keep the original state.
    if winner.goodput() < baseline.goodput() * (1 + policy.min_improvement):
        br, wr = baseline.retrans_rate(), winner.retrans_rate()
        if br is None or wr is None or wr >= br * (1 - policy.min_improvement):
            return baseline
    return winner


def detect_facts() -> dict:
    def read(path: str) -> str | None:
        try:
            return Path(path).read_text().strip()
        except OSError:
            return None

    def command(*cmd: str) -> str | None:
        try:
            return subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL, timeout=5).strip()
        except (OSError, subprocess.SubprocessError):
            return None

    mem_kb = next((x.split()[1] for x in (read("/proc/meminfo") or "").splitlines() if x.startswith("MemAvailable:")), None)
    quota = read("/sys/fs/cgroup/cpu.max") or read("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
    mem_limit = read("/sys/fs/cgroup/memory.max") or read("/sys/fs/cgroup/memory/memory.limit_in_bytes")
    route = command("ip", "route", "show", "default") or ""
    iface = route.split(" dev ", 1)[1].split()[0] if " dev " in route else None
    net = {}
    if iface:
        base = Path("/sys/class/net") / iface
        net = {"interface": iface, "mtu": read(str(base / "mtu")),
               "queues": len(list((base / "queues").glob("rx-*"))),
               "statistics": {k: read(str(base / "statistics" / k)) for k in
                              ("rx_errors", "tx_errors", "rx_dropped", "tx_dropped")},
               "offload": command("ethtool", "-k", iface)}
    stat = (read("/proc/stat") or "").splitlines()
    cpu = next((x.split() for x in stat if x.startswith("cpu ")), [])
    steal = int(cpu[8]) if len(cpu) > 8 else None
    return {
        "observed": {"architecture": command("uname", "-m"), "kernel": command("uname", "-r"),
                     "os": read("/etc/os-release"), "available_cores": os.cpu_count(),
                     "loadavg": read("/proc/loadavg"), "steal_ticks": steal,
                     "mem_available_kb": int(mem_kb) if mem_kb else None,
                     "cpu_cgroup_limit": quota, "memory_cgroup_limit": mem_limit,
                     "congestion_available": command("sysctl", "-n", "net.ipv4.tcp_available_congestion_control"),
                     "qdisc_available": read("/proc/net/psched"), "network": net},
        "declared_not_measured": {"link_speed": "ignored: virtual NIC speed is not usable bandwidth"},
        "unavailable": [k for k, v in {"steal_ticks": steal, "interface": iface,
                                          "offload": net.get("offload")}.items() if v is None],
    }


class Configurator:
    KEYS = ("net.ipv4.tcp_congestion_control", "net.core.default_qdisc", "net.core.rmem_max",
            "net.core.wmem_max", "net.core.netdev_max_backlog")

    def __init__(self, run: Callable[..., subprocess.CompletedProcess] = subprocess.run):
        self.run = run
        self.original: dict[str, str] = {}

    def snapshot(self) -> dict[str, str]:
        for key in self.KEYS:
            p = self.run(["sysctl", "-n", key], capture_output=True, text=True)
            if p.returncode == 0:
                self.original[key] = p.stdout.strip()
        return self.original.copy()

    def values(self, cfg: HostConfig) -> dict[str, str]:
        return {"net.ipv4.tcp_congestion_control": cfg.congestion,
                "net.core.default_qdisc": cfg.qdisc, "net.core.rmem_max": str(cfg.buffer_bytes),
                "net.core.wmem_max": str(cfg.buffer_bytes), "net.core.netdev_max_backlog": str(cfg.backlog)}

    def apply(self, values: dict[str, str]) -> tuple[bool, list[str]]:
        errors = []
        for key, value in values.items():
            p = self.run(["sysctl", "-w", f"{key}={value}"], capture_output=True, text=True)
            if p.returncode:
                errors.append(f"{key}: {p.stderr.strip() or 'unsupported/permission denied'}")
                continue
            check = self.run(["sysctl", "-n", key], capture_output=True, text=True)
            if check.returncode or check.stdout.strip() != value:
                errors.append(f"{key}: readback mismatch")
        return not errors, errors

    def restore(self) -> tuple[bool, list[str]]:
        return self.apply(self.original)

    def persist(self, cfg: HostConfig, path: Path) -> None:
        body = "# generated by tcpfit topology optimizer; validated as one global configuration\n"
        body += "\n".join(f"{k} = {v}" for k, v in self.values(cfg).items()) + "\n"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(body)
        os.replace(tmp, path)


def candidates(facts: dict, original: HostConfig, policy: Policy) -> list[HostConfig]:
    observed = facts["observed"]
    cc = (observed.get("congestion_available") or original.congestion).split()
    memory_kb = observed.get("mem_available_kb") or 262144
    cgroup = observed.get("memory_cgroup_limit")
    if cgroup and cgroup.isdigit() and int(cgroup) < 2**60:
        memory_kb = min(memory_kb, int(cgroup) // 1024)
    # At most 1/16 of available memory per socket ceiling; never below 1 MiB.
    ceiling = max(1 << 20, min(64 << 20, memory_kb * 1024 // 16))
    bufs = sorted({min(original.buffer_bytes, ceiling), min(8 << 20, ceiling), min(16 << 20, ceiling)})
    qdiscs = [original.qdisc] + (["fq"] if "bbr" in cc else ["fq_codel"])
    pool = [HostConfig(c, q, b, backlog) for c in cc for q in dict.fromkeys(qdiscs)
            for b in bufs for backlog in (1000, 5000)]
    pool = list(dict.fromkeys(pool))
    rng = random.Random(0)  # repeatable candidate order; baseline rechecks expose time drift
    rng.shuffle(pool)
    return [original] + [p for p in pool if p != original][:max(0, policy.max_candidates - 1)]


def load_spec(path: Path) -> tuple[list[Peer], list[Path], str, Workload, Policy]:
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("讀取 YAML 需要 PyYAML（apt install python3-yaml 或 pip install PyYAML）") from exc
    data = yaml.safe_load(path.read_text()) or {}
    peers = [Peer(**x) for x in data.get("peers", [])]
    paths = [Path(**x) for x in data.get("paths", [])]
    workload = Workload(**data.get("workload", {}))
    policy = Policy(**data.get("policy", {}))
    mode = data.get("node_mode")
    validate_topology(peers, paths, mode)
    return peers, paths, mode, workload, policy


def main() -> int:
    ap = argparse.ArgumentParser(description="tcpfit 多對端聯合、有界配置搜尋")
    ap.add_argument("spec", type=Path)
    ap.add_argument("--facts-only", action="store_true")
    ap.add_argument("--output", type=Path, default=Path("results/topology-run.json"))
    args = ap.parse_args()
    peers, paths, mode, workload, policy = load_spec(args.spec)
    facts = detect_facts()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Measurement transport is intentionally explicit rather than pretending that
    # segment iperf is end-to-end proof. A production run needs a workload driver
    # at both ends of every path; facts-only provides a safe preflight everywhere.
    record = {"facts": facts, "node_mode": mode, "peers": [asdict(p) for p in peers],
              "paths": [asdict(p) for p in paths], "workload": asdict(workload),
              "policy": asdict(policy), "end_to_end_verified": False,
              "limitations": ["尚未提供業務路徑 workload driver；未執行或套用候選配置"]}
    args.output.write_text(json.dumps(record, ensure_ascii=False, indent=2))
    print(json.dumps(record, ensure_ascii=False, indent=2))
    if not args.facts_only:
        print("拒絕以各段測速冒充端到端驗證；目前請使用 --facts-only", file=os.sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
