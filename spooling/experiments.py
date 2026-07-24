"""Experiments: scenario-based evaluation with Strands Cases + Evaluators.

This module wraps `strands_evals.Experiment` so you can define a bundle of
test cases + evaluators once, run them against any task function (typically
a Strands Agent you want to grade), and persist the reports into Spooling.

Two execution patterns are supported:

1. **Plain** — `task_fn(case) -> str` just produces an output string per
   case. The evaluators score those outputs.
2. **Simulated** — `task_fn` is built from `ActorSimulator.from_case_for_user_simulator`
   to drive a multi-turn back-and-forth between a user persona and the
   agent under test. The returned trajectory feeds the trace-level
   evaluators (Helpfulness, Trajectory, GoalSuccessRate).

Experiments and their runs land in two tables (see migrations/004_experiments.sql):
`experiments` stores the catalog; `experiment_runs` stores each run's
reports plus the trace ids generated along the way so you can click
through from a run to the Spooling trace it produced.

CLI:
    spooling experiment create --file cases.json
    spooling experiment list
    spooling experiment run --id <exp-id>
    spooling experiment show --run <run-id>
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from spooling.db import get_connection


# --- Strands imports deferred to runtime so evals.py can import us freely ---

def _strands_eval_classes():
    from strands_evals import Case, Experiment
    from strands_evals import evaluators as ev_mod
    from strands_evals.types import EvaluationData
    return Case, Experiment, ev_mod, EvaluationData


def _ollama_judge_model():
    """Reuse spool.evals judge model config so experiments get the same Ollama + qwen default."""
    from spooling.evals import _judge_config, _make_ollama_model
    return _make_ollama_model(_judge_config())


# --- Catalog operations ----------------------------------------------------

@dataclass
class ExperimentSpec:
    id: str
    name: str
    description: Optional[str]
    cases: list[dict]           # [{name, input, expected_output?, metadata?}, ...]
    evaluators: list[dict]      # [{type: "HelpfulnessEvaluator"}, {type: "OutputEvaluator", rubric: "..."}]
    config: dict                # optional knobs (e.g. {"simulated": true, "max_turns": 5})


def create_experiment(spec: ExperimentSpec) -> str:
    """Persist an experiment spec. Returns the id."""
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO experiments (id, name, description, cases, evaluators, config)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (id) DO UPDATE SET
                   name = EXCLUDED.name,
                   description = EXCLUDED.description,
                   cases = EXCLUDED.cases,
                   evaluators = EXCLUDED.evaluators,
                   config = EXCLUDED.config""",
            (
                spec.id, spec.name, spec.description,
                json.dumps(spec.cases),
                json.dumps(spec.evaluators),
                json.dumps(spec.config),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return spec.id


def load_experiment(experiment_id: str) -> Optional[ExperimentSpec]:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM experiments WHERE id = %s", (experiment_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return ExperimentSpec(
        id=row["id"],
        name=row["name"],
        description=row.get("description"),
        cases=row.get("cases") or [],
        evaluators=row.get("evaluators") or [],
        config=row.get("config") or {},
    )


def list_experiments() -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT id, name, description, created_at,
                      jsonb_array_length(cases) AS case_count,
                      jsonb_array_length(evaluators) AS evaluator_count
               FROM experiments ORDER BY created_at DESC"""
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


# --- Factory helpers for Strands Cases / Evaluators ------------------------

def _build_strands_cases(spec: ExperimentSpec):
    Case, _Experiment, _ev_mod, _EvaluationData = _strands_eval_classes()
    cases = []
    for i, c in enumerate(spec.cases):
        cases.append(Case(
            name=c.get("name") or f"case-{i}",
            input=c["input"],
            expected_output=c.get("expected_output"),
            metadata=c.get("metadata") or {},
        ))
    return cases


def _build_strands_evaluators(spec: ExperimentSpec) -> list:
    _Case, _Experiment, ev_mod, _EvaluationData = _strands_eval_classes()
    model = _ollama_judge_model()
    evaluators = []
    for e in spec.evaluators:
        type_name = e.get("type")
        if not type_name:
            continue
        cls = getattr(ev_mod, type_name, None)
        if cls is None:
            continue
        if type_name in ("OutputEvaluator", "TrajectoryEvaluator"):
            rubric = e.get("rubric") or (
                "Pass if the output directly and correctly addresses the user's "
                "request. Score 0-1 based on accuracy and completeness."
            )
            evaluators.append(cls(rubric=rubric, model=model))
        else:
            evaluators.append(cls(model=model))
    return evaluators


# --- Task function adapters ------------------------------------------------

def _plain_task_fn(experiment_id: str, captured_trace_ids: list) -> Callable:
    """Return a task function that runs a fresh Strands Agent per case.

    Wraps the agent call in an in-memory OTel exporter + StrandsInMemorySessionMapper
    so the returned payload has both `output` (string) and `trajectory`
    (Strands Session). Trace-level evaluators need the Session; output-
    level evaluators just read the string.

    As a side effect, each case's captured Session is also ingested into
    Spooling via `remote_otel.ingest_strands_session`, so the experiment's
    runs show up in the /traces page linked to their originating experiment.
    """
    from strands import Agent
    from strands_evals.telemetry import StrandsEvalsTelemetry
    from strands_evals.mappers import StrandsInMemorySessionMapper
    from spooling.remote_otel import ingest_strands_session

    telemetry = StrandsEvalsTelemetry().setup_in_memory_exporter()
    exporter = telemetry.in_memory_exporter
    mapper = StrandsInMemorySessionMapper()
    model = _ollama_judge_model()

    def _fn(case):
        session_id = case.session_id or uuid.uuid4().hex
        exporter.clear() if hasattr(exporter, "clear") else None
        agent = Agent(
            model=model,
            trace_attributes={
                "gen_ai.conversation.id": session_id,
                "session.id": session_id,
            },
            callback_handler=None,
        )
        response = agent(case.input)
        try:
            spans = list(exporter.get_finished_spans())
            session = mapper.map_to_session(spans, session_id=session_id)
        except Exception:
            session = None

        if session is not None:
            try:
                tid = ingest_strands_session(
                    session,
                    provider_id=f"experiment:{experiment_id}",
                    project=None,
                    title=f"{case.name or 'case'}: {str(case.input)[:60]}",
                )
                captured_trace_ids.append(tid)
            except Exception as e:
                print(f"[spooling.experiments] trace ingest failed: {type(e).__name__}: {e}")

        return {"output": str(response), "trajectory": session}

    return _fn


def _simulated_task_fn(max_turns: int = 5) -> Callable:
    """Return a task function that runs a simulated multi-turn conversation.

    Uses ActorSimulator so a user persona drives the conversation against
    a Strands Agent for up to `max_turns` turns. The final agent response
    is returned. Traces generated during the conversation land in Spooling
    via the live `spooling.sdk` tracer if configured.
    """
    from strands import Agent
    from strands_evals import ActorSimulator

    model = _ollama_judge_model()

    def _fn(case):
        try:
            simulator = ActorSimulator.from_case_for_user_simulator(
                case=case, max_turns=max_turns,
            )
        except Exception:
            simulator = None

        agent = Agent(model=model)
        user_message = case.input
        agent_response = ""
        turns = 0

        while True:
            resp = agent(user_message)
            agent_response = str(resp)
            turns += 1
            if simulator is None or turns >= max_turns:
                break
            if not simulator.has_next():
                break
            try:
                user_result = simulator.act(agent_response)
                user_message = str(user_result.structured_output.message)
            except Exception:
                break

        return agent_response

    return _fn


# --- Running ---------------------------------------------------------------

def run_experiment(experiment_id: str) -> str:
    """Run an experiment and persist a new experiment_runs row. Returns the run id."""
    spec = load_experiment(experiment_id)
    if spec is None:
        raise ValueError(f"Unknown experiment: {experiment_id}")

    _Case, Experiment, _ev_mod, _EvaluationData = _strands_eval_classes()

    cases = _build_strands_cases(spec)
    evaluators = _build_strands_evaluators(spec)

    if not cases or not evaluators:
        raise ValueError(
            "Experiment needs at least one case and one evaluator."
        )

    simulated = bool((spec.config or {}).get("simulated"))
    max_turns = int((spec.config or {}).get("max_turns") or 5)
    captured_trace_ids: list[str] = []
    task_fn = (
        _simulated_task_fn(max_turns=max_turns)
        if simulated
        else _plain_task_fn(experiment_id=spec.id, captured_trace_ids=captured_trace_ids)
    )

    run_id = f"run-{uuid.uuid4().hex[:12]}"
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO experiment_runs (id, experiment_id, status)
               VALUES (%s, %s, 'running')""",
            (run_id, experiment_id),
        )
        conn.commit()

        try:
            experiment = Experiment(cases=cases, evaluators=evaluators)
            reports = experiment.run_evaluations(task_fn)
        except Exception as e:
            conn.execute(
                """UPDATE experiment_runs
                   SET status = 'error', finished_at = now(), error = %s
                   WHERE id = %s""",
                (str(e)[:1000], run_id),
            )
            conn.commit()
            raise

        # Serialize reports into JSONB. EvaluationReport is a Pydantic
        # model; use .model_dump() and convert any Decimal/datetime via default=str.
        reports_payload = [_report_to_dict(r) for r in reports]
        overall = {
            r.evaluator_name: (
                float(r.overall_score) if r.overall_score is not None else None
            )
            for r in reports
        }

        conn.execute(
            """UPDATE experiment_runs
               SET status = 'complete',
                   finished_at = now(),
                   reports = %s,
                   overall_scores = %s,
                   created_trace_ids = %s
               WHERE id = %s""",
            (json.dumps(reports_payload, default=str),
             json.dumps(overall, default=str),
             json.dumps(captured_trace_ids),
             run_id),
        )
        conn.commit()
    finally:
        conn.close()
    return run_id


def _report_to_dict(report) -> dict:
    """Serialize a Strands EvaluationReport for JSONB storage."""
    try:
        return report.model_dump()
    except Exception:
        return {
            "evaluator_name": getattr(report, "evaluator_name", None),
            "overall_score": getattr(report, "overall_score", None),
            "scores": list(getattr(report, "scores", []) or []),
            "test_passes": list(getattr(report, "test_passes", []) or []),
            "reasons": list(getattr(report, "reasons", []) or []),
        }


def load_run(run_id: str) -> Optional[dict]:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM experiment_runs WHERE id = %s", (run_id,)
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def list_runs(experiment_id: Optional[str] = None, limit: int = 20) -> list[dict]:
    conn = get_connection()
    try:
        if experiment_id:
            rows = conn.execute(
                """SELECT id, experiment_id, started_at, finished_at,
                          status, overall_scores
                   FROM experiment_runs WHERE experiment_id = %s
                   ORDER BY started_at DESC LIMIT %s""",
                (experiment_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT id, experiment_id, started_at, finished_at,
                          status, overall_scores
                   FROM experiment_runs
                   ORDER BY started_at DESC LIMIT %s""",
                (limit,),
            ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


# --- File-based spec loader (for CLI `spooling experiment create --file`) ----

def load_spec_from_file(path: str) -> ExperimentSpec:
    with open(path) as f:
        data = json.load(f)
    return ExperimentSpec(
        id=data.get("id") or f"exp-{uuid.uuid4().hex[:10]}",
        name=data["name"],
        description=data.get("description"),
        cases=data.get("cases", []),
        evaluators=data.get("evaluators", []),
        config=data.get("config", {}),
    )
