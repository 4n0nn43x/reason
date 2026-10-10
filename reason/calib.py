"""Scores propres et recalibration, stdlib uniquement : Brier, log-loss, décomposition de Murphy (bins par quantiles),
skill score, bootstrap, Platt (2 paramètres, Newton), borne inférieure de Wilson. Pas d'ECE comme cible : ce n'est pas
une règle propre (un prédicteur constant au taux de base a un ECE nul)."""
from __future__ import annotations

import math
import random

EPS = 1e-4


def clip(p: float) -> float:
    return min(1 - EPS, max(EPS, float(p)))


def brier(p: float, o: int) -> float:
    return (float(p) - float(o)) ** 2


def logloss(p: float, o: int) -> float:
    p = clip(p)
    return -(o * math.log(p) + (1 - o) * math.log(1 - p))


def brier_multi(dist: list[float], idx: int) -> float:
    return sum((q - (1.0 if i == idx else 0.0)) ** 2 for i, q in enumerate(dist))


def rps(dist: list[float], idx: int) -> float:
    """Ranked probability score pour issues ordonnées ; égal au Brier pour K = 2."""
    k = len(dist)
    if k < 2:
        return 0.0
    f = o = 0.0
    tot = 0.0
    for i in range(k - 1):
        f += dist[i]; o = 1.0 if i >= idx else 0.0
        tot += (f - o) ** 2
    return tot / (k - 1)


def mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def murphy(ps: list[float], os_: list[int], bins: int = 5) -> dict | None:
    """Brier = REL - RES + UNC, bins par quantiles (égale masse). None sous 2 points par bin."""
    n = len(ps)
    if n == 0 or n < 2 * bins:
        return None
    order = sorted(range(n), key=lambda i: ps[i])
    obar = sum(os_) / n
    rel = res = 0.0
    for b in range(bins):
        idx = order[b * n // bins:(b + 1) * n // bins]
        if not idx:
            continue
        pk = sum(ps[i] for i in idx) / len(idx)
        ok = sum(os_[i] for i in idx) / len(idx)
        rel += len(idx) * (pk - ok) ** 2
        res += len(idx) * (ok - obar) ** 2
    return {"reliability": rel / n, "resolution": res / n, "uncertainty": obar * (1 - obar),
            "brier": sum(brier(p, o) for p, o in zip(ps, os_)) / n, "n": n, "bins": bins}


def skill(bs: float | None, bs_ref: float | None) -> float | None:
    if bs is None or not bs_ref:
        return None
    return 1 - bs / bs_ref


def bootstrap_ci(xs: list[float], iters: int = 1000, level: float = 0.9, seed: int = 0) -> tuple[float, float] | None:
    if len(xs) < 2:
        return None
    rng = random.Random(seed)
    n = len(xs)
    means = sorted(sum(rng.choice(xs) for _ in range(n)) / n for _ in range(iters))
    lo = means[int((1 - level) / 2 * iters)]
    hi = means[min(iters - 1, int((1 + level) / 2 * iters))]
    return lo, hi


def logit(p: float) -> float:
    p = clip(p)
    return math.log(p / (1 - p))


def sigmoid(x: float) -> float:
    if x >= 0:
        return 1 / (1 + math.exp(-x))
    e = math.exp(x)
    return e / (1 + e)


def platt_fit(ps: list[float], os_: list[int], iters: int = 50, l2: float = 1e-3) -> tuple[float, float]:
    """p_cal = sigmoid(a·logit(p) + b). Newton sur la log-vraisemblance avec une légère régularisation."""
    xs = [logit(p) for p in ps]
    a, b = 1.0, 0.0
    for _ in range(iters):
        g_a = g_b = h_aa = h_ab = h_bb = 0.0
        for x, o in zip(xs, os_):
            q = sigmoid(a * x + b)
            r = q - o
            w = q * (1 - q)
            g_a += r * x; g_b += r
            h_aa += w * x * x; h_ab += w * x; h_bb += w
        g_a += l2 * (a - 1); g_b += l2 * b
        h_aa += l2; h_bb += l2
        det = h_aa * h_bb - h_ab * h_ab
        if abs(det) < 1e-12:
            break
        da = (h_bb * g_a - h_ab * g_b) / det
        db = (h_aa * g_b - h_ab * g_a) / det
        a -= da; b -= db
        if abs(da) < 1e-8 and abs(db) < 1e-8:
            break
    return a, b


def platt_apply(p: float, a: float, b: float) -> float:
    return clip(sigmoid(a * logit(p) + b))


def fit_if_better(ps: list[float], os_: list[int], min_n: int = 30, split: float = 0.7) -> dict | None:
    """Ajuste Platt sur les 70 % les plus anciens (ordre donné), garde seulement si le Brier des 30 % récents baisse."""
    n = len(ps)
    if n < min_n:
        return None
    k = max(2, int(n * split))
    a, b = platt_fit(ps[:k], os_[:k])
    raw = mean([brier(p, o) for p, o in zip(ps[k:], os_[k:])])
    cal = mean([brier(platt_apply(p, a, b), o) for p, o in zip(ps[k:], os_[k:])])
    if raw is None or cal is None or cal >= raw:
        return {"method": "none", "fitted_on": k, "test_brier_raw": raw, "test_brier_cal": cal}
    return {"method": "platt", "a": a, "b": b, "fitted_on": k, "test_brier_raw": raw, "test_brier_cal": cal}


def wilson_lower(successes: int, n: int, z: float = 1.96) -> float:
    if n <= 0:
        return 0.0
    phat = successes / n
    denom = 1 + z * z / n
    centre = phat + z * z / (2 * n)
    margin = z * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n))
    return max(0.0, (centre - margin) / denom)


def aggregate_binary(ps: list[float]) -> tuple[float, tuple[float, float] | None]:
    """Agrégation de K échantillons : moyenne des log-odds ; étendue = min/max des échantillons (K ≥ 3), pas un intervalle à niveau garanti."""
    if not ps:
        raise ValueError("aucun échantillon")
    p = sigmoid(sum(logit(x) for x in ps) / len(ps))
    return p, ((min(ps), max(ps)) if len(ps) >= 3 else None)


def auroc(scores: list[float], labels: list[int]) -> float | None:
    """Probabilité qu'une décision juste ait une confiance plus haute qu'une décision fausse (rangs moyens, ex aequo gérés)."""
    pos = sum(labels); neg = len(labels) - pos
    if not pos or not neg:
        return None
    order = sorted(range(len(scores)), key=lambda i: scores[i])
    ranks = [0.0] * len(scores)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and scores[order[j + 1]] == scores[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    rank_pos = sum(r for r, l in zip(ranks, labels) if l)
    return (rank_pos - pos * (pos + 1) / 2) / (pos * neg)


def risk_coverage(scores: list[float], labels: list[int]) -> list[tuple[float, float, float]]:
    """Courbe risque-couverture de la classification sélective : pour chaque seuil t (confiance décroissante),
    (t, couverture = part des décisions gardées, risque = taux d'erreur parmi elles)."""
    pairs = sorted(zip(scores, labels), key=lambda x: -x[0])
    out, wrong = [], 0
    for k, (sc, lab) in enumerate(pairs, 1):
        wrong += 1 - lab
        if k == len(pairs) or pairs[k][0] != sc:
            out.append((sc, k / len(pairs), wrong / k))
    return out


def aurc(scores: list[float], labels: list[int]) -> float | None:
    """Aire sous la courbe risque-couverture (plus bas = la confiance trie mieux les erreurs)."""
    rc = risk_coverage(scores, labels)
    if not rc:
        return None
    area, prev_cov = 0.0, 0.0
    for _, cov, risk in rc:
        area += (cov - prev_cov) * risk
        prev_cov = cov
    return area


def selective_threshold(scores: list[float], labels: list[int], target_accuracy: float, min_kept: int = 10) -> float | None:
    """Plus petit seuil t tel que la BORNE INFÉRIEURE de Wilson de la précision des décisions de confiance ≥ t atteigne
    target_accuracy (et au moins min_kept décisions gardées). Prudent : 9 justes sur 10 ne suffisent pas pour 0,9.
    None si aucun seuil n'y arrive."""
    best = None
    pairs = sorted(zip(scores, labels), key=lambda x: -x[0])
    kept = right = 0
    for i, (sc, lab) in enumerate(pairs):
        kept += 1; right += lab
        last_of_tie = i + 1 == len(pairs) or pairs[i + 1][0] != sc
        if last_of_tie and kept >= min_kept and wilson_lower(right, kept) >= target_accuracy:
            best = sc
    return best


def sign_test(wins: int, losses: int) -> float:
    """p-valeur bilatérale exacte du test du signe sur paires discordantes (McNemar exact)."""
    n = wins + losses
    if n == 0:
        return 1.0
    k = min(wins, losses)
    p = sum(math.comb(n, i) for i in range(0, k + 1)) / 2 ** n
    return min(1.0, 2 * p)
