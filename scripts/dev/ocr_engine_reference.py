"""Private local full-transcript review; originals and drafts never leave loopback."""
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import hashlib
import os
from pathlib import Path
import secrets
import tempfile
from urllib.parse import urlsplit, parse_qs

from ocr_engines_compare import require_private, save
from ocr_engine_backends import file_hash


def atomic_save(path, data):
    path = Path(path)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.reference-', delete=False) as stream:
            name = stream.name
            os.chmod(name, 0o600)
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(name, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if name and Path(name).exists():
            Path(name).unlink()


def initial_selection(corpus):
    photos = [doc for doc in corpus['documents'] if doc['group'] == 'img1']
    # Source-metadata-only starting suggestion; never select by OCR accuracy.
    # The human can replace any selection after seeing the originals.
    severity = {3: 3, 6: 2, 8: 2}
    ordered = sorted(photos, key=lambda d: (-severity.get(d['pages'][0]['conversion'].get('original_orientation'), 0), d['index']))
    return {page['id'] for doc in corpus['documents']
            if doc['group'] == 'img2' or doc in ordered[:4] for page in doc['pages']}


def serve(corpus_path, port=8766, runs=None):
    corpus_path = Path(corpus_path).resolve(); require_private(corpus_path)
    if runs is None:
        runs = corpus_path.parent / 'measurements'
    runs = Path(runs).resolve(); require_private(runs)
    corpus = json.loads((corpus_path / 'corpus.json').read_text())
    pages = [{**page, 'group': doc['group'], 'document_index': doc['index']} for doc in corpus['documents'] for page in doc['pages']]
    reference_path = corpus_path / 'reference.json'
    reference = json.loads(reference_path.read_text())
    selected = initial_selection(corpus)
    changed = False
    for page in reference['pages']:
        if 'text' not in page:
            page.update({'text': '', 'selected': page['id'] in selected, 'seed': None})
            changed = True
    if changed:
        reference.update({'method': 'manual_original_review', 'engine_outputs_hidden': False,
                          'selection_policy': '4 PNG plus 4 HEIC source-orientation candidates; human-adjustable, not a verified difficulty ranking'})
        atomic_save(reference_path, reference)
    template = Path(__file__).with_name('ocr_engine_reference.html').read_bytes()
    token = secrets.token_urlsafe(24)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def authorized(self):
            wanted_host = f'127.0.0.1:{self.server.server_port}'
            supplied = self.headers.get('X-Review-Token') or parse_qs(urlsplit(self.path).query).get('token', [''])[0]
            return self.headers.get('Host') == wanted_host and secrets.compare_digest(supplied, token)

        def reply(self, body, content_type='application/json', code=200):
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self'")
            self.end_headers(); self.wfile.write(body)

        def do_GET(self):
            if not self.authorized():
                self.send_error(403); return
            requested = urlsplit(self.path)
            if requested.path == '/':
                self.reply(Path(__file__).with_name('ocr_engine_reference.html').read_bytes(), 'text/html; charset=utf-8')
            elif requested.path == '/state':
                self.reply(json.dumps({'pages': [{'id': p['id'], 'size': p['size'], 'group': p['group']} for p in pages],
                                      'reference': json.loads(reference_path.read_text()),
                                      'revision': file_hash(reference_path),
                                      'drafts': json.loads((corpus_path / 'draft-proposals.json').read_text()).get('pages', {})
                                      if (corpus_path / 'draft-proposals.json').exists() else {}}).encode())
            elif requested.path in {'/page', '/seed'}:
                try:
                    index = int(parse_qs(requested.query)['index'][0])
                    if not 0 <= index < len(pages):
                        raise ValueError()
                    page = pages[index]
                    if requested.path == '/page':
                        path = (corpus_path / page['path']).resolve()
                        if not path.is_relative_to(corpus_path):
                            raise ValueError()
                        payload = path.read_bytes()
                        if hashlib.sha256(payload).hexdigest() != page['sha256']:
                            raise ValueError()
                        self.reply(payload, 'image/png')
                    else:
                        for path in sorted(runs.glob('run-*.json')):
                            row = json.loads(path.read_text())
                            if (row['scenario'] == 'cold' and row['engine'] == 'tesseract'
                                    and row['repeat'] == 0 and row['document_index'] == page['document_index']):
                                result = next(p for p in row['pages'] if p['id'] == page['id'])
                                if result['pixel_sha256'] != page['pixel_sha256']:
                                    raise ValueError()
                                self.reply(json.dumps({'text': result['text'], 'seed': {
                                    'engine': 'tesseract', 'scenario': 'cold', 'repeat': 0,
                                    'run_sha256': file_hash(path), 'pixel_sha256': page['pixel_sha256']}}).encode())
                                return
                        self.reply(b'{"error":"OCR result not yet available"}', code=404)
                except (ValueError, KeyError, IndexError, StopIteration):
                    self.send_error(404)
            else:
                self.send_error(404)

        def do_POST(self):
            origin = f'http://127.0.0.1:{self.server.server_port}'
            if (not self.authorized() or urlsplit(self.path).path != '/reference'
                    or self.headers.get('Origin') != origin or self.headers.get_content_type() != 'application/json'):
                self.send_error(403); return
            if self.headers.get('If-Match') != file_hash(reference_path):
                self.reply(b'{"error":"stale draft; read back before retry"}', code=409); return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 16 * 1024 * 1024:
                    self.send_error(413); return
                new = json.loads(self.rfile.read(length)); old = json.loads(reference_path.read_text())
                if (new.get('corpus_sha256') != old['corpus_sha256'] or new.get('method') != 'manual_original_review'
                        or [(p['id'], p['pixel_sha256']) for p in new['pages']]
                        != [(p['id'], p['pixel_sha256']) for p in old['pages']]):
                    raise ValueError()
                atomic_save(reference_path, new)
                self.reply(json.dumps({'saved': True, 'revision': file_hash(reference_path),
                                      'drafts': json.loads((corpus_path / 'draft-proposals.json').read_text()).get('pages', {})
                                      if (corpus_path / 'draft-proposals.json').exists() else {}}).encode())
            except (ValueError, KeyError, TypeError):
                self.send_error(422)

    server = HTTPServer(('127.0.0.1', port), Handler)
    url = f'http://127.0.0.1:{server.server_port}/?token={token}'
    save(corpus_path / 'review-server.json', {'url': url, 'pid': os.getpid(), 'reference': str(reference_path)})
    launcher = corpus_path / 'open-review.html'
    launcher.write_text(f'<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0;url={url}"><title>Open local OCR review</title>')
    os.chmod(launcher, 0o600)
    print('Private original/text reviewer ready on loopback; open the local open-review.html launcher', flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus', type=Path, required=True)
    parser.add_argument('--runs', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8766)
    args = parser.parse_args()
    serve(args.corpus, args.port, args.runs)
