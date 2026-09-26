"""Set-based precision/recall/F1 and scalar accuracy helpers."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PRF:
    precision: float
    recall: float
    f1: float
    tp: int
    fp: int
    fn: int


def prf(tp: int, fp: int, fn: int) -> PRF:
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return PRF(precision=precision, recall=recall, f1=f1, tp=tp, fp=fp, fn=fn)


def score_sets(predicted: set, golden: set) -> PRF:
    tp = len(predicted & golden)
    fp = len(predicted - golden)
    fn = len(golden - predicted)
    return prf(tp, fp, fn)


def accuracy(correct: int, total: int) -> float | None:
    return correct / total if total else None


def aggregate_prf(scores: list[PRF]) -> PRF:
    return prf(
        tp=sum(s.tp for s in scores),
        fp=sum(s.fp for s in scores),
        fn=sum(s.fn for s in scores),
    )
