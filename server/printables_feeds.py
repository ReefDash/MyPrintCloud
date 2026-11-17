"""Printables browsing beyond "trending": other sort orders, contest winners, and the contests themselves.

All anonymous on api.printables.com/graphql (the same API models_sources.Printables uses). Orderings
and the competition fields were probed against the live API on 2026-11-01."""
from __future__ import annotations

import models_sources as ms

ORDERS = {  # sort key -> (morePrints ordering, label)
    "trending": ("-likes_count_7_days", "Trending this week"),
    "month": ("-likes_count_30_days", "Popular this month"),
    "alltime": ("-likes_count", "Most liked of all time"),
    "downloads": ("-download_count", "Most downloaded"),
    "makes": ("-makes_count", "Most made"),
    "rated": ("-rating_avg", "Best rated"),
    "newest": ("-first_publish", "Newest"),
    "winners": ("-first_publish", "Contest winners, newest first"),
}


class _Awarded(ms.Printables):
    """Only prints that won or placed in a Printables contest."""
    Q_TRENDING = ms.Printables.Q_TRENDING.replace("ordering: $ordering)", "ordering: $ordering, competitionAwarded: true)")


class Feeds:
    def __init__(self) -> None:
        self._feeds: dict[str, ms.Printables] = {}

    def _feed(self, sort: str) -> ms.Printables:
        if sort not in self._feeds:  # one per sort: each keeps its own page cursors
            cls = _Awarded if sort == "winners" else ms.Printables
            self._feeds[sort] = cls(trending_ordering=ORDERS[sort][0])
        return self._feeds[sort]

    async def feed(self, sort: str, page: int = 1) -> list[dict]:
        if sort not in ORDERS:
            raise ms.SourceError("printables: unknown sort")
        return await self._feed(sort).trending(page)

    async def contests(self) -> list[dict]:
        d = await self._feed("trending")._gql("{ competitions { id name slug isOpen printsCount openFrom openTo } }", {})
        out = [{"id": c["id"], "name": (c.get("name") or "").strip(), "open": bool(c.get("isOpen")),
                "entries": c.get("printsCount") or 0, "ends": c.get("openTo"),
                "url": f"https://www.printables.com/contests/{c.get('slug') or c['id']}"} for c in d.get("competitions") or []]
        return sorted(out, key=lambda c: (not c["open"], -(int(c["id"]) if str(c["id"]).isdigit() else 0)))

    async def contest(self, cid: str) -> dict:
        p = self._feed("trending")
        d = await p._gql("query($id: ID!) { competition(id: $id) { id name slug isOpen results { print { %s } } } }"
                         % ms.Printables._LIST_FIELDS, {"id": str(cid)})
        c = d.get("competition")
        if not c:
            raise ms.SourceError("printables: contest not found")
        seen, items = set(), []
        for r in c.get("results") or []:
            pr = r.get("print")
            if pr and pr["id"] not in seen:
                seen.add(pr["id"])
                items.append(p._summary(pr))
        return {"id": c["id"], "name": (c.get("name") or "").strip(), "open": bool(c.get("isOpen")), "items": items,
                "url": f"https://www.printables.com/contests/{c.get('slug') or c['id']}"}
