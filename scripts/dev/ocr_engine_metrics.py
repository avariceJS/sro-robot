"""Audit raw engine executions and score independently annotated text, never fields."""
import json
from pathlib import Path
import statistics
import unicodedata

from ocr_engine_backends import file_hash

CATEGORIES = ('fio', 'inn', 'snils', 'address')


def normalized(text):
    """NFC and one space between whitespace-delimited tokens; preserve case/digits/punctuation."""
    return unicodedata.normalize('NFC', ' '.join(text.split()))


def errors(reference, hypothesis):
    from rapidfuzz.distance import Levenshtein
    return {'characters': len(reference), 'character_errors': Levenshtein.distance(reference, hypothesis),
            'words': len(reference.split()), 'word_errors': Levenshtein.distance(reference.split(), hypothesis.split())}


def rates(counts):
    return {**counts, 'CER': counts.get('character_errors', 0) / counts['characters'] if counts.get('characters') else None,
            'WER': counts.get('word_errors', 0) / counts['words'] if counts.get('words') else None}


def add(target, source):
    for key, value in source.items():
        target[key] = target.get(key, 0) + value


def polygon(points, size):
    from shapely.geometry import Polygon
    if not isinstance(points, list) or len(points) < 3:
        raise ValueError('Reference requires source polygons')
    poly = Polygon(points)
    x1, y1, x2, y2 = poly.bounds
    if not poly.is_valid or poly.area <= 0 or x1 < 0 or y1 < 0 or x2 > size[0] or y2 > size[1]:
        raise ValueError('Invalid or out-of-source polygon')
    return poly


def validate_reference(reference, corpus, corpus_hash):
    methods = {'manual_original_only', 'manual_original_review'}
    if (reference.get('schema') != 1 or reference.get('corpus_sha256') != corpus_hash
            or reference.get('method') not in methods or not reference.get('reviewer', '').strip()):
        raise ValueError('Original-verified human reference provenance is required')
    if reference['method'] == 'manual_original_only' and reference.get('engine_outputs_hidden') is not True:
        raise ValueError('Original-only reference must not be seeded from OCR')
    expected = {p['id']: p for d in corpus['documents'] for p in d['pages']}
    actual = reference.get('pages', [])
    if len(actual) != len(expected) or len({p['id'] for p in actual}) != len(expected):
        raise ValueError('Reference source-page custody incomplete or duplicated')
    selected = {}
    for page in actual:
        source = expected.get(page['id'])
        if source is None or page.get('pixel_sha256') != source['pixel_sha256']:
            raise ValueError('Reference no longer matches the original raster')
        if not page.get('selected', True):
            continue
        if page.get('verified') is not True or page.get('complete') is not True:
            raise ValueError('Every selected page needs a complete, manually original-verified transcript')
        text = page.get('text', ' '.join(line['text'] for line in page.get('lines', [])))
        if not isinstance(text, str) or (not text.strip() and not page.get('blank') and not page.get('has_unreadable')):
            raise ValueError('Empty source reference needs explicit blank/unreadable attestation')
        if page.get('blank') and (text.strip() or page.get('has_unreadable') or page.get('unreadable_regions')):
            raise ValueError('Blank original attestation contradicts text/unreadable content')
        page = {**page, 'text': text}
        # Optional source regions are never inferred by a regex or another OCR.
        for line in page.get('lines', []):
            polygon(line['polygon'], source['size'])
        for span in page.get('spans', []):
            if (span['category'] not in CATEGORIES or not isinstance(span['start'], int)
                    or not isinstance(span['end'], int) or not 0 <= span['start'] < span['end'] <= len(text)):
                raise ValueError('Invalid manually selected character span')
        selected[page['id']] = page
    if not selected:
        raise ValueError('Select at least one independently verified source page')
    return selected


def span_errors(reference, hypothesis, start, end):
    """Character alignment of a manually selected source span; no ID/role parser."""
    from rapidfuzz.distance import Levenshtein
    counts = {'characters': end - start, 'correct_characters': 0, 'character_errors': 0}
    for op in Levenshtein.opcodes(reference, hypothesis):
        overlap = max(0, min(end, op.src_end) - max(start, op.src_start))
        if op.tag == 'equal':
            counts['correct_characters'] += overlap
        elif op.tag == 'insert':
            # Fixed half-open reference boundary rule, independent of output.
            if start <= op.src_start < end:
                counts['character_errors'] += op.dest_end - op.dest_start
        else:
            counts['character_errors'] += overlap
    return counts


def page_quality(reference, result, source):
    from shapely.ops import unary_union
    truth = reference['lines']
    # Engine polygons can have fractional subpixel drift at image borders;
    # intersect with the actual source canvas, without moving/resizing pixels.
    from shapely.geometry import Polygon, box
    canvas = box(0, 0, *source['size'])
    detections = [Polygon(p).intersection(canvas) for p in result['detected_regions']]
    coverage = unary_union(detections) if detections else Polygon()
    truth_polys = [polygon(line['polygon'], source['size']) for line in truth]
    assigned = [[] for _ in truth]
    unmatched = 0
    for observed in result['lines']:
        region = Polygon(observed['polygon']).intersection(canvas)
        overlap = [region.intersection(p).area for p in truth_polys]
        if overlap and max(overlap) / min(region.area, truth_polys[overlap.index(max(overlap))].area) >= 0.5:
            assigned[overlap.index(max(overlap))].append(observed['text'])
        else:
            unmatched += 1
    counts = {'lines': len(truth), 'detection_missing_lines': 0, 'detected_no_output_lines': 0, 'unmatched_output_lines': unmatched}
    readable = {}; conditional = {}; spans = {category: {} for category in CATEGORIES}
    for index, line in enumerate(truth):
        reference_text = line['text']
        hypothesis = normalized(' '.join(assigned[index]))
        detected = truth_polys[index].intersection(coverage).area / truth_polys[index].area >= 0.5
        counts['detection_missing_lines'] += not detected
        counts['detected_no_output_lines'] += detected and not hypothesis
        add(readable, errors(reference_text, hypothesis))
        if detected:
            add(conditional, errors(reference_text, hypothesis))
        for span in line.get('spans', []):
            add(spans[span['category']], span_errors(reference_text, hypothesis, span['start'], span['end']))
    # Illegible originals do not become invented reference characters. Full-page
    # metrics are unavailable for those pages; readable-region metrics remain labelled.
    full = None if reference.get('unreadable_regions') else errors(
        normalized(' '.join(line['text'] for line in truth)), normalized(result['text']))
    return {'coverage': counts, 'readable': readable, 'conditional_detected': conditional,
            'full': full, 'spans': spans}


def audit_and_summarize(directory, reference_path=None):
    directory = Path(directory)
    manifest = json.loads((directory / 'manifest.json').read_text())
    corpus_file = Path(manifest['corpus']) / 'corpus.json'
    if file_hash(corpus_file) != manifest['corpus_sha256']:
        raise ValueError('Prepared corpus changed')
    corpus = json.loads(corpus_file.read_text())
    docs = {d['index']: d for d in corpus['documents']}
    expected = {(scenario, engine, index, repeat) for scenario in ('cold', 'resident')
                for engine in ('tesseract', 'rapidocr') for index in docs for repeat in range(manifest['repeats'])}
    rows = {}
    for path in sorted(directory.glob('run-*.json')):
        row = json.loads(path.read_text())
        key = row['scenario'], row['engine'], row['document_index'], row['repeat']
        if key not in expected or key in rows or not row['temp_removed']:
            raise ValueError('Unexpected/duplicate run or unremoved input directory')
        source = docs[row['document_index']]
        if len(row['pages']) != len(source['pages']):
            raise ValueError('Missing OCR page')
        if row['engine'] == 'rapidocr':
            config = row['ready']['metadata']['effective_config']
            if (row['ready']['metadata']['providers'] != [['CPUExecutionProvider']] * 3
                    or config['Det']['model_type'] != 'SERVER'
                    or config['Global']['max_side_len'] != 2000
                    or config['Det']['limit_side_len'] != 736 or config['Det']['limit_type'] != 'min'
                    or config['Global']['return_word_box'] or config['Global']['return_single_char_box']):
                raise ValueError('Rapid stock server-detector/CPU/resize configuration drift')
        else:
            metadata = row['ready']['metadata']
            if metadata['psm'] != 3 or metadata['language'] != 'rus' or metadata['native'].splitlines()[0] != 'tesseract 5.5.3':
                raise ValueError('Tesseract runtime/stock configuration drift')
        for page, spec in zip(row['pages'], source['pages'], strict=True):
            if (page['id'] != spec['id'] or page['pixel_sha256'] != spec['pixel_sha256']
                    or page['sha256'] != spec['sha256'] or page['input_size'] != spec['size']):
                raise ValueError('Engine inputs differ')
            calls = page['calls']
            if calls.get('recognize_attempted') != 1 or calls.get('recognize_completed') != 1:
                raise ValueError('Engine did not perform exactly one pass per page')
            for name, count in calls.items():
                if name.endswith('_attempted') and calls.get(name[:-10] + '_completed') != count:
                    raise ValueError('Uncompleted runtime call')
        if row['scenario'] == 'cold' and not row.get('worker_temp_removed'):
            raise ValueError('Cold worker temporary directory remains')
        if row['memory_after']['request_rss_peak'] <= 0:
            raise ValueError('Missing sampled process-tree RSS')
        rows[key] = row
    if set(rows) != expected:
        raise ValueError(f'Incomplete measurement set: {len(rows)} of {len(expected)}')
    cold_pids = [r['pid'] for r in rows.values() if r['scenario'] == 'cold']
    if len(set(cold_pids)) != len(cold_pids):
        raise ValueError('Cold worker reused')
    for engine in ('tesseract', 'rapidocr'):
        if len({r['pid'] for r in rows.values() if r['scenario'] == 'resident' and r['engine'] == engine}) != 1:
            raise ValueError('Resident engine did not retain one process/model instance')
    summary = {'mode': 'raw-engines', 'verified_runs': len(rows), 'documents': len(docs),
               'pages': sum(len(d['pages']) for d in docs.values()), 'repeats': manifest['repeats'],
               'quality_scored': reference_path is not None,
               'quality_limit': 'Human verification against the original is required, including for OCR-prefilled drafts' if reference_path is None else None,
               'performance': {}, 'quality': {},
               'performance_environment': 'Interactive Mac with concurrent local reference UI/development; not an isolated machine or VPS. Background CPU/cache/I/O noise may affect timings.',
               'normalization': 'Unicode NFC; collapse whitespace; retain case, digits, punctuation; no regex or correction',
               'detection_limit': 'Coverage is geometric: Tesseract exposes post-recognition segmentation, Rapid pre-filter detector boxes; suppression cannot always be causally assigned.'}
    for group in ('img1', 'img2', 'all'):
        summary['performance'][group] = {}
        for engine in ('tesseract', 'rapidocr'):
            summary['performance'][group][engine] = {}
            for scenario in ('cold', 'resident'):
                selected = [r for r in rows.values() if r['engine'] == engine and r['scenario'] == scenario
                            and (group == 'all' or r['group'] == group)]
                median = lambda values: statistics.median(values)
                summary['performance'][group][engine][scenario] = {
                    'runs': len(selected),
                    'median_request_seconds': median(r['request_wall_seconds'] for r in selected),
                    'median_full_seconds': median(r['cold_full_seconds' if scenario == 'cold' else 'resident_full_seconds'] for r in selected),
                    'median_recognition_seconds': median(sum(p['seconds']['recognition'] for p in r['pages']) for r in selected),
                    'median_model_load_seconds': median(r['ready']['model_load_seconds'] for r in selected) if scenario == 'cold' else None,
                    'max_sampled_request_tree_rss_mib': max(r['memory_after']['request_rss_peak'] for r in selected) / 2**20,
                    'max_sampled_lifetime_tree_rss_mib': max(r.get('lifetime_memory', r['memory_after'])['rss_peak'] for r in selected) / 2**20 if scenario == 'cold' or group == 'all' else None,
                    'max_sampled_startup_tree_rss_mib': max(r['ready']['memory']['rss_peak'] for r in selected) / 2**20,
                    'median_pre_output_release_rss_mib': median(r['rss_after_document'] / 2**20 for r in selected)}
    if reference_path is not None:
        from rapidfuzz.distance import Levenshtein
        reference_document = json.loads(Path(reference_path).read_text())
        reference = validate_reference(reference_document, corpus, manifest['corpus_sha256'])
        summary['reference'] = {'selected_pages': len(reference), 'corpus_pages': summary['pages'],
                                'method': reference_document['method'],
                                'seeded_pages': sum(bool(p.get('seed')) for p in reference.values()),
                                'limit': 'Human attestations; OCR-seeded transcripts are original-verified, not blind. Selected subset does not establish whole-corpus accuracy.'}
        for group in ('img1', 'img2', 'all'):
            summary['quality'][group] = {}
            for engine in ('tesseract', 'rapidocr'):
                full = {}; full_pages = 0; unreadable = 0; line_deletions = 0
                spans = {c: {} for c in CATEGORIES}; geometries = []
                for doc in docs.values():
                    if group != 'all' and doc['group'] != group:
                        continue
                    row = rows['cold', engine, doc['index'], 0]
                    for result, source in zip(row['pages'], doc['pages'], strict=True):
                        verified = reference.get(source['id'])
                        if verified is None:
                            continue
                        if verified.get('has_unreadable') or verified.get('unreadable_regions'):
                            unreadable += 1
                            continue
                        full_pages += 1
                        add(full, errors(normalized(verified['text']), normalized(result['text'])))
                        ref_lines = [normalized(t) for t in verified['text'].splitlines() if t.strip()]
                        hyp_lines = [normalized(t) for t in result['text'].splitlines() if t.strip()]
                        line_deletions += sum(op.tag == 'delete' for op in Levenshtein.editops(ref_lines, hyp_lines))
                        for span in verified.get('spans', []):
                            add(spans[span['category']], span_errors(verified['text'], result['text'], span['start'], span['end']))
                        if verified.get('lines'):
                            geometries.append(page_quality(verified, result, source))
                summary['quality'][group][engine] = {
                    'full_page_scored_pages': full_pages, 'unreadable_original_pages_excluded': unreadable,
                    'full_page': rates(full), 'text_alignment_line_deletions': line_deletions,
                    'line_deletion_limit': 'Text-line edit alignment is not a detector-error diagnosis; wrapping/order may differ.',
                    'geometrically_annotated_pages': len(geometries),
                    'detection_reading_split': None if not geometries else [m['coverage'] for m in geometries],
                    'manual_spans': {category: {
                        **rates(values),
                        'character_recall': values.get('correct_characters', 0) / values['characters'] if values.get('characters') else None,
                        'one_minus_CER': 1 - values.get('character_errors', 0) / values['characters'] if values.get('characters') else None,
                        'scope': 'Partial manual text spans, line/page alignment only; includes insertions in CER; no spatial/participant attribution.'
                    } for category, values in spans.items()}}
    return summary
