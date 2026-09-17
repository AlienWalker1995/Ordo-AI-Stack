"""The ONE place the Langfuse SDK is used (langfuse==4.15.1, the version the Hermes image runs).

Mapping of a run onto Langfuse objects:
  * dataset      `ordo-evals.<suite>`, one per suite; items upserted with a stable id
                 (`<suite>.<item_id>`), so re-running a suite never duplicates items
  * trace        one per (run, suite, item), with the deterministic id from ids.trace_id_for; its
                 root span carries the item's input and output
  * dataset run  named `<run-id>`: each item's trace is linked to its dataset item
  * scores       programmatic scores at run time, `judge.<criterion>` at ingest time, on the item's
                 trace, with stable score ids (a re-ingest updates instead of duplicating)

The sink is passed around as an object so the runner and ingest code never import the SDK; `NullSink`
is what `--no-langfuse` uses.
"""
from __future__ import annotations

from typing import Any

from ordo_evals.ids import score_id_for

DATASET_PREFIX = "ordo-evals."
ENVIRONMENT = "evals"


def dataset_name(suite: str) -> str:
    return f"{DATASET_PREFIX}{suite}"


def dataset_item_id(suite: str, item_id: str) -> str:
    return f"{suite}.{item_id}"


class NullSink:
    """--no-langfuse: every call is a no-op."""

    enabled = False

    def ensure_dataset(self, suite: str, description: str) -> None:
        return None

    def record_item(self, run_id: str, item: dict[str, Any]) -> None:
        return None

    def post_score(self, run_id: str, suite: str, item_id: str, trace_id: str, name: str, value: Any,
                   comment: str | None = None) -> None:
        return None

    def flush(self) -> None:
        return None


class LangfuseSink:
    enabled = True

    def __init__(self, *, host: str, public_key: str, secret_key: str):
        if not (public_key and secret_key):
            raise ValueError("LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY are empty; enable the langfuse "
                             "plugin or run with --no-langfuse")
        from langfuse import Langfuse

        self._client = Langfuse(public_key=public_key, secret_key=secret_key, host=host,
                                environment=ENVIRONMENT)
        if not self._client.auth_check():
            raise RuntimeError(f"Langfuse at {host} rejected the project key pair")
        self._datasets: set[str] = set()

    def ensure_dataset(self, suite: str, description: str) -> None:
        name = dataset_name(suite)
        if name in self._datasets:
            return
        self._client.create_dataset(name=name, description=description, metadata={"suite": suite})
        self._datasets.add(name)

    def record_item(self, run_id: str, item: dict[str, Any]) -> None:
        suite, item_id, trace_id = item["suite"], item["item_id"], item["trace_id"]
        dataset_item = dataset_item_id(suite, item_id)
        self._client.create_dataset_item(
            dataset_name=dataset_name(suite), id=dataset_item, input=item["input"],
            expected_output=item.get("target"), metadata={"suite": suite, "item_id": item_id})
        span = self._client.start_observation(
            trace_context={"trace_id": trace_id}, name=f"{suite}/{item_id}", input=item["input"],
            output=item.get("output"),
            metadata={"run_id": run_id, "suite": suite, "subject": item["subject"],
                      "item_id": item_id, "error": item.get("error")})
        span.end()
        self._client.api.dataset_run_items.create(
            run_name=run_id, dataset_item_id=dataset_item, trace_id=trace_id,
            metadata={"suite": suite, "subject": item["subject"]})

    def post_score(self, run_id: str, suite: str, item_id: str, trace_id: str, name: str, value: Any,
                   comment: str | None = None) -> None:
        if isinstance(value, bool):
            data_type, posted = "BOOLEAN", 1.0 if value else 0.0
        elif isinstance(value, int | float):
            data_type, posted = "NUMERIC", float(value)
        else:
            data_type, posted = "CATEGORICAL", str(value)
        self._client.create_score(
            name=name, value=posted, data_type=data_type, trace_id=trace_id, comment=comment,
            score_id=score_id_for(run_id, suite, item_id, name), metadata={"run_id": run_id, "suite": suite})

    def flush(self) -> None:
        self._client.flush()
