"""Read-only frozen cohort adapters and paired scores. Eval scripts are never executed."""
from __future__ import annotations

from collections import Counter, defaultdict
import dataclasses
from pathlib import Path
from decimal import Decimal
import re
from urllib.parse import parse_qs, urlsplit

from .common import Stop, digest, dumps, loads, no_symlinks, page_limit
from .policy import Page, canonical_url
from .runner import baseline
from .schema import FIELDS, VERSION, validator

COHORTS = (
    ("root", "manifest.json", "corpus", "2026-10-29"),
    ("d1030", "d1030/manifest.json", "d1030/corpus", "2026-10-30"),
    ("b3", "d1030/manifest_b3.json", "d1030/corpus", "2026-10-30"),
    ("d1102", "d1102/manifest_ho.json", "d1102/corpus_ho", "2026-11-02"),
)
# Transcription of human headings in b3_gold.json, not historical model predictions.
B3_WRITING_CID = {"1810795", "1810802", "1810745"}
B3_NON_WRITING_CID = {"1810907", "1810911", "1810936", "1810962", "1810974", "1810982", "1811022", "1811028",
                      "1810742", "1810749", "1810750", "1810852", "1810867", "1810893", "1810899"}
# The x4 translation-support group, identified by heading in the frozen B3 corpus.
B3_TRANSLATION = {"15832", "15836", "15881", "15889"}


class Inputs:
    def __init__(self, root):
        self.root = no_symlinks(root).resolve(strict=True)
        self.files = {}

    def read(self, relative):
        p = no_symlinks(self.root / relative)
        if self.root not in p.resolve().parents or not p.is_file():
            raise Stop("unsafe_eval_path")
        before = p.stat()
        data = p.read_bytes()
        after = p.stat()
        if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
            raise Stop("eval_input_changed")
        entry = {"sha256": digest(data), "mtime_ns": after.st_mtime_ns, "size": after.st_size}
        if str(relative) in self.files and self.files[str(relative)] != entry:
            raise Stop("eval_input_changed")
        self.files[str(relative)] = entry
        return data

    def json(self, relative):
        return loads(self.read(relative))

    def checksum(self, path, expected_names):
        checks = {}
        for line in self.read(path).decode().splitlines():
            sha, name = line.split()
            if not re.fullmatch("[0-9a-f]{64}", sha) or name not in expected_names:
                raise Stop("invalid_eval_checksum")
            checks[name] = sha
        if set(checks) != set(expected_names):
            raise Stop("invalid_eval_checksum")
        for name, sha in checks.items():
            relative = str(Path(path).parent / name)
            if digest(self.read(relative)) != sha:
                raise Stop("eval_checksum_mismatch")

    def verify(self):
        for name, original in list(self.files.items()):
            self.read(name)
            if original != self.files[name]:
                raise Stop("eval_input_changed")


@dataclasses.dataclass
class Item:
    cohort: str
    url: str
    as_of: str
    page: Page = dataclasses.field(repr=False)
    body_hash: str
    text_hash: str
    language: str
    rules: dict
    gold_real_call: object = None
    aggregate: bool = True

    @property
    def key(self):
        return canonical_url(self.url), self.body_hash, self.as_of


def body(inputs, directory, url, metadata):
    sha = metadata.get("sha", "")
    if not isinstance(sha, str) or not re.fullmatch("[0-9a-f]{64}", sha) or sha != digest(url):
        raise Stop("invalid_corpus_sha")
    return inputs.read(str(Path(directory) / (sha + ".bin")))


def b3_gold(manifest, gold):
    if not isinstance(gold, dict) or set(gold.get("writing", [])) != {
        "cid=1810795 Asian Playwrights Meeting artist call",
        "cid=1810802 문학주간 우리동네 프로그램 모집 (ambiguous)",
        "cid=1810745 글로벌사우스 인적교류 참여자 공모 (ambiguous)",
        "ltikorea bbsIdx x4 (translation support calls)"}:
        raise Stop("unsupported_b3_gold")
    if {line.split()[0] for line in gold.get("non_writing", [])} != B3_NON_WRITING_CID:
        raise Stop("unsupported_b3_gold")
    mapped = {}
    for url in manifest:
        p = urlsplit(url)
        q = parse_qs(p.query)
        cid, idx = q.get("cid", [None])[0], q.get("bbsIdx", [None])[0]
        if p.hostname == "www.arko.or.kr" and cid in B3_WRITING_CID | B3_NON_WRITING_CID:
            mapped[url] = cid in B3_WRITING_CID
        if p.hostname == "www.ltikorea.or.kr" and idx in B3_TRANSLATION:
            mapped[url] = True
    if len(mapped) != 22 or sum(mapped.values()) != 7:
        raise Stop("incomplete_b3_mapping")
    return mapped


def load_cohorts(root, max_page_utf8_bytes=None):
    # One threshold shared with the runner: a lowered config must also screen
    # cohort eligibility rather than silently diverging at a copied literal.
    max_page_utf8_bytes = page_limit() if max_page_utf8_bytes is None else max_page_utf8_bytes
    if type(max_page_utf8_bytes) is not int or not 1 <= max_page_utf8_bytes <= 24000:
        raise Stop("invalid_config")
    inputs = Inputs(root)
    inputs.checksum("split_frozen.sha256", {"split.json", "manifest.json"})
    inputs.checksum("d1102/manifest_ho.sha256", {"manifest_ho.json"})
    split = inputs.json("split.json")
    root_manifest = inputs.json("manifest.json")
    if not isinstance(split, dict) or set(split) != set(root_manifest) or set(split.values()) - {"tune", "heldout"}:
        raise Stop("unsupported_split")
    # Exclude every known tune URL and body, even when repeated in a later cohort.
    tune_urls, tune_hashes = set(), set()
    tune_sources = [(root_manifest, "corpus", {u for u in split if split[u] == "tune"})]
    for path, directory in (("d1101/manifest2.json", "d1101/corpus2"), ("d1102/manifest_t.json", "d1102/corpus_t")):
        if (inputs.root / path).exists():
            man = inputs.json(path)
            tune_sources.append((man, directory, set(man)))
    for man, directory, urls in tune_sources:
        tune_urls.update(urls)
        for url in sorted(urls):
            try:
                canonical_url(url)
                canonical_url(man[url].get("final_url", url))
            except Stop:
                continue
            m = man[url]
            if m.get("outcome") == "ok" and (inputs.root / directory / (str(m.get("sha")) + ".bin")).exists():
                tune_hashes.add(digest(body(inputs, directory, url, m)))
    items, exclusions, overlaps = [], [], []
    seen_urls, seen_bodies, mapping = {}, {}, {}
    for cohort, path, directory, as_of in COHORTS:
        manifest = inputs.json(path)
        if not isinstance(manifest, dict):
            raise Stop("unsupported_manifest")
        if cohort == "b3":
            mapping = b3_gold(manifest, inputs.json("d1030/b3_gold.json"))
        for url, m in sorted(manifest.items()):
            if cohort == "root" and split[url] != "heldout":
                continue
            try:
                if not isinstance(m, dict):
                    raise Stop("unsupported_manifest")
                canonical_url(url)
                final = canonical_url(m.get("final_url", url))
                if m.get("outcome") != "ok" or type(m.get("status")) is not int or not 200 <= m["status"] < 300:
                    raise Stop("cached_fetch_denied")
                if m.get("ctype", "").split(";")[0] not in ("text/html", "application/xhtml+xml"):
                    raise Stop("unsupported_content")
                data = body(inputs, directory, url, m)
                body_hash = digest(data)
                if url in tune_urls or body_hash in tune_hashes:
                    raise Stop("tune_overlap")
                page = Page(url, final, data, (canonical_url(url), final), "offline_cached")
                doc = page.doc()
                if len(doc.text().encode("utf-8")) > max_page_utf8_bytes:
                    raise Stop("page_too_large")
                primary = seen_urls.get(canonical_url(url)) or seen_bodies.get(body_hash)
                if primary:
                    overlaps.append({"cohort": cohort, "url": url, "primary": primary})
                seen_urls.setdefault(canonical_url(url), cohort)
                seen_bodies.setdefault(body_hash, cohort)
                language = "ko" if doc.lang.startswith("ko") or len(re.findall("[가-힣]", doc.text()[:5000])) > 200 else "other"
                items.append(Item(cohort, url, as_of, page, body_hash, digest(doc.text()), language,
                                  baseline(doc, as_of), mapping.get(url) if cohort == "b3" else None, not primary))
            except FileNotFoundError:
                exclusions.append({"cohort": cohort, "url": url, "reason": "missing_corpus"})
            except Stop as exc:
                if str(exc) in ("invalid_corpus_sha", "unsupported_manifest", "unsafe_eval_path", "eval_input_changed"):
                    raise
                exclusions.append({"cohort": cohort, "url": url, "reason": str(exc)})
    inputs.verify()
    return items, {"exclusions": exclusions, "overlaps": overlaps, "b3_gold_mapping": mapping,
                   "inputs": inputs.files, "offline_provenance": "cached bytes; robots/redirect history unavailable"}, inputs


def predictions(path):
    if path is None:
        return {}
    if isinstance(path, (list, tuple)):
        combined = {}
        for file in path:
            for key, row in predictions(file).items():
                if key in combined and combined[key] != row:
                    raise Stop("conflicting_predictions")
                combined[key] = row
        return combined
    result = {}
    with open(no_symlinks(path), encoding="utf-8") as f:
        for line in f:
            row = loads(line)
            if not isinstance(row, dict):
                raise Stop("invalid_prediction")
            if row.get("outcome") != "ok":
                continue
            try:
                requested, body_sha, as_of = row["requested_url"], row["body_sha256"], row["as_of"]
            except KeyError:
                raise Stop("invalid_prediction") from None
            if not all(isinstance(value, str) for value in (requested, body_sha, as_of)):
                raise Stop("invalid_prediction")
            key = (canonical_url(requested), body_sha, as_of)
            if key in result and result[key] != row:
                raise Stop("conflicting_predictions")
            result[key] = row
    return result


def boolean_metrics(pairs):
    tp = sum(g is True and p is True for g, p in pairs)
    fp = sum(g is False and p is True for g, p in pairs)
    fn = sum(g is True and p is not True for g, p in pairs)
    tn = sum(g is False and p is False for g, p in pairs)
    covered = sum(p is not None for _, p in pairs)
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    return {"labeled": len(pairs), "covered": covered, "abstentions": len(pairs) - covered,
            "tp": tp, "fp": fp, "fn_including_abstentions": fn, "tn": tn,
            "precision": precision, "recall": recall,
            "f1": 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else None,
            # Abstentions count as wrong end-to-end; covered accuracy is separate
            # so the two denominators cannot be confused.
            "accuracy_end_to_end": sum(g == p for g, p in pairs if p is not None) / len(pairs) if pairs else None,
            "accuracy_covered": sum(g == p for g, p in pairs if p is not None) / covered if covered else None,
            "reason": None if pairs else "missing_gold"}



def field_comparison(item, fields, has_prediction=True):
    """Exact set comparison; extra rule values remain visible as ambiguity."""
    def decimal(value):
        return str(Decimal(str(value)).normalize()) if value is not None else None
    def pay_signature(value):
        return dumps({k: decimal(value.get(k)) if k.startswith("amount_") else value.get(k)
                      for k in ("amount_min", "amount_max", "currency", "unit")})
    extensions = item.rules["extensions"]
    rule_pay = [e for e in extensions if e["field"] == "compensation.rate"]
    states = sorted({"unpaid" if e["value_state"] == "explicitly_none" else "paid" for e in rule_pay}) or ["unknown"]
    amounts = sorted({pay_signature(e["value"]) for e in rule_pay if e["value_state"] == "stated"})
    llm_pay = fields["pay"]["value"] if fields["pay"] else None
    regions = sorted({dumps(e["value"]) for e in extensions if e["field"] == "eligibility.region"})
    statements = sorted({e["value"]["text"] for e in extensions if e["field"] == "eligibility.statement"})
    llm_eligibility = fields["eligibility"]["value"] if fields["eligibility"] else None
    # This projection is explicitly rules-derived from the LLM quote. Free text
    # cannot be converted to independently adjudicated constraints automatically.
    projected_regions = []
    if fields["eligibility"]:
        from ...profiles import pitchboard as prof
        quote = fields["eligibility"]["quote"]
        if prof.WORLD_RE.search(quote):
            projected_regions = [dumps({"scope": "worldwide"})]
        elif prof.region_value(quote):
            projected_regions = [dumps(prof.region_value(quote)[1])]
    return {"pay_state_rules": states, "pay_state_llm": (llm_pay["state"] if llm_pay else "unknown") if has_prediction else None,
            "pay_state_agrees": states == [llm_pay["state"] if llm_pay else "unknown"] if has_prediction else None,
            "pay_amount_rules": amounts, "pay_amount_llm": pay_signature(llm_pay) if llm_pay else None,
            "pay_amount_agrees": amounts == [pay_signature(llm_pay)] if llm_pay else None,
            "eligibility_regions_rules": regions, "eligibility_quote_regions": projected_regions,
            "eligibility_region_agrees": regions == projected_regions if llm_eligibility else None,
            "eligibility_statements_rules": statements,
            "eligibility_statement_agrees": statements == [llm_eligibility["text"]] if llm_eligibility else None}


def score(items, rows):
    groups = defaultdict(list)
    records = []
    for item in items:
        row = rows.get(item.key)
        fields = {f: None for f in FIELDS}
        if row is not None:
            if not isinstance(row, dict):
                raise Stop("invalid_prediction")
            if row.get("text_sha256") != item.text_hash:
                raise Stop("prediction_text_mismatch")
            proposals = row.get("fields")
            validation = validator(row.get("schema", VERSION))
            expected = set(FIELDS) - {"pb-shadow-1": {"submission_fee", "simultaneous"}, "pb-shadow-2": {"simultaneous"}}.get(row.get("schema"), set())
            if not isinstance(proposals, dict) or set(proposals) != expected:
                raise Stop("invalid_prediction_fields")
            try:
                raw = {f: ({"value": proposals[f]["value"], "quote": proposals[f]["quote"]}
                           if proposals[f] is not None else None) for f in expected}
            except (KeyError, TypeError):
                # Foreign/hand-edited rows must become a fixed Stop, never KeyError.
                raise Stop("invalid_prediction_fields") from None
            fields, reasons = validation(raw, item.page.doc(), item.as_of)
            fields.setdefault("submission_fee", None)
            fields.setdefault("simultaneous", None)
            if reasons:
                raise Stop("prediction_evidence_invalid")
        call = fields["open_call"]
        real = call["value"]["is_real_call"] if call else None
        current = (call["value"]["is_real_call"] and call["value"]["status"] == "open") if call else None
        record = {"cohort": item.cohort, "url": item.url, "body_sha256": item.body_hash,
                  "text_sha256": item.text_hash, "as_of": item.as_of, "language": item.language,
                  "gold_real_call": item.gold_real_call, "rules": item.rules, "llm": fields,
                  "real": real, "current": current, "has_prediction": row is not None,
                  "aggregate": item.aggregate, "comparison": field_comparison(item, fields, row is not None)}
        records.append(record)
        groups[item.cohort].append(record)
        groups[item.cohort + "/" + item.language].append(record)
        if item.aggregate:
            groups["deduplicated"].append(record)
    summaries = {}
    for group, rs in groups.items():
        paired = [r for r in rs if r["current"] is not None]
        deadline_paired = [r for r in rs if r["llm"]["deadline"] and r["llm"]["deadline"]["value"]["date"]]
        def agreement(name):
            values = [r["comparison"][name] for r in rs if r["comparison"][name] is not None]
            return {"paired": len(values), "agreement": sum(values) / len(values) if values else None}
        summaries[group] = {
            "eligible_pages": len(rs), "predictions": sum(r["has_prediction"] for r in rs),
            "paired_current_open": len(paired),
            "current_open_agreement": sum(r["current"] == r["rules"]["current_open"] for r in paired) / len(paired) if paired else None,
            "deadline_paired": len(deadline_paired),
            "deadline_agreement": sum([r["llm"]["deadline"]["value"]["date"]] == r["rules"]["deadline"] for r in deadline_paired) / len(deadline_paired) if deadline_paired else None,
            "pay_state": agreement("pay_state_agrees"),
            "pay_amount": agreement("pay_amount_agrees"),
            "eligibility_statement": agreement("eligibility_statement_agrees"),
            "eligibility_region_from_quote_rules": agreement("eligibility_region_agrees"),
            "field_coverage": {f: sum(r["llm"][f] is not None for r in rs) for f in FIELDS},
            "real_call_gold": {model: boolean_metrics([(r["gold_real_call"], r["real"] if model == "llm" else r["rules"]["real_call"])
                                                       for r in rs if r["gold_real_call"] is not None]) for model in ("rules", "llm")},
            "current_open_gold": {"accuracy": None, "labeled": 0, "reason": "missing_gold"},
            "extraction_gold": {f: {"accuracy": None, "labeled": 0, "reason": "missing_gold"} for f in FIELDS[1:]}}
    return {"summaries": summaries, "pages": records,
            "notes": ["Agreement is not accuracy.", "B3 labels writing-call existence, not current-open status.",
                      "No independent deadline/pay/eligibility gold exists in these cohorts.",
                      "Eligibility region agreement projects the LLM quote through current region rules; it is not independent accuracy.",
                      "Missing predictions remain in eligible denominators; exclusions are reported separately.",
                      "Access-wall screening can leave very few gold positives (the observed B3 set leaves 3 positives / 15 negatives); label metrics are low-power and a human must adjudicate borderline cached walls before enabling."]}
