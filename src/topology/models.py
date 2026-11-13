#!/usr/bin/env python3
"""
models.py — every record the topology skill reads or writes, in one place.

  topology.toml                 Topology, holding Machine, Backend, LLM,
                                Agent and Benchmark rows
  topology-playbook*.toml       Playbook, holding Task rows
  error-reports/*.toml          FailureReport

topology.toml (schema_version 2) stores each kind of record as its own flat
table, one row per thing:

  [[machines]]    one per host
  [[backends]]    one per inference server on a host (ollama, llama-server,
                  ggufs), kept even when it is down or serves nothing
  [[llms]]        one per LLM, naming the host and backend that serve it
  [[agents]]      one per agent runtime on a host (goose, hermes, ...)
  [[benchmarks]]  one per timing of an LLM on a host

Backends, LLMs and agents belong to a machine, linked by `hostname`; an LLM
also names its `backend`. The flat tables are the single source of truth —
Topology's lookup methods (backends_on, llms_on, ...) follow those links, so
a row whose host is not in [[machines]] is still kept and written back.

Round-tripping is lossless: `from_dict(d).to_dict() == d`. Optional fields
default to None and are left out of to_dict() when unset. Keys a model does
not know — such as fields a dependent skill adds — are kept in `extra`.
Fields marked runtime-only (`_runtime()`) describe a loaded record and are
never written by to_dict().

Schema version 1 stored backends and their LLMs together as [[model_state]]
rows and agents as [[agent_state]]; upgrade_v1() converts it.
"""

from dataclasses import MISSING, dataclass, field, fields
from typing import Any, ClassVar, Self

from .toml_io import read_toml

SCHEMA_VERSION = 2


def _runtime(default: Any) -> Any:
    """A field set while the program runs, never written back to the file."""
    return field(default=default, metadata={'runtime': True})


@dataclass
class Record:
    """Base for every model: conversion to and from the dicts TOML gives."""

    # Nested record lists, by field name, so from_dict can build them.
    NESTED: ClassVar[dict[str, type['Record']]] = {}

    extra: dict[str, Any] = field(default_factory=dict, kw_only=True)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        known = {f.name for f in fields(cls)} - {'extra'}
        values: dict[str, Any] = {}
        for key, value in data.items():
            if key not in known:
                continue
            if key in cls.NESTED:
                value = [cls.NESTED[key].from_dict(row) for row in value]
            values[key] = value
        missing = [
            f.name for f in fields(cls)
            if f.default is MISSING and f.default_factory is MISSING and f.name not in values
        ]
        if missing:
            raise ValueError(f'{cls.__name__} is missing {", ".join(missing)}')
        extra = {k: v for k, v in data.items() if k not in known}
        return cls(**values, extra=extra)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for f in fields(self):
            if f.name == 'extra' or f.metadata.get('runtime'):
                continue
            value = getattr(self, f.name)
            if value is None:
                continue
            if f.name in self.NESTED:
                value = [row.to_dict() for row in value]
            out[f.name] = value
        out.update(self.extra)
        return out


# ── topology.toml ────────────────────────────────────────────────────────────

@dataclass
class Machine(Record):
    """A [[machines]] row: one host, as written by init, sync and discover.

    Dependent skills add their own per-host fields here (ask-agent-skill's
    hermes_gateway and hermes_key_env, for example); those land in `extra`.
    """
    name: str
    hostname: str
    ssh: bool | None = None
    ssh_user: str | None = None
    local_ip: str | None = None
    tailscale_ip: str | None = None
    os: str | None = None
    gpu: str | None = None
    vram: str | None = None
    goose_acp_url: str | None = None
    last_verified: str | None = None


@dataclass
class Backend(Record):
    """A [[backends]] row: one inference server on one machine.

    `name` is the server kind: ollama, llama-server, or ggufs (GGUF files
    found on disk, which is not a server, so it has no port).
    `context_window` is per backend because that is where discover reads it.
    """
    hostname: str
    name: str
    status: str
    port: int | None = None
    context_window: int | None = None
    last_seen: str | None = None


@dataclass
class LLM(Record):
    """An [[llms]] row: one LLM, served by `backend` on `hostname`."""
    hostname: str
    backend: str
    name: str


@dataclass
class Agent(Record):
    """An [[agents]] row: one agent runtime (goose, hermes, ...) on one machine.

    `reasoning_buffer` is set by ask-agent-skill and read by track-tasks-skill;
    discover keeps it across runs.
    """
    hostname: str
    name: str
    status: str
    endpoint: str | None = None
    process: str | None = None
    last_seen: str | None = None
    reasoning_buffer: int | None = None


@dataclass
class Benchmark(Record):
    """A [[benchmarks]] row: timing for one LLM on one machine (benchmark_llm)."""
    hostname: str
    llm: str
    timestamp: str
    ttft_ms: float
    tok_s: float
    runs: int


@dataclass
class Topology(Record):
    """The whole of topology.toml."""
    NESTED: ClassVar[dict[str, type[Record]]] = {
        'machines': Machine,
        'backends': Backend,
        'llms': LLM,
        'agents': Agent,
        'benchmarks': Benchmark,
    }

    schema_version: int = SCHEMA_VERSION
    provider: str | None = None
    last_refreshed: str | None = None
    machines: list[Machine] = field(default_factory=list)
    backends: list[Backend] = field(default_factory=list)
    llms: list[LLM] = field(default_factory=list)
    agents: list[Agent] = field(default_factory=list)
    benchmarks: list[Benchmark] = field(default_factory=list)

    def machine(self, host: str) -> Machine | None:
        """The machine whose hostname or name is `host`."""
        return next((m for m in self.machines if host in (m.hostname, m.name)), None)

    def backends_on(self, hostname: str) -> list[Backend]:
        return [b for b in self.backends if b.hostname == hostname]

    def llms_on(self, hostname: str, backend: str | None = None) -> list[LLM]:
        """LLMs on a machine, optionally only those one backend serves."""
        return [l for l in self.llms
                if l.hostname == hostname and (backend is None or l.backend == backend)]

    def agents_on(self, hostname: str) -> list[Agent]:
        return [a for a in self.agents if a.hostname == hostname]

    def backend_of(self, llm: LLM) -> Backend | None:
        """The backend serving an LLM."""
        return next((b for b in self.backends
                     if b.hostname == llm.hostname and b.name == llm.backend), None)

    def benchmarks_for(self, llm: LLM) -> list[Benchmark]:
        return [b for b in self.benchmarks if b.hostname == llm.hostname and b.llm == llm.name]


def upgrade_v1(data: dict[str, Any]) -> dict[str, Any]:
    """Convert a schema_version 1 topology dict to version 2.

    Each [[model_state]] row becomes one [[backends]] row plus one [[llms]]
    row per entry in its `models`; [[agent_state]] becomes [[agents]] with
    `agent` renamed to `name`; a benchmark's `model` becomes `llm`. Other
    keys, including dependent skills' fields, are carried over unchanged.
    A dict already at version 2 is returned as is.
    """
    if data.get('schema_version', 1) >= SCHEMA_VERSION:
        return data
    out = {k: v for k, v in data.items() if k not in ('model_state', 'agent_state', 'benchmarks')}
    out['schema_version'] = SCHEMA_VERSION

    backends, llms = [], []
    for row in data.get('model_state', []):
        row = dict(row)
        models = row.pop('models', [])
        backend = {'hostname': row.pop('hostname'), 'name': row.pop('backend'), **row}
        backends.append(backend)
        llms += [{'hostname': backend['hostname'], 'backend': backend['name'], 'name': m} for m in models]
    out['backends'] = backends
    out['llms'] = llms

    out['agents'] = [
        {'hostname': row['hostname'], 'name': row['agent'],
         **{k: v for k, v in row.items() if k not in ('hostname', 'agent')}}
        for row in data.get('agent_state', [])
    ]
    out['benchmarks'] = [
        {'hostname': row['hostname'], 'llm': row['model'],
         **{k: v for k, v in row.items() if k not in ('hostname', 'model')}}
        for row in data.get('benchmarks', [])
    ]
    return out


def read_topology_data(path: str) -> dict[str, Any]:
    """topology.toml as a dict at the current schema version, for code that rewrites the file."""
    return upgrade_v1(read_toml(path))


def read_topology(path: str) -> Topology:
    """topology.toml as a Topology, upgrading a version 1 file in memory."""
    return Topology.from_dict(read_topology_data(path))


# ── topology-playbook*.toml ──────────────────────────────────────────────────

@dataclass
class Task(Record):
    """A [[playbook.tasks]] row: one step of a playbook.

    Either a command task (`hosts` and `command`) or a reference to another
    playbook (`ref`), never both — playbook._validate_task enforces that.
    `oversight` left as None means "decide by keyword heuristic".

    `last_run` (UTC timestamp) and `frequency` (successful runs) record how
    the task has been used. They are runtime-only: playbook files are
    hand-written and commented, so usage is never written back into them.
    """
    name: str | None = None
    hosts: str | None = None
    command: str | None = None
    ref: str | None = None
    oversight: bool | None = None
    last_run: str | None = _runtime(None)
    frequency: int = _runtime(0)


@dataclass
class Playbook(Record):
    """A [[playbook]] entry: a named, alias-tagged list of tasks."""
    NESTED: ClassVar[dict[str, type[Record]]] = {'tasks': Task}

    name: str
    aliases: list[str] = field(default_factory=list)
    description: str | None = None
    tasks: list[Task] = field(default_factory=list)
    # The file this playbook was loaded from.
    source: str | None = _runtime(None)


# ── error-reports/*.toml ─────────────────────────────────────────────────────

@dataclass
class FailureReport(Record):
    """A report written by playbook.write_failure_report when a task fails."""
    playbook: str
    task: str
    hosts: str
    command: str
    exit_code: int
    timestamp: str
    stdout: str = ''
    stderr: str = ''
