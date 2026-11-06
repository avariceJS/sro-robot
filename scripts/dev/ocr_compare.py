#!/usr/bin/env python3
"""Private, cold-process CPU photo benchmark. No app/DB/queue imports or result cache."""
from __future__ import annotations

import time
PROCESS_ENTERED = time.perf_counter()

import argparse
from collections import Counter, defaultdict
from contextlib import contextmanager
from functools import wraps
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
LOCK = Path(__file__).with_name('ocr_compare_models.json')
PRIVATE = ROOT / '.local-run/ocr-compare'
THREAD_ENV = {key: '1' for key in (
    'OMP_THREAD_LIMIT', 'OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS',
    'VECLIB_MAXIMUM_THREADS', 'MKL_NUM_THREADS', 'NUMEXPR_NUM_THREADS',
)}
FIELDS = ('fio_raw', 'inn', 'snils', 'address', 'case_number', 'doc_date',
          'legal_name', 'short_name', 'legal_form', 'ogrn', 'ogrnip', 'kpp')


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def private_json(path, value):
    path = Path(path)
    with path.open('w', encoding='utf-8') as target:
        os.chmod(path, 0o600)
        json.dump(value, target, ensure_ascii=False, indent=2)
        target.write('\n')


def models(directory, download=False):
    lock = json.loads(LOCK.read_text())
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    result = {}
    for kind, info in lock['models'].items():
        path = directory / (info['name'] + '.onnx')
        if not path.exists() and download:
            partial = path.with_suffix('.part')
            try:
                with urllib.request.urlopen(info['url'], timeout=120) as source, partial.open('wb') as target:
                    shutil.copyfileobj(source, target)
                if digest(partial) != info['sha256']:
                    raise ValueError('Model checksum mismatch: ' + kind)
                partial.replace(path)
            finally:
                partial.unlink(missing_ok=True)
        if not path.is_file() or digest(path) != info['sha256']:
            raise ValueError('Missing or changed model: ' + kind)
        result[kind] = str(path.resolve())
    return result


class Clock:
    """Exclusive phases: entering a nested phase pauses the enclosing one."""
    def __init__(self, started=None):
        self.seconds = defaultdict(float)
        self.phase = 'startup'
        self.mark = time.perf_counter() if started is None else started

    def switch(self, phase):
        now = time.perf_counter()
        self.seconds[self.phase] += now - self.mark
        self.mark, self.phase = now, phase

    @contextmanager
    def measure(self, phase):
        before = self.phase
        self.switch(phase)
        try:
            yield
        finally:
            self.switch(before)

    def wrap(self, owner, name, phase):
        original = getattr(owner, name)
        @wraps(original)
        def measured(*args, **kwargs):
            with self.measure(phase):
                return original(*args, **kwargs)
        setattr(owner, name, measured)


def deny_network(event, args):
    if event in {'socket.__new__', 'socket.connect', 'socket.getaddrinfo', 'socket.bind', 'socket.sendto', 'socket.sendmsg'}:
        raise RuntimeError('Network is forbidden in OCR benchmark workers')


def baseline_identity():
    import re
    executable = shutil.which('tesseract')
    if not executable:
        raise ValueError('Tesseract executable not installed')
    langs = subprocess.check_output([executable, '--list-langs'], text=True)
    match = re.search(r'in "([^"\n]+)"', langs)
    if not match:
        raise ValueError('Cannot establish effective Tesseract language directory')
    tessdata = Path(match[1]).resolve()
    return {'executable': str(Path(executable).resolve()), 'tessdata': str(tessdata),
            'version': subprocess.check_output([executable, '--version'], text=True).splitlines()[0],
            'executable_sha256': digest(executable),
            'traineddata_sha256': {name: digest(tessdata / (name + '.traineddata'))
                                   for name in ('rus', 'osd')}}


def worker(args):
    # All files and logs remain in a unique private directory owned by the parent.
    clock = Clock(PROCESS_ENTERED)
    sys.path.insert(0, str(ROOT))
    sys.addaudithook(deny_network)
    import logging
    logging.disable(logging.CRITICAL)
    from PIL import Image
    from sro_robot.core import ocr, extract, entity_extract
    from sro_robot.core.source_locations import SourceLocationCollector
    from sro_robot.core.models import card_to_fields
    counts = Counter()
    ocr.pytesseract.pytesseract.tesseract_cmd = args.tesseract
    original_run = ocr.pytesseract.pytesseract.run_tesseract
    def run_tesseract(*a, **kw):
        counts['tesseract_attempted'] += 1
        with clock.measure('backend_calls'):
            result = original_run(*a, **kw)
        counts['tesseract_completed'] += 1
        return result
    ocr.pytesseract.pytesseract.run_tesseract = run_tesseract
    clock.wrap(extract, 'parse', 'extraction')
    clock.wrap(entity_extract, 'parse_entity', 'extraction')
    clock.wrap(SourceLocationCollector, 'bind_card', 'extraction')
    clock.switch('preparation')
    path = Path(args.input)
    with clock.measure('provenance'):
        runtime_identity = baseline_identity()
    if digest(path) != args.sha256:
        raise ValueError('Worker input bytes differ from source')
    # Current local sample corpus contains single-frame HEIC photos only. Fail
    # closed on unsupported multi-page input instead of silently reading page 1.
    with Image.open(path) as image:
        page_count = getattr(image, 'n_frames', 1)
    if path.suffix.lower() not in ocr.IMAGE_SUFFIXES or page_count != 1:
        raise ValueError('This photo harness requires a single-frame image')
    versions = {name: importlib.metadata.version(name) for name in (
        'pytesseract', 'Pillow', 'pdf2image', 'pillow-heif', 'pymorphy3',
        'pymorphy3-dicts-ru',
    )}
    config = {'threads': THREAD_ENV, 'pages': page_count, 'dpi': 300,
              'psm': ocr.PSM_MODES, 'tesseract_language': 'rus',
              'heic_max_side': ocr.HEIC_OCR_MAX_SIDE,
              'limits': {'pdf_pages': 80, 'image_pixels': 12000000, 'tiff_frames': 80},
              'tesseract_runtime': runtime_identity,
              'budgets_seconds': {'overall': 540, 'tesseract': 45, 'pdfinfo': 20, 'pdftext': 60}}
    if args.engine == 'rapidocr':
        with clock.measure('startup'):
            import cv2
            import numpy as np
            from rapidocr import RapidOCR, OCRVersion, ModelType, LangRec
            from rapidocr.inference_engine.onnxruntime.main import OrtInferSession
            cv2.setNumThreads(1)
        for name in ('rapidocr', 'onnxruntime', 'opencv-python-headless', 'numpy'):
            versions[name] = importlib.metadata.version(name)
        lock = json.loads(LOCK.read_text())
        assert versions['rapidocr'] == lock['rapidocr']
        assert versions['onnxruntime'] == lock['onnxruntime']
        with clock.measure('provenance'):
            paths = models(args.models)
        params = {
            'Global.log_level': 'critical', 'Global.max_side_len': 3400,
            'Global.return_word_box': False,
            'EngineConfig.onnxruntime.intra_op_num_threads': 1,
            'EngineConfig.onnxruntime.inter_op_num_threads': 1,
            'EngineConfig.onnxruntime.use_coreml': False,
            'EngineConfig.onnxruntime.use_cuda': False,
            'Det.ocr_version': OCRVersion.PPOCRV5, 'Det.model_type': ModelType.SERVER,
            'Det.limit_side_len': 1600, 'Det.limit_type': 'max',
            'Rec.ocr_version': OCRVersion.PPOCRV5, 'Rec.model_type': ModelType.MOBILE, 'Rec.lang_type': LangRec.ESLAV,
            'Cls.ocr_version': OCRVersion.PPOCRV5, 'Cls.model_type': ModelType.MOBILE,
            **{kind.title() + '.model_path': value for kind, value in paths.items() if kind in {'det', 'rec', 'cls'}},
        }
        original_infer = OrtInferSession.__call__
        def infer(session, *a, **kw):
            counts['onnx_attempted'] += 1
            with clock.measure('backend_calls'):
                result = original_infer(session, *a, **kw)
            counts['onnx_completed'] += 1
            return result
        OrtInferSession.__call__ = infer
        with clock.measure('model_load'):
            engine = RapidOCR(params=params)
            sessions = [engine._load_det_model(), engine._load_cls_model(), engine._load_rec_model()]
        providers = [model.session.session.get_providers() for model in sessions]
        if any(p != ['CPUExecutionProvider'] for p in providers):
            raise ValueError('Unexpected execution provider')
        config.update({'rapidocr_params': {k: getattr(v, 'value', v) for k, v in params.items()}, 'providers': providers,
                       'models': lock['models'], 'geometry': 'not mapped to application word spans',
                       'recovery': 'single neural pass; no Tesseract recovery or full-page OSD rotation'})
        def rapid_recognize(source, **kw):
            pages = ocr.load_pages(source, dpi=kw.get('dpi', 300),
                                   max_image_pixels=kw.get('max_image_pixels', 12000000))
            if len(pages) != page_count:
                raise ValueError('Not all pages decoded')
            texts = []
            try:
                for page in pages:
                    pixels = np.asarray(page.convert('RGB'))[:, :, ::-1].copy()
                    result = engine(pixels)
                    counts['pages_completed'] += 1
                    texts.append('\n'.join(result.txts or ()))
            finally:
                for page in pages:
                    page.close()
            return ['\f'.join(texts)]
        ocr.recognize = rapid_recognize
        # Keep the same parse_file/extractor, but explicitly exclude baseline
        # Tesseract-only recovery passes from the neural candidate.
        ocr.recover_role_bound_inn = lambda *a, **kw: []
        ocr.recover_low_resolution_header = lambda *a, **kw: []
    else:
        config['geometry'] = 'existing Tesseract txt+TSV and source binding'
        config['recovery'] = 'unchanged parse_file policy including optional recovery'
    card = extract.parse_file(path, dpi=300, budget=ocr.OcrBudget())
    if args.engine == 'tesseract':
        counts['pages_completed'] = page_count
    if counts['tesseract_completed' if args.engine == 'tesseract' else 'onnx_completed'] <= 0:
        raise ValueError('No completed OCR execution')
    fields = card_to_fields(card)
    clock.switch('serialization')
    private_json(args.result, {
        'engine': args.engine, 'sha256': args.sha256, 'pid': os.getpid(),
        'seconds': dict(clock.seconds), 'calls': dict(counts), 'versions': versions,
        'config': config, 'fields': fields,
    })


def run_one(engine, source, sha, args, index):
    import psutil
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix='run-', dir=PRIVATE / 'tmp') as temp:
        work = Path(temp)
        target = work / ('input' + source.suffix.lower())
        shutil.copyfile(source, target)
        os.chmod(target, 0o600)
        if digest(target) != sha:
            raise ValueError('Copy changed document bytes')
        copy_seconds = time.perf_counter() - started
        executable = args.baseline_python if engine == 'tesseract' else sys.executable
        command = [str(executable), str(Path(__file__).resolve()), '_worker',
                   '--engine', engine, '--input', str(target), '--sha256', sha,
                   '--result', str(work / 'result.json'), '--models', str(args.models),
                   '--tesseract', args.baseline['executable']]
        env = {key: os.environ[key] for key in ('PATH', 'HOME', 'LANG', 'LC_ALL') if key in os.environ}
        env.update({**THREAD_ENV, 'TMPDIR': str(work), 'TMP': str(work), 'TEMP': str(work),
                    'PYTHONNOUSERSITE': '1', 'TESSDATA_PREFIX': args.baseline['tessdata']})
        # Never import application settings or inherit a requested application env file.
        env.pop('SRO_ROBOT_ENV_FILE', None)
        peak = 0
        with (work / 'worker.log').open('wb') as log:
            process = subprocess.Popen(command, cwd=work, env=env, stdout=log, stderr=log,
                                       start_new_session=True)
            monitored = psutil.Process(process.pid)
            try:
                while process.poll() is None:
                    rss = 0
                    try:
                        family = [monitored, *monitored.children(recursive=True)]
                    except psutil.NoSuchProcess:
                        family = []
                    for child in family:
                        try:
                            rss += child.memory_info().rss
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            pass
                    peak = max(peak, rss)
                    if time.perf_counter() - started > 660:
                        raise TimeoutError('Benchmark worker exceeded bound')
                    time.sleep(0.02)
                if process.returncode:
                    # No private OCR/log data is printed. Persist only in ignored output.
                    shutil.copyfile(work / 'worker.log', args.output / f'failure-{index}.log')
                    raise RuntimeError(f'Worker failed at run {index}; inspect private failure log')
            finally:
                if process.poll() is None:
                    import signal
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
        result = json.loads((work / 'result.json').read_text())
        if result['config']['tesseract_runtime'] != args.baseline:
            raise ValueError('Baseline runtime/weights changed during the benchmark')
        if result['sha256'] != sha or result['pid'] != process.pid:
            raise ValueError('Worker identity or input mismatch')
        result.update({'sample': index, 'copy_seconds': copy_seconds,
                       'total_seconds': time.perf_counter() - started,
                       'peak_tree_rss_bytes': peak, 'memory_sample_interval_ms': 20})
    result['temp_removed'] = not work.exists()
    result['accounted_seconds'] = {**result['seconds'], 'parent_copy': copy_seconds}
    result['accounted_seconds']['process_output_monitor_overhead'] = (
        result['total_seconds'] - sum(result['accounted_seconds'].values())
    )
    if result['accounted_seconds']['process_output_monitor_overhead'] < 0:
        raise ValueError('Nested timings were double counted')
    return result


def benchmark(args):
    if args.repeats < 3:
        raise ValueError('At least three repeats required')
    samples = (ROOT / 'samples').resolve()
    files = sorted(p for p in samples.rglob('*') if p.is_file() and p.suffix.lower() == '.heic')
    if not files:
        raise ValueError('No existing HEIC samples')
    if not args.output.resolve().is_relative_to(PRIVATE.resolve()):
        raise ValueError('Results must stay in ignored private benchmark directory')
    args.output.mkdir(parents=True, exist_ok=False)
    os.chmod(args.output, 0o700)
    (PRIVATE / 'tmp').mkdir(exist_ok=True, mode=0o700)
    models(args.models)  # All downloads and initial hash verification precede timing.
    from PIL import Image
    from pillow_heif import HeifImagePlugin  # register decoder before corpus preflight
    frames = []
    for path in files:
        with Image.open(path) as image:
            frames.append(getattr(image, 'n_frames', 1))
    if any(count != 1 for count in frames):
        raise ValueError('Corpus contains multi-frame input: extend the photo harness before measuring')
    hashes = [digest(path) for path in files]
    args.baseline = baseline_identity()
    private_json(args.output / 'manifest.json', {
        'source_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'platform': platform.platform(), 'machine': platform.machine(),
        'sample_count': len(files), 'repeats': args.repeats, 'threads': THREAD_ENV,
        'baseline': args.baseline,
        'source_sha256': {str(p.relative_to(ROOT)): digest(p) for p in [
            Path(__file__).resolve(), LOCK, Path(__file__).with_name('requirements-ocr-compare.txt'),
            *sorted((ROOT / 'sro_robot/core').glob('*.py')),
        ]},
        'inputs': [{'index': i, 'path': str(p.relative_to(ROOT)), 'sha256': h, 'frames': frames[i]}
                   for i, (p, h) in enumerate(zip(files, hashes))],
        'baseline_python': str(args.baseline_python),
        'truth': 'not established: no independently human-verified source reference',
    })
    results = []
    for repeat in range(args.repeats):
        for sample, (path, sha) in enumerate(zip(files, hashes)):
            order = ('tesseract', 'rapidocr') if (repeat + sample) % 2 == 0 else ('rapidocr', 'tesseract')
            for engine in order:
                result = run_one(engine, path, sha, args, len(results))
                result.update({'document_index': sample, 'repeat': repeat})
                private_json(args.output / f'run-{len(results):03}.json', result)
                results.append(result)
                print(json.dumps({'done': len(results), 'of': len(files) * args.repeats * 2,
                                  'engine': engine, 'seconds': round(result['total_seconds'], 2),
                                  'calls': result['calls']}), flush=True)
    summary = {}
    for engine in ('tesseract', 'rapidocr'):
        selected = [r for r in results if r['engine'] == engine]
        summary[engine] = {
            'runs': len(selected), 'median_seconds': statistics.median(r['total_seconds'] for r in selected),
            'min_seconds': min(r['total_seconds'] for r in selected),
            'max_seconds': max(r['total_seconds'] for r in selected),
            'max_peak_tree_rss_mib': max(r['peak_tree_rss_bytes'] for r in selected) / 1024**2,
            'median_phase_seconds': {phase: statistics.median(r['accounted_seconds'].get(phase, 0) for r in selected)
                                     for phase in ('startup', 'preparation', 'model_load', 'backend_calls', 'extraction',
                                                   'provenance', 'parent_copy', 'process_output_monitor_overhead')},
            'median_without_provenance_seconds': statistics.median(
                r['total_seconds'] - r['seconds'].get('provenance', 0) for r in selected),
            'correct_wrong_missing': None,
            'quality_limit': 'Unscored: original-source human ground truth is required, including ownership and apartment.',
            'all_temp_removed': all(r['temp_removed'] for r in selected),
            'completed_calls': sum(r['calls'].get('tesseract_completed' if engine == 'tesseract' else 'onnx_completed', 0)
                                   for r in selected),
        }
    private_json(args.output / 'summary.json', summary)
    print(json.dumps(summary, indent=2))


def main():
    # The historical command remains a full-pipeline comparison. The independent
    # engine mode deliberately never imports its worker or any SRO modules.
    if sys.argv[1:2] == ['engines']:
        from ocr_engines_compare import main as engines_main
        engines_main(sys.argv[2:])
        return
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('engines', help='independent stock OCR engines; see engines --help')
    setup = sub.add_parser('models')
    setup.add_argument('--models', type=Path, default=PRIVATE / 'models')
    run = sub.add_parser('run')
    run.add_argument('--models', type=Path, default=PRIVATE / 'models')
    run.add_argument('--baseline-python', type=Path, default=ROOT / '.venv/bin/python')
    run.add_argument('--output', type=Path, required=True)
    run.add_argument('--repeats', type=int, default=3)
    child = sub.add_parser('_worker')
    for name in ('engine', 'input', 'sha256', 'result', 'models', 'tesseract'):
        child.add_argument('--' + name, required=True)
    args = parser.parse_args()
    if args.command == 'models':
        models(args.models, download=True)
        print('All official model SHA-256 checks passed')
    elif args.command == '_worker':
        worker(args)
    else:
        benchmark(args)


if __name__ == '__main__':
    main()
