#!/usr/bin/env python3
"""
models.py — every record the topology skill reads or writes, in one place.

  topology.toml                 Topology, holding Machine, LLMProvider, LLM,
                                Agent, GGUF and Benchmark rows
  topology-playbook*.toml       Playbook, holding Task rows
  error-reports/*.toml          FailureReport

topology.toml (schema_version 2) stores each kind of record as its own flat
table, one row per thing:

  [[machines]]       one per host
  [[llm_providers]]  one per LLM server on a host (ollama, llama-server),
                     kept even when it is down or serves nothing
  [[llms]]           one per LLM, naming the host and provider that serve it
  [[agents]]         one per agent runtime on a host (goose, hermes, ...)
  [[ggufs]]          one per GGUF file on a host
  [[benchmarks]]     one per timing of an LLM on a host

Two base types group the tables. Server (LLMProvider, Agent) is a process
that listens for requests; FileResource (GGUF) is a file on disk. Each
subtype has its own table, so every row in a table has the same fields.

Everything belongs to a machine, linked by `hostname`; an LLM also names its
`provider`. The flat tables are the single source of truth — Topology's
lookup methods (servers_on, llms_on, ...) follow those links, so
a row whose host is not in [[machines]] is still kept and written back.

Round-tripping is lossless: `from_dict(d).to_dict() == d`. Optional fields
default to None and are left out of to_dict() when unset. Keys a model does
not know — such as fields a dependent skill adds — are kept in `extra`.
Fields marked runtime-only (`_runtime()`) describe a loaded record and are
never written by to_dict().

Schema version 1 stored servers, GGUF files and their LLMs together as
[[model_state]] rows and agents as [[agent_state]]; upgrade_v1() converts it.
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


# Servers: processes on a machine that listen for requests. Each subtype has
# its own table, so every row in a table has the same fields.

@dataclass
class Server(Record):
    """A process on a machine that listens for requests.

    `name` is the software (ollama, llama-server, goose, ...). Subtypes add
    what is particular to their kind of server.
    """
    hostname: str
    name: str
    status: str
    port: int | None = None
    last_seen: str | None = None


@dataclass
class LLMProvider(Server):
    """An [[llm_providers]] row: a server that serves LLMs (ollama, llama-server).

    `context_window` is per provider because that is where discover reads it.
    """
    context_window: int | None = None


@dataclass
class Agent(Server):
    """An [[agents]] row: an agent runtime (goose, hermes, ...) on a machine.

    `reasoning_buffer` is set by ask-agent-skill and read by track-tasks-skill;
    discover keeps it across runs.
    """
    endpoint: str | None = None
    process: str | None = None
    reasoning_buffer: int | None = None


# File resources: files on a machine that the topology tracks. Each subtype
# has its own table.

@dataclass
class FileResource(Record):
    """A file on a machine. `name` is the file name, `path` where it is."""
    hostname: str
    name: str
    path: str
    last_seen: str | None = None


@dataclass
class GGUF(FileResource):
    """A [[ggufs]] row: LLM weights in GGUF format, which llama-server can load."""


@dataclass
class LLM(Record):
    """An [[llms]] row: one LLM, served by the LLM provider `provider` on `hostname`."""
    hostname: str
    provider: str
    name: str


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
    """The whole of topology.toml.

    `network` is where the machine list comes from: tailscale or manual.
    """
    NESTED: ClassVar[dict[str, type[Record]]] = {
        'machines': Machine,
        'llm_providers': LLMProvider,
        'llms': LLM,
        'agents': Agent,
        'ggufs': GGUF,
        'benchmarks': Benchmark,
    }

    schema_version: int = SCHEMA_VERSION
    network: str | None = None
    last_refreshed: str | None = None
    machines: list[Machine] = field(default_factory=list)
    llm_providers: list[LLMProvider] = field(default_factory=list)
    llms: list[LLM] = field(default_factory=list)
    agents: list[Agent] = field(default_factory=list)
    ggufs: list[GGUF] = field(default_factory=list)
    benchmarks: list[Benchmark] = field(default_factory=list)

    def machine(self, host: str) -> Machine | None:
        """The machine whose hostname or name is `host`."""
        return next((m for m in self.machines if host in (m.hostname, m.name)), None)

    def servers_on(self, hostname: str) -> list[Server]:
        """Every server on a machine, of any kind."""
        return [*self.llm_providers_on(hostname), *self.agents_on(hostname)]

    def llm_providers_on(self, hostname: str) -> list[LLMProvider]:
        return [p for p in self.llm_providers if p.hostname == hostname]

    def llms_on(self, hostname: str, provider: str | None = None) -> list[LLM]:
        """LLMs on a machine, optionally only those one provider serves."""
        return [l for l in self.llms
                if l.hostname == hostname and (provider is None or l.provider == provider)]

    def agents_on(self, hostname: str) -> list[Agent]:
        return [a for a in self.agents if a.hostname == hostname]

    def ggufs_on(self, hostname: str) -> list[GGUF]:
        return [g for g in self.ggufs if g.hostname == hostname]

    def provider_of(self, llm: LLM) -> LLMProvider | None:
        """The LLM provider serving an LLM."""
        return next((p for p in self.llm_providers
                     if p.hostname == llm.hostname and p.name == llm.provider), None)

    def benchmarks_for(self, llm: LLM) -> list[Benchmark]:
        return [b for b in self.benchmarks if b.hostname == llm.hostname and b.llm == llm.name]


# Where discover looks for GGUF files; version 1 recorded only their names.
GGUF_DIR = '~/.local/share/gguf'


def upgrade_v1(data: dict[str, Any]) -> dict[str, Any]:
    """Convert a schema_version 1 topology dict to version 2.

    Each [[model_state]] row for a server becomes one [[llm_providers]] row
    plus one [[llms]] row per entry in its `models`. A `ggufs` row is not a
    server: each of its files becomes a [[ggufs]] row, with the path filled
    in as GGUF_DIR/<name>. [[agent_state]] becomes [[agents]] with `agent`
    renamed to `name`; a benchmark's `model` becomes `llm`; the top-level
    `provider` becomes `network`. Other keys, including dependent skills'
    fields, are carried over unchanged. A dict already at version 2 is
    returned as is.
    """
    if data.get('schema_version', 1) >= SCHEMA_VERSION:
        return data
    renamed = {'provider': 'network'}
    dropped = ('model_state', 'agent_state', 'benchmarks')
    out = {renamed.get(k, k): v for k, v in data.items() if k not in dropped}
    out['schema_version'] = SCHEMA_VERSION

    providers, llms, ggufs = [], [], []
    for row in data.get('model_state', []):
        row = dict(row)
        models = row.pop('models', [])
        hostname, name = row.pop('hostname'), row.pop('backend')
        if name == 'ggufs':
            ggufs += [{'hostname': hostname, 'name': m, 'path': f'{GGUF_DIR}/{m}'} for m in models]
            continue
        providers.append({'hostname': hostname, 'name': name, **row})
        llms += [{'hostname': hostname, 'provider': name, 'name': m} for m in models]
    out['llm_providers'] = providers
    out['llms'] = llms
    out['ggufs'] = ggufs

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
