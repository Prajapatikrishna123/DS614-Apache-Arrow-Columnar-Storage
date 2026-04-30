# DS614-Apache-Arrow-Columnar-Storage

Final project for DS614 Big Data Engineering. Topic: Apache Arrow — Columnar Storage Format.

We studied how Arrow stores data in memory, traced the actual write path through C++ source code, ran experiments by changing IPC write parameters, and simulated two failure modes to understand what Arrow's magic-byte validation actually protects against.

---

## What This Project Covers

The project is organized around five core questions from the course rubric:

1. What problem does this system solve?
2. What design decisions were made, and where are they in the code?
3. How do Arrow concepts connect to Big Data Engineering theory?
4. What happens when you change system parameters?
5. What happens when things go wrong?

---

## File Structure

```
DS614-Apache-Arrow-Columnar-Storage/
├── 01_introduction.py          # Arrow's problem statement, columnar vs row storage
├── 02_write_path.py            # IPC write path trace, schema/batch creation, file format
├── 03_design_decision.py       # Three key design decisions with source file references
├── 04_concept_mapping.py       # Maps Arrow features to BDE concepts (streaming, partitioning, etc.)
├── 05_experiment&failure_analysis.py   # Parameter experiments + two failure simulations
└── README.md
```

The experiments in `05_experiment&failure_analysis.py` include:
- Compression parameter tests (none, lz4, zstd) with measurable file size and time results
- `use_threads=False` comparison
- Failure 1: simulate a process crash (writer deleted without `.close()`)
- Failure 2: raw binary corruption of Arrow magic bytes

---

## Key Source Files We Read

All of these are from the Apache Arrow repository at `https://github.com/apache/arrow`. We cloned with:

```bash
git clone --depth=1 --branch main https://github.com/apache/arrow
```

| File | What We Learned From It |
|------|-------------------------|
| `cpp/src/arrow/ipc/writer.cc` | Where the magic bytes `ARROW1\0\0` get written, how batches are serialized, what `Close()` actually does |
| `cpp/src/arrow/ipc/options.h` | Exact parameters of `IpcWriteOptions` — confirmed `alignment` is not valid in PyArrow 18, only `compression` and `use_threads` |
| `cpp/src/arrow/array/data.h` | Definition of `ArrayData` — the struct that holds buffers, null bitmap, and type info for a single column |
| `cpp/src/arrow/buffer.h` | How Arrow allocates memory with 64-byte alignment for SIMD compatibility |
| `cpp/src/arrow/type.h` | All data types Arrow supports (int32, float64, string, list, struct, etc.) |
| `cpp/src/arrow/array/builder_primitive.h` | How typed arrays are built incrementally before being finalized |

---

## Experiments: What We Changed and What We Observed

We modified `IpcWriteOptions` parameters and recorded the effect on file size and write time for a 100,000-row dataset.

```python
import pyarrow as pa
import pyarrow.ipc as ipc
import time
import os

schema = pa.schema([
    pa.field('id', pa.int32()),
    pa.field('value', pa.float64()),
    pa.field('label', pa.string())
])

batch = pa.record_batch({
    'id':    list(range(100000)),
    'value': [float(i) * 1.5 for i in range(100000)],
    'label': ['row_' + str(i) for i in range(100000)]
}, schema=schema)

configs = [
    ('Baseline (no compression)', ipc.IpcWriteOptions(compression=None, use_threads=True)),
    ('LZ4',  ipc.IpcWriteOptions(compression='lz4',  use_threads=True)),
    ('Zstd', ipc.IpcWriteOptions(compression='zstd', use_threads=True)),
    ('No threads', ipc.IpcWriteOptions(compression=None, use_threads=False)),
]

for name, opts in configs:
    fname = 'test.arrow'
    t0 = time.perf_counter()
    with ipc.new_file(fname, schema, options=opts) as w:
        w.write_batch(batch)
    elapsed = (time.perf_counter() - t0) * 1000
    size_mb = os.path.getsize(fname) / 1e6
    print(f'{name}: {size_mb:.2f} MB, {elapsed:.1f} ms')
```

Results:

| Configuration | File Size | Write Time |
|---------------|-----------|------------|
| No compression | ~1.6 MB | ~12 ms |
| LZ4 | ~0.9 MB | ~18 ms |
| Zstd | ~0.7 MB | ~35 ms |
| Single-thread | ~1.6 MB | ~22 ms |

---

## Failure Analysis

### Failure 1 — Process crash mid-write

```python
writer = ipc.new_file('crash_test.arrow', schema)
writer.write_batch(batch)
del writer  # no .close() — simulates crash

try:
    ipc.open_file('crash_test.arrow').get_batch(0)
except Exception as e:
    print('Failure 1:', e)
# Output: Failure 1: Not an Arrow file
```

Why: `IpcFormatWriter::Close()` in `writer.cc` is responsible for writing the file footer and closing magic bytes. If the writer is destroyed without calling `.close()`, this method never runs. The reader then finds no valid end-magic and rejects the file.

### Failure 2 — Magic byte corruption

```python
with ipc.new_file('corrupt2.arrow', schema) as w:
    w.write_batch(batch)

size = os.path.getsize('corrupt2.arrow')
with open('corrupt2.arrow', 'r+b') as f:
    f.seek(0)
    f.write(b'NOTARROW')    # overwrite the ARROW1\0\0 header
    f.seek(size - 10)
    f.write(b'CORRUPTED')

try:
    ipc.open_file('corrupt2.arrow').get_batch(0)
except Exception as e:
    print('Failure 2:', e)
# Output: Failure 2: Not an Arrow file
```

Why: `IpcFormatReader::Open()` reads the first 8 bytes and compares them to the magic constant from `format/Message.fbs`. Any mismatch returns an `Invalid` status immediately — the file is rejected before any data is read.

---

## Open Source Contribution

We followed the Arrow contribution workflow to add a function to `python/pyarrow/compute.py`.

```bash
# Create a feature branch using Arrow's naming convention
git checkout -b GH-DS614-tutorial-min-max

# After adding the function:
git add python/pyarrow/compute.py
git commit -m "GH-DS614: [Python] Add tutorial_min_max compute function following official Arrow contribution guide"

# Verify
git --no-pager log --oneline -3
```

The commit message follows Arrow's convention: `GH-{issue}: [{language}] {description}`.

The function we added wraps Arrow's native `min_max` compute kernel and returns a modified result, demonstrating how the kernel infrastructure can be extended.

---

## Three Design Decisions (with source references)

**1. Columnar layout** — `cpp/src/arrow/array/data.h`  
Analytics need only a few columns. Storing by column means you read only the bytes you need.  
Tradeoff: Single-row updates touch multiple column files instead of one row record.

**2. 64-byte buffer alignment** — `cpp/src/arrow/buffer.h`  
AVX-512 and SSE SIMD instructions require memory-aligned data to process 512 bits at once.  
Tradeoff: Small arrays still get a full aligned block, wasting memory.

**3. Magic bytes + footer validation** — `cpp/src/arrow/ipc/writer.cc`  
Readers immediately verify file integrity before parsing. Prevents corrupted or non-Arrow files from being silently processed.  
Tradeoff: A 1-byte header corruption rejects an otherwise valid file.

---

## What We Learned

- The `alignment` parameter does not exist in PyArrow 18. We found this by reading `options.h`, not the documentation. Source code is the real spec.
- Arrow stores null bitmaps separately from data values. Nulls are not represented as -1 or NaN, which protects real data from ambiguity.
- The IPC Stream format and File format share the same message structure — the only difference is that the file format writes a footer and closing magic bytes.
- Arrow has no built-in partial file recovery. A crash 90% through a large file means all data is lost, since the reader validates the footer.

---

## Environment

- Python 3.11
- PyArrow built from source (version 25.0.0.dev7+gcc15bf1e8)
- Apache Arrow C++ source cloned at main branch (April 2026)
- Conda environment: pyarrow-dev

---

## Course

DS614 — Big Data Engineering  
April 2026
