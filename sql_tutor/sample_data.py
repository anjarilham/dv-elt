# sample_data.py
#
# Builds the small Dataverse-style sample the lessons read, in the same folder layout as the
# real raw bucket (hive-style key=value folders):
#
#   sample_data/extract_date=<date>/run_id=<id>/entity=dataset_metadata/dataverse=<name>/part-00000.jsonl
#
# Two runs of one dataverse, four datasets, chosen so that every lesson has something to show:
#   DS1  released, changed between the runs (title, one more file) -> de-duplication, history
#   DS2  deaccessioned (no latestVersion)                           -> NULL handling, UNNEST gotcha
#   DS3  new in run B, invalid publication date, UTC+7 timestamp    -> safe casting, time zones
#   DS4  draft, only in run A (no versionNumber / releaseTime)      -> "what disappeared" (EXCEPT)

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE / "sample_data"

RUN_A = ("2026-10-12", "20261012T000000Z_aaaaaa")
RUN_B = ("2026-10-13", "20261013T000000Z_bbbbbb")


def prim(name, value):
    """A sub-field of a compound field, as the Dataverse API writes it."""
    return {"typeName": name, "multiple": False, "typeClass": "primitive", "value": value}


def field(name, cls, value, multiple=False):
    return {"typeName": name, "multiple": multiple, "typeClass": cls, "value": value}


def data_file(file_id, name, size, restricted=False, content_type="text/csv"):
    return {"label": name, "restricted": restricted,
            "dataFile": {"id": file_id, "filename": name, "contentType": content_type, "filesize": size}}


def dataset(n, *, fields=None, files=None, state="RELEASED", version=(1, 0), published="2025-03-01",
            created="2024-12-19 06:20:56", updated="2025-01-01T00:00:00Z", released="2025-03-01T04:00:00Z",
            terms="", deaccessioned=False):
    data = {"id": 1000 + n, "identifier": f"DS{n}", "protocol": "doi", "authority": "10.99999",
            "publisher": "BRIN", "publicationDate": published}
    if not deaccessioned:
        data["latestVersion"] = {
            "datasetPersistentId": f"doi:10.99999/DS{n}",
            "versionNumber": version[0] if state == "RELEASED" else None,
            "versionMinorNumber": version[1] if state == "RELEASED" else None,
            "versionState": state,
            "createTime": created, "lastUpdateTime": updated, "releaseTime": released,
            "termsOfUse": terms,
            "license": {"name": "CC0 1.0", "uri": "http://creativecommons.org/publicdomain/zero/1.0"},
            "metadataBlocks": {"citation": {"fields": fields or []}},
            "files": files or [],
        }
    return {"status": "OK", "data": data}


def authors(*people):
    out = []
    for p in people:
        a = {"authorName": prim("authorName", p["name"])}
        if p.get("affiliation"):
            a["authorAffiliation"] = prim("authorAffiliation", p["affiliation"])
        if p.get("orcid"):
            a["authorIdentifierScheme"] = prim("authorIdentifierScheme", "ORCID")
            a["authorIdentifier"] = prim("authorIdentifier", p["orcid"])
        out.append(a)
    return field("author", "compound", out, multiple=True)


def common(title, people, depositor, deposited, subjects, keywords=(), description=None, grants=()):
    f = [field("title", "primitive", title), authors(*people),
         field("depositor", "primitive", depositor),
         field("dateOfDeposit", "primitive", deposited),
         field("subject", "controlledVocabulary", list(subjects), multiple=True)]
    if keywords:
        f.append(field("keyword", "compound", [{"keywordValue": prim("keywordValue", k)} for k in keywords], multiple=True))
    if description:
        f.append(field("dsDescription", "compound", [{"dsDescriptionValue": prim("dsDescriptionValue", description)}], multiple=True))
    if grants:
        f.append(field("grantNumber", "compound", [
            {k: prim(k, v) for k, v in g.items()} for g in grants], multiple=True))
    return f


DOE = {"name": "Doe, Jane", "affiliation": "BRIN"}
ROE = {"name": "Roe, Rick"}

RUNS = {
    RUN_A: [
        dataset(1, fields=common("Rice yield survey", [DOE, ROE], "Doe, Jane", "2024-12-19",
                                 ["Agricultural Sciences"], ["rice", "yield"], "Household survey of rice yield."),
                files=[data_file(1, "data.csv", 10240), data_file(2, "notes.pdf", 2048, True, "application/pdf")]),
        dataset(2, deaccessioned=True, published="2024-06-01"),
        dataset(4, state="DRAFT", published=None, released=None, created="2025-02-01 08:00:00",
                updated="2025-02-10 09:00:00",
                fields=common("Soil moisture sensors", [{"name": "Lee, Min"}], "Lee, Min", "2025-02-01",
                              ["Earth and Environmental Sciences", "Engineering"],
                              grants=[{"grantNumberAgency": "Kemdikbud"}])),
    ],
    RUN_B: [
        dataset(1, version=(1, 1), updated="2025-06-01T00:00:00Z",
                fields=common("Rice yield survey 2025", [DOE, ROE], "Doe, Jane", "2024-12-19",
                              ["Agricultural Sciences"], ["rice", "yield"], "Household survey of rice yield."),
                files=[data_file(1, "data.csv", 10240), data_file(2, "notes.pdf", 2048, True, "application/pdf"),
                       data_file(3, "codebook.pdf", 4096, False, "application/pdf")]),
        dataset(2, deaccessioned=True, published="2024-06-01"),
        dataset(3, published="2025-13-45", created="2025-05-19 10:00:00", updated="2025-05-20T08:30:00+07:00",
                released="2025-05-21T01:00:00Z", terms="Cite the source.",
                fields=common("Fish landing records",
                              [{"name": "Kim, Ana", "affiliation": "BRIN", "orcid": "0000-0000-0000-0001"}],
                              "Kim, Ana", "2025-05-19",
                              ["Agricultural Sciences", "Earth and Environmental Sciences"], ["fish"],
                              "Daily fish landings at three ports.",
                              grants=[{"grantNumberAgency": "BRIN", "grantNumberValue": "G-001"}]),
                files=[data_file(4, "landing.csv", 51200)]),
    ],
}


def build(root: Path = ROOT) -> None:
    for (extract_date, run_id), records in RUNS.items():
        folder = root / f"extract_date={extract_date}" / f"run_id={run_id}" / "entity=dataset_metadata" / "dataverse=orpertanian"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "part-00000.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")


def ensure_sample_data() -> None:
    if not any(ROOT.glob("*/*/entity=dataset_metadata/*/*.jsonl")):
        build()


if __name__ == "__main__":
    build()
    print(f"Sample data written to {ROOT}")
