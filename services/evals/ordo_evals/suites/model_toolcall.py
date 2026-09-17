"""model_toolcall: function calling through the OpenAI tools API on `local-chat`, 40 generic cases
(single call, parallel calls, multi-turn with tool results, no tool needed, argument types, enum
constraints), checked exactly by toolcall_match. Dataset: datasets/toolcall.jsonl.

The solver makes ONE generate call with the item's tools offered and never executes a tool: the
calls the model emits are the thing under test. (BFCL in inspect_evals 0.20.0 was not used: it clones
the gorilla repository at run time, which this offline, pinned image deliberately cannot do.)
"""
from __future__ import annotations

import json
from typing import Any

from inspect_ai import Task
from inspect_ai.dataset import MemoryDataset, Sample
from inspect_ai.model import ChatMessageAssistant, ChatMessageSystem, ChatMessageTool, ChatMessageUser, get_model
from inspect_ai.scorer import Score, Target, mean, scorer
from inspect_ai.solver import Generate, TaskState, solver
from inspect_ai.tool import ToolCall, ToolInfo, ToolParams

from ordo_evals.jsonl import read_jsonl
from ordo_evals.suites import common
from ordo_evals.toolcall_match import match_tool_calls

SUBJECT = "model"
DESCRIPTION = "Function calling via the OpenAI tools API: exact tool name, schema and argument checks."


def unavailable_reason(ctx: common.SuiteContext) -> str | None:
    return None if ctx.settings.litellm_key else "LITELLM_KEY_EVALS is empty"


def to_chat_messages(messages: list[dict[str, Any]]) -> list[Any]:
    """OpenAI chat messages (as stored in the dataset) -> Inspect chat messages."""
    converted: list[Any] = []
    for message in messages:
        role = message["role"]
        if role == "system":
            converted.append(ChatMessageSystem(content=message["content"]))
        elif role == "user":
            converted.append(ChatMessageUser(content=message["content"]))
        elif role == "assistant":
            calls = [ToolCall(id=c["id"], function=c["function"]["name"],
                              arguments=json.loads(c["function"]["arguments"] or "{}"))
                     for c in message.get("tool_calls") or []]
            converted.append(ChatMessageAssistant(content=message.get("content") or "", tool_calls=calls or None))
        elif role == "tool":
            name = next((c["function"]["name"] for m in messages for c in (m.get("tool_calls") or [])
                         if c["id"] == message["tool_call_id"]), None)
            converted.append(ChatMessageTool(content=message["content"], tool_call_id=message["tool_call_id"],
                                             function=name))
        else:
            raise ValueError(f"unsupported message role {role!r}")
    return converted


def to_tool_infos(tools: list[dict[str, Any]]) -> list[ToolInfo]:
    infos = []
    for tool in tools:
        function = tool["function"]
        infos.append(ToolInfo(name=function["name"], description=function.get("description", ""),
                              parameters=ToolParams.model_validate(function["parameters"])))
    return infos


@solver
def offer_tools_once():
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        tools = to_tool_infos(state.metadata["tools"])
        state.output = await get_model().generate(state.messages, tools=tools, tool_choice="auto")
        state.messages.append(state.output.message)
        return state

    return solve


@scorer(metrics={"correct": [mean()]})
def exact_tool_calls():
    async def score(state: TaskState, target: Target) -> Score:
        calls = [{"name": c.function, "arguments": c.arguments, "parse_error": c.parse_error}
                 for c in (state.output.message.tool_calls or [])]
        passed, reasons = match_tool_calls(state.metadata["expected"], calls, state.metadata["tools"])
        return Score(value={"correct": int(passed)}, answer=json.dumps(calls, sort_keys=True),
                     explanation="; ".join(reasons) or "exact match", metadata={"calls": calls, "reasons": reasons})

    return score


def _dataset(ctx: common.SuiteContext) -> MemoryDataset:
    samples = []
    for row in read_jsonl(ctx.settings.datasets_dir / "toolcall.jsonl"):
        samples.append(Sample(id=row["id"], input=to_chat_messages(row["messages"]),
                              target=json.dumps(row["expected"], sort_keys=True),
                              metadata={"category": row["category"], "tools": row["tools"], "expected": row["expected"]}))
    return MemoryDataset(samples)


def run(ctx: common.SuiteContext) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    task = Task(dataset=_dataset(ctx), solver=[offer_tools_once()], scorer=exact_tool_calls(), name="model_toolcall")
    log = common.run_task(task, ctx, model=common.model_spec(ctx.settings), limit=ctx.limit,
                          **common.generate_args(ctx))
    items = []
    for sample in log.samples or []:
        score = common.primary_score(sample)
        values = dict(score.value) if score else {}
        details = (score.metadata or {}) if score else {}
        output = sample.output.message if sample.output else None
        items.append(common.sample_item(
            sample, ctx=ctx, suite="model_toolcall", subject=SUBJECT,
            scores={"correct": bool(values.get("correct"))},
            metadata={"category": sample.metadata.get("category"), "calls": details.get("calls"),
                      "reasons": details.get("reasons"),
                      "stop_reason": sample.output.stop_reason if sample.output else None},
            output=json.dumps({"content": common.message_text(output.content) if output else None,
                               "tool_calls": details.get("calls")}, sort_keys=True),
            target=sample.metadata.get("expected")))
        items[-1]["infra_error"] = common.model_error_item(sample)
    return items, []
