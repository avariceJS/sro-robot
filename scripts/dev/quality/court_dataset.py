#!/usr/bin/env python3
"""Private, local court-document evaluation. Stdout contains aggregate counts only."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import statistics
import subprocess
import sys
import tempfile
import time
import unicodedata

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
EXTENSIONS = {".pdf", ".png", ".heic", ".jpg", ".jpeg", ".tif", ".tiff"}
LEGACY_FIELDS = ("fio_raw", "inn", "snils", "address", "address_kind", "case_number", "doc_date", "court")
FIELDS = LEGACY_FIELDS + ("entity_kind", "legal_name", "short_name", "legal_form", "debtor_name", "ogrn", "ogrnip", "kpp", "doc_type")
LAYERS = {"ocr_characters", "field_detection", "participant_attribution", "normalization", "source_location", "ambiguous_source", "other"}
DEFAULT_IMPORT_LIMIT = 100
MAX_RUN_DOCUMENTS = 10000
MAX_BYTES = 64 * 1024 * 1024
MEDIA = {"unknown", "real_photo", "scan", "text_pdf", "synthetic_distortion"}
SOURCES = {"existing-local", "manual-public-import", "licensed-local"}


@contextmanager
def dataset_lock(root):
    verify_storage(root)
    descriptor = os.open(root / ".writer.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another local dataset operation owns the lock") from None
        yield
    finally:
        os.close(descriptor)


def verify_storage(root):
    # A dedicated local directory, never a traversal into browser profiles or
    # unrelated data through a hand-edited/symlinked dataset path.
    if root.is_symlink():
        raise ValueError("Symlinked dataset root")
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError("Dataset storage boundary")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def identity(value):
    return digest(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode())


def private_root(path):
    path = Path(path).expanduser().resolve()
    if path.is_relative_to(ROOT) or ROOT.is_relative_to(path):
        raise ValueError("Private dataset must be outside the checkout")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.stat().st_mode & 0o077:
        raise ValueError("Private dataset directory must have mode 0700")
    return path


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def publish_original(path, data):
    """A crash may leave disposable temp bytes, never a partially published original."""
    with tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.is_symlink() or digest(path.read_bytes()) != digest(data):
                raise ValueError("Existing original integrity mismatch")
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path):
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("Private JSON size limit")
    return json.loads(path.read_text())


def page_fingerprint(image):
    from PIL import Image, ImageOps
    image = ImageOps.exif_transpose(image).convert("L")
    width, height = image.size
    canonical = image.copy()
    canonical.thumbnail((1200, 1200), Image.Resampling.LANCZOS)
    pixels = list(image.resize((9, 8), Image.Resampling.LANCZOS).get_flattened_data())
    bits = sum((pixels[y * 9 + x] > pixels[y * 9 + x + 1]) << (y * 8 + x) for y in range(8) for x in range(8))
    return {"sha256": digest(str(canonical.size).encode() + canonical.tobytes()),
            "dhash": f"{bits:016x}", "aspect": round(width / height, 3)}


def render_pages(path, directory):
    """Bounded local decoding, with no source content or subprocess errors in logs."""
    from PIL import Image, ImageOps
    import pillow_heif
    pillow_heif.register_heif_opener()
    if path.suffix.lower() == ".pdf":
        info = subprocess.run(["pdfinfo", str(path)], capture_output=True, timeout=15, check=True).stdout
        match = re.search(rb"^Pages:\s+(\d+)", info, re.M)
        if not match or not 1 <= int(match[1]) <= 60:
            raise ValueError("PDF page bound")
        subprocess.run(["pdftoppm", "-scale-to", "1200", "-gray", "-png", str(path), str(directory / "page")],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60, check=True)
        paths = sorted(directory.glob("page-*.png"), key=lambda p: int(p.stem.split("-")[-1]))
        if len(paths) != int(match[1]):
            raise ValueError("Incomplete page rendering")
        return paths
    result = []
    with Image.open(path) as image:
        frames = getattr(image, "n_frames", 1)
        if not 1 <= frames <= 60:
            raise ValueError("Frame bound")
        for frame in range(frames):
            image.seek(frame)
            if image.width * image.height > 50_000_000:
                raise ValueError("Image pixel bound")
            page = ImageOps.exif_transpose(image).convert("RGB")
            page.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
            target = directory / f"page-{frame + 1}.png"
            page.save(target)
            result.append(target)
    return result


def load_manifest(root):
    verify_storage(root)
    data = read_json(root / "manifest.json")
    if data.get("schema") != 1 or not isinstance(data.get("documents"), list):
        raise ValueError("Invalid manifest schema")
    ids = [d["id"] for d in data["documents"]]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate document IDs")
    for doc in data["documents"]:
        if not re.fullmatch(r"[0-9a-f]{64}", doc["id"]) or doc["id"] != doc["sha256"]:
            raise ValueError("Invalid content identity")
        if doc["extension"] not in EXTENSIONS or not doc.get("pages"):
            raise ValueError("Invalid asset metadata")
        if doc.get("medium") not in MEDIA or doc.get("source") not in SOURCES:
            raise ValueError("Invalid source/medium enum")
        if type(doc.get("previously_seen")) is not bool or type(doc.get("case_reviewed")) is not bool or type(doc.get("duplicate_reviewed")) is not bool:
            raise ValueError("Invalid review flag")
        if not isinstance(doc.get("case_key"), str) or not isinstance(doc.get("duplicate_cluster"), str):
            raise ValueError("Invalid group identity")
        if not 1 <= len(doc["pages"]) <= 60:
            raise ValueError("Invalid physical pages")
        for page in doc["pages"]:
            if (not re.fullmatch(r"[0-9a-f]{64}", page["sha256"]) or not re.fullmatch(r"[0-9a-f]{16}", page["dhash"])
                    or type(page["aspect"]) not in {float, int} or not 0 < page["aspect"] < 100):
                raise ValueError("Invalid page identity")
        if set(doc.get("strata", {})) != {"court", "year", "act_type"} or not all(isinstance(v, str) for v in doc["strata"].values()):
            raise ValueError("Invalid diversity metadata")
    return data


def asset_path(root, doc):
    path = root / "objects" / (doc["sha256"] + doc["extension"])
    if not path.resolve().is_relative_to(root) or digest(path.read_bytes()) != doc["sha256"]:
        raise ValueError("Original asset integrity failure")
    return path


def ingest(root, input_dir, source, medium, previously_seen=False, limit=DEFAULT_IMPORT_LIMIT):
    verify_storage(root)
    if source not in SOURCES or medium not in MEDIA:
        raise ValueError("Invalid acquisition metadata")
    if type(limit) is not int or not 1 <= limit <= MAX_RUN_DOCUMENTS:
        raise ValueError("Import count bound")
    previously_seen = previously_seen or source == "existing-local"
    if (root / "split.json").exists():
        raise ValueError("Frozen corpus: create a new dataset generation")
    manifest = load_manifest(root) if (root / "manifest.json").exists() else {"schema": 1, "documents": []}
    known = {d["sha256"]: d for d in manifest["documents"]}
    paths = sorted(p for p in input_dir.rglob("*") if p.is_file() and p.suffix.lower() in EXTENSIONS
                   and not any(part.startswith(".") for part in p.relative_to(input_dir).parts))
    added = duplicates = 0
    deadline = time.monotonic() + 240
    for path in paths:
        if added >= limit or time.monotonic() >= deadline:
            break
        if path.stat().st_size > MAX_BYTES:
            raise ValueError("Document size bound")
        data = path.read_bytes()
        sha = digest(data)
        if sha in known:
            if previously_seen and not known[sha]["previously_seen"]:
                known[sha]["previously_seen"] = True
                write_json(root / "manifest.json", manifest)
            duplicates += 1
            continue
        with tempfile.TemporaryDirectory(dir=root) as temporary:
            staging = Path(temporary)
            original = staging / ("input" + path.suffix.lower())
            original.write_bytes(data)
            pages = render_pages(original, staging)
            from PIL import Image
            fingerprints = []
            for page in pages:
                with Image.open(page) as image:
                    fingerprints.append(page_fingerprint(image))
            target = root / "objects" / (sha + path.suffix.lower())
            target.parent.mkdir(mode=0o700, exist_ok=True)
            publish_original(target, data)
            preview_dir = root / "previews" / sha
            preview_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
            for number, page in enumerate(pages, 1):
                preview = preview_dir / f"{number}.png"
                shutil.copyfile(page, preview)
                os.chmod(preview, 0o600)
        doc = dict(id=sha, sha256=sha, extension=path.suffix.lower(), source=source, medium=medium,
                   previously_seen=previously_seen, pages=fingerprints, case_key="", case_reviewed=False,
                   duplicate_reviewed=False, duplicate_cluster="", strata={"court": "unknown", "year": "unknown", "act_type": "unknown"})
        manifest["documents"].append(doc)
        known[sha] = doc
        write_json(root / "manifest.json", manifest)
        added += 1
    return dict(added=added, exact_duplicates=duplicates, documents=len(known))


def groups(documents, history=None):
    """Exact indexes plus complete radius-four dHash candidate bands.

    Five disjoint bands guarantee a shared band for any <=4-bit collision.
    Aspect bins retain the strict .05 check; repeated page signatures share one
    representative so common layouts do not create an all-document pair scan.
    Crops/rephotos still require the explicit reviewed duplicate_cluster link.
    """
    history = history or {}
    parents = list(range(len(documents)))
    def find(i):
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i
    def join(i, j):
        parents[find(j)] = find(i)
    exact = {}
    bands = {}
    signatures = {}
    widths = (13, 13, 13, 13, 12)
    for i, doc in enumerate(documents):
        prior = history.get(doc["id"], {})
        links = [("case", doc["case_key"]), ("cluster", doc["duplicate_cluster"]), ("asset", doc["sha256"])]
        links.extend(("case", value) for value in prior.get("case_aliases", []))
        links.extend(("cluster", value) for value in prior.get("cluster_aliases", []))
        for kind, value in links:
            if value:
                key = (kind, value)
                if key in exact:
                    join(i, exact[key])
                else:
                    exact[key] = i
        for page in doc["pages"]:
            key = ("page", page["sha256"])
            if key in exact:
                join(i, exact[key])
            else:
                exact[key] = i
            value, aspect = int(page["dhash"], 16), page["aspect"]
            signature = (value, aspect)
            if signature in signatures:
                join(i, signatures[signature])
                continue
            aspect_bin = int(aspect / .05)
            shift = 0
            keys = []
            candidates = set()
            for band, width in enumerate(widths):
                part = (value >> shift) & ((1 << width) - 1)
                for nearby in (aspect_bin - 1, aspect_bin, aspect_bin + 1):
                    candidates.update(bands.get((band, part, nearby), ()))
                keys.append((band, part, aspect_bin))
                shift += width
            for other_value, other_aspect, j in candidates:
                if find(i) != find(j) and abs(aspect - other_aspect) < .05 and (value ^ other_value).bit_count() <= 4:
                    join(i, j)
            signatures[signature] = i
            for key in keys:
                bands.setdefault(key, []).append((value, aspect, i))
    result = {}
    for i, doc in enumerate(documents):
        result.setdefault(find(i), []).append(doc)
    return list(result.values())


def original_identity(doc):
    return identity({key: doc[key] for key in ("id", "sha256", "extension", "pages")})


def inherited_assignments(manifest):
    history = manifest.get("history")
    if history is None:
        return {}, {}, {}
    if (history.get("schema") != 1 or not all(isinstance(history.get(k), dict) for k in ("documents", "cases", "clusters"))
            or not all(re.fullmatch(r"[0-9a-f]{64}", history.get(k, "")) for k in ("parent_manifest_sha256", "parent_split_sha256"))):
        raise ValueError("Invalid corpus history")
    docs = {d["id"]: d for d in manifest["documents"]}
    for key, record in history["documents"].items():
        if key not in docs or record.get("original_sha256") != original_identity(docs[key]):
            raise ValueError("Inherited original/page membership changed")
        if record.get("split") not in {"development", "holdout"}:
            raise ValueError("Invalid inherited document split")
        for name, registry in (("case_aliases", history["cases"]), ("cluster_aliases", history["clusters"])):
            aliases = record.get(name)
            if not isinstance(aliases, list) or not all(isinstance(alias, str) and alias and registry.get(alias) == record["split"] for alias in aliases):
                raise ValueError("Inherited case alias assignment changed")
    for registry in (history["cases"], history["clusters"]):
        if not all(isinstance(key, str) and key and value in {"development", "holdout"} for key, value in registry.items()):
            raise ValueError("Invalid inherited case registry")
    return {key: value["split"] for key, value in history["documents"].items()}, history["cases"], history["clusters"]


def cluster_assignment(cluster, history):
    old_docs, old_cases, old_clusters = history
    inherited = set()
    for doc in cluster:
        for registry, key in ((old_docs, doc["id"]), (old_cases, doc["case_key"]), (old_clusters, doc["duplicate_cluster"])):
            if key in registry:
                inherited.add(registry[key])
    if "holdout" in inherited and (len(inherited) > 1 or any(d["previously_seen"] for d in cluster)):
        raise ValueError("Inherited holdout exposure/link conflict; explicitly expose in a new generation")
    if any(d["previously_seen"] for d in cluster) or inherited == {"development"}:
        return "development"
    if not all(d["case_key"] and d["case_reviewed"] is True and d["duplicate_reviewed"] is True for d in cluster):
        if "holdout" in inherited:
            raise ValueError("Inherited holdout qualification changed")
        return "quarantine"
    if inherited:
        return "holdout"
    key = min(d["case_key"] for d in cluster)
    return "holdout" if int(digest(("sro-court-eval-v1:" + key).encode())[:8], 16) % 5 == 0 else "development"


def _case_tokens(value):
    if not isinstance(value, str) or not value or len(value) > 500:
        return set()
    tokens = {("key", value)}
    for match in re.finditer(r"(?i)(?<![0-9a-f])[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?![0-9a-f])", value):
        tokens.add(("uuid", match[0].lower()))
    return tokens


def review_exposure_ids(manifest, ledger_path):
    """Read a private owner-exposure export; no feedback becomes a gold label."""
    path = Path(ledger_path).expanduser()
    if path.suffix.lower() not in {".json", ".sqlite3", ".sqlite", ".db"} or path.is_symlink() or path.resolve().is_relative_to(ROOT):
        raise ValueError("Review exposure storage boundary")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
            raise ValueError("Review exposure private permissions")
        live_database = stream.read(16) == b"SQLite format 3\x00"
        stream.seek(0)
        raw = b"" if live_database else stream.read(16 * 1024 * 1024 + 1)
    if live_database:
        # Read only exposure identity metadata in one WAL-consistent snapshot.
        # The much larger capture/review/comment columns are never selected.
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            cursor = connection.execute('''SELECT e.document_id, d.asset_sha256, e.first_seen_at,
                CASE WHEN length(d.metadata_json)<=8192 THEN d.metadata_json ELSE NULL END
                FROM exposure e JOIN documents d ON d.id=e.document_id ORDER BY e.document_id LIMIT 100001''')
            rows, metadata_bytes = [], 0
            for row in cursor:
                metadata_bytes += sum(len(value.encode()) for value in row if isinstance(value, str))
                if row[3] is None or len(rows) >= 100000 or metadata_bytes > 16 * 1024 * 1024:
                    raise ValueError("Review exposure size bound")
                rows.append(row)
            ledger = {"schema": 1, "exposures": [{"document_id": row[0], "asset_sha256": row[1],
                "first_seen_at": row[2], "metadata": json.loads(row[3])} for row in rows]}
        finally:
            connection.close()
        receipt_hash = identity(ledger)
    else:
        if len(raw) > 16 * 1024 * 1024:
            raise ValueError("Review exposure size bound")
        ledger = json.loads(raw)
        receipt_hash = digest(raw)
    if (not isinstance(ledger, dict) or ledger.get("schema") != 1
            or not isinstance(ledger.get("exposures"), list) or len(ledger["exposures"]) > 100000):
        raise ValueError("Review exposure schema")
    assets, cases = set(), set()
    for record in ledger["exposures"]:
        if (not isinstance(record, dict) or not isinstance(record.get("asset_sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", record["asset_sha256"])
                or record.get("document_id") != record["asset_sha256"] or not isinstance(record.get("metadata", {}), dict)):
            raise ValueError("Review exposure identity")
        assets.add(record["asset_sha256"])
        metadata = record.get("metadata", {})
        if any(name in metadata and (not isinstance(metadata[name], str) or len(metadata[name]) > 500)
               for name in ("case_key", "case_id")):
            raise ValueError("Review exposure case identity")
        cases.update(_case_tokens(metadata.get("case_key")))
        cases.update(_case_tokens(metadata.get("case_id")))
    history = manifest.get("history", {}).get("documents", {})
    matched = set()
    for doc in manifest["documents"]:
        tokens = _case_tokens(doc["case_key"])
        for alias in history.get(doc["id"], {}).get("case_aliases", []):
            tokens.update(_case_tokens(alias))
        if doc["sha256"] in assets or tokens & cases:
            matched.add(doc["id"])
    return matched, {"ledger_sha256": receipt_hash, "ledger_path": str(path.resolve()),
                     "records": len(ledger["exposures"]), "matched_documents": len(matched)}


def apply_review_exposure(root, ledger_path):
    """Explicitly expose linked components in an unfrozen generation only."""
    if (root / "split.json").exists():
        raise ValueError("Frozen corpus: expose review feedback in a new generation")
    manifest = load_manifest(root)
    inherited_assignments(manifest)
    exposed, receipt = review_exposure_ids(manifest, ledger_path)
    history = manifest.get("history", {})
    affected = 0
    for component in groups(manifest["documents"], history.get("documents")):
        if not any(doc["id"] in exposed for doc in component):
            continue
        for doc in component:
            affected += not doc["previously_seen"]
            doc["previously_seen"] = True
            prior = history.get("documents", {}).get(doc["id"], {})
            if prior:
                prior["split"] = "development"
            for registry_name, alias_name, current in (("cases", "case_aliases", doc["case_key"]),
                                                         ("clusters", "cluster_aliases", doc["duplicate_cluster"])):
                registry = history.get(registry_name, {})
                for alias in prior.get(alias_name, []) + ([current] if current else []):
                    if alias in registry:
                        registry[alias] = "development"
    manifest["review_exposure"] = receipt
    write_json(root / "manifest.json", manifest)
    return {"newly_exposed_documents": affected, "matched_documents": len(exposed)}


def extend(root, previous, exposed_ids=(), review_exposure_ledger=None):
    """Appendable generation with an immutable, flattened case/exposure registry.

    No captures/audits are reused. Human original references remain private and
    asset-bound. Exposing an old control is explicit and propagates through its
    full previous case/page/duplicate component before any new split is frozen.
    """
    verify_storage(root)
    verify_storage(previous)
    root, previous = root.resolve(), previous.resolve()
    if root == previous or root.is_relative_to(previous) or previous.is_relative_to(root):
        raise ValueError("Generation roots must be separate")
    if (root / "manifest.json").exists() or (root / "split.json").exists():
        raise ValueError("Generation target already initialized")
    with dataset_lock(previous):
        manifest = load_manifest(previous)
        # Historical bytes remain valid even after a later owner view. The
        # successor consumes that exposure explicitly before inheriting splits.
        split = validate_split(previous, manifest, check_review_exposure=False)
        docs = manifest["documents"]
        exposure_receipt = None
        if review_exposure_ledger is None:
            review_exposure_ledger = manifest.get("review_exposure", {}).get("ledger_path")
        if review_exposure_ledger is not None:
            ledger_ids, exposure_receipt = review_exposure_ids(manifest, review_exposure_ledger)
            exposed_ids = set(exposed_ids) | ledger_ids
        if not set(exposed_ids) <= {d["id"] for d in docs}:
            raise ValueError("Unknown exposure identity")
        old_docs, old_cases, old_clusters = inherited_assignments(manifest)
        assignments = dict(split["assignments"])
        for cluster in groups(docs, manifest.get("history", {}).get("documents")):
            if any(d["id"] in exposed_ids for d in cluster):
                for doc in cluster:
                    assignments[doc["id"]] = "development"
                    doc["previously_seen"] = True
        # A prior quarantine is known material, never fresh independent control.
        assignments = {key: ("holdout" if value == "holdout" else "development") for key, value in assignments.items()}
        cases, clusters = dict(old_cases), dict(old_clusters)
        for doc in docs:
            assignment = assignments[doc["id"]]
            for registry, key in ((cases, doc["case_key"]), (clusters, doc["duplicate_cluster"])):
                if key:
                    registry[key] = assignment
            source = asset_path(previous, doc)
            target = root / "objects" / source.name
            target.parent.mkdir(mode=0o700, exist_ok=True)
            publish_original(target, source.read_bytes())
            for number in range(1, len(doc["pages"]) + 1):
                source_page = previous / "previews" / doc["id"] / f"{number}.png"
                target_page = root / "previews" / doc["id"] / source_page.name
                target_page.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                publish_original(target_page, source_page.read_bytes())
            reference = previous / "references" / (doc["id"] + ".json")
            if reference.exists():
                value = read_json(reference)
                validate_reference(value, doc)
                write_json(root / "references" / reference.name, value)
        # Preserve aliases from every generation and downgrade aliases for an
        # explicitly exposed component, including corrected canonical case keys.
        for key, old_assignment in old_docs.items():
            if old_assignment == "holdout" and assignments[key] == "development":
                prior = manifest.get("history", {})
                record = prior.get("documents", {}).get(key, {})
                for registry, aliases in ((cases, record.get("case_aliases", [])), (clusters, record.get("cluster_aliases", []))):
                    for alias in aliases:
                        registry[alias] = "development"
        records = {}
        previous_records = manifest.get("history", {}).get("documents", {})
        for doc in docs:
            prior = previous_records.get(doc["id"], {})
            records[doc["id"]] = dict(split=assignments[doc["id"]], original_sha256=original_identity(doc),
                case_aliases=sorted(set(prior.get("case_aliases", [])) | ({doc["case_key"]} if doc["case_key"] else set())),
                cluster_aliases=sorted(set(prior.get("cluster_aliases", [])) | ({doc["duplicate_cluster"]} if doc["duplicate_cluster"] else set())))
        value = dict(schema=1, documents=docs, history=dict(schema=1,
            parent_manifest_sha256=split["manifest_sha256"], parent_split_sha256=identity(split),
            documents=records, cases=cases, clusters=clusters))
        if exposure_receipt is not None:
            value["review_exposure"] = exposure_receipt
        write_json(root / "manifest.json", value)
    return dict(inherited_documents=len(docs), inherited_split_counts=dict(Counter(assignments.values())), exposed_documents=sum(d["previously_seen"] for d in docs))


def freeze(root, review_exposure_ledger=None):
    if review_exposure_ledger is None:
        # A successor may import additional assets after extend. Re-read its
        # explicitly selected private ledger before freezing those new members.
        review_exposure_ledger = load_manifest(root).get("review_exposure", {}).get("ledger_path")
    if review_exposure_ledger is not None:
        if (root / "split.json").exists():
            manifest = load_manifest(root)
            split = validate_split(root, manifest)
            exposed, _ = review_exposure_ids(manifest, review_exposure_ledger)
            if any(split["assignments"][key] != "development" for key in exposed):
                raise ValueError("Frozen corpus: expose review feedback in a new generation")
            return split
        apply_review_exposure(root, review_exposure_ledger)
    manifest = load_manifest(root)
    if (root / "split.json").exists():
        return validate_split(root, manifest)
    history = inherited_assignments(manifest)
    assignments = {}
    for cluster in groups(manifest["documents"], manifest.get("history", {}).get("documents")):
        assignment = cluster_assignment(cluster, history)
        assignments.update({d["id"]: assignment for d in cluster})
    value = dict(schema=1, manifest_sha256=identity(manifest), assignments=assignments)
    write_json(root / "split.json", value)
    return value


def validate_split(root, manifest, *, check_review_exposure=True):
    split = read_json(root / "split.json")
    if split.get("schema") != 1 or split.get("manifest_sha256") != identity(manifest):
        raise ValueError("Frozen manifest changed")
    if set(split["assignments"]) != {d["id"] for d in manifest["documents"]}:
        raise ValueError("Split membership differs")
    history = inherited_assignments(manifest)
    for cluster in groups(manifest["documents"], manifest.get("history", {}).get("documents")):
        assigned = {split["assignments"][d["id"]] for d in cluster}
        if len(assigned) != 1 or not assigned <= {"development", "holdout", "quarantine"}:
            raise ValueError("Case/page/duplicate split leakage")
        if "holdout" in assigned and any(d["previously_seen"] or not d["case_key"] or
                 d["case_reviewed"] is not True or d["duplicate_reviewed"] is not True for d in cluster):
            raise ValueError("Unqualified holdout")
        if assigned != {cluster_assignment(cluster, history)}:
            raise ValueError("Split differs from immutable case/exposure assignment")
    ledger = manifest.get("review_exposure", {}).get("ledger_path")
    if check_review_exposure and ledger:
        exposed, _ = review_exposure_ids(manifest, ledger)
        if any(split["assignments"][key] != "development" for key in exposed):
            raise ValueError("Frozen corpus: owner review exposed a control; create a new generation")
    return split


def reference_template(doc):
    return dict(schema=1, document_id=doc["id"], asset_sha256=doc["sha256"],
                provenance="human-original", reviewer="", second_reviewer="", disputed=False,
                original_inspected=False, transcript=None,
                fields={name: dict(state="unreviewed", value=None, page=None, participant="", role="", occurrence="") for name in FIELDS})


def normalize(text):
    return " ".join(unicodedata.normalize("NFC", text).split())


def distance(a, b):
    # Explicit memory/time bounds; long full-act transcripts are not silently sampled.
    if len(a) * len(b) > 25_000_000:
        raise ValueError("Transcript distance work bound; annotate shorter documents")
    if len(a) < len(b):
        a, b = b, a
    row = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        next_row = [i]
        for j, cb in enumerate(b, 1):
            next_row.append(min(row[j] + 1, next_row[j - 1] + 1, row[j - 1] + (ca != cb)))
        row = next_row
    return row[-1]


def transcript_pages(text):
    pages = text.split("\f")
    if text.endswith("\f"):
        pages.pop()
    return pages


def validate_reference(ref, doc):
    if ref.get("schema") != 1 or ref.get("document_id") != doc["id"] or ref.get("asset_sha256") != doc["sha256"]:
        raise ValueError("Reference asset identity mismatch")
    if ref.get("provenance") != "human-original" or ref.get("original_inspected") is not True or not ref.get("reviewer", "").strip():
        raise ValueError("Reference requires original inspection by human")
    if not ref.get("second_reviewer", "").strip() or ref["second_reviewer"].strip().casefold() == ref["reviewer"].strip().casefold():
        raise ValueError("Reference needs two independent distinct human reads")
    if ref.get("disputed"):
        raise ValueError("Unresolved disputed reference cannot score")
    if set(ref["fields"]) not in (set(FIELDS), set(LEGACY_FIELDS)):
        raise ValueError("Reference field schema mismatch")
    for value in ref["fields"].values():
        state = value.get("state")
        if state not in {"unreviewed", "present", "absent", "ambiguous", "unreadable"}:
            raise ValueError("Invalid reference state")
        if state == "present":
            page = value.get("page")
            if not isinstance(value.get("value"), str) or not value["value"].strip() or type(page) is not int or not 1 <= page <= len(doc["pages"]):
                raise ValueError("Present reference needs value and physical page")
            if not all(isinstance(value.get(k), str) and value[k].strip() for k in ("participant", "role", "occurrence")):
                raise ValueError("Present reference needs participant/role/occurrence evidence")
        elif value.get("value") is not None:
            raise ValueError("Unknown or absent reference cannot contain a value")
    transcript = ref.get("transcript")
    if transcript is not None and (not isinstance(transcript, str) or not normalize(transcript)):
        raise ValueError("Transcript must be nonempty human text or null")
    if transcript is not None and len(transcript_pages(transcript)) != len(doc["pages"]):
        raise ValueError("Human transcript must preserve physical page slots")


def validate_audit(audit, ref, captured, doc):
    if (audit.get("document_id") != doc["id"] or audit.get("capture_sha256") != identity(captured)
            or audit.get("reference_sha256") != identity(ref) or not audit.get("reviewer", "").strip()
            or audit.get("original_inspected") is not True or set(audit.get("fields", {})) not in (set(FIELDS), set(ref["fields"]))):
        raise ValueError("Human audit does not match exact source/reference")
    for verdict in audit["fields"].values():
        if (verdict.get("owner") not in {"correct", "wrong", "ambiguous", "unreviewed"}
                or verdict.get("source") not in {"correct", "wrong", "unreviewed"}
                or verdict.get("error_layer") not in LAYERS | {None}):
            raise ValueError("Invalid audit verdict")


def cohort(captured):
    evidence = captured["capture"]
    runtime = dict(evidence["runtime"])
    executables = runtime.pop("ocr_executables")
    return dict(source={key: evidence[key] for key in ("revision", "source_dirty", "dirty_sha256")},
                runtime=runtime, ocr_executables=executables or None)


def check_cohort(baseline, candidate):
    if baseline["source"] != candidate["source"] or baseline["runtime"] != candidate["runtime"]:
        raise ValueError("Baseline source/runtime differs; use a new corpus generation")
    if baseline["ocr_executables"] and candidate["ocr_executables"] and baseline["ocr_executables"] != candidate["ocr_executables"]:
        raise ValueError("Baseline OCR runtime differs")


def adopt(root, path):
    manifest = load_manifest(root)
    split = validate_split(root, manifest)
    packet = read_json(path)
    if (packet.get("schema") != 1 or packet.get("manifest_sha256") != identity(manifest)
            or packet.get("split_sha256") != identity(split) or packet.get("kind") not in {"references", "audits"}
            or not isinstance(packet.get("records"), list) or not 1 <= len(packet["records"]) <= 10):
        raise ValueError("Review packet does not match frozen corpus")
    docs = {d["id"]: d for d in manifest["documents"]}
    seen = set()
    for record in packet["records"]:
        key = record["document_id"]
        if key not in docs or key in seen:
            raise ValueError("Unknown or duplicate review identity")
        seen.add(key)
        if packet["kind"] == "references":
            validate_reference(record, docs[key])
        else:
            ref = read_json(root / "references" / (key + ".json"))
            captured = read_json(root / "captures" / (key + ".json"))
            validate_reference(ref, docs[key])
            validate_audit(record, ref, captured, docs[key])
        target = root / packet["kind"] / (key + ".json")
        if target.exists() and read_json(target) != record:
            raise ValueError("Frozen review differs; use a new reference generation")
    for record in packet["records"]:
        write_json(root / packet["kind"] / (record["document_id"] + ".json"), record)
    return dict(adopted=len(seen), kind=packet["kind"])


def field_metrics(counts, documents):
    positive = counts["positive_fields"]
    reference = counts["reference_present"] + counts["reference_absent"]
    ownership = counts["owner_correct"] + counts["owner_wrong"]
    qualified = (positive and not any(counts[key] for key in ("owner_unreviewed", "owner_ambiguous", "source_unreviewed", "unobserved_reference_fields")))
    return dict(counts=dict(counts),
        reference_coverage=reference / documents if documents else None,
        detection_coverage=counts["detected"] / positive if positive else None,
        exact_value_accuracy=counts["exact_correct"] / positive if positive else None,
        normalized_value_accuracy=counts["normalized_correct"] / positive if positive else None,
        owner_accuracy=counts["owner_correct"] / ownership if ownership else None,
        owner_review_coverage=ownership / positive if positive else None,
        qualified_value_owner_source_accuracy=counts["qualified_correct"] / positive if qualified else None)


def score(root):
    manifest = load_manifest(root)
    split = validate_split(root, manifest)
    reports = {}
    baseline = read_json(root / "baseline.json") if (root / "baseline.json").exists() else None
    inherited_docs, inherited_cases, _ = inherited_assignments(manifest)
    for partition in ("development", "holdout", "quarantine"):
        counts = Counter()
        layers = Counter()
        strata = Counter()
        seconds = []
        per_field = {name: Counter() for name in FIELDS}
        per_medium = {medium: dict(counts=Counter(), fields={name: Counter() for name in FIELDS}) for medium in sorted(MEDIA)}
        char_edits = char_total = weak_edits = weak_total = 0
        for doc in manifest["documents"]:
            if split["assignments"][doc["id"]] != partition:
                continue
            asset_path(root, doc)
            counts["documents"] += 1
            strata[doc["medium"]] += 1
            medium_counts = per_medium[doc["medium"]]["counts"]
            medium_counts["documents"] += 1
            capture_path = root / "captures" / (doc["id"] + ".json")
            capture = read_json(capture_path) if capture_path.exists() else None
            if capture and capture.get("input_sha256") != doc["sha256"]:
                raise ValueError("Capture asset identity mismatch")
            if capture:
                if baseline is None:
                    raise ValueError("Capture has no frozen baseline identity")
                check_cohort(baseline, cohort(capture))
            counts["captured" if capture else "missing_capture"] += 1
            medium_counts["captured" if capture else "missing_capture"] += 1
            if capture:
                for page in capture.get("primary_pages", []):
                    origin = page.get("origin")
                    name = {"ocr": "ocr_pages", "embedded-text-weak": "embedded_text_weak_pages"}.get(origin, "unknown_origin_pages")
                    counts[name] += 1
                    medium_counts[name] += 1
            if capture:
                seconds.append(capture["capture"]["seconds"])
                counts["completed_ocr"] += capture["capture"]["completed_ocr"]
            ref_path = root / "references" / (doc["id"] + ".json")
            if not ref_path.exists():
                counts["unverified_documents"] += 1
                medium_counts["unverified_documents"] += 1
                for name in FIELDS:
                    per_field[name]["reference_unreviewed"] += 1
                    per_medium[doc["medium"]]["fields"][name]["reference_unreviewed"] += 1
                continue
            ref = read_json(ref_path)
            validate_reference(ref, doc)
            counts["human_reference_documents"] += 1
            medium_counts["human_reference_documents"] += 1
            audit_path = root / "audits" / (doc["id"] + ".json")
            audit = read_json(audit_path) if audit_path.exists() else {}
            if audit:
                validate_audit(audit, ref, capture, doc)
            observed = capture.get("observed_pipeline", {}) if capture else {}
            for name in FIELDS:
                truth = ref["fields"].get(name, {"state": "unreviewed"})
                field_counts = per_field[name]
                medium_field = per_medium[doc["medium"]]["fields"][name]
                def increment(key):
                    for counter in (counts, field_counts, medium_field):
                        counter[key] += 1
                state = truth["state"]
                increment("reference_" + state)
                if state not in {"present", "absent"}:
                    continue
                if not capture:
                    increment("unobserved_reference_fields")
                    if state == "present":
                        increment("positive_fields")
                        increment("missing")
                    continue
                verdict = audit.get("fields", {}).get(name, {})
                layer = verdict.get("error_layer")
                if layer:
                    layers[layer] += 1
                    field_counts["error_" + layer] += 1
                    medium_field["error_" + layer] += 1
                predicted = observed.get(name, {})
                actual = predicted.get("value") if isinstance(predicted, dict) else predicted
                if actual is not None and not isinstance(actual, str):
                    raise ValueError("Observed field is not a literal string")
                present = actual is not None and actual != ""
                if state == "absent":
                    increment("false_positive" if present else "correct_absence")
                    continue
                increment("positive_fields")
                if not present:
                    increment("missing")
                else:
                    increment("detected")
                    increment("exact_correct" if actual == truth["value"] else "exact_wrong")
                    increment("normalized_correct" if normalize(actual) == normalize(truth["value"]) else "normalized_wrong")
                owner = verdict.get("owner", "unreviewed")
                source = verdict.get("source", "unreviewed")
                increment("owner_" + owner)
                increment("source_" + source)
                if present and actual == truth["value"] and owner == "correct" and source == "correct":
                    increment("qualified_correct")
            if ref.get("transcript") is not None:
                if not capture:
                    counts["transcript_missing_capture"] += 1
                elif capture.get("variants"):
                    # Fixed first accepted reading, never best-of-reference selection.
                    truth_pages = transcript_pages(ref["transcript"])
                    primary = capture.get("primary_pages", [])
                    if len(primary) != len(truth_pages) or [p["page"] for p in primary] != list(range(1, len(truth_pages) + 1)):
                        raise ValueError("Physical transcript page mapping differs")
                    for expected_page, actual_page in zip(truth_pages, primary):
                        expected, actual = normalize(expected_page), normalize(actual_page["text"])
                        if actual_page["origin"] == "ocr":
                            char_edits += distance(expected, actual)
                            char_total += len(expected)
                        elif actual_page["origin"] == "embedded-text-weak":
                            weak_edits += distance(expected, actual)
                            weak_total += len(expected)
                        else:
                            counts["unknown_origin_transcript_pages"] += 1
                    counts["transcript_documents"] += 1
                    counts["ocr_transcript_documents" if capture["capture"]["completed_ocr"] else "text_layer_transcript_documents"] += 1
        denominator = counts["positive_fields"]
        documents = [d for d in manifest["documents"] if split["assignments"][d["id"]] == partition]
        known_cases = {d["case_key"] for d in documents if d["case_key"] and (d["id"] in inherited_docs or d["case_key"] in inherited_cases)}
        cases = {d["case_key"] for d in documents if d["case_key"]}
        counts["cases"] = len(cases)
        counts["inherited_cases"] = len(known_cases)
        counts["new_cases"] = len(cases - known_cases)
        counts["inherited_documents"] = sum(d["id"] in inherited_docs for d in documents)
        counts["new_documents"] = len(documents) - counts["inherited_documents"]
        reports[partition] = dict(counts=dict(counts), medium_counts=dict(strata), human_error_categories=dict(layers),
            fields={name: field_metrics(values, counts["documents"]) for name, values in per_field.items()},
            by_medium={medium: dict(counts=dict(bucket["counts"]), fields={name: field_metrics(values, bucket["counts"]["documents"])
                for name, values in bucket["fields"].items()}) for medium, bucket in per_medium.items()},
            diversity_counts={key: len({d["strata"][key] for d in documents if d["strata"][key] not in {"", "unknown"}}) for key in ("court", "year", "act_type")},
            exact_value_accuracy=counts["exact_correct"] / denominator if denominator else None,
            qualified_value_owner_source_accuracy=counts["qualified_correct"] / denominator if denominator and counts["owner_unreviewed"] == 0 and counts["owner_ambiguous"] == 0 and counts["source_unreviewed"] == 0 and not counts["unobserved_reference_fields"] else None,
            reading_order_cer=char_edits / char_total if char_total else None, reference_characters=char_total,
            embedded_text_comparison_cer=weak_edits / weak_total if weak_total else None,
            processing_seconds=sum(seconds), median_seconds=statistics.median(seconds) if seconds else None)
    return dict(schema=1, manifest_sha256=identity(manifest), split_sha256=identity(split),
                accuracy_status="human-references-required" if not any(r["counts"].get("human_reference_documents") for r in reports.values()) else "partial-human-reference-coverage",
                baseline=baseline, readiness=dict(new_holdout_cases=bool(reports["holdout"]["counts"].get("new_cases")),
                    immutable_holdout_available=bool(reports["holdout"]["counts"].get("documents")),
                    real_photo_holdout=bool(reports["holdout"]["medium_counts"].get("real_photo")),
                    human_reference_holdout=bool(reports["holdout"]["counts"].get("human_reference_documents")),
                    population_accuracy_qualified=False), partitions=reports)


def capture(root, limit):
    from scripts.dev.tesseract_native import run_owned, resources, admit, LIMIT_ENV
    manifest = load_manifest(root)
    validate_split(root, manifest)
    from scripts.dev.quality.dataset_capture import source_identity, runtime_identity
    baseline_path = root / "baseline.json"
    baseline = read_json(baseline_path) if baseline_path.exists() else None
    if baseline:
        runtime = runtime_identity([])
        runtime.pop("ocr_executables")
        check_cohort(baseline, dict(source=source_identity(), runtime=runtime, ocr_executables=None))
    start_swap = resources()[1] if sys.platform == "darwin" else 0
    deadline = time.monotonic() + 240
    completed = failures = cached = 0
    for doc in manifest["documents"]:
        if completed + failures >= limit:
            break
        target = root / "captures" / (doc["id"] + ".json")
        if target.exists():
            existing = read_json(target)
            if existing.get("input_sha256") != doc["sha256"]:
                raise ValueError("Existing capture identity mismatch")
            if baseline is None:
                raise ValueError("Existing capture has no frozen baseline identity")
            check_cohort(baseline, cohort(existing))
            cached += 1
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        source = asset_path(root, doc)
        with tempfile.TemporaryDirectory(dir=root) as temporary:
            environment = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "TESSDATA_PREFIX", "SRO_TESSERACT_NATIVE") if k in os.environ}
            environment.update(LIMIT_ENV, TMPDIR=temporary)
            result_path = Path(temporary) / "capture.json"
            command = [sys.executable, str(ROOT / "scripts/dev/quality/dataset_capture.py"), "--input", str(source),
                       "--output", str(result_path), "--private-root", str(root)]
            try:
                run_owned(command, timeout=min(65, remaining), env=environment,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          health=(lambda: admit(start_swap)) if sys.platform == "darwin" else None)
                result = read_json(result_path)
                candidate = cohort(result)
                if baseline:
                    check_cohort(baseline, candidate)
                    if baseline["ocr_executables"] is None and candidate["ocr_executables"]:
                        baseline["ocr_executables"] = candidate["ocr_executables"]
                else:
                    baseline = candidate
                write_json(baseline_path, baseline)
                write_json(target, result)
                completed += 1
            except (TimeoutError, RuntimeError, subprocess.SubprocessError):
                failures += 1
                write_json(root / "failures" / (doc["id"] + ".json"), {"status": "capture_failed", "input_sha256": doc["sha256"]})
    return dict(completed=completed, failures=failures, reused_existing_baselines=cached)


class PrivateParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError("Invalid local dataset arguments")


def run_command(args, root):
    if args.command == "import":
        return ingest(root, args.input_dir.resolve(), args.source, args.medium, args.previously_seen or args.source == "existing-local", args.limit)
    if args.command == "extend":
        return extend(root, private_root(args.from_root), args.expose_document_id, args.review_exposure_ledger)
    if args.command == "freeze":
        return dict(split_counts=dict(Counter(freeze(root, args.review_exposure_ledger)["assignments"].values())))
    if args.command == "apply-review-exposure":
        return apply_review_exposure(root, args.review_exposure_ledger)
    if args.command == "capture":
        if not 1 <= args.limit <= MAX_RUN_DOCUMENTS:
            raise ValueError("Capture count bound")
        return capture(root, args.limit)
    if args.command == "review":
        from scripts.dev.quality.dataset_review import make_review
        return make_review(root, min(max(args.limit, 1), 10), audit=args.audit)
    if args.command == "adopt":
        return adopt(root, args.input)
    result = score(root)
    write_json(root / "metrics.json", result)
    # Private evidence may grow extra fields; never echo it wholesale.
    return {key: result[key] for key in ("accuracy_status", "readiness", "partitions")}


def main(argv=None):
    parser = PrivateParser(description=__doc__)
    parser.add_argument("--private-root", type=Path, required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    importing = sub.add_parser("import")
    importing.add_argument("--input-dir", type=Path, required=True)
    importing.add_argument("--source", choices=("existing-local", "manual-public-import", "licensed-local"), required=True)
    importing.add_argument("--medium", choices=("unknown", "real_photo", "scan", "text_pdf", "synthetic_distortion"), default="unknown")
    importing.add_argument("--previously-seen", action="store_true")
    importing.add_argument("--limit", type=int, default=DEFAULT_IMPORT_LIMIT)
    extending = sub.add_parser("extend")
    extending.add_argument("--from-root", type=Path, required=True)
    extending.add_argument("--expose-document-id", action="append", default=[])
    extending.add_argument("--review-exposure-ledger", type=Path)
    freezing = sub.add_parser("freeze")
    freezing.add_argument("--review-exposure-ledger", type=Path)
    exposing = sub.add_parser("apply-review-exposure")
    exposing.add_argument("--review-exposure-ledger", type=Path, required=True)
    capturing = sub.add_parser("capture")
    capturing.add_argument("--limit", type=int, default=25)
    sub.add_parser("score")
    review = sub.add_parser("review")
    review.add_argument("--limit", type=int, default=5)
    review.add_argument("--audit", action="store_true")
    adopting = sub.add_parser("adopt")
    adopting.add_argument("--input", type=Path, required=True)
    try:
        args = parser.parse_args(argv)
        root = private_root(args.private_root)
        with dataset_lock(root):
            result = run_command(args, root)
        print(json.dumps(result))
    except Exception:
        # Exceptions can carry paths, OCR excerpts or identifiers: never print them.
        print(json.dumps({"status": "failed", "detail": "Check private inputs, schema and documented bounds locally"}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
