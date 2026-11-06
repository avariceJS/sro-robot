#!/usr/bin/env python3
"""Browser-check the reference form with synthetic data only, never the real corpus."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).parent))
from ocr_engines_compare import PRIVATE, save
from ocr_engine_backends import file_hash, rgb_hash


def main():
    from PIL import Image
    from playwright.sync_api import sync_playwright
    PRIVATE.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='synthetic-ui-', dir=PRIVATE) as temporary:
        folder = Path(temporary).resolve(); corpus = folder / 'corpus'; corpus.mkdir(); runs = folder / 'runs'; runs.mkdir()
        image = Image.new('RGB', (1000, 600), 'white'); image.save(corpus / 'synthetic.png')
        spec = {'id': 'synthetic', 'size': [1000, 600], 'path': 'synthetic.png',
                'pixel_sha256': rgb_hash(image), 'sha256': file_hash(corpus / 'synthetic.png'), 'conversion': {}}
        spec2 = {**spec, 'id': 'synthetic-second'}
        save(corpus / 'corpus.json', {'documents': [{'group': 'img2', 'index': 0, 'pages': [spec]}, {'group': 'img2', 'index': 1, 'pages': [spec2]}]})
        save(corpus / 'reference.json', {'schema': 1, 'method': 'manual_original_review',
             'corpus_sha256': file_hash(corpus / 'corpus.json'), 'reviewer': '',
             'pages': [{'id': 'synthetic', 'pixel_sha256': spec['pixel_sha256'], 'selected': True,
                        'text': '', 'seed': None, 'verified': False, 'complete': False},
                       {'id': 'synthetic-second', 'pixel_sha256': spec2['pixel_sha256'], 'selected': True,
                        'text': '', 'seed': None, 'verified': False, 'complete': False}]})
        save(runs / 'run-000.json', {'scenario': 'cold', 'engine': 'tesseract', 'repeat': 0,
             'document_index': 0, 'pages': [{'id': 'synthetic', 'pixel_sha256': spec['pixel_sha256'], 'text': 'SYNTHETIC OCR 123'}]})
        process = subprocess.Popen([sys.executable, str(Path(__file__).with_name('ocr_engine_reference.py')),
             '--corpus', str(corpus), '--runs', str(runs), '--port', '0'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 15
            while not (corpus / 'review-server.json').exists():
                if time.monotonic() > deadline or process.poll() is not None:
                    raise RuntimeError('Synthetic reviewer failed to start')
                time.sleep(.05)
            metadata = json.loads((corpus / 'review-server.json').read_text()); url = metadata['url']
            base = url.split('/?')[0]; token = url.split('token=')[1]
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(viewport={'width': 1400, 'height': 950})
                errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(url)
                page.wait_for_function("() => document.querySelector('#original').complete && document.querySelector('#original').naturalWidth===1000")
                page.locator('#actual').click()
                assert abs(page.locator('#original').bounding_box()['width'] - 1000) < 1
                page.locator('#reviewer').fill('Synthetic reviewer')
                page.locator('#seed').click()
                page.wait_for_function("() => document.querySelector('#text').value==='SYNTHETIC OCR 123'")
                page.locator('#save').click(); page.wait_for_function("() => document.querySelector('#status').textContent==='Сохранено локально'")
                page.locator('#text').fill('SYNTHETIC VERIFIED 123')
                page.locator('#verified').check(); page.locator('#save').click()
                page.wait_for_function("() => document.querySelector('#status').textContent==='Сохранено локально'")
                saved = json.loads((corpus / 'reference.json').read_text())
                assert saved['pages'][0]['text'] == 'SYNTHETIC VERIFIED 123' and saved['pages'][0]['verified']
                page.locator('#text').fill('SYNTHETIC EDIT INVALIDATES 123')
                assert not page.locator('#verified').is_checked()
                page.locator('#save').click(); page.wait_for_function("() => document.querySelector('#status').textContent==='Сохранено локально'")
                assert not json.loads((corpus / 'reference.json').read_text())['pages'][0]['verified']
                stale = urllib.request.Request(base + '/reference', data=json.dumps(saved).encode(),
                     headers={'X-Review-Token': token, 'Origin': base, 'Content-Type': 'application/json', 'If-Match': 'stale'}, method='POST')
                try:
                    urllib.request.urlopen(stale); raise AssertionError('Stale tab overwrote reference')
                except urllib.error.HTTPError as error:
                    assert error.code == 409
                for headers in ({}, {'X-Review-Token': token, 'Host': 'attacker.invalid'}):
                    try:
                        urllib.request.urlopen(urllib.request.Request(base + '/state', headers=headers)); raise AssertionError('Private document access')
                    except urllib.error.HTTPError as error:
                        assert error.code == 403
                # A delayed seed for A must not overwrite B after navigation.
                page.on('dialog', lambda dialog: dialog.accept())
                page.evaluate('''() => { window.seedRelease = null; window.originalFetch = window.fetch;
                    window.fetch = (url, options) => String(url).startsWith('/seed?')
                      ? new Promise(resolve => { window.seedRelease = () => resolve(new Response(JSON.stringify({text:'STALE PAGE A',seed:{pixel_sha256:state.reference.pages[0].pixel_sha256}}),{status:200,headers:{'Content-Type':'application/json'}})); })
                      : window.originalFetch(url, options); }''')
                page.locator('#seed').click()
                page.wait_for_function('() => window.seedRelease !== null')
                page.locator('#next').click()
                page.wait_for_function("() => document.querySelector('#page').value==='1'")
                page.evaluate('() => window.seedRelease()')
                page.wait_for_function("() => document.querySelector('#status').textContent.includes('Устаревшая')")
                assert page.locator('#text').input_value() == ''
                assert not page.locator('#verified').is_checked()
                assert not errors, 'Synthetic browser had script errors'
                browser.close()
            print('Synthetic reference browser: zoom, full-text edit, OCR seed, verification invalidation, atomic save, delayed-seed page identity, stale/Host/token guards passed')
        finally:
            process.terminate(); process.wait(timeout=10)


if __name__ == '__main__':
    main()
