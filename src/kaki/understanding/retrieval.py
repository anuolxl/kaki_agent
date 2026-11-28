"""Weighted reciprocal-rank fusion; the unchanged user query is always first."""


def fused_rank(queries, documents, min_score=0.0):
    # 每条查询先词法排序，再做加权 RRF；原文权重高于扩展，避免派生词淹没用户原意。
    from kaki.context.knowledge import rank

    scores = {}
    for index, query in enumerate(dict.fromkeys(queries)):
        weight = 2.0 if index == 0 else 1.0
        for position, (row, score) in enumerate(rank(query, documents), 1):
            if score < min_score:
                continue
            scores[row] = scores.get(row, 0.0) + weight / (60 + position)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))
