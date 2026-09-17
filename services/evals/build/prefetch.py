"""Image build step: bake the IFEval dataset and the NLTK tokenizer data into the image.

Runs ONCE at `docker build` (as the runtime user, so the caches land in its home). The runtime then
sets HF_HUB_OFFLINE / HF_DATASETS_OFFLINE, so an eval run never downloads anything: the dataset is
the revision inspect_evals pins (IFEVAL_DATASET_REVISION) and a rebuild of the same Dockerfile bakes
the same bytes. Fails the build loudly if either resource cannot be fetched.
"""
from __future__ import annotations

from inspect_evals.ifeval.ifeval import IFEVAL_DATASET_REVISION, ifeval
from instruction_following_eval.evaluation import ensure_nltk_resource


def main() -> None:
    task = ifeval()  # downloads google/IFEval at the pinned revision into the HF cache
    count = len(task.dataset)
    if count < 60:
        raise SystemExit(f"IFEval dataset has {count} samples; model_ifeval samples 60")
    ensure_nltk_resource()  # punkt + punkt_tab into ~/.cache/nltk_data
    print(f"prefetched IFEval revision {IFEVAL_DATASET_REVISION} ({count} samples) and NLTK punkt data")


if __name__ == "__main__":
    main()
