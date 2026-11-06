"""Stock, one-pass OCR adapters. No SRO imports, field parsing or correction."""
from collections import Counter
import hashlib
import importlib.metadata
import json
from pathlib import Path
import time

LOCK = Path(__file__).with_name('ocr_compare_models.json')


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def rgb_hash(image):
    h = hashlib.sha256(f'RGB:{image.width}:{image.height}:'.encode())
    h.update(image.tobytes())
    return h.hexdigest()


def model_paths(directory):
    result = {}
    for kind, spec in json.loads(LOCK.read_text())['models'].items():
        path = Path(directory) / (spec['name'] + '.onnx')
        if file_hash(path) != spec['sha256']:
            raise ValueError('Model checksum mismatch: ' + kind)
        result[kind] = str(path.resolve())
    return result


def imports(engine):
    # Import time is measured separately from native/model initialization.
    if engine == 'tesseract':
        import tesserocr
        if tesserocr.tesseract_version().splitlines()[0] != 'tesseract 5.5.3':
            raise ValueError('Raw comparison requires native Tesseract 5.5.3')
        return tesserocr
    import cv2
    import numpy as np
    import rapidocr
    cv2.setNumThreads(1)
    lock = json.loads(LOCK.read_text())
    for name in ('rapidocr', 'onnxruntime'):
        if importlib.metadata.version(name) != lock[name]:
            raise ValueError('Unexpected experiment runtime: ' + name)
    # Resolve the lazy import now, outside the model-load interval.
    rapidocr.RapidOCR
    from omegaconf import OmegaConf
    from rapidocr.inference_engine.onnxruntime.main import OrtInferSession
    from rapidocr.utils.process_img import map_boxes_to_original
    return rapidocr


class Tesseract:
    def __init__(self, module, config):
        self.module = module
        self.api = module.PyTessBaseAPI(path=config['tessdata'], lang='rus',
                                       oem=module.OEM.DEFAULT, psm=module.PSM.AUTO)
        if int(self.api.GetPageSegMode()) != 3 or self.api.GetInitLanguagesAsString() != 'rus':
            raise ValueError('Tesseract stock CLI segmentation/language mismatch')
        self.metadata = {'native': module.tesseract_version(), 'binding': module.__version__,
                         'psm': 3, 'oem': 'DEFAULT', 'language': 'rus',
                         'internal_preprocessing': 'native Tesseract only',
                         'geometry_output': 'native text-line segmentation only',
                         'adaptive_state': 'cleared at each document boundary'}
        self.passes = 0

    def begin_document(self):
        self.api.Clear()
        self.api.ClearAdaptiveClassifier()

    def recognize(self, image):
        started = time.perf_counter()
        self.api.SetImage(image)
        adapter = time.perf_counter() - started
        started = time.perf_counter()
        # One and only one explicit Recognize per supplied page. Getters read its
        # recognition_done_ result; they do not call Recognize again on success.
        if not self.api.Recognize():
            raise RuntimeError('Tesseract recognition failed')
        elapsed = time.perf_counter() - started
        self.passes += 1
        started = time.perf_counter()
        text = self.api.GetUTF8Text()
        lines = []
        iterator = self.api.GetIterator()
        if iterator is not None:
            while True:
                box = iterator.BoundingBox(self.module.RIL.TEXTLINE)
                if box is not None:
                    x1, y1, x2, y2 = box
                    lines.append({'text': iterator.GetUTF8Text(self.module.RIL.TEXTLINE) or '',
                                  'polygon': [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]})
                if not iterator.Next(self.module.RIL.TEXTLINE):
                    break
        output = time.perf_counter() - started
        del iterator
        self.api.Clear()  # release results/image, retain initialized language data
        return {'text': text, 'lines': lines,
                'detected_regions': [line['polygon'] for line in lines],
                'detection_scope': 'native post-recognition text-line segmentation; suppression cannot be causally separated',
                'seconds': {'adapter': adapter, 'recognition': elapsed, 'output': output},
                'calls': {'recognize_attempted': 1, 'recognize_completed': 1},
                'pass_index': self.passes, 'input_size': list(image.size)}

    def close(self):
        self.api.End()


class Rapid:
    def __init__(self, module, config):
        from omegaconf import OmegaConf
        from rapidocr.inference_engine.onnxruntime.main import OrtInferSession
        from rapidocr.utils.process_img import map_boxes_to_original
        self.np = __import__('numpy')
        paths = config['models']
        params = {'Global.log_level': 'critical',
                  'Det.ocr_version': module.OCRVersion.PPOCRV5, 'Det.model_type': module.ModelType.SERVER,
                  'Rec.ocr_version': module.OCRVersion.PPOCRV5, 'Rec.model_type': module.ModelType.MOBILE,
                  'Rec.lang_type': module.LangRec.ESLAV,
                  'Cls.ocr_version': module.OCRVersion.PPOCRV5, 'Cls.model_type': module.ModelType.MOBILE,
                  'EngineConfig.onnxruntime.intra_op_num_threads': 1,
                  'EngineConfig.onnxruntime.inter_op_num_threads': 1,
                  'EngineConfig.onnxruntime.use_cuda': False,
                  'EngineConfig.onnxruntime.use_coreml': False,
                  **{key.title() + '.model_path': path for key, path in paths.items()}}
        # No resize, threshold, score, or other image-quality override: the pinned
        # package defaults are the baseline. Server detector is mandatory.
        self.engine = module.RapidOCR(params=params)
        stages = {'det': self.engine._load_det_model(), 'cls': self.engine._load_cls_model(),
                  'rec': self.engine._load_rec_model()}
        providers = [stage.session.session.get_providers() for stage in stages.values()]
        if providers != [['CPUExecutionProvider']] * 3:
            raise ValueError('Exactly three CPU-only sessions required')
        self.counts = Counter()
        self.shapes = []
        self.det_regions = []
        self.preprocessed_size = None
        self.stage_ids = {id(stage.session): name for name, stage in stages.items()}
        original = OrtInferSession.__call__
        def inferred(session, pixels):
            stage = self.stage_ids[id(session)]
            self.counts[stage + '_attempted'] += 1
            self.shapes.append({'stage': stage, 'shape': list(pixels.shape)})
            result = original(session, pixels)
            self.counts[stage + '_completed'] += 1
            return result
        OrtInferSession.__call__ = inferred
        original_preprocess = self.engine.preprocess_img
        def preprocess(pixels):
            result, operations = original_preprocess(pixels)
            self.preprocessed_size = [int(result.shape[1]), int(result.shape[0])]
            return result, operations
        self.engine.preprocess_img = preprocess
        original_output = self.engine.build_final_output
        def output(ori_img, det_res, cls_res, rec_res, crops, operations):
            if det_res.boxes is not None:
                self.det_regions = map_boxes_to_original(det_res.boxes.copy(), operations,
                                                        ori_img.shape[0], ori_img.shape[1]).tolist()
            return original_output(ori_img, det_res, cls_res, rec_res, crops, operations)
        self.engine.build_final_output = output
        self.metadata = {'rapidocr': importlib.metadata.version('rapidocr'),
                         'onnxruntime': importlib.metadata.version('onnxruntime'),
                         'providers': providers,
                         'effective_config': json.loads(json.dumps(OmegaConf.to_container(
                             self.engine.cfg, resolve=True, enum_to_str=True), default=str)),
                         'classifier_input_shape': list(self.engine.text_cls.cls_image_shape),
                         'geometry_output': 'stock line boxes; character geometry disabled'}
        self.passes = 0

    def begin_document(self):
        pass  # no previous result is stored or consulted by this adapter

    def recognize(self, image):
        self.counts.clear()
        self.shapes.clear()
        self.det_regions = []
        started = time.perf_counter()
        pixels = self.np.asarray(image)[:, :, ::-1].copy()  # RGB -> BGR, identical colors/pixels
        adapter = time.perf_counter() - started
        started = time.perf_counter()
        result = self.engine(pixels)
        elapsed = time.perf_counter() - started
        self.passes += 1
        started = time.perf_counter()
        lines = []
        if result.txts is not None:
            lines = [{'text': text, 'polygon': polygon.tolist()}
                     for text, polygon in zip(result.txts, result.boxes, strict=True)]
        output = time.perf_counter() - started
        return {'text': '\n'.join(result.txts or ()), 'lines': lines,
                'detected_regions': self.det_regions,
                'detection_scope': 'detector regions before recognition/score filtering',
                'seconds': {'adapter': adapter, 'recognition': elapsed, 'output': output},
                'calls': {'recognize_attempted': 1, 'recognize_completed': 1, **self.counts},
                'pass_index': self.passes, 'input_size': list(image.size),
                'preprocessed_size': self.preprocessed_size, 'tensor_shapes': self.shapes.copy()}

    def close(self):
        pass
