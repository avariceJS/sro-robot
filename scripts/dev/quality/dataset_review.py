"""Generate a private offline, blind-first document reference/audit form."""
from __future__ import annotations

import base64
import hashlib
import html
import json
import os
from pathlib import Path
import tempfile

from scripts.dev.quality.court_dataset import (
    load_manifest, validate_split, asset_path, reference_template, identity,
    validate_reference, FIELDS, LAYERS,
)

MAX_REVIEW_DOCUMENTS = 20
MAX_HTML_BYTES = 80 * 1024 * 1024

STYLE = """
body{font:16px system-ui,sans-serif;max-width:1200px;margin:24px auto;padding:0 16px;color:#202124;background:#fff}
article{border-top:2px solid #777;margin-top:32px;padding-top:12px}label{display:block;margin:8px 0}
input,select,textarea,button{font:inherit;padding:5px;max-width:100%;box-sizing:border-box}textarea{width:100%;min-height:90px}
input[type=text]{width:100%}.pages{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px}
figure{margin:8px 0}img{width:100%;height:auto;border:1px solid #aaa}fieldset{margin:14px 0;border:1px solid #bbb}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f1f3f4;padding:12px}
.notice{padding:12px;background:#fff1c9}button{margin:12px 0}small{display:block;color:#555}
""".strip()

SCRIPT = r"""
'use strict';
const payload=JSON.parse(document.getElementById('payload').textContent);
const records=payload.documents.map(d=>d.record);
function node(tag,parent,text){const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(parent)parent.append(e);return e;}
function control(parent,label,type,value,update,options){
 const wrap=node('label',parent,label+' ');let e;
 if(type==='select'){e=node('select',wrap);for(const item of options){const o=node('option',e,item);o.value=item;}e.value=value??'';}
 else {e=node(type==='textarea'?'textarea':'input',wrap);if(type!=='textarea')e.type=type;
 if(type==='checkbox')e.checked=Boolean(value);else e.value=value??'';}
 e.addEventListener('input',()=>update(type==='checkbox'?e.checked:e.value));return e;
}
const main=document.getElementById('documents');
payload.documents.forEach((doc,index)=>{
 const record=records[index],article=node('article',main);
 node('h2',article,'Document '+(index+1));node('small',article,'Content identity: '+doc.id);
 const link=node('a',article,'Open full original locally');link.href=doc.original;link.target='_blank';link.rel='noopener noreferrer';
 node('p',article,'Previews are downscaled. Inspect the full original for spelling, small digits, page boundaries and uncertain values.');
 const pages=node('div',article);pages.className='pages';
 doc.pages.forEach((data,i)=>{const figure=node('figure',pages);node('figcaption',figure,'Page '+(i+1));const image=node('img',figure);image.src=data;image.alt='Private document page '+(i+1);});
 if(payload.kind==='audits'){
   node('h3',article,'Frozen independent human reference');node('pre',article,JSON.stringify(doc.reference,null,2));
   node('h3',article,'Observed pipeline for this exact capture');node('pre',article,JSON.stringify(doc.observed_pipeline,null,2));
 }
 const readers=node('div',article);readers.className='grid';
 control(readers,'Reviewer','text',record.reviewer,v=>record.reviewer=v);
 if(payload.kind==='references'){
   control(readers,'Second independent reviewer','text',record.second_reviewer,v=>record.second_reviewer=v);
   control(readers,'Disputed value / adjudication required','checkbox',record.disputed,v=>record.disputed=v);
 }
 control(readers,'I inspected the original','checkbox',record.original_inspected,v=>record.original_inspected=v);
 payload.fields.forEach(name=>{
   const value=record.fields[name],box=node('fieldset',article);node('legend',box,name);const grid=node('div',box);grid.className='grid';
   if(payload.kind==='references'){
     let text;
     control(grid,'State','select',value.state,v=>{value.state=v;value.value=v==='present'?text.value:null;},['unreviewed','present','absent','ambiguous','unreadable']);
     text=control(grid,'Literal value from original','text','',v=>value.value=value.state==='present'?v:null);
     const page=control(grid,'Physical page (1-based)','number',value.page,v=>value.page=v===''?null:Number(v));page.min='1';page.max=String(doc.pages.length);page.step='1';
     for(const key of ['participant','role','occurrence'])control(grid,key,'text',value[key],v=>value[key]=v);
   }else{
     control(grid,'Participant ownership','select',value.owner,v=>value.owner=v,['unreviewed','correct','wrong','ambiguous']);
     control(grid,'Source occurrence / location','select',value.source,v=>value.source=v,['unreviewed','correct','wrong']);
     control(grid,'Error layer','select',value.error_layer,v=>value.error_layer=v||null,['',...payload.layers]);
   }
 });
 if(payload.kind==='references'){
   node('p',article,'Optional transcript: type it from the original. Use literal \\f between physical pages; export converts those separators to form-feed characters. Leave blank when not transcribed.');
   control(article,'Independent human transcript','textarea','',v=>record.transcript=v.trim()?v.replace(/\\f/g,'\f'):null);
 }
});
document.getElementById('download').addEventListener('click',()=>{
 const packet={schema:1,kind:payload.kind,manifest_sha256:payload.manifest_sha256,split_sha256:payload.split_sha256,records};
 const blob=new Blob([JSON.stringify(packet,null,2)],{type:'application/json'});
 const url=URL.createObjectURL(blob);const link=document.createElement('a');link.href=url;link.download=payload.kind+'.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
});
""".strip()


def _json_for_html(value):
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            .replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def _read_json(root, path):
    if not path.resolve().is_relative_to(root) or path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError("Private review JSON boundary")
    return json.loads(path.read_text())


def make_review(root, limit=5, audit=False):
    """Write root/review.html; never print or transmit document content.

    Blind mode does not open captures, existing references or PDF text layers.
    Audit mode exposes observations only after two named independent human reads.
    The parent validates downloaded packets before adopting any scored reference.
    """
    if type(limit) is not int or not 1 <= limit <= MAX_REVIEW_DOCUMENTS:
        raise ValueError("Review document limit must be 1..20")
    root = Path(root).resolve()
    manifest = load_manifest(root)
    split = validate_split(root, manifest)
    documents = []
    total_preview_bytes = 0
    for doc in manifest["documents"]:
        if len(documents) >= limit:
            break
        ref_path = root / "references" / (doc["id"] + ".json")
        capture_path = root / "captures" / (doc["id"] + ".json")
        if (not audit and ref_path.exists()) or (audit and not (ref_path.exists() and capture_path.exists())):
            continue
        original = asset_path(root, doc)
        if not 1 <= len(doc["pages"]) <= 60:
            raise ValueError("Review physical page bound")
        record = reference_template(doc)
        item = {"id": doc["id"], "original": original.relative_to(root).as_posix(), "pages": [], "record": record}
        if audit:
            ref = _read_json(root, ref_path)
            captured = _read_json(root, capture_path)
            validate_reference(ref, doc)
            first, second = ref.get("reviewer", "").strip(), ref.get("second_reviewer", "").strip()
            if (ref.get("document_id") != doc["id"] or ref.get("asset_sha256") != doc["sha256"]
                    or ref.get("provenance") != "human-original" or ref.get("original_inspected") is not True
                    or not first or not second or first.casefold() == second.casefold()
                    or captured.get("input_sha256") != doc["sha256"]):
                raise ValueError("Audit requires exact capture and independent original reference")
            item.update(reference=ref, observed_pipeline=captured.get("observed_pipeline", {}))
            item["record"] = {"document_id": doc["id"], "reviewer": "", "original_inspected": False,
                              "capture_sha256": identity(captured), "reference_sha256": identity(ref),
                              "fields": {name: {"owner": "unreviewed", "source": "unreviewed", "error_layer": None} for name in FIELDS}}
        for number in range(1, len(doc["pages"]) + 1):
            preview = root / "previews" / doc["id"] / f"{number}.png"
            if not preview.resolve().is_relative_to(root) or preview.stat().st_size > 4 * 1024 * 1024:
                raise ValueError("Review preview boundary")
            data = preview.read_bytes()
            if not data.startswith(b"\x89PNG\r\n\x1a\n"):
                raise ValueError("Review preview must be PNG")
            total_preview_bytes += len(data)
            if total_preview_bytes > MAX_HTML_BYTES // 2:
                raise ValueError("Review preview total bound")
            item["pages"].append("data:image/png;base64," + base64.b64encode(data).decode("ascii"))
        documents.append(item)
    payload = {"kind": "audits" if audit else "references", "manifest_sha256": identity(manifest),
               "split_sha256": identity(split), "fields": list(FIELDS), "layers": sorted(LAYERS), "documents": documents}
    script_hash = base64.b64encode(hashlib.sha256(SCRIPT.encode()).digest()).decode()
    style_hash = base64.b64encode(hashlib.sha256(STYLE.encode()).digest()).decode()
    csp = ("default-src 'none'; connect-src 'none'; img-src data:; "
           f"script-src 'sha256-{script_hash}'; style-src 'sha256-{style_hash}'; "
           "base-uri 'none'; form-action 'none'; object-src 'none'; frame-src 'none'; worker-src 'none'")
    title = "Independent source audit" if audit else "Blind original-document reference"
    page = ("<!doctype html><html lang=en><head><meta charset=utf-8>"
            '<meta http-equiv="Content-Security-Policy" content="' + html.escape(csp, quote=True) + '">'
            '<meta name="referrer" content="no-referrer"><title>' + title + "</title><style>" + STYLE + "</style></head><body>"
            "<h1>" + title + "</h1><p class=notice>This private file works offline. Do not upload or share it. "
            "Scoring requires two distinct human reads of the original. Enter a second reviewer only after that independent read. "
            "Unresolved values must remain ambiguous, unreadable or unreviewed. Saving a draft does not qualify a reference.</p>"
            '<div id="documents"></div><button type="button" id="download">Download review JSON</button>'
            '<script type="application/json" id="payload">' + _json_for_html(payload) + "</script><script>" + SCRIPT + "</script></body></html>")
    encoded = page.encode()
    if len(encoded) > MAX_HTML_BYTES:
        raise ValueError("Review HTML size bound")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=root, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, root / "review.html")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {"documents": len(documents), "mode": payload["kind"], "html_bytes": len(encoded)}
