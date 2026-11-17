"""Community model sources for MyPrintCloud.

One async client class per site, all returning the same normalised shapes:

  summary / detail dict:
    {source, id, title, author, thumb, images[], likes, downloads,
     description_html, files: [{id, name, size}], url, license}
    (search/trending results leave description_html="" and files=[]; call detail())

  download(id, file_id) -> (filename, bytes)

Endpoint status as probed 2026-11-01 from a residential IP (see report in the
docstring of each class). Requires: httpx.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
from typing import Any
from urllib.parse import quote, unquote, urlparse

import httpx

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
TIMEOUT = httpx.Timeout(30.0, connect=15.0)


class SourceError(RuntimeError):
    """Generic failure talking to a model site."""


class AuthRequired(SourceError):
    """The operation needs a token/cookie that was not supplied or was rejected."""


class Blocked(SourceError):
    """Cloudflare (or similar) served a bot challenge instead of data."""


def _check_blocked(r: httpx.Response, source: str) -> None:
    if r.headers.get("cf-mitigated") == "challenge" or (
        r.status_code == 403 and "Just a moment" in r.text[:600]
    ):
        raise Blocked(f"{source}: Cloudflare challenge (HTTP {r.status_code}) on {r.request.url}")


def _item(source: str, **kw: Any) -> dict:
    d = {"source": source, "id": "", "title": "", "author": "", "thumb": "", "images": [],
         "likes": 0, "downloads": 0, "description_html": "", "files": [], "url": "", "license": ""}
    d.update({k: v for k, v in kw.items() if v is not None})
    d["id"] = str(d["id"])
    return d


def _filename_from(resp: httpx.Response, fallback: str) -> str:
    cd = resp.headers.get("content-disposition", "")
    m = re.search(r"filename\*=UTF-8''([^;]+)", cd) or re.search(r'filename="?([^";]+)"?', cd)
    if m:
        return unquote(m.group(1))
    name = unquote(urlparse(str(resp.url)).path.rsplit("/", 1)[-1])
    return name or fallback


class _Base:
    source = "base"

    def __init__(self, client: httpx.AsyncClient | None = None):
        self._own = client is None
        self.client = client or httpx.AsyncClient(
            headers={"User-Agent": UA, "Accept": "application/json, text/plain, */*"},
            timeout=TIMEOUT, follow_redirects=True)

    async def aclose(self):
        if self._own:
            await self.client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()

    async def _fetch_file(self, url: str, fallback_name: str, headers: dict | None = None) -> tuple[str, bytes]:
        r = await self.client.get(url, headers=headers or {})
        _check_blocked(r, self.source)
        r.raise_for_status()
        return _filename_from(r, fallback_name), r.content


# --------------------------------------------------------------------------- #
# Printables (Prusa) -- GraphQL, fully anonymous incl. downloads (verified)
# --------------------------------------------------------------------------- #
class Printables(_Base):
    """api.printables.com/graphql/ - search, trending, detail and file download
    all work anonymously (verified). www.printables.com HTML is Cloudflare-challenged
    (403) but the API host is not. Introspection is disabled.

    file_id format: "<type>:<id>" where type is stl | gcode | sla | other
    (values of DownloadFileTypeEnum)."""
    source = "printables"
    GQL = "https://api.printables.com/graphql/"
    MEDIA = "https://media.printables.com/"
    PAGE_SIZE = 24

    _LIST_FIELDS = ("id name slug likesCount downloadCount nsfw "
                    "image { filePath } user { publicUsername handle }")
    Q_SEARCH = ("query Search($q: String!, $limit: Int, $offset: Int) { "
                "searchPrints2(query: $q, limit: $limit, offset: $offset) { totalCount items { %s } } }"
                % _LIST_FIELDS)
    Q_TRENDING = ("query Trending($limit: Int, $cursor: String, $ordering: String) { "
                  "morePrints(limit: $limit, cursor: $cursor, ordering: $ordering) { cursor items { %s } } }"
                  % _LIST_FIELDS)
    Q_DETAIL = """query PrintDetail($id: ID!) { print(id: $id) {
        id name slug likesCount downloadCount nsfw summary description
        license { name abbreviation }
        user { publicUsername handle }
        image { filePath } images { filePath }
        stls { id name fileSize } gcodes { id name fileSize }
        slas { id name fileSize } otherFiles { id name fileSize } } }"""
    M_LINK = """mutation GetDownloadLink($id: ID!, $printId: ID!, $fileType: DownloadFileTypeEnum!,
        $source: DownloadSourceEnum!) { getDownloadLink(id: $id, printId: $printId,
        fileType: $fileType, source: $source) { ok errors { field messages } output { link ttl } } }"""

    def __init__(self, token: str | None = None, client=None, trending_ordering: str = "-likes_count_7_days"):
        super().__init__(client)
        # Prusa-account login (Bearer). Only used for adult results: logged out, Printables hides them.
        # Pasted straight from the browser's request headers, so accept "Bearer xyz", "authorization: Bearer xyz", quotes...
        t = (token or "").strip().strip('"\'').strip()
        t = re.sub(r"^(authorization\s*:\s*)?bearer\s+", "", t, flags=re.I).strip()
        self.token = t or None
        self.trending_ordering = trending_ordering
        self._cursors: dict[tuple[int, bool], str | None] = {}   # (page, adult) -> cursor (morePrints is cursor paged)

    async def _gql(self, query: str, variables: dict, auth: bool = False) -> dict:
        h = {"Content-Type": "application/json", "Origin": "https://www.printables.com",
             "Referer": "https://www.printables.com/"}
        if auth:
            if not self.token:
                raise AuthRequired("printables: adult models need your Printables login (Settings > Model site logins)")
            h["Authorization"] = f"Bearer {self.token}"
        r = await self.client.post(self.GQL, json={"query": query, "variables": variables}, headers=h)
        _check_blocked(r, self.source)
        if auth and (r.status_code in (401, 403) or "Token is invalid" in r.text[:200]):
            raise AuthRequired("printables: your Printables login has expired; paste a fresh one in Settings")
        j = r.json()
        if j.get("errors"):
            raise SourceError(f"printables: {j['errors'][0].get('message')}")
        return j["data"]

    def _img(self, p: dict | None) -> str:
        return self.MEDIA + p["filePath"] if p and p.get("filePath") else ""

    def _summary(self, p: dict) -> dict:
        u = p.get("user") or {}
        return _item(self.source, id=p["id"], title=p.get("name"),
                     author=u.get("publicUsername") or u.get("handle"),
                     thumb=self._img(p.get("image")), images=[self._img(p.get("image"))] if p.get("image") else [],
                     likes=p.get("likesCount") or 0, downloads=p.get("downloadCount") or 0,
                     url=f"https://www.printables.com/model/{p['id']}-{p.get('slug', '')}")

    def _list(self, items: list, safe: bool) -> list[dict]:
        return [self._summary(p) for p in items if not (safe and p.get("nsfw"))]

    async def search(self, q: str, page: int = 1, safe: bool = True) -> list[dict]:
        d = await self._gql(self.Q_SEARCH, {"q": q, "limit": self.PAGE_SIZE,
                                            "offset": (page - 1) * self.PAGE_SIZE}, auth=not safe)
        return self._list(d["searchPrints2"]["items"], safe)

    async def trending(self, page: int = 1, safe: bool = True) -> list[dict]:
        # cursor paging: walk forward from the last known page (adult and safe lists kept apart)
        a = not safe
        self._cursors.setdefault((1, a), None)
        known = max(k for k, b in self._cursors if b == a and k <= page)
        items: list = []
        for pg in range(known, page + 1):
            d = (await self._gql(self.Q_TRENDING, {"limit": self.PAGE_SIZE, "cursor": self._cursors[(pg, a)],
                                                   "ordering": self.trending_ordering}, auth=a))["morePrints"]
            self._cursors[(pg + 1, a)] = d.get("cursor")
            items = d["items"]
            if not d.get("cursor"):
                break
        return self._list(items, safe)

    async def detail(self, id: str) -> dict:
        p = (await self._gql(self.Q_DETAIL, {"id": str(id)}))["print"]
        if not p and self.token:  # adult models are invisible logged out
            p = (await self._gql(self.Q_DETAIL, {"id": str(id)}, auth=True))["print"]
        if not p:
            raise SourceError(f"printables: print {id} not found")
        d = self._summary(p)
        d["images"] = [self._img(i) for i in p.get("images") or []] or d["images"]
        d["description_html"] = p.get("description") or ""
        lic = p.get("license") or {}
        d["license"] = lic.get("abbreviation") or lic.get("name") or ""
        files = []
        for key, ftype in (("stls", "stl"), ("gcodes", "gcode"), ("slas", "sla"), ("otherFiles", "other")):
            for f in p.get(key) or []:
                files.append({"id": f"{ftype}:{f['id']}", "name": f["name"], "size": f.get("fileSize") or 0})
        d["files"] = files
        return d

    async def download(self, id: str, file_id: str) -> tuple[str, bytes]:
        ftype, _, fid = str(file_id).partition(":")
        if not fid:
            raise SourceError("printables: file_id must look like 'stl:12345' (use ids from detail())")
        v = {"id": fid, "printId": str(id), "fileType": ftype, "source": "model_detail"}
        res = (await self._gql(self.M_LINK, v))["getDownloadLink"]
        if not res.get("ok") and self.token:  # adult model: ask again logged in
            res = (await self._gql(self.M_LINK, v, auth=True))["getDownloadLink"]
        if not res.get("ok"):
            raise SourceError(f"printables: download refused: {res.get('errors')}")
        return await self._fetch_file(res["output"]["link"], f"{fid}.{ftype}")


# --------------------------------------------------------------------------- #
# Nexprint (Elegoo) -- REST gateway; browse anonymous, download needs login
# --------------------------------------------------------------------------- #
class Nexprint(_Base):
    """www.nexprint.com (Nuxt app). API base https://www.nexprint.com/gateway.
    Search / hot list / recommend / detail: anonymous (verified, HTTP 200, code 0).
    Download: PUT .../common/presigned-download-url returns {"code":401,"msg":"账号未登录"}
    (not logged in) anonymously; needs `Authorization: Bearer <access_token>` from a logged-in
    session (cookie `auth_token`, or localStorage ACCOUNT_INFO.access_token on nexprint.com).
    The authenticated download path was NOT verified (no account used).

    file_id formats (from detail()):
      "model:<fileId>"              raw model file (stl/3mf/step), type 2 MODEL_FILE, bizId = model id
      "setting:<settingId>:<fileId>" print-profile .3mf (sliced project), type 5 MODEL_CONFIG_FILE
      "attach:<fileId>"             attachment (gcode/bgcode/ctb/zip...), type 3 MODEL_ATTACHMENT
    """
    source = "nexprint"
    BASE = "https://www.nexprint.com/gateway/api/v1/model-library-server"
    PAGE_SIZE = 24
    SORT_RELEVANT, SORT_NEW, SORT_DOWNLOADS, SORT_RANDOM, SORT_HOT, SORT_COMPREHENSIVE = 1, 2, 3, 4, 5, 6

    def __init__(self, access_token: str | None = None, client=None, lang: str = "en"):
        super().__init__(client)
        self.token = access_token
        self.lang = lang

    def _h(self, auth: bool = False) -> dict:
        h = {"User-Lang": self.lang, "Referer": "https://www.nexprint.com/" + self.lang}
        if self.token and auth:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    async def _call(self, method: str, path: str, *, params=None, body=None, auth=False) -> Any:
        r = await self.client.request(method, self.BASE + path, params=params, json=body, headers=self._h(auth))
        _check_blocked(r, self.source)
        r.raise_for_status()
        j = r.json()
        if j.get("code") == 401:
            raise AuthRequired("nexprint: login required (pass access_token=...; get it from the "
                               "'auth_token' cookie of a logged-in nexprint.com browser session)")
        if j.get("code") != 0:
            raise SourceError(f"nexprint: code={j.get('code')} msg={j.get('msg')}")
        return j["data"]

    def _summary(self, m: dict) -> dict:
        st = m.get("statistics") or {}
        a = m.get("author") or {}
        return _item(self.source, id=m["id"], title=m.get("modelName"),
                     author=m.get("authorName") or a.get("nickname"),
                     thumb=m.get("coverImgUrl") or "", images=[m["coverImgUrl"]] if m.get("coverImgUrl") else [],
                     likes=st.get("staticsModelStarPeoPleCount") or 0,
                     downloads=st.get("staticsModelDownloadCount") or 0,
                     url=f"https://www.nexprint.com/{self.lang}/models/{m['id']}")

    async def _list(self, keyword: str, page: int, sort: int, time_frame: int | None = None) -> list[dict]:
        p = {"keyword": keyword, "sortType": sort, "pageNo": page, "pageSize": self.PAGE_SIZE}
        if time_frame is not None:
            p["timeFrame"] = time_frame
        d = await self._call("GET", "/model-base-info/search", params=p)
        return [self._summary(m) for m in d["pageResult"]["list"]]

    async def search(self, q: str, page: int = 1) -> list[dict]:
        return await self._list(q, page, self.SORT_COMPREHENSIVE)

    async def trending(self, page: int = 1) -> list[dict]:
        # same call the site's "Hot" list uses (sortType=5, timeFrame=2)
        return await self._list("", page, self.SORT_HOT, time_frame=2)

    async def recommended(self, page: int = 1) -> list[dict]:
        d = await self._call("GET", "/model-base-info/recommend/web",
                             params={"pageNo": page, "pageSize": self.PAGE_SIZE})
        return [self._summary(x["model"]) for x in d["list"] if x.get("contentType") == "MODEL" and x.get("model")]

    async def detail(self, id: str) -> dict:
        m = await self._call("GET", "/model-base-info/get", params={"id": str(id)})
        d = self._summary(m)
        d["images"] = [p["fileUrl"] for p in m.get("modelPicList") or [] if p.get("fileUrl")] or d["images"]
        d["description_html"] = m.get("modelDetail") or ""
        d["license"] = f"licenseType:{m.get('licenseType')}"   # numeric enum; mapping not decoded
        files = [{"id": f"model:{f['fileId']}", "name": f["fileName"], "size": int(f.get("fileSize") or 0)}
                 for f in m.get("modelFileList") or [] if f.get("fileExt")]   # ext=None rows duplicate profiles
        for s in m.get("settingInfoList") or []:
            sf = s.get("settingFile") or {}
            if sf.get("fileId"):
                files.append({"id": f"setting:{s['id']}:{sf['fileId']}",
                              "name": sf.get("fileName"), "size": int(sf.get("fileSize") or 0),
                              "profile": s.get("settingName"), "printable": s.get("isPrintable")})
        for f in m.get("modelAttachList") or []:
            files.append({"id": f"attach:{f['fileId']}", "name": f.get("fileName"), "size": int(f.get("fileSize") or 0)})
        d["files"] = files
        return d

    async def download(self, id: str, file_id: str) -> tuple[str, bytes]:
        if not self.token:
            raise AuthRequired("nexprint: downloads need access_token (Bearer token from a logged-in "
                               "nexprint.com session: 'auth_token' cookie)")
        kind, _, rest = str(file_id).partition(":")
        if kind == "model":
            info = {"fileId": rest, "type": "2", "bizId": str(id)}
        elif kind == "setting":
            sid, _, fid = rest.partition(":")
            info = {"fileId": fid, "type": "5", "bizId": sid}
        elif kind == "attach":
            info = {"fileId": rest, "type": "3", "bizId": str(id)}
        else:
            raise SourceError("nexprint: bad file_id (use ids from detail())")
        d = await self._call("PUT", "/common/presigned-download-url", body={"fileInfoList": [info]}, auth=True)
        url = ((d or {}).get("fileInfoList") or [{}])[0].get("resultUrl")
        if not url:
            raise SourceError(f"nexprint: no resultUrl in {d}")
        return await self._fetch_file(url, info["fileId"])


# --------------------------------------------------------------------------- #
# MakerWorld (Bambu Lab)
# --------------------------------------------------------------------------- #
class MakerWorld(_Base):
    """makerworld.com/api/v1. Detail (design-service/design/{id}) works anonymously
    (verified). search-service/* is Cloudflare-challenged most of the time from a plain
    HTTP client (403 cf-mitigated: challenge; 1 of ~6 attempts returned 200). Downloads need
    a Bambu login: design/{id}/model -> {"code":1,"error":"Please log in to download models."}.

    token: value of the `token` cookie from a logged-in makerworld.com browser session
    (Bambu account). Sent as both Cookie and Bearer header. Authenticated paths NOT verified.

    file_id formats: "instance:<instanceId>" (print-profile .3mf) or "model" (raw model files zip).
    """
    source = "makerworld"
    BASE = "https://makerworld.com/api/v1"
    PAGE_SIZE = 20

    def __init__(self, token: str | None = None, client=None):
        super().__init__(client)
        self.token = token

    def _h(self, auth=False) -> dict:
        h = {"Referer": "https://makerworld.com/en", "x-bbl-client-type": "web"}
        if auth and self.token:
            h["Authorization"] = f"Bearer {self.token}"
            h["Cookie"] = f"token={self.token}"
        return h

    async def _get(self, path: str, params=None, auth=False) -> Any:
        r = await self.client.get(self.BASE + path, params=params, headers=self._h(auth))
        _check_blocked(r, self.source)
        j = r.json() if "json" in r.headers.get("content-type", "") else {}
        if r.status_code in (401, 403) or (isinstance(j, dict) and "log in" in str(j.get("error", ""))):
            raise AuthRequired(f"makerworld: login required ({j.get('error') or r.status_code}); "
                               "pass token= (the 'token' cookie of a logged-in makerworld.com session)")
        r.raise_for_status()
        return j

    def _summary(self, h: dict) -> dict:
        c = h.get("designCreator") or {}
        cover = h.get("cover") or h.get("coverUrl") or ""
        return _item(self.source, id=h["id"], title=h.get("title"), author=c.get("name") or c.get("handle"),
                     thumb=cover, images=[cover] if cover else [],
                     likes=h.get("likeCount") or 0, downloads=h.get("downloadCount") or 0,
                     url=f"https://makerworld.com/en/models/{h['id']}-{h.get('slug', '')}")

    async def search(self, q: str, page: int = 1) -> list[dict]:
        j = await self._get("/search-service/select/design2",
                            {"keyword": q, "limit": self.PAGE_SIZE, "offset": (page - 1) * self.PAGE_SIZE})
        return [self._summary(h) for h in j.get("hits") or []]

    async def trending(self, page: int = 1) -> list[dict]:
        # UNVERIFIED (challenged): the site's Trending tab endpoint
        j = await self._get("/search-service/select/design/nav",
                            {"navKey": "Trending", "limit": self.PAGE_SIZE, "offset": (page - 1) * self.PAGE_SIZE})
        return [self._summary(h) for h in j.get("hits") or []]

    async def detail(self, id: str) -> dict:
        j = await self._get(f"/design-service/design/{id}")
        if not j.get("id"):
            raise SourceError(f"makerworld: design {id} not found")
        d = self._summary(j)
        ext = j.get("designExtension") or {}
        pics = [p["url"] for p in ext.get("design_pictures") or [] if p.get("url")]
        d["images"] = pics or d["images"]
        d["description_html"] = j.get("summary") or ""
        d["license"] = j.get("license") or ""
        files = [{"id": "model", "name": f"{j.get('title', id)} (raw model files)",
                  "size": sum(int(m.get("modelSize") or 0) for m in ext.get("model_files") or []),
                  "contents": [m.get("modelName") for m in ext.get("model_files") or []]}]
        for inst in j.get("instances") or []:
            f3 = ((inst.get("extention") or {}).get("modelInfo") or {}).get("f3mf") or {}
            files.append({"id": f"instance:{inst['id']}", "name": f"{inst.get('title')}.3mf",
                          "size": int(f3.get("size") or 0)})
        d["files"] = files
        return d

    async def download(self, id: str, file_id: str) -> tuple[str, bytes]:
        if not self.token:
            raise AuthRequired("makerworld: downloads need a Bambu login token (pass token=...)")
        if file_id == "model":
            j = await self._get(f"/design-service/design/{id}/model", auth=True)
        elif str(file_id).startswith("instance:"):
            j = await self._get(f"/design-service/instance/{file_id.split(':', 1)[1]}/f3mf",
                                {"type": "download"}, auth=True)
        else:
            raise SourceError("makerworld: bad file_id (use ids from detail())")
        url = j.get("url")
        if not url:
            raise SourceError(f"makerworld: no url in response {str(j)[:200]}")
        name, data = await self._fetch_file(url, j.get("name") or f"{id}.3mf")
        return (j.get("name") or name), data


# --------------------------------------------------------------------------- #
# Thingiverse -- official REST API, needs an app token
# --------------------------------------------------------------------------- #
class Thingiverse(_Base):
    """api.thingiverse.com. Requires an App Token (create an app at
    https://www.thingiverse.com/developers -> "Create an App" -> copy the App Token).
    From the probing IP EVERY thingiverse host (api., www., cdn.) returned
    403 cf-mitigated: challenge even before auth, so none of these calls could be verified;
    field names follow the published API docs."""
    source = "thingiverse"
    BASE = "https://api.thingiverse.com"
    PAGE_SIZE = 24

    def __init__(self, app_token: str | None = None, client=None):
        super().__init__(client)
        self.token = app_token

    async def _get(self, path: str, params=None) -> Any:
        if not self.token:
            raise AuthRequired("thingiverse: app token required - create one at "
                               "https://www.thingiverse.com/developers and pass app_token=...")
        r = await self.client.get(self.BASE + path, params=params,
                                  headers={"Authorization": f"Bearer {self.token}"})
        _check_blocked(r, self.source)
        if r.status_code == 401:
            raise AuthRequired("thingiverse: app token rejected (401)")
        r.raise_for_status()
        return r.json()

    def _summary(self, t: dict) -> dict:
        c = t.get("creator") or {}
        thumb = t.get("preview_image") or t.get("thumbnail") or ""
        return _item(self.source, id=t["id"], title=t.get("name"), author=c.get("name"),
                     thumb=thumb, images=[thumb] if thumb else [],
                     likes=t.get("like_count") or 0, downloads=t.get("download_count") or 0,
                     url=t.get("public_url") or f"https://www.thingiverse.com/thing:{t['id']}")

    async def _search(self, term: str, page: int, sort: str) -> list[dict]:
        path = f"/search/{quote(term)}" if term else "/search/"
        j = await self._get(path, {"type": "things", "page": page, "per_page": self.PAGE_SIZE, "sort": sort})
        hits = j.get("hits", j) if isinstance(j, dict) else j
        return [self._summary(t) for t in hits or []]

    async def search(self, q: str, page: int = 1) -> list[dict]:
        return await self._search(q, page, "relevant")

    async def trending(self, page: int = 1) -> list[dict]:
        return await self._search("", page, "popular")

    async def detail(self, id: str) -> dict:
        t = await self._get(f"/things/{id}")
        d = self._summary(t)
        d["description_html"] = t.get("description_html") or t.get("description") or ""
        d["license"] = t.get("license") or ""
        imgs = await self._get(f"/things/{id}/images")
        urls = []
        for im in imgs or []:
            sizes = {(s.get("type"), s.get("size")): s.get("url") for s in im.get("sizes") or []}
            u = sizes.get(("display", "large")) or next(iter(sizes.values()), None)
            if u:
                urls.append(u)
        d["images"] = urls or d["images"]
        files = await self._get(f"/things/{id}/files")
        d["files"] = [{"id": f["id"], "name": f.get("name"), "size": f.get("size") or 0} for f in files or []]
        return d

    async def download(self, id: str, file_id: str) -> tuple[str, bytes]:
        if not self.token:
            raise AuthRequired("thingiverse: app token required")
        f = await self._get(f"/files/{file_id}")
        url = f.get("direct_url") or f"{self.BASE}/files/{file_id}/download"
        hdr = {"Authorization": f"Bearer {self.token}"} if url.startswith(self.BASE) else {}
        name, data = await self._fetch_file(url, f.get("name") or str(file_id), hdr)
        return f.get("name") or name, data


# --------------------------------------------------------------------------- #
# Cults3D -- official GraphQL API (HTTP Basic: username + API key from cults3d.com/en/api/keys).
# Field names from the published schema (manyfold3d's copy). The API gives metadata only: files
# are bought/downloaded on the website, so `download` only works if a blueprint's fileUrl is public.
# --------------------------------------------------------------------------- #
class Cults3D(_Base):
    source = "cults3d"
    URL = "https://cults3d.com/graphql"
    PAGE_SIZE = 24
    FIELDS = """slug name(locale: EN) url(locale: EN) illustrationImageUrl likesCount downloadsCount
                price(currency: USD) { cents formatted } creator { nick }"""

    def __init__(self, login: str | None = None, client=None):
        super().__init__(client)
        self.login = login  # "username:apikey"

    async def _q(self, query: str, variables: dict) -> dict:
        if not self.login or ":" not in self.login:
            raise AuthRequired("cults3d: needs your Cults3D username and API key "
                               "(create one at cults3d.com/en/api/keys)")
        user, key = self.login.split(":", 1)
        r = await self.client.post(self.URL, json={"query": query, "variables": variables}, auth=(user.strip(), key.strip()))
        _check_blocked(r, self.source)
        if r.status_code == 401:
            raise AuthRequired("cults3d: username or API key rejected")
        r.raise_for_status()
        j = r.json()
        if j.get("errors"):
            raise SourceError("cults3d: " + "; ".join(e.get("message", "?") for e in j["errors"])[:300])
        return j.get("data") or {}

    def _summary(self, c: dict) -> dict:
        price = c.get("price") or {}
        paid = (price.get("cents") or 0) > 0
        return _item(self.source, id=c.get("slug"), title=c.get("name"), author=(c.get("creator") or {}).get("nick"),
                     thumb=c.get("illustrationImageUrl") or "", likes=c.get("likesCount") or 0,
                     downloads=c.get("downloadsCount") or 0, url=c.get("url") or "",
                     price=price.get("formatted") if paid else "")

    async def _batch(self, q: str, page: int, sort: str, safe: bool = True) -> list[dict]:
        d = await self._q(f"""query($q: String!, $limit: Int, $offset: Int) {{
            creationsSearchBatch(query: $q, sort: {sort}, direction: DESC, onlySafe: {"true" if safe else "false"}, limit: $limit, offset: $offset) {{
              results {{ {self.FIELDS} }} }} }}""", {"q": q, "limit": self.PAGE_SIZE, "offset": (page - 1) * self.PAGE_SIZE})
        return [self._summary(c) for c in (d.get("creationsSearchBatch") or {}).get("results") or []]

    async def search(self, q: str, page: int = 1, safe: bool = True) -> list[dict]:
        return await self._batch(q, page, "BY_RELEVANCE", safe)

    async def trending(self, page: int = 1, safe: bool = True) -> list[dict]:
        d = await self._q(f"""query($limit: Int, $offset: Int) {{
            creationsBatch(sort: BY_DOWNLOADS, direction: DESC, onlyFree: true, safe: {"true" if safe else "false"}, limit: $limit, offset: $offset) {{
              results {{ {self.FIELDS} }} }} }}""", {"limit": self.PAGE_SIZE, "offset": (page - 1) * self.PAGE_SIZE})
        return [self._summary(c) for c in (d.get("creationsBatch") or {}).get("results") or []]

    async def detail(self, id: str) -> dict:
        d = await self._q(f"""query($slug: String!) {{ creation(slug: $slug) {{ {self.FIELDS}
            description(locale: EN) license {{ name(locale: EN) }} illustrations {{ imageUrl }}
            blueprints {{ id fileName fileUrl }} }} }}""", {"slug": id})
        c = d.get("creation")
        if not c:
            raise SourceError("cults3d: model not found")
        out = self._summary(c)
        note = ("<p><b>Paid model (" + out["price"] + ")</b>: buy and download it on Cults3D, then use "
                "<i>Print a file from this phone</i>.</p>") if out.get("price") else ""
        bps = c.get("blueprints") or []
        if not out.get("price") and bps and not any(b.get("fileUrl") for b in bps):
            # Cults3D's API never hands out file links: free files are downloaded on the website (free account)
            names = ", ".join(b.get("fileName") or "?" for b in bps[:8]) + (" …" if len(bps) > 8 else "")
            note = ("<p><b>Free</b>: Cults3D only lets you download on its website. Tap <i>Open</i> to get it there "
                    "(free account), then use <i>Print a file from this phone</i>.<br><small>Files: " + names + "</small></p>")
        out["description_html"] = note + (c.get("description") or "")
        out["license"] = (c.get("license") or {}).get("name") or ""
        out["images"] = [i["imageUrl"] for i in c.get("illustrations") or [] if i.get("imageUrl")] or ([out["thumb"]] if out["thumb"] else [])
        self._files = {str(b.get("id")): b for b in c.get("blueprints") or []}
        out["files"] = [] if out.get("price") else [{"id": str(b.get("id")), "name": b.get("fileName") or "model", "size": 0}
                                                  for b in c.get("blueprints") or [] if b.get("fileUrl")]
        return out

    async def download(self, id: str, file_id: str) -> tuple[str, bytes]:
        if str(file_id) not in getattr(self, "_files", {}):
            await self.detail(id)
        b = self._files.get(str(file_id)) or {}
        if not b.get("fileUrl"):
            raise SourceError("cults3d: no file link for that file")
        r = await self.client.get(b["fileUrl"])
        if r.status_code >= 400 or "text/html" in r.headers.get("content-type", ""):
            raise SourceError("Cults3D only lets you download this on its website: tap Open on cults3d, download it, "
                              "then use Print a file from this phone")
        return b.get("fileName") or _filename_from(r, "model.stl"), r.content


ALL_SOURCES = {"printables": Printables, "nexprint": Nexprint, "makerworld": MakerWorld,
               "thingiverse": Thingiverse, "cults3d": Cults3D}


async def _smoke():
    import os
    async with httpx.AsyncClient(headers={"User-Agent": UA, "Accept": "application/json, text/plain, */*"},
                                 timeout=TIMEOUT, follow_redirects=True) as c:
        sites = [Printables(client=c), Nexprint(access_token=os.getenv("NEXPRINT_TOKEN"), client=c),
                 MakerWorld(token=os.getenv("MAKERWORLD_TOKEN"), client=c),
                 Thingiverse(app_token=os.getenv("THINGIVERSE_TOKEN"), client=c)]
        for s in sites:
            try:
                res = await s.search("benchy", 1)
                first = res[0] if res else {}
                print(f"{s.source:12s} search('benchy'): {len(res)} results; first: "
                      f"{first.get('title')!r} by {first.get('author')!r} likes={first.get('likes')} "
                      f"dl={first.get('downloads')}")
            except SourceError as e:
                print(f"{s.source:12s} search('benchy'): {type(e).__name__}: {e}")
            except Exception as e:
                print(f"{s.source:12s} search('benchy'): ERROR {type(e).__name__}: {e}")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    asyncio.run(_smoke())
