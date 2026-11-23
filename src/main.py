#!/usr/bin/env python3
"""OParl reader: read any OParl 1.0/1.1 endpoint, emit normalised records.

Stdlib only, so the image stays small and there is nothing to break in a build.

The design rule comes from measurement on 12 endpoints (23 Nov 2026), not from
the spec: HTTP 200 is not evidence. zv-kisa answers every path with 200 plus an
OParl Error object; SOMACOS Session answers collections only under a body and
silently ignores `offset`; other vendors reject `modified_since` with 401.
So: every payload is checked for its OParl `type`, paging is chosen by probing,
and no filter is used that has not been probed first.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "oparl-reader/0.1 (Apify Actor; contact: anselm@posteo.de)"
TIMEOUT = 25
ALL_COLLECTIONS = ("organizations", "people", "locations", "meetings",
                   "agendas", "papers", "drafts", "files", "committees",
                   "participations", "elections")


def get(url):
    """(status, parsed_json_or_None, note). Never raises."""
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw, status = r.read(600_000), r.status
    except urllib.error.HTTPError as e:
        raw = e.read(4000) if e.fp else b""
        status = e.code
    except Exception as exc:                      # timeout, DNS, TLS, refused
        return None, None, "%s: %s" % (type(exc).__name__, exc)
    try:
        return status, json.loads(raw), None
    except Exception:
        return status, None, "non-JSON body: %s" % raw[:120].decode("utf-8", "replace")


def oparl_type(payload):
    return str(payload.get("type", "")) if isinstance(payload, dict) else ""


def is_system(payload):
    """A 200 with parseable JSON is NOT a System object (measured, zv-kisa)."""
    if not isinstance(payload, dict):
        return False
    return oparl_type(payload).endswith("/System") or "oparlVersion" in payload


def is_error(payload):
    return oparl_type(payload).endswith("/Error")


def collection(payload):
    """Normalise both collection shapes; None for anything that is not one."""
    if not isinstance(payload, dict) or is_error(payload):
        return None
    items = payload.get("data")
    if not isinstance(items, list):
        items = payload.get("items")
    if not isinstance(items, list):
        return None
    first = items[0] if items and isinstance(items[0], dict) else None
    return {"count": len(items),
            "sample_id": first.get("id") if first else None,
            "paging": {k: payload[k] for k in
                       ("pagination", "page", "total", "totalElements",
                        "numberOfElements") if k in payload}}


def probe_paging(url):
    """Choose paging by measurement. Two traps, both measured 23 Nov:
    SOMACOS silently ignores `offset` (page one forever), and SD.NET RIM
    refuses `limit`/`size` outright with HTTP 400 while happily accepting
    `page=N`. Probing with a parameter the server forbids is itself a bug."""
    st0, p0, _ = get(url)
    c0 = collection(p0) if st0 == 200 else None
    if c0 and c0["count"]:
        st2, p2, _ = get("%s?page=2" % url)
        c2 = collection(p2) if st2 == 200 else None
        if c2 and c2["sample_id"] and c2["sample_id"] != c0["sample_id"]:
            return {"mode": "page", "size_param": None,
                    "page_size_observed": c0["count"]}
        return {"mode": "single_page", "size_param": None,
                "page_size_observed": c0["count"]}
    for size_param in ("size", "limit"):
        st1, p1, _ = get("%s?%s=1" % (url, size_param))
        c1 = collection(p1) if st1 == 200 else None
        if not c1:
            continue
        st2, p2, _ = get("%s?%s=1&page=2" % (url, size_param))
        c2 = collection(p2) if st2 == 200 else None
        if c2 and c2["sample_id"] and c2["sample_id"] != c1["sample_id"]:
            return {"mode": "page", "size_param": size_param}
        st3, p3, _ = get("%s?%s=1&offset=1" % (url, size_param))
        c3 = collection(p3) if st3 == 200 else None
        if c3 and c3["sample_id"] and c3["sample_id"] != c1["sample_id"]:
            return {"mode": "offset", "size_param": size_param}
        return {"mode": "single_page", "size_param": size_param}
    return {"mode": "unreachable", "size_param": None}


def fetch(url, paging, page_size, max_items, log):
    """Page through one collection with the paging the server proved it has."""
    items, page, offset = [], 1, 0
    while len(items) < max_items:
        if paging["mode"] == "page" and paging["size_param"]:
            q = "%s?%s=%d&page=%d" % (url, paging["size_param"], page_size, page)
        elif paging["mode"] == "page":
            q = "%s?page=%d" % (url, page)
        elif paging["mode"] == "offset":
            q = "%s?%s=%d&offset=%d" % (url, paging["size_param"], page_size, offset)
        else:
            q = "%s?%s=%d" % (url, paging["size_param"] or "limit",
                              min(page_size, max_items))
        st, payload, note = get(q)
        col = collection(payload) if st == 200 else None
        if col is None:
            log.append("stop %s http=%s %s" % (url, st, note or oparl_type(payload)[:80]))
            break
        got = payload.get("data") if isinstance(payload.get("data"), list) else payload.get("items")
        items.extend(got)
        if col["count"] == 0 or col["count"] < page_size:
            break
        page += 1
        offset += page_size
        if page > 200:
            log.append("page cap reached at %s" % url)
            break
    return items[:max_items]


def flatten(obj, meta):
    """Keep the OParl object intact and add the fields a caller actually filters on."""
    loc = obj.get("location")
    out = {
        "id": obj.get("id"),
        "type": oparl_type(obj).rsplit("/", 1)[-1],
        "name": obj.get("name") or obj.get("fullName"),
        "start": obj.get("start"),
        "end": obj.get("end"),
        "public": obj.get("public"),
        "location": (loc.get("name") if isinstance(loc, dict) else loc),
        "organizationIds": obj.get("organization"),
        "participantIds": obj.get("participant"),
        "license": meta["license"],
        "sourceSystem": meta["system_name"],
        "vendor": meta["vendor"],
        "fetchedAt": meta["fetched_at"],
        "raw": obj,
    }
    return {k: v for k, v in out.items() if v is not None}


_TOKEN_ENV = ("APIFY_TOKEN", "APIFY_API_TOKEN", "APFY_API_TOKEN",
              "ACTOR_API_TOKEN")
_STORE_ENV = ("APIFY_DEFAULT_KEY_VALUE_STORE_ID", "ACTOR_DEFAULT_KEY_VALUE_STORE_ID")
_DATASET_ENV = ("APIFY_DEFAULT_DATASET_ID", "ACTOR_DEFAULT_DATASET_ID")


def _env(names):
    """First env var that is set. The token VALUE is never printed anywhere."""
    for n in names:
        v = os.environ.get(n)
        if v:
            return v, n
    return None, None


def read_input():
    """Read the Actor input, and say how it went.

    Order: official client, then plain REST over the key-value store, then a
    local INPUT.json. The first two platform runs ended FAILED with an empty
    dataset and no readable log, so the plumbing now reports which route it
    took and which APIFY_*/ACTOR_* variable names exist at all. No token value
    is ever printed, only variable names and HTTP statuses.
    """
    diag = {"env_present": sorted(k for k in os.environ
                                  if k.startswith(("APIFY_", "APFY_", "ACTOR_")))}
    rec, why = read_input_via_sdk()
    diag["sdk"] = why
    if rec:
        diag["input_source"] = "sdk"
        return rec, diag
    token, tname = _env(_TOKEN_ENV)
    store, sname = _env(_STORE_ENV)
    diag["token_env"] = tname
    diag["store_env"] = sname
    if token and store:
        url = ("https://api.apify.com/v2/key-value-stores/%s/records/INPUT"
               "?token=%s" % (store, token))
        st, payload, note = get(url)
        diag["input_http"] = st
        if note:
            diag["input_note"] = note
        if isinstance(payload, dict) and payload:
            diag["input_source"] = "rest"
            return payload, diag
    if os.path.exists("INPUT.json"):
        with open("INPUT.json", encoding="utf-8") as fh:
            diag["input_source"] = "INPUT.json"
            return json.load(fh), diag
    diag["input_source"] = "none"
    return {}, diag


def push_via_sdk(items):
    """Preferred path: the official client, which finds the token, the run and
    the default dataset from the environment itself. My REST fallback guessed
    at env-var names; the first two platform runs produced an empty dataset,
    which is what a wrong guess looks like."""
    try:
        from apify_client import ApifyClient
    except Exception as exc:
        return "sdk import FAILED %s: %s" % (type(exc).__name__, exc)
    try:
        client = ApifyClient()
        ds = os.environ.get("APIFY_DEFAULT_DATASET_ID")
        if ds:
            client.dataset(ds).push_items(items)
            return "sdk dataset %s n=%d" % (ds, len(items))
        # No explicit dataset id: the client still knows the current run.
        rid = os.environ.get("APIFY_ACTOR_RUN_ID")
        if rid:
            run_data = client.run(rid).get()
            did = (run_data.get("defaultDatasetId"))
            client.dataset(did).push_items(items)
            return "sdk dataset via run %s n=%d" % (did, len(items))
        return "sdk: no dataset id in env"
    except Exception as exc:
        return "sdk push FAILED %s: %s" % (type(exc).__name__, exc)


def read_input_via_sdk():
    try:
        from apify_client import ApifyClient
        store = os.environ.get("APIFY_DEFAULT_KEY_VALUE_STORE_ID")
        if not store:
            return None, "sdk: no store id in env"
        rec = ApifyClient().key_value_store(store).get_record("INPUT")
        if isinstance(rec, dict) and rec:
            return rec, "sdk rest"
        return None, "sdk: INPUT record empty or missing"
    except Exception as exc:
        return None, "sdk input FAILED %s: %s" % (type(exc).__name__, exc)


def push(items):
    """Dataset push: official client first, plain REST second, file third.
    Returns a receipt string and never raises."""
    if not items:
        return "none (no items)"
    r = push_via_sdk(items)
    if r.startswith("sdk dataset"):
        return r
    token, _ = _env(_TOKEN_ENV)
    ds, _ = _env(_DATASET_ENV)
    rest = "skipped (no token/dataset env)"
    if token and ds:
        url = "https://api.apify.com/v2/datasets/%s/items?token=%s" % (ds, token)
        body = json.dumps(items).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/json", "User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return "rest dataset %s http=%s n=%d" % (ds, resp.status, len(items))
        except Exception as exc:
            rest = "rest FAILED %s: %s" % (type(exc).__name__, exc)
    with open("output.jsonl", "a", encoding="utf-8") as fh:
        for it in items:
            fh.write(json.dumps(it, ensure_ascii=False) + "\n")
    return "file only | %s | %s" % (r, rest)


def body_collection_links(body_id):
    """The OParl-correct way to find a body's collections: the Body object
    itself names them. Measured 23 Nov on more! Rubin (westerburg): the plural
    path under the body id returns an error object, while the Body object
    advertises singular links (meeting, paper, organization, person) that work.
    Guessing the plural path is therefore not enough."""
    st, payload, _ = get(body_id)
    if st != 200 or not isinstance(payload, dict) or is_error(payload):
        return {}, st
    links = {}
    for key, val in payload.items():
        if isinstance(val, str) and val.startswith("http"):
            links[key] = val
    return links, st


def resolve_collection(base, coll, bodies, log):
    """Return (url, how) or (None, why). Three routes, tried in order of cost."""
    plural = "%s/%s" % (base, coll)
    st, payload, _ = get(plural)
    if st == 200 and collection(payload):
        return plural, "top_level"
    top_status = st
    tried = []
    for bid in bodies[:3]:
        links, lst = body_collection_links(bid)
        # singular name first: that is what the vendor itself published
        for name in (coll.rstrip("s"), coll):
            cand = links.get(name)
            if not cand:
                continue
            st2, p2, _ = get(cand)
            if st2 == 200 and collection(p2):
                return cand, "body_link:%s" % name
            tried.append((cand, st2))
        cand = "%s/%s" % (bid.rstrip("/"), coll)
        st3, p3, _ = get(cand)
        if st3 == 200 and collection(p3):
            return cand, "body_path"
        tried.append((cand, st3))
    return None, {"top_level_http": top_status,
                  "bodies_tried": min(len(bodies), 3),
                  "attempts": tried[:6]}


def run(inp):
    entry = (inp.get("systemUrl") or "").strip()
    wanted = inp.get("collections") or ["meetings", "papers"]
    max_items = int(inp.get("maxItems") or 200)
    page_size = min(int(inp.get("pageSize") or 50), 100)
    log, report, items_out = [], {}, []
    if not entry:
        return {"error": "systemUrl is required"}, [], log

    st, sysobj, note = get(entry)
    if not is_system(sysobj):
        report["fatal"] = ("entry point is not an OParl System object "
                           "(http=%s type=%r): %s"
                           % (st, oparl_type(sysobj), note or ""))
        return report, [], log

    meta = {"system_name": sysobj.get("name"),
            "vendor": sysobj.get("vendor"),
            "license": sysobj.get("license"),
            "oparlVersion": sysobj.get("oparlVersion"),
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    report["system"] = meta

    base = (sysobj.get("system") or entry).rstrip("/")
    bodies = []
    body_url = sysobj.get("body") or sysobj.get("bodies")
    if body_url:
        stb, bp, _ = get(body_url)
        if stb == 200 and collection(bp):
            src = bp.get("data") if isinstance(bp.get("data"), list) else bp.get("items")
            bodies = [b.get("id") for b in (src or []) if isinstance(b, dict) and b.get("id")]
        report["bodies_http"] = stb
        report["bodies_count"] = len(bodies)

    for coll in wanted:
        if coll not in ALL_COLLECTIONS:
            log.append("unknown collection %r skipped" % coll)
            continue
        target, how = resolve_collection(base, coll, bodies, log)
        if target is None:
            report[coll] = dict(status="not_available", **how)
            continue
        paging = probe_paging(target)
        items = fetch(target, paging, page_size, max_items, log)
        report[coll] = {"status": "ok", "resolved_via": how, "url": target,
                        "paging": paging, "count": len(items),
                        "first_id": items[0].get("id") if items else None}
        for obj in items:
            items_out.append(flatten(obj, meta))
    return report, items_out, log


def main():
    inp, diag = read_input()
    try:
        report, items, log = run(inp)
    except Exception:
        import traceback
        report = {"fatal": "unhandled exception in run()",
                  "traceback": traceback.format_exc()[-3000:]}
        items, log = [], []
    report["plumbing"] = diag
    report["items_total"] = len(items)
    report["log"] = log[-20:]
    receipt_items = push(items) if items else "no items"
    # The report goes into the dataset as its own item, so a run that found
    # nothing is still readable. Exit code stays 0 on purpose: the platform
    # then reports SUCCEEDED/FAILED for what the *process* did, and an empty
    # dataset stops being ambiguous about whether the push or the code failed.
    receipt_report = push([{"_report": True, "report": report}])
    report["delivery_items"] = receipt_items
    report["delivery_report"] = receipt_report
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback
        print(traceback.format_exc())
        sys.exit(0)
