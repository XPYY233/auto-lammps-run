"""Read-only, loopback-only Zotero metadata discovery using API v3.

No PDF downloads, model calls or database access. Search hits are discovery
signals; they do not establish that a paper's results were produced by LAMMPS.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class ZoteroError(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ZoteroError("Zotero redirects are disabled")


@dataclass(frozen=True)
class Page:
    items: list[dict]
    total: int
    version: str | None


class LocalZotero:
    def __init__(self, *, port: int = 23119, timeout: float = 15):
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("Invalid local API port")
        self.base = f"http://127.0.0.1:{port}/api/users/0/items"
        self.timeout = timeout
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def page(self, params: dict, *, top: bool = False) -> Page:
        request = Request(self.base + ("/top" if top else "") + "?" + urlencode(params),
                          headers={"Zotero-API-Version": "3", "Accept": "application/json"})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                body = response.read(8_000_001)
                if len(body) > 8_000_000:
                    raise ZoteroError("Zotero page exceeds size limit")
                items = json.loads(body)
                total = int(response.headers["Total-Results"])
                version = response.headers.get("Last-Modified-Version")
        except ZoteroError:
            raise
        except Exception as exc:
            # Do not surface response bodies, local identities or server paths.
            raise ZoteroError(f"Local Zotero read failed ({type(exc).__name__})") from None
        if not isinstance(items, list) or total < 0:
            raise ZoteroError("Invalid Zotero response")
        for item in items:
            if (not isinstance(item, dict) or not isinstance(item.get("data"), dict)
                    or not re.fullmatch(r"[A-Z0-9]{8}", str(item.get("key", "")))):
                raise ZoteroError("Invalid Zotero item")
        return Page(items, total, version)

    def collect(self, params: dict, *, top: bool = False, max_items: int = 30000) -> tuple[list[dict], str | None]:
        """Detect pagination drift; never report truncated results as complete."""
        if max_items < 1:
            raise ValueError("max_items must be positive")
        initial = self.page({**params, "limit": 100, "start": 0}, top=top)
        if initial.total > max_items:
            raise ZoteroError("Query exceeds item budget; narrow the search")
        records = list(initial.items)
        while len(records) < initial.total:
            page = self.page({**params, "limit": 100, "start": len(records)}, top=top)
            if (page.total != initial.total or page.version != initial.version or not page.items):
                raise ZoteroError("Library changed or pagination is incomplete; retry")
            records.extend(page.items)
        if len(records) != initial.total or len({x['key'] for x in records}) != initial.total:
            raise ZoteroError("Duplicate or inconsistent pagination; retry")
        final = self.page({**params, "limit": 1, "start": 0}, top=top)
        if final.total != initial.total or final.version != initial.version:
            raise ZoteroError("Library changed during read; retry")
        return records, initial.version

    def discover(self, query: str) -> dict:
        if not isinstance(query, str) or not query.strip() or len(query) > 200:
            raise ValueError("Supply a search query of 1–200 characters")
        hits, version = self.collect({"q": query, "qmode": "everything"})
        # Annotations can point to attachments, which in turn point to papers.
        by_key = {x["key"]: x for x in hits}
        for _ in range(3):
            missing = sorted({x["data"].get("parentItem") for x in by_key.values()
                              if x["data"].get("parentItem") not in by_key} - {None})
            if not missing:
                break
            for offset in range(0, len(missing), 50):
                batch = missing[offset:offset + 50]
                if any(not re.fullmatch(r"[A-Z0-9]{8}", key) for key in batch):
                    raise ZoteroError("Invalid parent item key")
                parents, parent_version = self.collect({"itemKey": ",".join(batch)})
                # The local API may include descendants of requested items.
                # They are not new search hits and must not expand the corpus.
                parents = [x for x in parents if x["key"] in batch]
                if {x["key"] for x in parents} != set(batch) or parent_version != version:
                    raise ZoteroError("Missing parents or library changed; retry")
                by_key.update({x["key"]: x for x in parents})
        if any(x["data"].get("parentItem") not in by_key for x in by_key.values()
               if x["data"].get("parentItem")):
            raise ZoteroError("Unresolved parent chain")
        papers = []
        for key, item in sorted(by_key.items()):
            data = item["data"]
            if data.get("itemType") in {"attachment", "annotation", "note"}:
                continue
            papers.append({"local_item_key": key, "item_type": data.get("itemType"),
                           "title": data.get("title", ""), "doi": data.get("DOI", ""),
                           "url": data.get("url", ""), "date": data.get("date", ""),
                           "source_class": "unresolved", "verification": "not_assessed"})
        final = self.page({"q": query, "qmode": "everything", "limit": 1})
        if final.total != len(hits) or final.version != version:
            raise ZoteroError("Library changed during parent resolution; retry")
        return {"schema_version": 1, "query": query, "library_version": version,
                "consistency": "version_and_count_checked" if version is not None else "count_only",
                "search_hits": len(hits), "unique_papers": len(papers), "papers": papers,
                "limitation": "Search covers Zotero indexed content, not guaranteed full PDF coverage."}
