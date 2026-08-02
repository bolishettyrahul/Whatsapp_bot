"""Scoring against the five axes the challenge actually grades.

    correctness of action
    correctness of message_type
    usefulness and consistency of reason
    whether evidence_message_ids point to relevant historical messages
    reasonable confidence calibration

Accuracy alone answers only the first two, and hides the rest. In particular a
class predicted seven times and correct zero times looks fine in an aggregate,
and a confidence column that carries no information at all still "passes" a
range check. Everything here is objective arithmetic -- no judge model.
"""

import collections
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import taxonomy       # noqa: E402  -- CLAIMS live with the catalogue


# --- classification ---------------------------------------------------------
def per_class(pairs, universe):
    """{class: (gold, predicted, correct, precision, recall, f1)}.

    `pairs` is [(gold, predicted), ...].
    """
    out = {}
    for name in universe:
        gold = sum(1 for g, _p in pairs if g == name)
        pred = sum(1 for _g, p in pairs if p == name)
        true = sum(1 for g, p in pairs if g == p == name)
        precision = true / pred if pred else 0.0
        recall = true / gold if gold else 0.0
        f1 = (2 * precision * recall / (precision + recall)
              if precision + recall else 0.0)
        if gold or pred:
            out[name] = (gold, pred, true, precision, recall, f1)
    return out


def macro_f1(pairs, universe):
    """Unweighted mean F1 over classes PRESENT IN THE GOLD.

    Macro rather than micro because the classes are wildly imbalanced -- gold
    `promotion` appears 6 times and `forward` once. Micro-F1 would let the
    common classes hide total failure on the rare ones, which is exactly the
    failure mode here. Classes absent from the gold are excluded so that never
    predicting a non-existent class is not rewarded.
    """
    scored = per_class(pairs, universe)
    present = [v[5] for name, v in scored.items() if v[0] > 0]
    return sum(present) / len(present) if present else 0.0


def loo_ceiling(samples, taxonomy_cls):
    """What leave-one-out makes achievable at all.

    19 of the 30 gold reasons appear exactly once. LOO rebuilds the catalogue
    from the other 29 rows, which deletes that reason entirely -- so for those
    rows the correct (action, type) pair cannot be selected by any system.

    Reporting a score without this is misleading: `forward`, `spam` and
    `unknown` sit at 0/1 because their only catalogue entry vanishes, not
    because the pipeline is wrong about them. On a real run the full catalogue
    is present and every type is reachable.

    Returns (type_reachable, pair_reachable, macro_f1_ceiling, per_class).
    """
    type_ok = pair_ok = 0
    gold = collections.Counter(r["message_type"] for r in samples)
    reach = collections.Counter()
    for row in samples:
        others = [s for s in samples if s["message_id"] != row["message_id"]]
        options = taxonomy_cls(others).options
        if any(row["message_type"] in o.types for o in options):
            type_ok += 1
            reach[row["message_type"]] += 1
        if any(o.action == row["action"] and row["message_type"] in o.types
               for o in options):
            pair_ok += 1

    # Best case assumes perfect precision, so F1 = 2R/(1+R).
    per_class = {}
    scores = []
    for name, total in gold.items():
        recall = reach[name] / total if total else 0.0
        f1 = 2 * recall / (1 + recall) if recall else 0.0
        per_class[name] = (total, reach[name], f1)
        scores.append(f1)
    return type_ok, pair_ok, (sum(scores) / len(scores) if scores else 0.0), per_class


def loo_pair_reachability(samples, taxonomy_cls):
    """How many rows of each type retain their exact action/type pair in LOO.

    A zero-recall coverage gate is only meaningful where the held-out
    catalogue can express the gold pair. Singleton reasons can remove a type
    or its only compatible action from a LOO fold; those rows are reported as
    protocol-limited rather than counted as a model coverage failure.
    """
    reachable = collections.Counter()
    for row in samples:
        others = [s for s in samples if s["message_id"] != row["message_id"]]
        options = taxonomy_cls(others).options
        if any(o.action == row["action"] and row["message_type"] in o.types
               for o in options):
            reachable[row["message_type"]] += 1
    return dict(reachable)


def zero_recall_classes(pairs, universe, reachable=None):
    """Types with gold support but no correct prediction.

    When ``reachable`` is supplied it maps type -> number of rows whose exact
    action/type pair was available in the evaluation protocol. Types with no
    reachable rows are excluded from the returned gate failures, because
    failing them would make the gate impossible to satisfy.
    """
    out = []
    for name, stats in per_class(pairs, universe).items():
        gold, _pred, correct, _precision, _recall, _f1 = stats
        if gold and correct == 0 and (reachable is None or reachable.get(name, 0)):
            out.append(name)
    return out


# --- evidence ---------------------------------------------------------------
def evidence_scores(rows):
    """Set precision/recall/F1 over evidence ids, plus exact-match.

    `rows` is [(predicted_ids:set, gold_ids:set), ...] with `none` already
    normalised to the empty set. Exact-match is the strictest reading; set
    precision/recall is the fairer one when gold cites two ids and we cite one.
    """
    exact = tp = fp = fn = 0
    both_empty = 0
    for predicted, gold in rows:
        if predicted == gold:
            exact += 1
        if not gold and not predicted:
            both_empty += 1
        tp += len(predicted & gold)
        fp += len(predicted - gold)
        fn += len(gold - predicted)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"exact": exact, "total": len(rows), "precision": precision,
            "recall": recall, "f1": f1, "correct_abstentions": both_empty}


# --- confidence -------------------------------------------------------------
def calibration(confidences, corrects, bins=5):
    """Expected Calibration Error, Brier score, and discrimination.

    ECE asks: when we say 0.85, are we right 85% of the time?
    Brier is the mean squared error of the probability.
    **Discrimination** is the one that matters most here: the gap between mean
    confidence on correct answers and on wrong ones. A confidence that does not
    separate the two carries no information, however well-centred it is.
    """
    if not confidences:
        return {}
    paired = list(zip(confidences, corrects))
    accuracy = sum(corrects) / len(corrects)

    edges = [i / bins for i in range(bins + 1)]
    ece = 0.0
    buckets = []
    for low, high in zip(edges, edges[1:]):
        chunk = [(c, k) for c, k in paired if low < c <= high] or (
            [(c, k) for c, k in paired if c == 0.0 and low == 0.0])
        if not chunk:
            continue
        mean_conf = sum(c for c, _ in chunk) / len(chunk)
        mean_acc = sum(k for _, k in chunk) / len(chunk)
        ece += (len(chunk) / len(paired)) * abs(mean_conf - mean_acc)
        buckets.append((low, high, len(chunk), mean_conf, mean_acc))

    right = [c for c, k in paired if k]
    wrong = [c for c, k in paired if not k]
    discrimination = ((sum(right) / len(right) if right else 0.0)
                      - (sum(wrong) / len(wrong) if wrong else 0.0))
    return {
        "ece": ece,
        "brier": sum((c - k) ** 2 for c, k in paired) / len(paired),
        "accuracy": accuracy,
        "mean_confidence": sum(confidences) / len(confidences),
        "overconfidence": sum(confidences) / len(confidences) - accuracy,
        "mean_conf_when_right": sum(right) / len(right) if right else 0.0,
        "mean_conf_when_wrong": sum(wrong) / len(wrong) if wrong else 0.0,
        "discrimination": discrimination,
        "distinct_values": len(set(confidences)),
        "buckets": buckets,
    }


# --- reason -----------------------------------------------------------------
# Each catalogue reason asserts something checkable about the context. Matching
# on a distinctive phrase, we can test whether the claim is actually true of the
# row we emitted it for. This is what "consistency of reason" means
# operationally: not prose quality, but whether the stated justification holds.
def reason_entailment(records):
    """(supported, contradicted, unchecked, details).

    `records` is [(reason_text, facts_dict), ...]. A reason whose claim cannot
    be checked is counted separately rather than assumed correct -- silently
    scoring unverifiable claims as passes would make the metric meaningless.
    """
    supported = contradicted = unchecked = 0
    details = collections.Counter()
    for reason, facts in records:
        claim = taxonomy.claim_of(reason)
        if claim is None:
            unchecked += 1
            continue
        if claim[1](facts):
            supported += 1
        else:
            contradicted += 1
            details[claim[0]] += 1
    return supported, contradicted, unchecked, details


# --- presentation -----------------------------------------------------------
def render_per_class(pairs, gold_i, pred_i, title, universe):
    """Precision and recall for every class.

    An aggregate accuracy hid a class that was predicted 7 times and correct
    zero times. Per-class is the only view that shows which class moved, and
    it is what stops a fix for one class quietly breaking another.
    """
    print()
    print(f"  {title}: per-class")
    print(f"    {'class':18s} {'gold':>5s} {'pred':>5s} {'ok':>4s} "
          f"{'recall':>9s} {'precision':>10s}")
    for name in universe:
        gold = [p for p in pairs if p[gold_i] == name]
        pred = [p for p in pairs if p[pred_i] == name]
        true = [p for p in pairs if p[gold_i] == name and p[pred_i] == name]
        if not gold and not pred:
            continue
        recall = f"{len(true)}/{len(gold)}" if gold else "-"
        precision = f"{len(true)}/{len(pred)}" if pred else "-"
        flag = "  <-- never predicted" if gold and not pred else (
               "  <-- 0 correct" if pred and not true else "")
        print(f"    {name:18s} {len(gold):5d} {len(pred):5d} {len(true):4d} "
              f"{recall:>9s} {precision:>10s}{flag}")
