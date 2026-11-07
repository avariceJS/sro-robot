#!/usr/bin/env python3
"""Build/qualify a host-local Tesseract CLI. No models, private data or binaries in Git."""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import ctypes
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.request import urlopen
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from sro_robot.core import native_tesseract as native

ARCHIVE_SHA = "21dbaff9867d937c156d0b898e90b726aa339129cfebd7e1365cd5fee5046d67"
WHEEL_SHA = "ad697643a00d9ba85179590a383c4f7401169b55ebf4b8b2938daf28c6bdeb6d"
ORIGINAL_SHA = "7b9a23786afa1a622d05db6016cdae20497ab394f38658b1f2eb3ade16b3c393"
PATCHED_SHA = "5f742e6117cf4db27d037a05200a7bd36b33f5a34b6876136902fda615c1547e"
LIMIT_ENV = dict(OMP_THREAD_LIMIT="1", OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", VECLIB_MAXIMUM_THREADS="1")


def write_json(path, value):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, sort_keys=True)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@contextmanager
def runtime_lock():
    import fcntl
    native.RUNTIME.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (native.RUNTIME / ".build.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another native build/qualification is active") from None
        yield


def fetch(url, target, expected):
    with urlopen(url, timeout=30) as response, target.open("wb") as output:
        size = 0
        while chunk := response.read(1024 * 1024):
            size += len(chunk)
            if size > 80 * 1024 * 1024:
                raise ValueError("Public dependency download exceeded bound")
            output.write(chunk)
    if native.sha256(target) != expected:
        raise ValueError("Dependency SHA-256 mismatch")


def thermal_state():
    ctypes.CDLL("/System/Library/Frameworks/Foundation.framework/Foundation")
    lib = ctypes.CDLL("/usr/lib/libobjc.A.dylib")
    lib.objc_getClass.argtypes = [ctypes.c_char_p]; lib.objc_getClass.restype = ctypes.c_void_p
    lib.sel_registerName.argtypes = [ctypes.c_char_p]; lib.sel_registerName.restype = ctypes.c_void_p
    ptr = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(("objc_msgSend", lib))
    integer = ctypes.CFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p)(("objc_msgSend", lib))
    process = ptr(lib.objc_getClass(b"NSProcessInfo"), lib.sel_registerName(b"processInfo"))
    return integer(process, lib.sel_registerName(b"thermalState"))


def resources():
    memory = subprocess.check_output(["memory_pressure", "-Q"], text=True, timeout=3)
    swap = subprocess.check_output(["sysctl", "vm.swapusage"], text=True, timeout=3)
    return (int(re.search(r"free percentage:\s*(\d+)", memory)[1]),
            float(re.search(r"used\s*=\s*([\d.]+)M", swap)[1]), thermal_state())


def admit(start_swap):
    available, swap, heat = resources()
    if available < 20 or swap > start_swap + 256 or heat >= 2:
        raise RuntimeError("Stopped at memory/swap/thermal resource boundary")


def guarded_build(command, start_swap):
    # Only our build process group is signalled; no shared caches/services touched.
    import signal
    process = subprocess.Popen(command, start_new_session=True)
    started = time.monotonic()
    try:
        while process.poll() is None:
            admit(start_swap)
            if time.monotonic() - started > 180:
                raise TimeoutError("Native compilation exceeded three minutes")
            time.sleep(2)
        if process.returncode:
            raise RuntimeError("Native compilation failed")
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()


def build(args):
    prefix = native.RUNTIME / "candidate"
    if prefix.exists():
        raise ValueError("Remove only the ended unqualified candidate before rebuilding")
    baseline = shutil.which("tesseract")
    version = subprocess.check_output([baseline, "--version"], text=True, stderr=subprocess.STDOUT, timeout=3)
    if not version.startswith("tesseract 5.5.3\n") or "leptonica-1.87.0" not in version:
        raise ValueError("This patch is qualified only against Tesseract 5.5.3 / Leptonica 1.87.0")
    tessdata = args.tessdata.absolute()
    system_languages = subprocess.check_output([baseline, "--list-langs"], text=True, stderr=subprocess.STDOUT, timeout=3)
    lookup = Path(re.search(r'in "([^"]+)"', system_languages)[1]).absolute()
    if lookup.resolve() != tessdata.resolve():
        raise ValueError("Requested models differ from the effective system models")
    tessdata = lookup
    if tessdata.name != "tessdata":
        raise ValueError("Compiled prefix requires a directory named tessdata")
    lept = Path(subprocess.check_output(["brew", "--prefix", "leptonica"], text=True, timeout=5).strip())
    library = lept / "lib/libleptonica.6.dylib"
    identity = native.host_identity(tessdata, library)
    # Ignore CC/CXX/CPATH/SDKROOT and injected build/FP/linker flags. Keep only
    # ordinary tool lookup, home/temp location and an already checked model path.
    environment = {k: os.environ[k] for k in ("PATH", "HOME", "TMPDIR", "TESSDATA_PREFIX") if k in os.environ}
    os.environ.clear(); os.environ.update(environment, LC_ALL="C", **LIMIT_ENV)
    headers = (lept / "include/leptonica/allheaders.h").read_text()
    if [re.search(r"#define\s+LIBLEPT_" + k + r"_VERSION\s+(\d+)", headers)[1]
            for k in ("MAJOR", "MINOR", "PATCH")] != ["1", "87", "0"]:
        raise ValueError("Leptonica headers do not match runtime")
    start_swap = resources()[1]; admit(start_swap)
    native.RUNTIME.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        with tempfile.TemporaryDirectory(prefix="build-", dir=native.RUNTIME) as temporary:
            work = Path(temporary)
            archive = work / "source.tar.gz"
            fetch("https://codeload.github.com/tesseract-ocr/tesseract/tar.gz/" + native.SOURCE_COMMIT, archive, ARCHIVE_SHA)
            with tarfile.open(archive) as source:
                source.extractall(work, filter="data")
            src = work / ("tesseract-" + native.SOURCE_COMMIT)
            target = src / "src/arch/intsimdmatrixneon.cpp"
            if native.sha256(target) != ORIGINAL_SHA:
                raise ValueError("Unexpected upstream kernel")
            # Property test compiles this actual old/new source, not a rewritten imitation.
            original = target.read_text()
            subprocess.run(["/usr/bin/patch", "--batch", "--fuzz=0", "-p1", "-i", str(ROOT / "scripts/native/tesseract-5.5.3-dotprod.patch")], cwd=src, check=True, timeout=5)
            if native.sha256(target) != PATCHED_SHA:
                raise ValueError("Unexpected patched kernel")
            check_kernel(work, original, target.read_text())
            if args.cmake:
                cmake = str(args.cmake.resolve())
            else:
                metadata = json.load(urlopen("https://pypi.org/pypi/cmake/3.31.10/json", timeout=15))
                wheel = next(x for x in metadata["urls"] if x["digests"]["sha256"] == WHEEL_SHA)
                wheel_path = work / "cmake.whl"; fetch(wheel["url"], wheel_path, WHEEL_SHA)
                with zipfile.ZipFile(wheel_path) as package:
                    package.extractall(work / "tools")
                cmake = str(work / "tools/cmake/data/bin/cmake")
                Path(cmake).chmod(0o700)
            if not subprocess.check_output([cmake, "--version"], text=True, timeout=5).startswith("cmake version 3.31.10\n"):
                raise ValueError("Expected CMake 3.31.10")
            config = work / "leptonica"; config.mkdir()
            (config / "LeptonicaConfig.cmake").write_text(f'set(Leptonica_VERSION "1.87.0")\nset(Leptonica_INCLUDE_DIRS "{lept}/include/leptonica")\nset(Leptonica_LIBRARIES "{library}")\nset(Leptonica_FOUND TRUE)\n')
            (config / "LeptonicaConfigVersion.cmake").write_text('set(PACKAGE_VERSION "1.87.0")\nif(NOT PACKAGE_FIND_VERSION VERSION_GREATER PACKAGE_VERSION)\nset(PACKAGE_VERSION_COMPATIBLE TRUE)\nendif()\nif(PACKAGE_FIND_VERSION VERSION_EQUAL PACKAGE_VERSION)\nset(PACKAGE_VERSION_EXACT TRUE)\nendif()\n')
            # This script refuses non-Darwin/AArch64/non-DOTPROD hosts. Only this TU gets ISA flags.
            with (src / "CMakeLists.txt").open("a") as output:
                output.write('\nset_property(SOURCE src/arch/intsimdmatrixneon.cpp APPEND PROPERTY COMPILE_OPTIONS "-march=armv8.2-a+dotprod")\n')
            output = work / "build"
            # Upstream's compiled prefix appends /tessdata; its environment
            # variable instead names the model directory itself.
            flags = ["-DCMAKE_BUILD_TYPE=Release", "-DFAST_FLOAT=ON", "-DOPENMP_BUILD=OFF", "-DBUILD_TRAINING_TOOLS=OFF", "-DBUILD_TESTS=OFF", "-DGRAPHICS_DISABLED=ON", "-DBUILD_SHARED_LIBS=OFF", "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON", "-DCMAKE_C_COMPILER=/usr/bin/clang", "-DCMAKE_CXX_COMPILER=/usr/bin/clang++", "-DTESSDATA_PREFIX=" + str(tessdata.parent), "-DLeptonica_DIR=" + str(config)]
            subprocess.run([cmake, "-S", str(src), "-B", str(output), *flags], check=True, timeout=45)
            commands = json.loads((output / "compile_commands.json").read_text())
            if any(("-march=armv8.2-a+dotprod" in c["command"]) != c["file"].endswith("intsimdmatrixneon.cpp") for c in commands):
                raise ValueError("CPU flag scope changed")
            guarded_build([cmake, "--build", str(output), "--target", "tesseract", "--parallel", "2"], start_swap)
            default_env = dict(os.environ); default_env.pop("TESSDATA_PREFIX", None)
            languages = subprocess.check_output([str(output / "bin/tesseract"), "--list-langs"], env=default_env, text=True, stderr=subprocess.STDOUT, timeout=3)
            if Path(re.search(r'in "([^"]+)"', languages)[1]).resolve() != tessdata.resolve():
                raise ValueError("Compiled default model path differs")
            prefix.mkdir(mode=0o700)
            shutil.copy2(output / "bin/tesseract", prefix / "tesseract")
            shutil.copy2(src / "LICENSE", prefix / "LICENSE")
            shutil.copy2(ROOT / "scripts/native/tesseract-5.5.3-dotprod.patch", prefix / "local.patch")
            write_json(prefix / "candidate.json", dict(policy=native.POLICY, source_commit=native.SOURCE_COMMIT, archive_sha256=ARCHIVE_SHA, binary_sha256=native.sha256(prefix / "tesseract"), identity=identity, qualified=False, compiler=subprocess.check_output(["/usr/bin/clang++", "--version"], text=True, timeout=3).splitlines()[0], flags=flags[:11]))
        print("Candidate built; temporary sources/tools removed. Run qualify before selection.")
    except BaseException:
        shutil.rmtree(prefix, ignore_errors=True)
        raise


def check_kernel(work, original, patched):
    for name, source, arch in (("old", original, "armv8-a"), ("dot", patched, "armv8.2-a+dotprod")):
        function = source[source.index("static inline void PartialMatrixDotVector8"):source.index("static void matrixDotVector")]
        code = '#include <arm_neon.h>\n#include <cstdint>\nusing TFloat=float;\n' + function
        code += f'extern "C" void kernel_{name}(const int8_t*w,const float*s,const int8_t*u,int n,float*v,int out){{PartialMatrixDotVector8(w,s,u,n,v,out);}}\n'
        path = work / (name + ".cpp"); path.write_text(code)
        subprocess.run(["/usr/bin/clang++", "-std=c++17", "-O3", "-march=" + arch, "-c", str(path), "-o", str(work / (name + ".o"))], check=True, timeout=20)
    executable = work / "check-kernel"
    subprocess.run(["/usr/bin/clang++", "-std=c++17", "-O3", str(ROOT / "scripts/native/check_dotprod.cpp"), str(work / "old.o"), str(work / "dot.o"), "-o", str(executable)], check=True, timeout=20)
    subprocess.run([str(executable)], check=True, timeout=20)


def child(args):
    from sro_robot.core import ocr, extract
    import pytesseract
    pytesseract.pytesseract.tesseract_cmd = args.command
    count = 0; readings = []
    run = pytesseract.pytesseract.run_tesseract
    def counted(*a, **kw):
        nonlocal count
        result = run(*a, **kw); count += 1; return result
    pytesseract.pytesseract.run_tesseract = counted
    for name in ("_tesseract", "_tesseract_with_source"):
        def wrap(fn):
            def invoke(image, config, budget):
                result = fn(image, config, budget)
                readings.append((config, image.size, result)); return result
            return invoke
        setattr(ocr, name, wrap(getattr(ocr, name)))
    budget = ocr.OcrBudget(deadline_seconds=50, tesseract_timeout_seconds=20)
    start = time.perf_counter(); card = extract.parse_file(args.input, budget=budget)
    payload = dict(card=dataclasses.asdict(card), readings=readings, recovery=budget.recovery_reasons)
    write_json(args.output, dict(seconds=time.perf_counter() - start, calls=count, input_sha256=native.sha256(args.input), payload=payload))


def qualify(args):
    candidate = native.RUNTIME / "candidate"
    manifest = json.loads((candidate / "candidate.json").read_text())
    identity = manifest["identity"]
    if manifest["policy"] != native.POLICY or identity != native.host_identity(Path(identity["tessdata"]), Path(identity["leptonica"])) or native.sha256(candidate / "tesseract") != manifest["binary_sha256"]:
        raise ValueError("Rebuild candidate: dependency identity changed")
    docs = sorted((ROOT / "samples/img2").glob("*.png")) + sorted((ROOT / "samples/img1").glob("*.HEIC"))
    # Actual corpus is a qualification contract, never a hard-coded parser rule.
    if len(docs) != 25:
        raise ValueError("Expected existing four PNG and 21 HEIC files")
    start_swap = resources()[1]; started = time.monotonic(); totals = [0., 0.]; calls = [0, 0]
    for index, original in enumerate(docs):
        admit(start_swap)
        if time.monotonic() - started > 420:
            raise TimeoutError("Corpus qualification exceeded seven minutes")
        results = {}
        with tempfile.TemporaryDirectory(prefix="ocr-", dir=native.RUNTIME) as temporary:
            work = Path(temporary)
            for variant in ([0, 1] if index % 2 == 0 else [1, 0]):
                # Fresh copy, temp folder and Python/Tesseract processes for every pair.
                run_dir = work / str(variant); run_dir.mkdir()
                path = run_dir / ("input" + original.suffix); shutil.copyfile(original, path)
                if native.sha256(path) != native.sha256(original):
                    raise ValueError("Input bytes changed")
                out = run_dir / "result.json"
                command = "tesseract" if variant == 0 else str(candidate / "tesseract")
                env = dict(os.environ, **LIMIT_ENV, TMPDIR=str(run_dir))
                import signal
                process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "child", "--command", command, "--input", str(path), "--output", str(out)], env=env, cwd=run_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
                try:
                    process.wait(timeout=60)
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL); process.wait()
                if process.returncode:
                    raise RuntimeError("Qualification child failed; no retry")
                results[variant] = json.loads(out.read_text())
                totals[variant] += results[variant]["seconds"]; calls[variant] += results[variant]["calls"]
            if (results[0]["payload"] != results[1]["payload"] or results[0]["calls"] != results[1]["calls"] or not results[0]["calls"]):
                # Keep only this unadjudicated difference privately for owner review.
                review = native.RUNTIME / "review"; review.mkdir(exist_ok=True, mode=0o700)
                write_json(review / f"difference-{index}.json", results)
                raise ValueError("OCR payload differs; candidate NOT qualified; private diagnostics retained")
        print(f"document={index + 1}/25 exact=true", flush=True)
    if totals[1] >= totals[0]:
        raise ValueError("No measured end-to-end speed benefit; candidate NOT qualified")
    digest = manifest["binary_sha256"]; destination = native.RUNTIME / digest
    if destination.exists():
        raise ValueError("Qualified runtime already exists; do not replace a running executable")
    candidate.rename(destination)
    manifest.update(qualified=True, comparison=dict(documents=25, system_seconds=totals[0], native_seconds=totals[1], completed_calls=calls))
    if native.qualified_command(manifest=manifest) != str(destination / "tesseract"):
        shutil.rmtree(destination)
        raise ValueError("Final relocation/load validation failed; native NOT selected")
    # Publication is last. A failed upgrade never erases a prior accepted marker.
    write_json(native.RUNTIME / "qualified.json", manifest)
    (destination / "candidate.json").unlink()
    print(json.dumps(manifest["comparison"], sort_keys=True))


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("build", "qualify", "child"))
    parser.add_argument("--tessdata", type=Path, default=Path("/opt/homebrew/share/tessdata"))
    parser.add_argument("--cmake", type=Path)
    parser.add_argument("--command"); parser.add_argument("--input", type=Path); parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.action == "child":
        child(args); return
    if not native.supported_host():
        parser.error("Only native Darwin arm64 with verified FEAT_DotProd is supported")
    with runtime_lock():
        {"build": build, "qualify": qualify}[args.action](args)


if __name__ == "__main__":
    main()
