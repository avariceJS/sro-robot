#!/usr/bin/env python3
"""Independent raw OCR experiment: shared rasters, one pass, cold/resident CPU."""
from __future__ import annotations
import time
ENTERED = time.perf_counter()
import argparse
import importlib.abc
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import select
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import threading

from ocr_engine_backends import file_hash, rgb_hash, model_paths

ROOT = Path(__file__).resolve().parents[2]
PRIVATE = ROOT / '.local-run/ocr-engines'
THREADS = {name: '1' for name in ('OMP_THREAD_LIMIT', 'OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS',
                                 'MKL_NUM_THREADS', 'VECLIB_MAXIMUM_THREADS', 'NUMEXPR_NUM_THREADS')}
IMAGE_SUFFIXES = {'.heic', '.png', '.jpg', '.jpeg', '.tif', '.tiff', '.bmp', '.webp'}


def save(path, data):
    with Path(path).open('w', encoding='utf-8') as stream:
        os.chmod(path, 0o600)
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write('\n')


def require_private(path):
    if not path.resolve().is_relative_to(PRIVATE.resolve()):
        raise ValueError('Experiment artifacts must remain under ignored .local-run/ocr-engines')


def orient_native(image):
    """Display orientation only; HEIF irot/imir is already applied by libheif.

    Same metadata rule as the verified reader, with no pixel bound/downscale.
    The raw benchmark never imports that reader or any SRO processing code.
    """
    from PIL import Image, ImageOps
    if image.format != 'HEIF':
        return ImageOps.exif_transpose(image), {'exif': int(image.getexif().get(274, 1))}
    orientation = image.info.get('original_orientation')
    frames = getattr(image, '_heif_file', None)
    frame = frames[image.tell()] if frames is not None else None
    native = getattr(frame, '_c_image', None)
    transforms = getattr(native, 'transformations', None)
    if transforms is None and orientation in range(2, 9):
        raise ValueError('Cannot verify HEIF header orientation')
    applied = any(t and ((t[0] == 'irot' and len(t) > 1 and t[1] not in (None, 0)) or t[0] == 'imir')
                  for t in transforms or ())
    operations = {2: Image.Transpose.FLIP_LEFT_RIGHT, 3: Image.Transpose.ROTATE_180,
                  4: Image.Transpose.FLIP_TOP_BOTTOM, 5: Image.Transpose.TRANSPOSE,
                  6: Image.Transpose.ROTATE_270, 7: Image.Transpose.TRANSVERSE, 8: Image.Transpose.ROTATE_90}
    oriented = image.transpose(operations[orientation]) if not applied and orientation in operations else image.copy()
    return oriented, {'original_orientation': orientation, 'native_transforms': transforms,
                      'native_orientation_already_applied': applied}


def canonical_rgb(image):
    from PIL import Image
    oriented, orientation = orient_native(image)
    mode = oriented.mode
    bit_depth = image.info.get('bit_depth', 8)
    if mode not in {'RGB', 'RGBA', 'L', 'LA'} or bit_depth != 8:
        oriented.close()
        raise ValueError('Raw benchmark requires explicitly supported 8-bit image modes')
    alpha = oriented.getchannel('A').getextrema() if 'A' in mode else None
    if 'A' in mode:
        rgba = oriented.convert('RGBA')
        background = Image.new('RGBA', rgba.size, (255, 255, 255, 255))
        background.alpha_composite(rgba)
        rgb = background.convert('RGB')
        rgba.close(); background.close()
    else:
        rgb = oriented.convert('RGB')
    oriented.close()
    return rgb, {**orientation, 'source_mode': mode, 'decoded_bit_depth': bit_depth,
                 'alpha_extrema': alpha, 'alpha_policy': 'composite on white equally for both engines'}


def prepare(args):
    args.output = args.output.resolve()
    from PIL import Image, ImageOps
    from pillow_heif import HeifImagePlugin  # decoder only, never the SRO reader
    from pdf2image import pdfinfo_from_path, convert_from_path
    require_private(args.output)
    args.output.mkdir(parents=True, exist_ok=False)
    os.chmod(args.output, 0o700)
    pages_dir = args.output / 'pages'
    pages_dir.mkdir(mode=0o700)
    documents = []
    for group, expected_count in (('img1', 21), ('img2', 4)):
        sources = sorted(p for p in (ROOT / 'samples' / group).rglob('*')
                         if p.is_file() and p.name != '.DS_Store')
        if len(sources) != expected_count:
            raise ValueError(f'{group}: expected {expected_count} original files, got {len(sources)}')
        for source in sources:
            if source.suffix.lower() not in IMAGE_SUFFIXES | {'.pdf'}:
                raise ValueError('Unsupported input format in corpus')
            started = time.perf_counter()
            if source.suffix.lower() == '.png':
                with source.open('rb') as stream:
                    header = stream.read(25)
                if header[:8] != b'\x89PNG\r\n\x1a\n' or header[24] != 8:
                    raise ValueError('PNG must be 8-bit; do not silently quantize a higher-depth source')
            original_hash = file_hash(source)
            document = {'index': len(documents), 'group': group,
                        'source': str(source.relative_to(ROOT)), 'source_sha256': original_hash,
                        'pages': []}
            def record(image, frame, raw_size, orientation, dpi=None):
                # EXIF display orientation is applied equally, once. No OCR-based
                # rotation, grayscale, sharpening, thresholding or resizing.
                canonical, conversion = canonical_rgb(image)
                target = pages_dir / f'd{document["index"]:03}-p{frame:03}.png'
                canonical.save(target, format='PNG')
                os.chmod(target, 0o600)
                page = {'id': f'd{document["index"]:03}-p{frame:03}', 'page': frame,
                        'path': str(target.relative_to(args.output)), 'sha256': file_hash(target),
                        'pixel_sha256': rgb_hash(canonical), 'size': list(canonical.size),
                        'decoded_size': list(raw_size), 'conversion': conversion,
                        'pdf_raster_dpi': dpi}
                document['pages'].append(page)
                canonical.close()
            if source.suffix.lower() == '.pdf':
                info = pdfinfo_from_path(str(source), timeout=30)
                page_count = info['Pages']
                if not 1 <= page_count <= 1000:
                    raise ValueError('PDF page count outside explicit experiment bounds')
                for page in range(1, page_count + 1):
                    images = convert_from_path(str(source), dpi=args.pdf_dpi, first_page=page,
                                               last_page=page, thread_count=1, timeout=120)
                    try:
                        if len(images) != 1:
                            raise ValueError('PDF rasterization omitted a page')
                        record(images[0], page, images[0].size, 1, args.pdf_dpi)
                    finally:
                        for image in images:
                            image.close()
                document['pdf_policy'] = 'all pages rasterized; no text-layer extraction'
            else:
                with Image.open(source) as image:
                    for frame in range(getattr(image, 'n_frames', 1)):
                        image.seek(frame)
                        record(image, frame + 1, image.size, int(image.getexif().get(274, 1)))
            if file_hash(source) != original_hash:
                raise ValueError('Source changed during canonical preparation')
            document['shared_preparation_seconds'] = time.perf_counter() - started
            documents.append(document)
    manifest = {'schema': 1, 'mode': 'raw-engines', 'documents': documents,
                'pixel_policy': 'native dimensions; verified HEIF/EXIF display orientation; 8-bit RGB; alpha on common white; no enhancement',
                'pdf_dpi': args.pdf_dpi, 'platform': platform.platform(), 'machine': platform.machine()}
    save(args.output / 'corpus.json', manifest)
    # Independent reference starts blank: no OCR text, guessed labels or hidden
    # extraction is used to seed the human transcription.
    save(args.output / 'reference.json', {'schema': 1, 'corpus_sha256': file_hash(args.output / 'corpus.json'),
         'method': 'manual_original_only', 'reviewer': '', 'engine_outputs_hidden': True,
         'pages': [{'id': p['id'], 'pixel_sha256': p['pixel_sha256'], 'verified': False,
                    'complete': False, 'lines': [], 'unreadable_regions': [],
                    'category_coverage': {key: 'unreviewed' for key in ('fio', 'inn', 'snils', 'address')}}
                   for d in documents for p in d['pages']]})
    print(json.dumps({'documents': len(documents), 'pages': sum(len(d['pages']) for d in documents),
                      'groups': {g: sum(d['group'] == g for d in documents) for g in ('img1', 'img2')}}))


def identity(args):
    # No regex, document parser or application settings are involved.
    exe = shutil.which('tesseract')
    if not exe:
        raise ValueError('Tesseract not installed')
    lines = subprocess.check_output([exe, '--list-langs'], text=True).splitlines()
    tessdata = Path(lines[0].split('"')[1]).resolve()
    version = subprocess.check_output([exe, '--version'], text=True).splitlines()[0]
    if version != 'tesseract 5.5.3':
        raise ValueError('Raw comparison requires Tesseract 5.5.3')
    return {'tessdata': str(tessdata), 'version': version,
            'rus_sha256': file_hash(tessdata / 'rus.traineddata'),
            'models': model_paths(args.models),
            'versions': {name: importlib.metadata.version(name) for name in
                         ('tesserocr', 'rapidocr', 'onnxruntime', 'Pillow', 'pillow-heif', 'numpy',
                          'opencv-python-headless', 'rapidfuzz')},
            'model_lock_sha256': file_hash(Path(__file__).with_name('ocr_compare_models.json'))}


class NoBusinessLogic(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'sro_robot', 'app', 'ocr_compare'}:
            raise ImportError('SRO/business logic is forbidden in raw OCR workers')


def worker(args):
    sys.meta_path.insert(0, NoBusinessLogic())
    def network_guard(event, values):
        if event in {'socket.__new__', 'socket.connect', 'socket.getaddrinfo', 'socket.sendto', 'socket.sendmsg'}:
            raise RuntimeError('Python network use is forbidden during OCR')
    sys.addaudithook(network_guard)
    from PIL import Image
    from ocr_engine_backends import imports, Tesseract, Rapid
    config = json.loads(Path(args.config).read_text())
    started = time.perf_counter()
    module = imports(args.engine)
    imports_seconds = time.perf_counter() - started
    if file_hash(Path(config['tessdata']) / 'rus.traineddata') != config['rus_sha256']:
        raise ValueError('Tesseract language data drift')
    if args.engine == 'rapidocr' and model_paths(Path(next(iter(config['models'].values()))).parent) != config['models']:
        raise ValueError('Rapid model drift')
    load_started = time.perf_counter()
    backend = (Tesseract if args.engine == 'tesseract' else Rapid)(module, config)
    load_seconds = time.perf_counter() - load_started
    print(json.dumps({'event': 'ready', 'pid': os.getpid(), 'engine': args.engine,
                      'model_load_seconds': load_seconds, 'engine_import_seconds': imports_seconds,
                      'worker_bootstrap_seconds': load_started - ENTERED - imports_seconds,
                      'metadata': backend.metadata}), flush=True)
    try:
        for raw in sys.stdin:
            command = json.loads(raw)
            if command.get('stop'):
                break
            started = time.perf_counter()
            backend.begin_document()
            pages = []
            for page in command['pages']:
                read_started = time.perf_counter()
                path = Path(page['path'])
                if file_hash(path) != page['sha256']:
                    raise ValueError('Encoded input bytes changed')
                with Image.open(path) as original:
                    image = original.convert('RGB')
                    image.load()
                if list(image.size) != page['size'] or rgb_hash(image) != page['pixel_sha256']:
                    raise ValueError('Input pixels, dimensions or color mode changed')
                read_seconds = time.perf_counter() - read_started
                result = backend.recognize(image)
                image.close()
                result.update({'id': page['id'], 'sha256': page['sha256'],
                               'pixel_sha256': page['pixel_sha256'], 'input_read_verify_seconds': read_seconds})
                pages.append(result)
            response = {'event': 'result', 'request': command['request'], 'pid': os.getpid(),
                        'pages': pages, 'worker_document_seconds': time.perf_counter() - started,
                        'rss_after_document': __import__('psutil').Process().memory_info().rss}
            print(json.dumps(response, ensure_ascii=False), flush=True)
            del pages, response, result, command
    finally:
        backend.close()


class RssSampler:
    def __init__(self, pid):
        import psutil
        self.psutil = psutil
        self.root = psutil.Process(pid)
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.peak = self.current = self.interval_peak = 0
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()

    def loop(self):
        while not self.stopped.is_set():
            total = 0
            try:
                family = [self.root, *self.root.children(recursive=True)]
            except self.psutil.NoSuchProcess:
                family = []
            for process in family:
                try:
                    total += process.memory_info().rss
                except (self.psutil.NoSuchProcess, self.psutil.AccessDenied):
                    pass
            with self.lock:
                self.current = total
                self.peak = max(self.peak, total)
                self.interval_peak = max(self.interval_peak, total)
            self.stopped.wait(0.02)

    def snapshot(self, reset=False):
        with self.lock:
            result = {'rss_now': self.current, 'rss_peak': self.peak, 'request_rss_peak': self.interval_peak}
            if reset:
                self.interval_peak = self.current
            return result

    def close(self):
        self.stopped.set()
        self.thread.join()


class Session:
    def __init__(self, engine, config, scratch):
        self.scratch = scratch
        self.started = time.perf_counter()
        self.work = tempfile.TemporaryDirectory(prefix='worker-', dir=scratch)
        self.engine = engine
        env = {key: os.environ[key] for key in ('PATH', 'HOME', 'LANG', 'LC_ALL') if key in os.environ}
        env.update(THREADS, TMPDIR=self.work.name, TMP=self.work.name, TEMP=self.work.name,
                   PYTHONNOUSERSITE='1', TESSDATA_PREFIX=config['tessdata'])
        config_path = Path(self.work.name) / 'config.json'
        save(config_path, config)
        self.process = subprocess.Popen([sys.executable, '-u', str(Path(__file__).resolve()), '_worker',
                                         '--engine', engine, '--config', str(config_path)],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        text=True, cwd=self.work.name, env=env, start_new_session=True)
        self.sampler = RssSampler(self.process.pid)
        try:
            self.ready = self.receive()
            if self.ready['event'] != 'ready' or self.ready['pid'] != self.process.pid:
                raise ValueError('Worker handshake mismatch')
            self.ready['parent_ready_seconds'] = time.perf_counter() - self.started
            self.ready['memory'] = self.sampler.snapshot()
        except BaseException:
            self.close(force=True)
            raise
        self.last_pass = 0

    def receive(self):
        if not select.select([self.process.stdout], [], [], 660)[0]:
            raise TimeoutError('OCR worker response deadline exceeded')
        raw = self.process.stdout.readline()
        if not raw:
            raise RuntimeError('OCR worker exited without a complete response; private input is not logged')
        return json.loads(raw)

    def process_document(self, document, prepared, request):
        started = time.perf_counter()
        with tempfile.TemporaryDirectory(prefix='input-', dir=self.scratch) as temporary:
            pages = []
            for page in document['pages']:
                destination = Path(temporary) / (page['id'] + '.png')
                shutil.copyfile(prepared / page['path'], destination)
                if file_hash(destination) != page['sha256']:
                    raise ValueError('New input copy changed bytes')
                pages.append({**page, 'path': str(destination)})
            copied = time.perf_counter() - started
            before = self.sampler.snapshot(reset=True)
            self.process.stdin.write(json.dumps({'request': request, 'pages': pages}) + '\n')
            self.process.stdin.flush()
            response = self.receive()
            after = self.sampler.snapshot()
            if response['event'] != 'result' or response['request'] != request or response['pid'] != self.process.pid:
                raise ValueError('Stale or mismatched worker response')
            if len(response['pages']) != len(pages):
                raise ValueError('Page omitted by engine')
            for result, page in zip(response['pages'], pages, strict=True):
                self.last_pass += 1
                if (result['pass_index'] != self.last_pass or result['pixel_sha256'] != page['pixel_sha256']
                        or result['sha256'] != page['sha256'] or result['input_size'] != page['size']
                        or result['id'] != page['id']):
                    raise ValueError('Cached result, wrong raster or pass counter')
                calls = result['calls']
                if calls.get('recognize_completed') != 1 or calls.get('recognize_attempted') != 1:
                    raise ValueError('Exactly one completed engine pass per page required')
                if self.engine == 'rapidocr' and calls.get('det_completed', 0) < 1:
                    raise ValueError('No actual ONNX detector execution')
                for key, count in calls.items():
                    if key.endswith('_attempted') and count != calls.get(key[:-10] + '_completed'):
                        raise ValueError('Uncompleted native/runtime call')
            response.update({'copy_verify_seconds': copied, 'memory_before': before,
                             'memory_after': after})
        response['temp_removed'] = not Path(temporary).exists()
        response['request_wall_seconds'] = time.perf_counter() - started
        return response

    def close(self, force=False):
        if hasattr(self, 'process'):
            try:
                if self.process.poll() is None and not force:
                    self.process.stdin.write('{"stop": true}\n')
                    self.process.stdin.flush()
                    self.process.wait(timeout=15)
            finally:
                if self.process.poll() is None:
                    os.killpg(self.process.pid, signal.SIGKILL)
                    self.process.wait()
                self.final_memory = self.sampler.snapshot()
                self.process.stdin.close()
                self.process.stdout.close()
                self.sampler.close()
        self.work.cleanup()


def run(args):
    args.output = args.output.resolve()
    if args.repeats < 3:
        raise ValueError('At least three repeats per engine and document are required')
    require_private(args.output)
    args.output.mkdir(parents=True, exist_ok=False)
    os.chmod(args.output, 0o700)
    prepared = args.corpus.resolve()
    corpus = json.loads((prepared / 'corpus.json').read_text())
    config = identity(args)  # packages, downloads, weights and hashes verified before timing
    scratch = args.output / 'tmp'
    scratch.mkdir(mode=0o700)
    source_paths = [Path(__file__), Path(__file__).with_name('ocr_engine_backends.py'),
                    Path(__file__).with_name('ocr_compare_models.json'),
                    Path(__file__).with_name('requirements-ocr-engines.txt')]
    save(args.output / 'manifest.json', {'schema': 1, 'mode': 'raw-engines', 'corpus': str(prepared),
         'corpus_sha256': file_hash(prepared / 'corpus.json'), 'repeats': args.repeats, 'threads': THREADS,
         'config': config, 'platform': platform.platform(), 'machine': platform.machine(),
         'source_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
         'sources': {str(p.relative_to(ROOT)): file_hash(p) for p in source_paths},
         'warmup_policy': 'one separately recorded pass over first document per resident engine; never scored/timed as steady-state'})
    results = []
    for scenario in ('cold', 'resident'):
        residents = {}
        try:
            if scenario == 'resident':
                for engine in ('tesseract', 'rapidocr'):
                    residents[engine] = Session(engine, config, scratch)
                    prime = residents[engine].process_document(corpus['documents'][0], prepared, 'warmup')
                    save(args.output / f'warmup-{engine}.json', {'ready': residents[engine].ready, 'result': prime})
            for repeat in range(args.repeats):
                for document in corpus['documents']:
                    order = ('tesseract', 'rapidocr') if (repeat + document['index']) % 2 == 0 else ('rapidocr', 'tesseract')
                    for engine in order:
                        started = time.perf_counter()
                        session = Session(engine, config, scratch) if scenario == 'cold' else residents[engine]
                        try:
                            row = session.process_document(document, prepared, len(results))
                            row.update({'scenario': scenario, 'engine': engine, 'document_index': document['index'],
                                        'group': document['group'], 'repeat': repeat, 'ready': session.ready})
                        finally:
                            if scenario == 'cold':
                                session.close()
                        row['cold_full_seconds' if scenario == 'cold' else 'resident_full_seconds'] = time.perf_counter() - started
                        if scenario == 'cold':
                            row['worker_temp_removed'] = not Path(session.work.name).exists()
                            row['lifetime_memory'] = session.final_memory
                        save(args.output / f'run-{len(results):03}.json', row)
                        results.append(row)
                        print(json.dumps({'done': len(results), 'of': len(corpus['documents']) * args.repeats * 4,
                                          'scenario': scenario, 'engine': engine,
                                          'seconds': round(row['request_wall_seconds'], 3)}), flush=True)
        finally:
            for session in residents.values():
                session.close()
    if list(scratch.iterdir()):
        raise ValueError('Temporary worker/input directories remain')
    from ocr_engine_metrics import audit_and_summarize
    summary = audit_and_summarize(args.output)
    save(args.output / 'summary.json', summary)
    print(json.dumps(summary, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    prep = sub.add_parser('prepare')
    prep.add_argument('--output', type=Path, required=True)
    prep.add_argument('--pdf-dpi', type=int, default=300)
    compare = sub.add_parser('run')
    compare.add_argument('--corpus', type=Path, required=True)
    compare.add_argument('--output', type=Path, required=True)
    compare.add_argument('--repeats', type=int, default=3)
    compare.add_argument('--models', type=Path, default=ROOT / '.local-run/ocr-compare/models')
    child = sub.add_parser('_worker')
    child.add_argument('--engine', choices=('tesseract', 'rapidocr'), required=True)
    child.add_argument('--config', required=True)
    score = sub.add_parser('score')
    score.add_argument('--runs', type=Path, required=True)
    score.add_argument('--reference', type=Path, required=True)
    review = sub.add_parser('reference')
    review.add_argument('--corpus', type=Path, required=True)
    review.add_argument('--port', type=int, default=8766)
    args = parser.parse_args(argv)
    if args.command == 'prepare':
        prepare(args)
    elif args.command == '_worker':
        worker(args)
    elif args.command == 'run':
        run(args)
    elif args.command == 'score':
        from ocr_engine_metrics import audit_and_summarize
        print(json.dumps(audit_and_summarize(args.runs, args.reference), indent=2))
    else:
        from ocr_engine_reference import serve
        serve(args.corpus, args.port)


if __name__ == '__main__':
    main()
