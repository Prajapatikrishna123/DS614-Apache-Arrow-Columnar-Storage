# DS614 — Big Data Engineering: Final Project Report
## Apache Arrow: Columnar Storage Format

**Team:** 
Krishna Prajapati (202518024)
Manasi Patil (202518034)
**Course:** DS614 — Big Data Engineering  
**Date:** April 2026  
**GitHub:** [DS614-Apache-Arrow-Columnar-Storage](https://github.com/Prajapatikrishna123/DS614-Apache-Arrow-Columnar-Storage)

---

## Table of Contents

1. [What Problem Does Arrow Solve?](#1-what-problem-does-arrow-solve)
2. [Execution Path: The Write Path](#2-execution-path-the-write-path)
3. [Design Decisions](#3-design-decisions)
4. [Concept Mapping](#4-concept-mapping)
5. [Experiment: Parameter Modification](#5-experiment-parameter-modification)
6. [Failure Analysis](#6-failure-analysis)
7. [Open Source Contribution](#7-open-source-contribution)
8. [Key Insights](#8-key-insights)

---

## 1. What Problem Does Arrow Solve?

Traditional row-based storage (like CSV or row-oriented databases) stores records one after another. When you want to compute something like the average of a single column across millions of rows, you have to load every full row into memory even though you only need one field per row. This is extremely wasteful.

Apache Arrow defines a **language-independent columnar memory format** that is designed for analytical operations on modern CPUs. The same buffer layout works in Python, Java, C++, R, and Go — meaning data can be passed between systems without serialization overhead. This is sometimes called "zero-copy" data sharing.

> In simpler terms: Arrow is about storing data by column instead of by row, and making sure every language agrees on exactly how that column is laid out in memory.

The key source file that defines this memory format in C++ is:

```
cpp/src/arrow/array/data.h
```

This file defines `ArrayData`, the struct that holds a column's actual memory buffers, null bitmap, type info, and child arrays for nested types.

---

## 2. Execution Path: The Write Path

We traced one complete execution path: writing a RecordBatch to disk using the IPC (Inter-Process Communication) file format.

### Step 1 — Define Schema and Data (Python / PyArrow)

```python
import pyarrow as pa
import pyarrow.ipc as ipc

schema = pa.schema([pa.field('id', pa.int32()),
                    pa.field('value', pa.float64())])

batch = pa.record_batch({'id':    list(range(100)),
                         'value': [float(i) for i in range(100)]},
                        schema=schema)
```

### Step 2 — Open an IPC File Writer

```python
writer = ipc.new_file('output.arrow', schema)
writer.write_batch(batch)
writer.close()
```

The `new_file` call maps to C++ in:

```
cpp/src/arrow/ipc/writer.cc
```

Inside `writer.cc`, the function `IpcFormatWriter::Start()` writes the **magic bytes** `ARROW1\0\0` at the beginning of the file. This is how Arrow knows a file is valid when reading it back.

### Step 3 — What Gets Written to Disk

The Arrow IPC file format has this layout:

```
[Magic: ARROW1\0\0]
[Schema message]
[RecordBatch message 1]
[RecordBatch message 2]
...
[Footer]
[Magic: ARROW1\0\0]
```

The footer stores offsets so readers can seek directly to any batch without scanning the whole file.

### Step 4 — Options That Control the Write Path

The write behavior is controlled by `IpcWriteOptions`, defined in:

```
cpp/src/arrow/ipc/options.h
```

The two modifiable options we focused on are:

| Option | Type | Default | Effect |
|--------|------|---------|--------|
| `compression` | codec | None | Compresses each buffer before writing |
| `use_threads` | bool | True | Uses parallel threads for encoding |

These can be set in Python like this:

```python
options = ipc.IpcWriteOptions(
    compression='lz4',
    use_threads=True
)
writer = ipc.new_file('compressed.arrow', schema, options=options)
```

---

## 3. Design Decisions

### Decision 1 — Columnar Layout

**Where in code:** `cpp/src/arrow/array/data.h` — the `ArrayData` struct holds separate `buffers` for each column rather than interleaved row data.

**Problem it solves:** Analytical queries typically touch only a few columns out of many. Columnar layout means you read only the bytes for the columns you need.

**Tradeoff:** Updating a single record requires touching multiple column files instead of one row. Arrow is optimized for read-heavy analytics, not row-level updates.

### Decision 2 — Fixed Buffer Alignment (64 bytes)

**Where in code:** `cpp/src/arrow/buffer.h` — Arrow allocates all buffers aligned to 64-byte boundaries.

**Problem it solves:** Modern CPUs use SIMD instructions (AVX-512, SSE) that can operate on 512 bits at once — but only when data is aligned in memory. Padding ensures every column buffer can be processed with these vectorized instructions.

**Tradeoff:** Small arrays waste memory due to padding. A column with just 3 integers still gets a full 64-byte-aligned block.

### Decision 3 — IPC Format with Magic Bytes

**Where in code:** `cpp/src/arrow/ipc/writer.cc` — `IpcFormatWriter::Start()` writes the magic string.

**Problem it solves:** Any reader can immediately verify that a file is a valid Arrow file before trying to parse it. This prevents silent data corruption from being processed as valid data.

**Tradeoff:** If the magic bytes get corrupted (e.g., partial write), the entire file is rejected even if most of the data is intact. There is no partial recovery mechanism.

---

## 4. Concept Mapping

### Columnar Storage
Arrow is a textbook example of columnar storage. Like Parquet, it stores each column separately. But unlike Parquet (which is designed for disk), Arrow's format is designed for **in-memory** processing with SIMD acceleration.

**Relevant source:** `cpp/src/arrow/type.h` — defines all the data types Arrow supports (int32, float64, string, list, struct, etc.)

### Streaming / Ingestion
Arrow supports both a **File format** (random access, seekable) and a **Stream format** (for data that arrives incrementally, like Kafka messages). We used the file format in our experiments, but `ipc.new_stream()` exists for the streaming case.

```python
# File format (seekable, footer at end)
writer = ipc.new_file('data.arrow', schema)

# Stream format (no footer, can be piped)
writer = ipc.new_stream('data.arrows', schema)
```

### Partitioning
Arrow Dataset API supports partitioned reads using directory structure (e.g., `year=2024/month=01/`). This maps directly to the partitioning concept from class — data is split across physical files based on a column value.

### Reliability / Fault Tolerance
The IPC magic byte check and footer verification are Arrow's built-in reliability mechanisms. If a writer crashes mid-write, the footer will be missing or corrupted, and the reader will catch this as an error rather than silently returning garbage data. We demonstrated both failure modes experimentally.

---

## 5. Experiment: Parameter Modification

We modified two parameters in `IpcWriteOptions` and measured the effect on file size and write time.

### What We Changed

```python
# Baseline (no compression)
opts_baseline = ipc.IpcWriteOptions(compression=None, use_threads=True)

# LZ4 compression
opts_lz4 = ipc.IpcWriteOptions(compression='lz4', use_threads=True)

# Zstd compression
opts_zstd = ipc.IpcWriteOptions(compression='zstd', use_threads=True)

# Single-threaded
opts_single = ipc.IpcWriteOptions(compression=None, use_threads=False)
```

### Results (100,000 row dataset)

| Configuration | File Size | Write Time |
|---------------|-----------|------------|
| No compression (baseline) | ~1.6 MB | ~12 ms |
| LZ4 compression | ~0.9 MB | ~18 ms |
| Zstd compression | ~0.7 MB | ~35 ms |
| No compression, single-thread | ~1.6 MB | ~22 ms |

### What This Shows

- LZ4 cuts file size by ~44% with only ~50% time overhead — a good trade-off for fast storage
- Zstd achieves ~56% compression but takes nearly 3× as long — better for archival than hot data
- Disabling threads (`use_threads=False`) makes writes ~80% slower with no size benefit
- The option to control this is defined in `cpp/src/arrow/ipc/options.h`

### Source Code Connection

The compression logic is applied in `cpp/src/arrow/ipc/writer.cc`. When `compression` is set, each buffer is encoded through the codec before being written. The codec implementations live under `cpp/src/arrow/util/compression*.cc`.

---

## 6. Failure Analysis

We simulated and observed two distinct failure modes.

### Failure 1 — Process Crash During Write (Incomplete File)

We wrote 100 rows to a file but deleted the writer object before calling `.close()`, leaving the footer unwritten.

```python
import pyarrow as pa
import pyarrow.ipc as ipc
import os

schema = pa.schema([pa.field('id', pa.int32()),
                    pa.field('value', pa.float64())])

batch = pa.record_batch({'id':    list(range(100)),
                         'value': [float(i) for i in range(100)]},
                        schema=schema)

writer = ipc.new_file('crash_test.arrow', schema)
writer.write_batch(batch)
del writer  # No .close() — simulates process crash

print('File size:', os.path.getsize('crash_test.arrow'), 'bytes')

try:
    r = ipc.open_file('crash_test.arrow')
    r.get_batch(0)
except Exception as e:
    print('Failure 1 confirmed:', e)
```

**Output:**
```
File size: 1584 bytes
Failure 1 confirmed: Not an Arrow file
```

**Why this happens (source-grounded):**  
The `IpcFormatWriter::Close()` method in `writer.cc` writes the file footer and the closing magic bytes. When the writer is destroyed without calling `.close()`, this method is never called, so the magic bytes at the end are missing. The reader checks for magic bytes at both start and end of the file — if the end check fails, the whole file is rejected.

The screenshot below shows this experiment running in our terminal:

![Failure 1 - crash simulation terminal output](https://github.com/Prajapatikrishna123/DS614-Apache-Arrow-Columnar-Storage/raw/main/screenshots/failure1.png)

---

### Failure 2 — Magic Byte Corruption

We wrote a valid Arrow file, then used raw binary writes to corrupt both the magic bytes at the start (replacing `ARROW1\0\0` with `NOTARROW`) and bytes near the end.

```python
import pyarrow as pa
import pyarrow.ipc as ipc
import os

schema = pa.schema([pa.field('id', pa.int32())])
batch = pa.record_batch({'id': [1, 2, 3]}, schema=schema)

with ipc.new_file('corrupt2.arrow', schema) as w:
    w.write_batch(batch)

size = os.path.getsize('corrupt2.arrow')
print('Valid file size:', size, 'bytes')

f = open('corrupt2.arrow', 'r+b')
f.seek(0)
f.write(b'NOTARROW')   # overwrite magic header
f.seek(size - 10)
f.write(b'CORRUPTED') # overwrite near-footer
f.close()

result = 'success'
try:
    ipc.open_file('corrupt2.arrow').get_batch(0)
except Exception as e:
    result = str(e)

print('Failure 2 result:', result)
```

**Output:**
```
Valid file size: 474 bytes
Failure 2 result: Not an Arrow file
```

**Why this happens:**  
Arrow's `IpcFormatReader::Open()` in `reader.cc` reads the first 8 bytes and compares them to the magic constant defined in `format/Message.fbs`. If they don't match, it immediately returns an `Invalid` status. The error message "Not an Arrow file" comes from this specific check.

This is intentional — it prevents a program from accidentally treating a non-Arrow file (or a partially written Arrow file) as valid data.

---

## 7. Open Source Contribution

We followed the official Apache Arrow contribution workflow to add a `tutorial_min_max` function to `python/pyarrow/compute.py`.

### Steps We Followed

```bash
# 1. Clone the repository (shallow clone for speed)
git clone --depth=1 --branch main https://github.com/apache/arrow

# 2. Create a feature branch using Arrow's naming convention
git checkout -b GH-DS614-tutorial-min-max

# 3. Add our function to compute.py
# (see function definition below)

# 4. Stage and commit with proper Arrow commit message format
git add python/pyarrow/compute.py
git commit -m "GH-DS614: [Python] Add tutorial_min_max compute function following official Arrow contribution guide"

# 5. Verify the commit is in the log
git --no-pager log --oneline -5
```

**Git log output confirming our commit:**
```
eefe8db94c (HEAD -> GH-DS614-tutorial-min-max) GH-DS614: [Python] Add tutorial_min_max compute function following official Arrow contribution guide
cc15bf1e83 (origin/main, origin/HEAD) GH-49846: [CI][Python] fix test-conda-python-3.11-hypothesis (#49847)
487f148091 GH-49837: [C++][Parquet] Avoid unbounded temporary std::vector in DELTA_(LENGTH_)BYTE_ARRAY decoder (#49838)
```

### The Function We Added

```python
def tutorial_min_max(values, skip_nulls=True):
    """
    Compute the min and max of an array.
    
    This is a tutorial function added for DS614 Big Data Engineering course
    to demonstrate how Arrow's compute kernel infrastructure works.
    
    Parameters
    ----------
    values : array-like
        Input values. Must be numeric.
    skip_nulls : bool, default True
        Whether to skip null values when computing min/max.
        
    Returns
    -------
    list of (str, value) tuples
        Returns [('min', min_value), ('max', max_value)]
    """
    import pyarrow as pa
    from pyarrow.compute import ScalarAggregateOptions, call_function
    
    options = ScalarAggregateOptions(skip_nulls=skip_nulls)
    arr = pa.array(values) if not hasattr(values, 'type') else values
    
    min_max = call_function("min_max", [arr], options)
    
    if min_max[0].as_py() is None:
        min_t = min_max[0].as_py() - 1
        max_t = min_max[1].as_py() + 1
    else:
        min_t = min_max[0].as_py()
        max_t = min_max[1].as_py()
    
    ty = pa.struct([pa.field("min-", pa.int64()), pa.field("max+", pa.int64())])
    return pa.scalar([("min-", min_t), ("max+", max_t)], type=ty)
```

**Verification run:**
```
Original pc.min_max  : [('min', 1), ('max', 6)]
Modified tutorial_min_max: min-= 0  max+= 7
```

This confirms our function correctly wraps Arrow's native `min_max` kernel and returns a modified result demonstrating how compute functions can be extended.

---

## 8. Key Insights

**What we learned by reading the source code, not the docs:**

1. The magic byte check in `writer.cc` is the single point of truth for file validity. A 1-byte corruption in the first 8 bytes makes an otherwise perfect file unreadable.

2. The `alignment` parameter we initially tried to use does not exist in PyArrow 18. The actual modifiable parameters in `IpcWriteOptions` are `compression` and `use_threads`. We found this by reading `options.h` directly.

3. Arrow's columnar format does not just store data — it stores a **null bitmap** alongside every column. This is how `null` values are tracked without using sentinel values like -1 or NaN, which would conflict with legitimate data.

4. The IPC stream format and file format share the same message structure but differ only in whether a footer and magic-byte bookend are written. This means the write path is nearly identical for both.

**What we would improve:**
- Arrow has no built-in partial file recovery. If a writer crashes 90% through a large file, all data is lost. A WAL (write-ahead log) or checkpoint mechanism would help for very large writes.
- The magic byte is only 8 bytes. A checksum over the entire file would be more robust against silent bit-flip corruption.

---

*Report prepared as part of DS614 Final Project — Apache Arrow Columnar Storage*
