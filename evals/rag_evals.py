"""
NEXUS AI · evals/rag_evals.py
RAGAS evaluation suite for the RAG pipeline.

Metrics measured:
  - faithfulness       : Answer only uses retrieved context (no hallucination)
  - answer_relevancy   : Answer directly addresses the question
  - context_recall     : Retrieved docs cover the ground truth
  - context_precision  : Retrieved docs are relevant (no noise)

Run:
    python -m evals.rag_evals --output evals/results/rag_results.json
"""

import asyncio
import json
import argparse
from datetime import datetime
from pathlib import Path

from loguru import logger
from dotenv import load_dotenv
load_dotenv()          # loads .env into os.environ
load_dotenv(".env.local", override=True)   # then override with local-specific values

# ── Test dataset ──────────────────────────────────────────────
# In production: load from a curated JSONL file or Postgres
EVAL_DATASET = [
    {
        "question": "How do I fix PostgreSQL max connections error?",
        "ground_truth": "Increase max_connections in postgresql.conf, use connection pooling (PgBouncer), and terminate idle connections.",
        "contexts_hint": ["database", "postgresql"],
    },
    {
        "question": "Server is unreachable after maintenance, what should I do?",
        "ground_truth": "Check security groups/firewall rules, verify instance is running, test with ping/traceroute, check route tables.",
        "contexts_hint": ["network"],
    },
    {
        "question": "How to resolve DNS resolution failure on Linux?",
        "ground_truth": "Check /etc/resolv.conf, restart systemd-resolved, flush DNS cache with resolvectl flush-caches.",
        "contexts_hint": ["network", "dns"],
    },
    {
        "question": "Disk space full due to WAL logs in PostgreSQL, what to do?",
        "ground_truth": "Check pg_wal directory size, look for unused replication slots, drop them after confirmation, run CHECKPOINT.",
        "contexts_hint": ["database", "disk"],
    },
    {
        "question": "How to identify and kill long-running PostgreSQL queries?",
        "ground_truth": "Query pg_stat_activity for long-running queries, use pg_cancel_backend() for graceful cancel or pg_terminate_backend() to hard kill.",
        "contexts_hint": ["database"],
    },
]


async def build_ragas_dataset(dataset: list[dict]) -> tuple:
    """
    Run retrieval on each eval question and build RAGAS-compatible dataset.
    Returns (questions, answers, contexts, ground_truths)
    """
    from rag.pipeline import rag_query

    questions, answers, contexts_list, ground_truths = [], [], [], []

    for item in dataset:
        logger.info(f"Evaluating: {item['question'][:50]}")
        try:
            result = await rag_query(
                query=item["question"],
                category_filter=item["contexts_hint"][0] if item["contexts_hint"] else None,
                return_sources=True,
            )
            questions.append(item["question"])
            answers.append(result["answer"])
            contexts_list.append([s["excerpt"] for s in result["sources"]])
            ground_truths.append(item["ground_truth"])
        except Exception as e:
            logger.warning(f"Eval item failed: {e}")

    return questions, answers, contexts_list, ground_truths


async def run_ragas_evals(output_path: str = "evals/results/rag_results.json") -> dict:
    """
    Full RAGAS evaluation run.
    Returns dict with metric scores and saves to output_path.
    """
    try:
        from ragas import evaluate
        from ragas.metrics import faithfulness, answer_relevancy, context_recall, context_precision
        from datasets import Dataset
    except ImportError:
        logger.error("RAGAS not installed. Run: pip install ragas datasets")
        return {"error": "ragas not installed"}

    logger.info("Building eval dataset...")
    questions, answers, contexts, ground_truths = await build_ragas_dataset(EVAL_DATASET)

    if not questions:
        return {"error": "No eval items completed successfully"}

    dataset = Dataset.from_dict({
        "question": questions,
        "answer": answers,
        "contexts": contexts,
        "ground_truth": ground_truths,
    })

    logger.info(f"Running RAGAS on {len(questions)} examples...")
    result = evaluate(
        dataset=dataset,
        metrics=[faithfulness, answer_relevancy, context_recall, context_precision],
        raise_exceptions=False,
    )

    scores = {
        "faithfulness":       round(sum(result["faithfulness"]) / len(result["faithfulness"]), 4),
        "answer_relevancy":   round(sum(result["answer_relevancy"]) / len(result["answer_relevancy"]), 4),
        "context_recall":     round(sum(result["context_recall"]) / len(result["context_recall"]), 4),
        "context_precision":  round(sum(result["context_precision"]) / len(result["context_precision"]), 4),
        "timestamp":          datetime.utcnow().isoformat(),
        "n_samples":          len(questions),
    }

    # Save results
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(scores, f, indent=2)

    logger.info(f"RAGAS results: {scores}")

    # Record to Prometheus
    try:
        from app.middleware.tracing import record_eval_score
        for metric, score in scores.items():
            if isinstance(score, float):
                record_eval_score(f"ragas_{metric}", score)
    except Exception:
        pass

    return scores


# ── CI Gate ───────────────────────────────────────────────────
THRESHOLDS = {
    "faithfulness":      0.75,
    "answer_relevancy":  0.70,
    "context_recall":    0.65,
    "context_precision": 0.60,
}

def check_thresholds(scores: dict) -> tuple[bool, list[str]]:
    """Return (passed, list_of_failures) for CI gate."""
    failures = []
    for metric, threshold in THRESHOLDS.items():
        score = scores.get(metric, 0)
        if score < threshold:
            failures.append(f"{metric}: {score:.3f} < {threshold} (threshold)")
    return len(failures) == 0, failures


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="evals/results/rag_results.json")
    parser.add_argument("--ci", action="store_true", help="Exit 1 if thresholds not met")
    args = parser.parse_args()

    scores = asyncio.run(run_ragas_evals(args.output))
    print(json.dumps(scores, indent=2))

    if args.ci:
        passed, failures = check_thresholds(scores)
        if not passed:
            print(f"\n❌ CI GATE FAILED:\n" + "\n".join(f"  - {f}" for f in failures))
            exit(1)
        else:
            print("\n✅ All eval thresholds passed")
