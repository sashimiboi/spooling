"""Spooling eval runner backed by the Strands Evals SDK.

The rubric catalog lives in the `eval_rubrics` table. Each row either names
a Strands `Evaluator` subclass (`evaluator_type` column) or is a function
rubric handled by our own registry below. When a rubric is run we:

1. Load the rubric row + the target trace (or span) + its descendants.
2. Build a Strands `EvaluationData` from the trace by collecting the first
   user message as `input`, the assistant's final output as `actual_output`,
   and the tool-name sequence as `actual_trajectory`.
3. Instantiate the Strands evaluator with an Ollama model (gemma by default)
   so it works out of the box with no API key required.
4. Call `evaluator.evaluate(data)` and persist the first `EvaluationOutput`
   into our `evals` table (score / test_pass / reason / label).

Function rubrics (deterministic, non-LLM) still live in the
`_FUNCTION_RUBRICS` registry. They short-circuit the Strands path.

Custom rubrics are just rows with `evaluator_type='OutputEvaluator'` and a
`rubric_text` value — anyone can add one via POST /api/evals/rubrics.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from spooling.db import get_connection


# --- function rubric registry (deterministic) ------------------------------

FunctionGrader = Callable[[dict, list[dict]], "EvalResult"]
_FUNCTION_RUBRICS: dict[str, FunctionGrader] = {}


@dataclass
class EvalResult:
    score: float | None = None
    passed: bool | None = None
    label: str | None = None
    rationale: str | None = None
    attrs: dict[str, Any] | None = None


def register_function_rubric(rubric_id: str):
    def _wrap(fn: FunctionGrader) -> FunctionGrader:
        _FUNCTION_RUBRICS[rubric_id] = fn
        return fn
    return _wrap


PASS_SCORE_THRESHOLD = 0.95


@register_function_rubric("tool-error-rate")
def _tool_error_rate(target: dict, children: list[dict]) -> EvalResult:
    tool_spans = [c for c in children if c["kind"] == "tool"]
    if not tool_spans:
        return EvalResult(
            score=None, passed=None, label="no-tools",
            rationale="No tool spans in trace — skipped.",
        )
    errors = sum(1 for t in tool_spans if t.get("tool_is_error") or t.get("status") == "error")
    rate = errors / len(tool_spans)
    score = round(1 - rate, 3)
    return EvalResult(
        score=score,
        passed=score >= PASS_SCORE_THRESHOLD,
        label=f"{errors}/{len(tool_spans)} errors",
        rationale=f"Tool error rate: {rate:.1%} (pass threshold: {1 - PASS_SCORE_THRESHOLD:.0%}).",
        attrs={"tool_count": len(tool_spans), "error_count": errors},
    )


# --- Strands evaluator factory ---------------------------------------------

# Default Ollama host + model. Overridable via spool-agent settings row.
#
# We default to `qwen2.5:7b` because Strands evaluators use tool-calling
# under the hood to return structured output, and Ollama's gemma3 family
# does not expose tool support. The 7b size is the smallest local model
# that's reliable at structured output under pressure from the evaluator
# prompts. Users can override via the SPOOLING_JUDGE_MODEL env var or the
# `spool-agent` settings row (judge_model field).
DEFAULT_OLLAMA_HOST = os.environ.get("SPOOLING_OLLAMA_HOST", "http://localhost:11434")
DEFAULT_JUDGE_MODEL = os.environ.get("SPOOLING_JUDGE_MODEL", "qwen2.5:7b")


def _judge_config() -> dict:
    """Load judge config from the spool-agent providers row, or fall back."""
    try:
        conn = get_connection()
        row = conn.execute(
            "SELECT config FROM providers WHERE id = 'spooling-agent'"
        ).fetchone()
        conn.close()
    except Exception:
        row = None
    cfg = (row or {}).get("config") if row else {}
    if isinstance(cfg, str):
        try:
            cfg = json.loads(cfg)
        except Exception:
            cfg = {}
    return cfg or {}


def _pick_judge_model(cfg: dict) -> str:
    """Pick the Ollama model to use as the Strands judge.

    Precedence:
      1. `judge_model` field in the spool-agent settings row (explicit override)
      2. $SPOOLING_JUDGE_MODEL env var (via DEFAULT_JUDGE_MODEL)
      3. `qwen2.5:3b` default

    Note: the `model` field on spool-agent is the *chat* model (often gemma),
    not the judge model. Gemma doesn't support tool-calling so it can't serve
    as a Strands judge even though it works fine for the chat page.
    """
    judge = (cfg or {}).get("judge_model")
    if judge:
        return judge
    return DEFAULT_JUDGE_MODEL


def _make_ollama_model(cfg: dict):
    """Build a Strands OllamaModel pointing at the local daemon."""
    from strands.models.ollama import OllamaModel

    host = (cfg or {}).get("ollama_url") or DEFAULT_OLLAMA_HOST
    model_id = _pick_judge_model(cfg)
    return OllamaModel(host=host, model_id=model_id)


# Evaluators that auto-parse traces and require a Strands Session object
# as `actual_trajectory`. Everything else can accept a plain list of tool
# dicts or nothing at all.
_SESSION_EVALUATORS = {
    "HelpfulnessEvaluator",
    "CoherenceEvaluator",
    "ConcisenessEvaluator",
    "FaithfulnessEvaluator",
    "HarmfulnessEvaluator",
    "ResponseRelevanceEvaluator",
    "ToolSelectionAccuracyEvaluator",
    "ToolParameterAccuracyEvaluator",
    "GoalSuccessRateEvaluator",
}


def _evaluator_factory(evaluator_type: str, rubric_text: Optional[str], model):
    """Instantiate a Strands evaluator class by name with the Ollama model."""
    from strands_evals import evaluators as ev

    cls = getattr(ev, evaluator_type, None)
    if cls is None:
        raise ValueError(f"Unknown Strands evaluator: {evaluator_type}")

    # OutputEvaluator and TrajectoryEvaluator require a rubric string.
    if evaluator_type in ("OutputEvaluator", "TrajectoryEvaluator"):
        rubric = rubric_text or (
            "Pass if the output directly and correctly addresses the user's "
            "request. Score 0-1 based on accuracy and completeness."
        )
        return cls(rubric=rubric, model=model)

    return cls(model=model)


# --- Trace → Strands Session / EvaluationData extraction -----------------

# Local models fall over on very long evaluator prompts. Cap the number of
# tool spans we replay per Session so the judge stays reliable, and trim
# each tool's argument/output payload before handing it off.
_MAX_TOOL_SPANS_PER_TRACE = 20
_MAX_TOOL_OUTPUT_CHARS = 400
_MAX_TOOL_ARG_CHARS = 300


def _trim_tool_input(payload: dict) -> dict:
    out = {}
    for k, v in (payload or {}).items():
        if isinstance(v, str) and len(v) > _MAX_TOOL_ARG_CHARS:
            out[k] = v[:_MAX_TOOL_ARG_CHARS] + "…"
        else:
            out[k] = v
    return out


def _build_strands_session(conn, trace_id: str):
    """Materialize a Strands-shaped Session from our stored spans.

    The Strands evaluators that auto-parse traces (HelpfulnessEvaluator,
    ToolSelectionAccuracyEvaluator, etc.) require an `actual_trajectory`
    that's a `Session` object, not a list. We translate each spool span
    into the corresponding Strands span type.
    """
    from datetime import datetime, timezone
    from strands_evals.types.trace import (
        Session, Trace as StrandsTrace, InferenceSpan, ToolExecutionSpan,
        AgentInvocationSpan, ToolConfig,
        SpanInfo, SpanType, Role, ContentType,
        UserMessage, AssistantMessage, TextContent,
        ToolCall, ToolResult,
    )

    trace_row = conn.execute(
        "SELECT id, session_id, started_at, ended_at FROM traces WHERE id = %s",
        (trace_id,),
    ).fetchone()
    if not trace_row:
        return None
    session_id = trace_row["session_id"] or trace_id

    spans = conn.execute(
        """SELECT id, parent_id, kind, name, started_at, ended_at,
                  input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
                  model, tool_name, tool_input, tool_output, tool_is_error,
                  sequence
           FROM spans WHERE trace_id = %s ORDER BY sequence""",
        (trace_id,),
    ).fetchall()

    # Pull the user-message content keyed by session_id so we can attach it
    # to the corresponding inference span at the start of the conversation.
    msg_rows = conn.execute(
        """SELECT role, content, timestamp FROM messages
           WHERE session_id = %s
             AND COALESCE(length(trim(content)), 0) > 0
           ORDER BY timestamp ASC NULLS LAST""",
        (session_id,),
    ).fetchall()
    user_messages = [m["content"][:2000] for m in msg_rows if m["role"] == "user"]
    assistant_messages = [m["content"][:2000] for m in msg_rows if m["role"] == "assistant"]

    def _ts(val) -> datetime:
        if isinstance(val, datetime):
            return val
        return datetime.now(tz=timezone.utc)

    # Keep the span list tractable for the judge model: drop most tool
    # spans if the trace has many, keeping a representative slice of the
    # first N so tool-level evaluators don't choke on a 144-tool replay.
    tool_span_rows = [sp for sp in spans if sp["kind"] == "tool"]
    if len(tool_span_rows) > _MAX_TOOL_SPANS_PER_TRACE:
        keep_tool_ids = {sp["id"] for sp in tool_span_rows[:_MAX_TOOL_SPANS_PER_TRACE]}
        spans = [sp for sp in spans if sp["kind"] != "tool" or sp["id"] in keep_tool_ids]

    built_spans = []
    user_ix = 0
    assistant_ix = 0

    # Top-level AgentInvocationSpan wrapping the whole conversation. Trace-
    # level Strands evaluators (HelpfulnessEvaluator etc.) require at least
    # one of these to anchor the agent-turn input/output.
    if spans:
        top_start = _ts(trace_row["started_at"] or spans[0]["started_at"])
        top_end = _ts(trace_row["ended_at"] or spans[-1]["ended_at"] or spans[-1]["started_at"])
        top_info = SpanInfo(
            trace_id=trace_id,
            span_id=f"{trace_id}-root",
            session_id=session_id,
            parent_span_id=None,
            start_time=top_start,
            end_time=top_end,
        )
        first_user = user_messages[0] if user_messages else "(no user input)"
        last_assistant = assistant_messages[-1] if assistant_messages else "(no assistant output)"
        tool_names = sorted({sp["tool_name"] for sp in spans if sp.get("tool_name")})
        try:
            built_spans.append(AgentInvocationSpan(
                span_info=top_info,
                metadata={},
                span_type=SpanType.AGENT_INVOCATION,
                user_prompt=first_user,
                agent_response=last_assistant,
                available_tools=[ToolConfig(name=n) for n in tool_names],
            ))
        except Exception as e:
            # Bubble the error up so _run_strands_evaluator reports it
            # instead of silently running on an empty session.
            raise RuntimeError(f"AgentInvocationSpan build failed: {e}") from e

    for sp in spans:
        start = _ts(sp["started_at"])
        end = _ts(sp["ended_at"] or sp["started_at"])
        span_info = SpanInfo(
            trace_id=trace_id,
            span_id=sp["id"],
            session_id=session_id,
            parent_span_id=sp["parent_id"],
            start_time=start,
            end_time=end,
        )

        if sp["kind"] == "llm_call":
            user_text = user_messages[user_ix] if user_ix < len(user_messages) else "(no user message)"
            assistant_text = (
                assistant_messages[assistant_ix] if assistant_ix < len(assistant_messages)
                else "(no assistant output)"
            )
            user_ix += 1
            assistant_ix += 1

            messages = [
                UserMessage(role=Role.USER, content=[TextContent(content_type=ContentType.TEXT, text=user_text)]),
                AssistantMessage(role=Role.ASSISTANT, content=[TextContent(content_type=ContentType.TEXT, text=assistant_text)]),
            ]
            try:
                built_spans.append(InferenceSpan(
                    span_info=span_info,
                    metadata={"model": sp.get("model") or ""},
                    span_type=SpanType.INFERENCE,
                    messages=messages,
                ))
            except Exception:
                # If InferenceSpan pydantic validation rejects the shape on a
                # particular version, skip the span rather than fail the trace.
                continue

        elif sp["kind"] == "tool" and sp.get("tool_name"):
            tool_input = sp.get("tool_input")
            if isinstance(tool_input, str):
                try:
                    tool_input = json.loads(tool_input)
                except Exception:
                    tool_input = {"raw": tool_input}
            if not isinstance(tool_input, dict):
                tool_input = {}

            tool_output = sp.get("tool_output") or ""
            is_error = bool(sp.get("tool_is_error"))
            # ToolResult.error is a string (error message), not a bool.
            # On success we pass None; on failure we reuse the tool output.
            error_text = tool_output[:_MAX_TOOL_OUTPUT_CHARS] if is_error else None
            content_text = "" if is_error else tool_output[:_MAX_TOOL_OUTPUT_CHARS]
            built_spans.append(ToolExecutionSpan(
                span_info=span_info,
                metadata={},
                span_type=SpanType.TOOL_EXECUTION,
                tool_call=ToolCall(
                    name=sp["tool_name"],
                    arguments=_trim_tool_input(tool_input),
                    tool_call_id=sp["id"],
                ),
                tool_result=ToolResult(
                    content=content_text,
                    error=error_text,
                    tool_call_id=sp["id"],
                ),
            ))

    strands_trace = StrandsTrace(
        spans=built_spans,
        trace_id=trace_id,
        session_id=session_id,
    )
    return Session(traces=[strands_trace], session_id=session_id)


def _extract_evaluation_data(conn, trace_id: str, target: dict, needs_session: bool):
    """Build a Strands EvaluationData from a trace's stored spans + messages.

    - input:             first user message's content
    - actual_output:     concatenation of the final assistant turn's content
    - actual_trajectory: list of {tool} dicts for output-level evaluators, or
                         a full Strands Session object for trace/tool/session-
                         level evaluators.
    """
    from strands_evals.types import EvaluationData

    first_user = conn.execute(
        """SELECT content FROM messages
           WHERE session_id = (SELECT session_id FROM traces WHERE id = %s)
             AND role = 'user'
             AND COALESCE(length(trim(content)), 0) > 0
           ORDER BY timestamp ASC NULLS LAST LIMIT 1""",
        (trace_id,),
    ).fetchone()
    user_input = (first_user or {}).get("content") or "(no user input)"

    last_assistant = conn.execute(
        """SELECT content FROM messages
           WHERE session_id = (SELECT session_id FROM traces WHERE id = %s)
             AND role = 'assistant'
             AND COALESCE(length(trim(content)), 0) > 0
           ORDER BY timestamp DESC NULLS LAST LIMIT 1""",
        (trace_id,),
    ).fetchone()
    actual_output = (last_assistant or {}).get("content") or "(no assistant output)"

    user_input = user_input[:4000]
    actual_output = actual_output[:4000]

    trajectory: Any
    if needs_session:
        trajectory = _build_strands_session(conn, trace_id)
    else:
        tool_rows = conn.execute(
            """SELECT tool_name FROM spans
               WHERE trace_id = %s AND kind = 'tool' AND tool_name IS NOT NULL
               ORDER BY sequence""",
            (trace_id,),
        ).fetchall()
        trajectory = [{"tool": r["tool_name"]} for r in tool_rows] or None

    return EvaluationData(
        input=user_input,
        actual_output=actual_output,
        actual_trajectory=trajectory,
        metadata={
            "trace_id": trace_id,
            "provider_id": target.get("provider_id"),
            "project": target.get("project"),
        },
    )


# --- Loaders ----------------------------------------------------------------

def _load_rubric(conn, rubric_id: str) -> dict | None:
    row = conn.execute(
        """SELECT id, name, description, kind, target_kind,
                  evaluator_type, rubric_text, model_id, config
           FROM eval_rubrics WHERE id = %s""",
        (rubric_id,),
    ).fetchone()
    return dict(row) if row else None


def _target_row(conn, rubric: dict, trace_id: str, span_id: Optional[str]) -> dict | None:
    if rubric["target_kind"] == "trace":
        row = conn.execute("SELECT * FROM traces WHERE id = %s", (trace_id,)).fetchone()
        if not row:
            return None
        target = dict(row)
        target["_target_kind"] = "trace"
        return target
    sid = span_id
    if not sid:
        cfg = rubric.get("config") or {}
        if isinstance(cfg, str):
            cfg = json.loads(cfg)
        wanted = cfg.get("span_kind") if isinstance(cfg, dict) else None
        if wanted:
            row = conn.execute(
                "SELECT * FROM spans WHERE trace_id = %s AND kind = %s ORDER BY sequence LIMIT 1",
                (trace_id, wanted),
            ).fetchone()
            if row:
                sid = row["id"]
    if not sid:
        return None
    row = conn.execute("SELECT * FROM spans WHERE id = %s", (sid,)).fetchone()
    if not row:
        return None
    target = dict(row)
    target["_target_kind"] = "span"
    return target


def _trace_children(conn, trace_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM spans WHERE trace_id = %s ORDER BY sequence", (trace_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def _span_descendants(conn, span_id: str) -> list[dict]:
    rows = conn.execute(
        """WITH RECURSIVE descendants AS (
              SELECT * FROM spans WHERE id = %s
              UNION ALL
              SELECT s.* FROM spans s JOIN descendants d ON s.parent_id = d.id
           )
           SELECT * FROM descendants WHERE id <> %s""",
        (span_id, span_id),
    ).fetchall()
    return [dict(r) for r in rows]


# --- Runners ----------------------------------------------------------------

def _run_strands_evaluator(conn, rubric: dict, target: dict, trace_id: str) -> EvalResult:
    """Invoke a Strands evaluator, persist the outcome."""
    try:
        cfg = _judge_config()
        model = _make_ollama_model(cfg)
        model_id = _pick_judge_model(cfg)
    except Exception as e:
        return EvalResult(score=None, passed=None, label="model-init-error", rationale=str(e)[:300])

    evaluator_type = rubric["evaluator_type"]
    try:
        evaluator = _evaluator_factory(
            evaluator_type, rubric.get("rubric_text"), model
        )
    except Exception as e:
        return EvalResult(score=None, passed=None, label="factory-error", rationale=str(e)[:300])

    needs_session = evaluator_type in _SESSION_EVALUATORS
    try:
        data = _extract_evaluation_data(conn, trace_id, target, needs_session=needs_session)
    except Exception as e:
        return EvalResult(score=None, passed=None, label="extract-error", rationale=str(e)[:300])

    try:
        outputs = evaluator.evaluate(data)
    except Exception as e:
        msg = str(e)
        label = "ollama-down" if "ConnectError" in msg or "Connection refused" in msg else "judge-error"
        return EvalResult(
            score=None, passed=None, label=label,
            rationale=msg[:400],
            attrs={"judge_model": model_id},
        )

    if not outputs:
        return EvalResult(score=None, passed=None, label="no-output", rationale="Evaluator returned no outputs.")

    out = outputs[0]
    return EvalResult(
        score=float(out.score) if out.score is not None else None,
        passed=bool(out.test_pass) if out.test_pass is not None else None,
        label=out.label or "judged",
        rationale=(out.reason or "")[:2000],
        attrs={"judge_model": model_id, "judge_cost_usd": 0.0},
    )


def run_rubric(
    rubric_id: str,
    trace_id: str,
    span_id: Optional[str] = None,
) -> Optional[int]:
    """Run a rubric against a trace or span. Returns the new eval row id."""
    conn = get_connection()
    try:
        rubric = _load_rubric(conn, rubric_id)
        if not rubric:
            return None

        target = _target_row(conn, rubric, trace_id, span_id)
        if not target:
            return None

        is_trace = target["_target_kind"] == "trace"
        resolved_trace_id = trace_id if is_trace else target.get("trace_id")
        resolved_span_id = None if is_trace else target.get("id")

        if rubric["kind"] == "function":
            grader = _FUNCTION_RUBRICS.get(rubric_id)
            if not grader:
                return None
            children = _trace_children(conn, trace_id) if is_trace else _span_descendants(conn, target["id"])
            result = grader(target, children)
        elif rubric["kind"] == "llm_judge":
            if not rubric.get("evaluator_type"):
                return None
            result = _run_strands_evaluator(conn, rubric, target, resolved_trace_id)
        else:
            return None

        row = conn.execute(
            """INSERT INTO evals (
                rubric_id, trace_id, span_id, score, passed, label, rationale,
                judge_model, judge_cost_usd, attrs
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id""",
            (
                rubric_id, resolved_trace_id, resolved_span_id,
                result.score, result.passed, result.label, result.rationale,
                (result.attrs or {}).get("judge_model"),
                float((result.attrs or {}).get("judge_cost_usd", 0.0)),
                json.dumps(result.attrs or {}),
            ),
        ).fetchone()
        conn.commit()
        return row["id"] if row else None
    finally:
        conn.close()


def run_rubric_bulk(rubric_id: str, since: Optional[datetime] = None) -> dict:
    """Run a rubric against every trace created since a cutoff."""
    conn = get_connection()
    try:
        if since:
            rows = conn.execute(
                "SELECT id FROM traces WHERE started_at >= %s ORDER BY started_at DESC",
                (since,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT id FROM traces ORDER BY started_at DESC").fetchall()
        trace_ids = [r["id"] for r in rows]
    finally:
        conn.close()

    ok = 0
    failed = 0
    for tid in trace_ids:
        res = run_rubric(rubric_id, tid)
        if res is not None:
            ok += 1
        else:
            failed += 1
    return {"rubric": rubric_id, "traces": len(trace_ids), "scored": ok, "skipped": failed}
