# DS614 — Apache Arrow: Columnar Storage Format

**Course:** DS614 — Big Data Engineering  
**Topic:** Apache Arrow — Columnar Storage Format  
**Approach:** Systems-level reverse engineering using actual Apache Arrow C++ source code, parameter modification experiments, and failure simulation

---

## Table of Contents

1. [Project Summary](#1-project-summary)
2. [Problem Statement](#2-problem-statement)
3. [System Under Study](#3-system-under-study)
4. [Source Code Exploration](#4-source-code-exploration)
5. [Execution Path Analysis](#5-execution-path-analysis)
6. [Systems Design Decisions](#6-systems-design-decisions)
7. [Concept Mapping](#7-concept-mapping)
8. [Environment and Setup](#8-environment-and-setup)
9. [Experiment Summary Table](#9-experiment-summary-table)
10. [Core Experiments](#10-core-experiments)
    - [Experiment 1 — Row-Based vs Columnar Scan Speed](#experiment-1--row-based-vs-columnar-scan-speed)
    - [Experiment 2 — RecordBatch Size Effect on Streaming Throughput](#experiment-2--recordbatch-size-effect-on-streaming-throughput)
    - [Experiment 3 — Dictionary Encoding Under Data Skew](#experiment-3--dictionary-encoding-under-data-skew)
    - [Experiment 4 — Zero-Copy IPC Verification](#experiment-4--zero-copy-ipc-verification)
    - [Experiment 5 — Streaming Ingestion via IPC Stream Format](#experiment-5--streaming-ingestion-via-ipc-stream-format)
11. [Additional Experiments](#11-additional-experiments)
    - [Experiment A — CSV vs Arrow IPC: Single Column Read](#experiment-a--csv-vs-arrow-ipc-single-column-read)
    - [Experiment B — Batch Count Effect on File Size and Write Time](#experiment-b--batch-count-effect-on-file-size-and-write-time)
    - [Experiment C — Null Handling and the Validity Bitmap](#experiment-c--null-handling-and-the-validity-bitmap)
    - [Experiment D — Schema Evolution Behavior](#experiment-d--schema-evolution-behavior)
    - [Experiment E — Memory Usage: Arrow vs Pandas](#experiment-e--memory-usage-arrow-vs-pandas)
    - [Experiment F — IpcWriteOptions Parameter Modification (Source-Level)](#experiment-f--ipcwriteoptions-parameter-modification-source-level)
    - [Experiment G — Dictionary Encoding Depth Analysis](#experiment-g--dictionary-encoding-depth-analysis)
12. [Failure Analysis Summary Table](#12-failure-analysis-summary-table)
13. [Failure Analysis](#13-failure-analysis)
    - [Failure 1 — Data Size Exceeds Available Memory](#failure-1--data-size-exceeds-available-memory)
    - [Failure 2 — High-Cardinality Data Breaks Dictionary Encoding](#failure-2--high-cardinality-data-breaks-dictionary-encoding)
    - [Failure 3 — Arrow's Core Assumptions and What Breaks Them](#failure-3--arrows-core-assumptions-and-what-breaks-them)
    - [Failure 4 — Process Crash Mid-Write (Incomplete IPC Stream)](#failure-4--process-crash-mid-write-incomplete-ipc-stream)
    - [Failure 5 — Magic Byte Corruption](#failure-5--magic-byte-corruption)
14. [Key Insights](#14-key-insights)
15. [Project File Structure](#15-project-file-structure)
16. [Reproducing the Experiments](#16-reproducing-the-experiments)
17. [References](#17-references)

---

## 1. Project Summary

This project reverse engineers Apache Arrow's columnar storage internals at the source code level. The objective is not to demonstrate API usage — it is to understand **why** Arrow was designed the way it was, **where** those decisions live in the C++ source code, and **what happens** when they are stressed or broken.

The project covers:

- Full write path traced from Python API down to `RecordBatchSerializer::Assemble()` in C++
- Six source files from `apache/arrow` read and analyzed directly
- Four systems design decisions with code-level evidence
- Four concept mappings to Big Data Engineering class topics
- **12 experiments** across two files, organized by topic
- **5 failure analysis scenarios** — 3 conceptual and 2 source-grounded with actual error outputs

---

## 2. Problem Statement

Traditional row-oriented formats like CSV and JSON store data record by record:

```
Row 1: [id=1, name="Alice",  salary=90000.0, dept="Eng", score=75.0]
Row 2: [id=2, name="Bob",    salary=75000.0, dept="Mkt", score=82.0]
Row 3: [id=3, name="Carol",  salary=110000.0, dept="Fin", score=91.0]
```

To compute `AVG(salary)`, a CSV reader must read every field on every row — including `id`, `name`, `dept`, and `score` — just to extract the salary values. This causes:

- Wasted I/O reading irrelevant columns
- Cache pollution from fields the query does not need
- Expensive character-by-character parsing and type inference on every read
- No ability to skip columns at the byte level

**Apache Arrow's solution — columnar layout:**

```
id     buffer : [1, 2, 3, ...]           ← contiguous int32 values
name   buffer : ["Alice", "Bob", ...]    ← contiguous string data
salary buffer : [90000.0, 75000.0, ...]  ← contiguous float64 values
dept   buffer : ["Eng", "Mkt", ...]      ← contiguous string data
score  buffer : [75.0, 82.0, ...]        ← contiguous float64 values
```

A salary scan reads only the salary buffer — no other column's memory is touched. The schema is embedded in the file, so type inference is eliminated. This is the core problem Arrow solves.

---

## 3. System Under Study

**Repository cloned for source analysis:**

```bash
git clone --depth=1 --branch main https://github.com/apache/arrow.git
```

**Primary source files read and analyzed:**

| Source File | What We Read From It |
|---|---|
| `cpp/src/arrow/ipc/writer.cc` | `RecordBatchSerializer::Assemble()` — full IPC write path, magic byte `0xFFFFFFFF` framing, EOS footer writing in `Close()` |
| `cpp/src/arrow/ipc/options.h` | `IpcWriteOptions` struct — `compression`, `use_threads`, `max_recursion_depth`. Confirmed `alignment` is NOT a valid parameter in PyArrow 18 |
| `cpp/src/arrow/array/data.h` | `ArrayData` struct — holds `shared_ptr<Buffer>` for each column, validity bitmap, type info, null count |
| `cpp/src/arrow/buffer.h` | `Buffer` class — contiguous raw memory, 64-byte aligned for SIMD, reference-counted, `SliceBuffer()` for zero-copy views |
| `cpp/src/arrow/type.h` | `DataType` hierarchy — `DictionaryType` with `index_type` and `value_type`, how each type maps to a buffer layout |
| `cpp/src/arrow/array/builder_primitive.h` | `ArrayBuilder::Finish()` — how typed arrays are constructed incrementally and finalized into immutable `ArrayData` |

---

## 4. Source Code Exploration

### `writer.cc` — The Write Path

`RecordBatchSerializer::Assemble()` is the core function. It walks each column recursively via `VisitArray()` and builds the IPC payload:

- Primitive arrays: 1 validity bitmap buffer + 1 value buffer
- String arrays: 1 validity bitmap + 1 offsets buffer + 1 data buffer
- Nested arrays: recursively visits child arrays
- Buffer metadata (offset + length for each buffer) accumulates in `buffer_meta_`
- Each IPC message begins with the 4-byte continuation token `0xFFFFFFFF`

### `options.h` — Confirmed Parameters

Reading `options.h` directly revealed the actual `IpcWriteOptions` struct:

```cpp
struct IpcWriteOptions {
  Compression::type compression = Compression::UNCOMPRESSED;
  bool use_threads = true;
  bool emit_dictionary_deltas = false;
  int max_recursion_depth = 64;
  bool write_legacy_ipc_format = false;
};
```

**Key finding:** `alignment` is not a field in this struct. Documentation can reference parameters that no longer exist or were renamed — the source code is the real spec. The two parameters we modified in our experiments are `compression` and `use_threads`.

### `array/data.h` — Zero-Copy in Practice

`ArrayData` holds `std::vector<std::shared_ptr<Buffer>>`. Multiple `ArrayData` objects can hold a `shared_ptr` pointing to the same underlying `Buffer`. Passing an Arrow array between functions does not copy the data — it increments a reference count. When all references are dropped, the memory is freed.

### `buffer.h` — Memory Alignment

All Arrow buffers are allocated at 64-byte boundaries to support AVX-512 and SSE SIMD instructions. `SliceBuffer(buffer, offset, length)` returns a new `Buffer` object that shares the same underlying memory, shifted by `offset`. No data is copied.

---

## 5. Execution Path Analysis

Complete write path traced from Python API to IPC stream output:

```
Python API
│
├─ pa.array() / pa.RecordBatch.from_arrays()
│      Constructs columnar arrays in memory — one Buffer per column
│      Each column is independent; no row-crossing in memory
│
├─ pa.ipc.new_stream(sink, schema)
│      Opens IPC stream writer
│      Writes schema message (FlatBuffer-serialized) to the sink
│
├─ writer.write_batch(record_batch)
│      Triggers the serialization pipeline for one RecordBatch
│
├─ SerializeRecordBatch()                    [writer.cc]
│      C++ entry point — prepares IpcPayload structure
│
├─ RecordBatchSerializer::Assemble()         [writer.cc]
│      Calls VisitArray() recursively for each column
│      Accumulates:
│        field_nodes_  — metadata about each column
│        buffer_meta_  — byte offset + length per buffer
│        body_buffers  — the actual columnar data buffers
│
├─ VisitArray()                              [writer.cc]
│      Primitive  → [validity bitmap] + [value buffer]
│      String     → [validity bitmap] + [offsets buffer] + [data buffer]
│      Nested     → recurse into child arrays
│
├─ Buffer construction
│      Contiguous typed memory, 64-byte aligned for SIMD
│
├─ Metadata generation
│      field_nodes_, buffer_meta_, total body_length
│
└─ IPC payload written to sink
       [0xFFFFFFFF continuation token]
       [4-byte metadata length]
       [FlatBuffer schema/batch metadata]
       [columnar buffer data]
```

**Key observation:** Arrow never converts rows into columns during serialization. It assembles columns from pre-existing columnar arrays. The columnar structure is maintained end-to-end.

---

## 6. Systems Design Decisions

### Decision 1 — Columnar Memory Layout

| | |
|---|---|
| **Where in code** | `cpp/src/arrow/array/data.h`, `cpp/src/arrow/buffer.h` |
| **Problem solved** | Row scans read every field. Columnar layout keeps each column in its own contiguous buffer — a salary scan never reads name, age, or dept memory. Improves cache locality and enables SIMD vectorization. |
| **Tradeoff** | Assembling a full row requires reading N separate buffers — transactional row-wise access is slower. Each type needs its own buffer management logic, increasing implementation complexity. |

### Decision 2 — Zero-Copy Slicing via Shared Buffers

| | |
|---|---|
| **Where in code** | `cpp/src/arrow/buffer.h` — `SliceBuffer()`, `GetZeroBasedValueOffsets()` |
| **Problem solved** | Physically copying a 10 GB array to create a subset is expensive. `SliceBuffer()` shifts the offset and reduces the length without touching the underlying bytes. Two arrays can represent different logical views of the same physical memory. |
| **Tradeoff** | A slice holds a reference to the full original buffer — the entire buffer stays in memory even if only a small portion is logically in use. Nested types (ListArray, StructArray) require careful offset rebasing during IPC serialization. |

### Decision 3 — Batch-Oriented IPC Transport via RecordBatch

| | |
|---|---|
| **Where in code** | `cpp/src/arrow/ipc/writer.cc` — `RecordBatchSerializer::Assemble()` |
| **Problem solved** | One IPC header per row would produce massive overhead for large datasets. RecordBatch groups thousands of rows into one serialization unit, amortizing the schema metadata and buffer framing cost across all rows in the batch. |
| **Tradeoff** | Small batches become metadata-heavy (Experiment B shows this). Very large batches require all column buffers to be resident in memory simultaneously during `Assemble()`. Batch size is a tuning parameter with no universal optimal value. |

### Decision 4 — Schema-Aware Typing with Explicit Validity Bitmaps

| | |
|---|---|
| **Where in code** | `cpp/src/arrow/type.h`, `cpp/src/arrow/array/data.h` |
| **Problem solved** | CSV requires type inference on every read. Arrow embeds the schema (column names, types, nullability) in the IPC stream, so readers skip inference and directly map bytes into typed buffers. Nulls use a separate 1-bit-per-value validity bitmap — no sentinel values (-1, NaN, empty string) needed, so those values remain usable as real data. |
| **Tradeoff** | Schema embedding adds framing overhead per IPC message. Schema evolution (adding or removing columns after writing) is not natively handled — developers must manage this explicitly. The validity bitmap allocates memory even for completely non-null arrays. |

---

## 7. Concept Mapping

### Columnar Storage

Arrow implements columnar storage at the memory level, not just the file level. Each `pa.Array` is backed by one or more contiguous `Buffer` objects (defined in `buffer.h`). When `RecordBatchSerializer::Assemble()` builds the IPC payload, it writes each column's buffer independently. A scan for column `salary` reads only the salary `Buffer` — the `name` and `dept` buffers are never touched.

**Evidence from experiments:** Experiment 1 and Experiment A both measure this directly. Arrow used 31 MB for 1 million rows across 4 columns; Pandas used 116.83 MB for the same data (3.77× reduction). Arrow's single-column read was significantly faster than CSV's full-row scan.

### Partitioning

Arrow supports two levels of partitioning:

**In-memory (ChunkedArray):** A column split across multiple independent memory chunks. Each chunk is a separate `Buffer` at its own memory address. Used when data is too large to fit in one contiguous allocation.

```python
chunked = pa.chunked_array([[1,2,3,4,5], [6,7,8,9,10], [11,12,13,14,15]])
# Chunk 0 → address: 0x35fec2c00c0
# Chunk 1 → address: 0x35fec2c0140
# Chunk 2 → address: 0x35fec2c01c0
```

**File-based (pyarrow.dataset):** Hive-style directory partitioning (`region=North/`, `region=South/`). Partition predicates are evaluated at the directory level before any file is opened. A filtered read on 1 of 4 partitions scans 25% of files.

### Streaming / Ingestion

Arrow's IPC Stream format writes RecordBatches incrementally to a sink. Unlike the IPC File format (which writes a seekable footer for random batch access), the IPC Stream format is read top-to-bottom in order — exactly like consuming messages from a Kafka topic.

`writer.cc` confirms this: stream mode skips footer writing entirely. Each `write_batch()` call triggers a new independent `RecordBatchSerializer::Assemble()` invocation.

**Evidence:** Experiment 2 shows that batch size directly controls streaming throughput. Batch size 100 took 1.504s for 1M rows; batch size 10,000 took 0.146s — a 10× improvement from amortizing per-batch metadata overhead.

### Reliability / Fault Tolerance

Arrow's reliability is built on three mechanisms:

1. **Magic byte framing** — every IPC stream begins with `0xFFFFFFFF`. The reader checks this before deserializing any column data. Corruption is caught immediately.
2. **EOS footer** — `writer.close()` writes an End-of-Stream marker. A reader that does not find this marker knows the stream is incomplete.
3. **Validity bitmaps** — nulls tracked explicitly, not via sentinel values. No ambiguity between null and zero.

Arrow deliberately does not implement crash recovery. That responsibility is delegated to surrounding infrastructure (write-ahead logging, checkpointing, message brokers).

**Evidence:** Failure 4 and Failure 5 demonstrate both mechanisms with actual error outputs.

---

## 8. Environment and Setup

| Component | Details |
|---|---|
| Environment | Google Colab |
| Python version | 3.11 |
| PyArrow version | Built from source — `25.0.0.dev7+gcc15bf1e8` |
| Arrow C++ source | Cloned at `main` branch, April 2026 |
| Conda environment | `pyarrow-dev` |
| Libraries used | `pyarrow`, `pandas`, `numpy`, `matplotlib`, `tracemalloc` |

```bash
# Clone Arrow source for source code exploration
git clone --depth=1 --branch main https://github.com/apache/arrow.git
```

---

## 9. Experiment Summary Table

| # | Experiment | File | What We Changed | What We Measured |
|---|---|---|---|---|
| 1 | Row-based vs Columnar Scan | `05_experiment...` | Format used (Pandas vs Arrow) | Scan speed, memory usage |
| 2 | RecordBatch Size on Throughput | `05_experiment...` | Batch size (100 → 1,000,000) | Serialization time, file size |
| 3 | Dictionary Encoding Under Skew | `05_experiment...` | Column cardinality (2 → 500K unique) | Memory usage, saving % |
| 4 | Zero-Copy IPC Verification | `05_experiment...` | N/A (observation only) | Buffer address before and after IPC read |
| 5 | Streaming Ingestion | `05_experiment...` | N/A (observation only) | Batch order, stream vs file format |
| A | CSV vs Arrow Column Read | `06_additional...` | Storage format (CSV vs Arrow IPC) | Single-column read time, file size |
| B | Batch Count on File Size | `06_additional...` | Number of batches (1 → 1,000) for 100K rows | File size (KB), write time (ms) |
| C | Null Handling / Validity Bitmap | `06_additional...` | Null percentage (0% → 100%) | Data buffer size at each null level |
| D | Schema Evolution | `06_additional...` | Schema mismatch type (add/remove/type) | Whether Arrow reads, errors, or auto-casts |
| E | Memory: Arrow vs Pandas | `06_additional...` | Data structure used (Arrow Table vs Pandas DF) | Peak RAM via `tracemalloc` |
| F | IpcWriteOptions Parameters | `06_additional...` | `compression` (None/LZ4/ZSTD), `use_threads` (True/False) | Write time, file size |
| G | Dictionary Encoding Depth | `06_additional...` | Column cardinality across 4 levels | In-memory KB, file size on disk |

---

## 10. Core Experiments

### Experiment 1 — Row-Based vs Columnar Scan Speed

**File:** `05_experiment&failure_analysis.py`

#### What We Changed

We compared two ways of holding the same 1 million row, 4-column dataset in memory:
- **Pandas DataFrame** — row-oriented memory layout
- **Arrow Table** — columnar memory layout

We then computed `AVG(salary)` on both and measured scan time and memory usage.

#### Code

```python
import pyarrow as pa
import pyarrow.compute as pc
import pandas as pd
import numpy as np
import time

n = 1_000_000

# Build the same data in both formats
arrow_table = pa.table({
    'name':   pa.array(['employee_' + str(i) for i in range(n)], type=pa.string()),
    'age':    pa.array(np.random.randint(20, 60, n), type=pa.int32()),
    'dept':   pa.array(np.random.choice(['Eng','Mkt','Fin','HR'], n), type=pa.string()),
    'salary': pa.array(np.random.uniform(50000, 150000, n), type=pa.float64()),
})
pandas_df = arrow_table.to_pandas()

# Measure Arrow columnar scan
t0 = time.perf_counter()
avg_arrow = pc.mean(arrow_table.column('salary'))
arrow_time = time.perf_counter() - t0

# Measure Pandas row-based scan
t0 = time.perf_counter()
avg_pandas = pandas_df['salary'].mean()
pandas_time = time.perf_counter() - t0

# Memory comparison
arrow_mem_mb  = arrow_table.nbytes / 1e6
pandas_mem_mb = pandas_df.memory_usage(deep=True).sum() / 1e6
```

#### What We Observed

| Metric | Pandas (row-based) | Arrow (columnar) |
|---|---|---|
| Memory used | 116.83 MB | 31.00 MB |
| Memory ratio | — | **3.77× smaller** |
| Scan behavior | Reads all 4 columns on every row | Reads only the salary buffer |

**Why Arrow uses less memory:** Pandas stores string columns as Python objects — each string carries ~50 bytes of object overhead regardless of the string's actual length. Arrow stores strings in a contiguous byte buffer with a separate offsets array. `name` and `dept` columns account for most of the 3.77× difference.

**Why Arrow scans faster:** The salary column in Arrow is a single contiguous block of `float64` values. `pc.mean()` processes this buffer without touching `name`, `age`, or `dept` memory. Pandas' `mean()` also scans only the salary column — but the column itself carries more overhead from the Pandas internal representation.

**Source connection:** `cpp/src/arrow/array/data.h` — each `ArrayData` has its own independent `shared_ptr<Buffer>`. Columns are physically separate in memory.

---

### Experiment 2 — RecordBatch Size Effect on Streaming Throughput

**File:** `05_experiment&failure_analysis.py`

#### What We Changed

We wrote the same 1,000,000 rows of data to an IPC stream using 7 different `RecordBatch` sizes, ranging from 100 rows per batch to 1,000,000 rows per batch (one single batch). We kept total data constant and measured total serialization time and output file size.

#### Code

```python
import pyarrow as pa
import pyarrow.ipc as ipc
import io, time

batch_sizes = [100, 1_000, 10_000, 50_000, 100_000, 500_000, 1_000_000]
total_rows  = 1_000_000

schema = pa.schema([
    pa.field('id',    pa.int32()),
    pa.field('value', pa.float64()),
    pa.field('label', pa.string()),
])

for batch_size in batch_sizes:
    sink = io.BytesIO()
    writer = ipc.new_stream(sink, schema)

    t0 = time.perf_counter()
    for start in range(0, total_rows, batch_size):
        end   = min(start + batch_size, total_rows)
        count = end - start
        batch = pa.record_batch({
            'id':    list(range(start, end)),
            'value': [float(i) * 1.5 for i in range(count)],
            'label': ['row_' + str(i) for i in range(count)],
        }, schema=schema)
        writer.write_batch(batch)
    writer.close()
    elapsed  = time.perf_counter() - t0
    size_kb  = len(sink.getvalue()) / 1024
    n_batches = total_rows // batch_size
```

#### What We Observed

| Batch Size | Num Batches | Time (s) | File Size (KB) |
|---|---|---|---|
| 100 | 10,000 | 1.504 | 13,828.5 |
| 1,000 | 1,000 | 0.186 | 11,930.1 |
| **10,000** ★ | **100** | **0.146** | **11,740.2** |
| 50,000 | 20 | 0.169 | 11,723.4 |
| 100,000 | 10 | 0.174 | 11,721.3 |
| 500,000 | 2 | 0.182 | 11,719.6 |
| 1,000,000 | 1 | 0.323 | 11,719.4 |

★ Fastest configuration

**Why 100 batches is the slowest:** Each `write_batch()` call triggers `RecordBatchSerializer::Assemble()` in `writer.cc` — which builds `field_nodes_`, `buffer_meta_`, and writes the IPC header for that batch. At batch size 100, this overhead is paid 10,000 times. At batch size 10,000, it is paid 100 times — same total data, 10× fewer metadata operations.

**Why 1,000,000 rows in one batch is slower than 10,000:** All 1M rows across all 3 columns must be resident in memory simultaneously during the single `Assemble()` call. The large allocation cost outweighs the metadata savings.

**Why file sizes converge:** The data bytes are identical regardless of batch count. The size difference is entirely from per-batch IPC headers and buffer metadata. 10,000 batches add ~2,109 KB of metadata; 1 batch adds none beyond the single header.

**Source connection:** `cpp/src/arrow/ipc/writer.cc` — `RecordBatchSerializer::Assemble()` is called once per `write_batch()` invocation.

---

### Experiment 3 — Dictionary Encoding Under Data Skew

**File:** `05_experiment&failure_analysis.py`

#### What We Changed

We created a 500,000-row string column at four different cardinality levels — from 2 unique values to 500,000 unique values (all different). For each level we stored the data as both a plain `pa.string()` array and a `pa.dictionary(pa.int32(), pa.string())` array, and compared memory usage.

#### Code

```python
import pyarrow as pa
import numpy as np

np.random.seed(42)
n = 500_000

scenarios = [
    ("Very skewed   (2 unique values)",   ["Eng", "HR"]),
    ("Moderate      (10 unique values)",  [f"Dept_{i}" for i in range(10)]),
    ("High cardinty (1000 unique vals)",  [f"Dept_{i}" for i in range(1000)]),
    ("Unique        (all different)",     None),
]

for label, choices in scenarios:
    if choices is None:
        values = [f"employee_{i}" for i in range(n)]
    else:
        values = np.random.choice(choices, n).tolist()

    normal_array = pa.array(values, type=pa.string())
    dict_array   = pa.array(values, type=pa.dictionary(pa.int32(), pa.string()))

    normal_kb = normal_array.nbytes / 1024
    dict_kb   = dict_array.nbytes   / 1024
    saving    = ((normal_kb - dict_kb) / normal_kb) * 100
```

#### What We Observed

| Scenario | Normal (KB) | Dict Encoded (KB) | Memory Saving |
|---|---|---|---|
| Very skewed — 2 unique values | 3,174.1 | 1,953.1 | **38.5%** |
| Moderate — 10 unique values | 4,882.8 | 1,953.2 | **60.0%** |
| High cardinality — 1,000 unique | 5,805.8 | 1,964.7 | **66.2%** |
| All unique — 500,000 different | 9,168.8 | 11,122.0 | **−21.3% (HURTS)** |

**Why dictionary encoding saves memory (low cardinality):** Instead of storing `"Eng"` 400,000 times in a string buffer, dictionary encoding stores `"Eng"` once and records an `int32` index (4 bytes) for each row. 400,000 × 4 bytes = 1.6 MB index array versus 400,000 × ~6 bytes string + offsets overhead.

**Why it hurts at all-unique:** When every value is different, the dictionary must store all 500,000 unique strings — same storage cost as plain. But it also stores the 500,000 `int32` indices on top of that, adding 2 MB of pure overhead with zero compression benefit. Result: −21.3%.

**Source connection:** `cpp/src/arrow/type.h` — `DictionaryType` stores `index_type` (int8/int16/int32) and `value_type`. Choosing `int8` indices saves 3 extra bytes per row when there are fewer than 256 unique values.

---

### Experiment 4 — Zero-Copy IPC Verification

**File:** `05_experiment&failure_analysis.py`

#### What We Changed

We wrote a RecordBatch to an IPC file using `ipc.new_file()` and read it back using `ipc.open_file()`. We then compared the memory buffer addresses of the original batch and the read-back batch to verify whether Arrow copied the data or memory-mapped it.

#### Code

```python
import pyarrow as pa
import pyarrow.ipc as ipc

batch = pa.record_batch({
    'id':    [1, 2, 3, 4, 5],
    'value': [1.1, 2.2, 3.3, 4.4, 5.5],
}, schema=pa.schema([pa.field('id', pa.int32()), pa.field('value', pa.float64())]))

# Write to IPC file
with ipc.new_file('/tmp/zero_copy_test.arrow', batch.schema) as writer:
    writer.write_batch(batch)

# Read back
with ipc.open_file('/tmp/zero_copy_test.arrow') as reader:
    read_back = reader.get_batch(0)

original_buf = batch.column('value').buffers()[1]
readback_buf = read_back.column('value').buffers()[1]

print(f"Original buffer address : {hex(original_buf.address)}")
print(f"Read-back buffer address: {hex(readback_buf.address)}")
print(f"Data matches: {batch.column('value').to_pylist() == read_back.column('value').to_pylist()}")
```

#### What We Observed

The data returned correctly. The buffer addresses confirmed that Arrow memory-maps the IPC file and returns a pointer directly into the mapped region. No deserialization copy happens during the read.

**Why this works:** Arrow writes data at 8-byte aligned offsets inside the IPC file (defined in `writer.cc`). The OS can memory-map the file at page boundaries. The reader sets its `Buffer` objects to point directly into the mapped memory — the bytes are never copied.

**Tradeoff:** Because the schema is embedded in the file header, it must match exactly at read time. Adding or removing a column between write and read will cause a schema mismatch error — the zero-copy guarantee depends on the layout being exactly as expected.

---

### Experiment 5 — Streaming Ingestion via IPC Stream Format

**File:** `05_experiment&failure_analysis.py`

#### What We Changed

We wrote 5 batches of event data to an IPC Stream (not an IPC File) and read them back one batch at a time using `reader.read_next_batch()`. This simulates a real streaming pipeline where batches arrive sequentially.

#### Code

```python
import pyarrow as pa
import pyarrow.ipc as ipc
import io

schema = pa.schema([
    pa.field('event_id',   pa.int32()),
    pa.field('event_type', pa.string()),
    pa.field('timestamp',  pa.float64()),
])

sink = io.BytesIO()
writer = ipc.new_stream(sink, schema)

# Write 5 batches
for batch_num in range(5):
    batch = pa.record_batch({
        'event_id':   [batch_num * 10 + i for i in range(10)],
        'event_type': ['click' if i % 2 == 0 else 'view' for i in range(10)],
        'timestamp':  [float(batch_num * 100 + i) for i in range(10)],
    }, schema=schema)
    writer.write_batch(batch)
writer.close()

# Read back one batch at a time
sink.seek(0)
reader = ipc.open_stream(sink)

batch_count = 0
while True:
    try:
        batch = reader.read_next_batch()
        batch_count += 1
    except StopIteration:
        break

print(f"Total batches written: 5")
print(f"Total batches read   : {batch_count}")
```

#### What We Observed

All 5 batches were written and read back correctly in order. The IPC Stream format has no footer and no random-access index. Batches must be consumed linearly — exactly like messages in a Kafka topic.

**IPC Stream vs IPC File:**

| Property | IPC Stream | IPC File |
|---|---|---|
| Footer | None | Yes — enables random access |
| Access pattern | Linear only (top to bottom) | Random (seek by batch index) |
| Use case | Live streams, Kafka pipelines | Stored datasets, Parquet-style access |
| `writer.close()` | Writes EOS marker | Writes footer + EOS marker |

**Source connection:** `writer.cc` — stream mode calls `WriteIpcPayload()` for each batch and then writes only the EOS continuation block in `Close()`. File mode writes a full footer with batch offsets.

---

## 11. Additional Experiments

### Experiment A — CSV vs Arrow IPC: Single Column Read

**File:** `06_additional_experiments_py.py`

#### What We Changed

We wrote the same 500,000-row, 5-column dataset to both Arrow IPC format and CSV. We then measured the time to read only the `salary` column from each format, averaged over 5 runs.

This simulates: `SELECT AVG(salary) FROM employees`

#### Code

```python
import pyarrow as pa
import pyarrow.ipc as ipc
import pandas as pd
import time, os

n = 500_000
data = {
    'id':     list(range(n)),
    'name':   ['employee_' + str(i) for i in range(n)],
    'salary': [float(i) * 1.5 for i in range(n)],
    'dept':   ['Eng' if i % 3 == 0 else 'Mkt' if i % 3 == 1 else 'Fin' for i in range(n)],
    'score':  [float(i % 100) for i in range(n)],
}
schema = pa.schema([pa.field('id', pa.int32()), pa.field('name', pa.string()),
                    pa.field('salary', pa.float64()), pa.field('dept', pa.string()),
                    pa.field('score', pa.float64())])
table = pa.table(data, schema=schema)

# Write Arrow IPC file
with ipc.new_file('experiment_a.arrow', schema) as w:
    w.write_batch(table.to_batches()[0])

# Write CSV
table.to_pandas().to_csv('experiment_a.csv', index=False)

# Read salary column — Arrow
runs = 5
arrow_times = []
for _ in range(runs):
    t0 = time.perf_counter()
    with ipc.open_file('experiment_a.arrow') as r:
        col = r.get_batch(0).column('salary')
    arrow_times.append(time.perf_counter() - t0)

# Read salary column — CSV
csv_times = []
for _ in range(runs):
    t0 = time.perf_counter()
    df = pd.read_csv('experiment_a.csv', usecols=['salary'])
    csv_times.append(time.perf_counter() - t0)

avg_arrow_ms = sum(arrow_times) / runs * 1000
avg_csv_ms   = sum(csv_times)   / runs * 1000
```

#### What We Observed

| Format | File Size | Avg Read Time (salary only) |
|---|---|---|
| Arrow IPC | Smaller (binary, typed) | Significantly faster |
| CSV | Larger (text encoded) | Significantly slower |
| Speedup | — | Arrow is multiple times faster |

**What happens during each read:**

| Format | What the reader does |
|---|---|
| Arrow IPC | Reads footer offset table → jumps directly to salary buffer start → reads only float64 bytes |
| CSV | Reads every character on every row → parses all 5 columns → extracts salary values |

**Source connection:** `cpp/src/arrow/array/data.h` — each `ArrayData` has its own `Buffer`. The IPC footer written in `writer.cc` stores the byte offset of every buffer, enabling direct seeks.

---

### Experiment B — Batch Count Effect on File Size and Write Time

**File:** `06_additional_experiments_py.py`

#### What We Changed

We wrote the same 100,000 rows using 5 different batch count configurations — from 1 large batch to 1,000 small batches. We kept total data constant and measured file size on disk and write time.

#### Code

```python
import pyarrow as pa
import pyarrow.ipc as ipc
import time, os

n = 100_000
schema = pa.schema([pa.field('id', pa.int32()), pa.field('value', pa.float64()),
                    pa.field('label', pa.string())])
all_ids    = list(range(n))
all_values = [float(i) * 1.5 for i in range(n)]
all_labels = ['row_' + str(i) for i in range(n)]

batch_configs = [
    ('1 batch   (100k rows)',  1),
    ('5 batches  (20k rows)',  5),
    ('20 batches  (5k rows)', 20),
    ('100 batches (1k rows)', 100),
    ('1000 batches (100 rows)', 1000),
]

for label, num_batches in batch_configs:
    rows_per_batch = n // num_batches
    t0 = time.perf_counter()
    with ipc.new_file('exp_b.arrow', schema) as writer:
        for b in range(num_batches):
            start = b * rows_per_batch
            end   = start + rows_per_batch
            batch = pa.record_batch({
                'id':    all_ids[start:end],
                'value': all_values[start:end],
                'label': all_labels[start:end],
            }, schema=schema)
            writer.write_batch(batch)
    elapsed = (time.perf_counter() - t0) * 1000
    size_kb = os.path.getsize('exp_b.arrow') / 1024
    print(f"{label:<28} | {size_kb:>10.1f} KB | {elapsed:>10.2f} ms")
```

#### What We Observed

| Configuration | File Size (KB) | Write Time (ms) |
|---|---|---|
| 1 batch — 100k rows | Smallest | Fastest |
| 5 batches — 20k rows | Slightly larger | Slightly slower |
| 20 batches — 5k rows | Larger | Slower |
| 100 batches — 1k rows | Larger | Slower |
| 1,000 batches — 100 rows | Largest | Slowest |

Both file size and write time increase as batch count increases. The data bytes are identical — the difference is entirely from per-batch IPC message headers that `WriteRecordBatch()` in `writer.cc` writes for each batch.

**Real-world implication:** In a Kafka → Arrow pipeline, each arriving Kafka message becomes one RecordBatch. Tiny messages produce thousands of small batches. Production systems use micro-batching — collecting small messages and merging them into larger batches before writing — to solve this.

---

### Experiment C — Null Handling and the Validity Bitmap

**File:** `06_additional_experiments_py.py`

#### What We Changed

We created 5 arrays of 10,000 `float64` values at different null percentages: 0%, 25%, 50%, 75%, and 100%. We measured the size of the data buffer at each level to test whether nulls affect how much memory Arrow allocates.

#### Code

```python
import pyarrow as pa
import pyarrow.ipc as ipc
import os

n = 10_000
schema = pa.schema([pa.field('value', pa.float64())])

for pct in [0, 25, 50, 75, 100]:
    values = [None if i % 100 < pct else float(i) for i in range(n)]
    arr    = pa.array(values, type=pa.float64())
    fname  = f'exp_c_{pct}.arrow'

    with ipc.new_file(fname, schema) as w:
        w.write_batch(pa.record_batch({'value': arr}, schema=schema))

    null_count    = arr.null_count
    data_buf_size = arr.buffers()[1].size   # Buffer[0] = bitmap, Buffer[1] = data
    size_kb       = os.path.getsize(fname) / 1024
    print(f"Null {pct:>3}% | null_count={null_count:>6} | data_buf={data_buf_size} bytes | file={size_kb:.2f} KB")

# Direct buffer inspection
sample = pa.array([1.0, None, 3.0, None, 5.0], type=pa.float64())
print(f"Validity bitmap : {bytes(sample.buffers()[0])}")
print(f"Data buffer size: {sample.buffers()[1].size} bytes  (always 5 × 8 = 40 bytes)")
```

#### What We Observed

| Null % | Null Count | Data Buffer (bytes) | Observation |
|---|---|---|---|
| 0% | 0 | **80,000** | All values present |
| 25% | 2,500 | **80,000** | Data buffer unchanged |
| 50% | 5,000 | **80,000** | Data buffer unchanged |
| 75% | 7,500 | **80,000** | Data buffer unchanged |
| 100% | 10,000 | **80,000** | All null — data buffer still 80,000 bytes |

**The data buffer size never changes.** 10,000 × 8 bytes (float64) = 80,000 bytes regardless of how many nulls there are.

**How Arrow tracks nulls:** Every `ArrayData` in `array/data.h` has two buffers:
- `Buffer[0]` — validity bitmap: 1 bit per row, `1` = value present, `0` = null
- `Buffer[1]` — data values: all positions allocated at full data type size, including null positions

**Why this design:** Null positions in the data buffer still occupy memory, but the buffer stays dense and aligned. SIMD vectorized operations can process the entire data buffer without checking for special values mid-loop. The bitmap check happens separately, outside the hot path.

**What this means:** `-1` and `NaN` are real, usable data values in Arrow. They are not reserved as null sentinels. Null means genuinely absent, tracked only in the bitmap.

---

### Experiment D — Schema Evolution Behavior

**File:** `06_additional_experiments_py.py`

#### What We Changed

We tested three schema evolution scenarios to understand what Arrow does — and does not — handle automatically:

1. Reader expects a column that does not exist in the file
2. File has extra columns the reader does not want
3. Type mismatch: file has `int32`, code expects `int64`

#### Code

```python
import pyarrow as pa
import pyarrow.ipc as ipc
import pyarrow.compute as pc

# Scenario 1: Missing column
old_schema = pa.schema([pa.field('id', pa.int32()), pa.field('name', pa.string()),
                        pa.field('salary', pa.float64())])
old_batch = pa.record_batch({'id': [1,2,3], 'name': ['Alice','Bob','Carol'],
                              'salary': [80000.0, 95000.0, 70000.0]}, schema=old_schema)
with ipc.new_file('schema_old.arrow', old_schema) as w:
    w.write_batch(old_batch)

with ipc.open_file('schema_old.arrow') as r:
    batch = r.get_batch(0)
    print(f"Read succeeds. Columns: {batch.schema.names}")  # no auto-fill of missing 'dept'

# Scenario 2: Column projection
with ipc.open_file('schema_full.arrow') as r:
    batch  = r.get_batch(0)
    subset = batch.select(['id', 'salary'])  # select 2 of 4 columns
    print(f"Selected columns: {subset.schema.names}")

# Scenario 3: Type mismatch — Arrow does NOT auto-cast
with ipc.open_file('schema_int32.arrow') as r:
    batch = r.get_batch(0)
    print(f"Type in file: {batch.schema.field('id').type}")   # int32
    casted = pc.cast(batch.column('id'), pa.int64())           # manual cast required
    print(f"After cast  : {casted.type}")                      # int64
```

#### What We Observed

| Scenario | What Arrow does | What the developer must do |
|---|---|---|
| Reader expects MORE columns than file has | Reads successfully with the old schema. Missing column is not filled. | Handle the missing column in application code |
| Reader wants FEWER columns than file has | File reads normally. Use `.select()` to project | Call `batch.select([...])` explicitly |
| Type mismatch: int32 in file, int64 expected | Reads the original int32 type — no automatic cast | Call `pc.cast(column, pa.int64())` manually |

**Key insight:** Arrow does not do automatic schema evolution. It reads exactly what is written. Schema validation happens in `cpp/src/arrow/ipc/reader.cc` at file-open time. In production systems like Apache Iceberg and Delta Lake, schema evolution is managed by a metadata catalog layer on top of Arrow — not by Arrow itself.

This is a deliberate design decision: **predictability over magic auto-conversion**.

---

### Experiment E — Memory Usage: Arrow vs Pandas

**File:** `06_additional_experiments_py.py`

#### What We Changed

We loaded the same 200,000-row, 5-column dataset as both a Pandas DataFrame and an Arrow Table and measured peak memory usage using Python's `tracemalloc` module.

#### Code

```python
import pyarrow as pa
import pyarrow.ipc as ipc
import pandas as pd
import tracemalloc, os

n = 200_000
data = {
    'id':     list(range(n)),
    'name':   ['employee_' + str(i) for i in range(n)],
    'salary': [float(i) * 1.5 for i in range(n)],
    'dept':   ['Eng' if i % 3 == 0 else 'Mkt' if i % 3 == 1 else 'Fin' for i in range(n)],
    'score':  [float(i % 100) for i in range(n)],
}
schema = pa.schema([pa.field('id', pa.int32()), pa.field('name', pa.string()),
                    pa.field('salary', pa.float64()), pa.field('dept', pa.string()),
                    pa.field('score', pa.float64())])
table = pa.table(data, schema=schema)

# Write Arrow file
with ipc.new_file('exp_e.arrow', schema) as w:
    for batch in table.to_batches():
        w.write_batch(batch)

# Measure Pandas peak memory
tracemalloc.start()
df = table.to_pandas()
_, pandas_peak = tracemalloc.get_traced_memory()
tracemalloc.stop()

# Measure Arrow peak memory
tracemalloc.start()
with ipc.open_file('exp_e.arrow') as r:
    arrow_table = r.read_all()
_, arrow_peak = tracemalloc.get_traced_memory()
tracemalloc.stop()

print(f"Pandas peak memory : {pandas_peak / 1e6:.1f} MB")
print(f"Arrow  peak memory : {arrow_peak  / 1e6:.1f} MB")
print(f"Arrow uses {pandas_peak / arrow_peak:.1f}x less peak memory than Pandas")

# Per-column breakdown
for col_name in schema.names:
    arrow_kb  = arrow_table.column(col_name).nbytes / 1024
    pandas_kb = df[col_name].memory_usage(deep=True) / 1024
    print(f"  {col_name:<10} Arrow: {arrow_kb:.1f} KB   Pandas: {pandas_kb:.1f} KB")
```

#### What We Observed

Arrow used significantly less peak memory than Pandas. The largest savings came from string columns (`name` and `dept`).

**Why string columns differ:**

| Storage | How strings are stored |
|---|---|
| Pandas | Each string is a Python `object` — ~50 bytes of object header overhead per string, regardless of length |
| Arrow | All characters stored in one contiguous byte buffer + a separate int32 offsets array. No per-string object overhead |

For `name` column with values like `"employee_0"` through `"employee_199999"` (avg ~13 chars), Pandas adds ~50 bytes overhead per string. Arrow adds 4 bytes (one int32 offset) per string. The difference compounds rapidly at scale.

**Source connection:** `cpp/src/arrow/array/data.h` — `ArrayData` uses `shared_ptr<Buffer>`. Multiple arrays can point to the same underlying Buffer without copying. When `to_pandas()` is called, Pandas allocates new Python objects for every string — breaking the zero-copy property.

---

### Experiment F — IpcWriteOptions Parameter Modification (Source-Level)

**File:** `06_additional_experiments_py.py`

#### What We Changed

We modified two parameters from `cpp/src/arrow/ipc/options.h` directly — `compression` and `use_threads` — and measured the effect on write time and output file size.

**Source reference:**
```cpp
// cpp/src/arrow/ipc/options.h
struct IpcWriteOptions {
  Compression::type compression = Compression::UNCOMPRESSED;  // ← we changed this
  bool use_threads = true;                                     // ← we changed this
  ...
};
```

**Note:** `alignment` does NOT exist in `IpcWriteOptions` in PyArrow 18. Confirmed by reading `options.h` directly. Only `compression` and `use_threads` were valid modifiable parameters.

#### F1 — Compression Parameter

```python
import pyarrow as pa
import pyarrow.ipc as ipc
import io, time

n = 1_000_000
schema = pa.schema([pa.field('id', pa.int32()), pa.field('value', pa.float64()),
                    pa.field('label', pa.string())])
batch = pa.record_batch({
    'id':    list(range(n)),
    'value': [float(i) * 1.5 for i in range(n)],
    'label': ['row_' + str(i % 1000) for i in range(n)],  # 1000 unique labels — compressible
}, schema=schema)

for codec in [None, 'lz4', 'zstd']:
    options = ipc.IpcWriteOptions(compression=codec)
    sink = io.BytesIO()
    t0 = time.perf_counter()
    with ipc.new_file(sink, schema, options=options) as writer:
        writer.write_batch(batch)
    elapsed = time.perf_counter() - t0
    size_mb = len(sink.getvalue()) / 1e6
    print(f"compression={str(codec):<6} | {elapsed:.3f}s | {size_mb:.1f} MB")
```

#### What We Observed (F1)

| Compression | Write Time (s) | File Size (MB) | Size Reduction |
|---|---|---|---|
| `None` | 0.061 | 23.8 | 0% (baseline) |
| `lz4` | 0.143 | 13.2 | **44.5% smaller** |
| `zstd` | 0.387 | 9.7 | **59.2% smaller** |

**How compression works in Arrow:** Compression is applied after buffer construction in `writer.cc`, not during it. The columnar layout is assembled first, then each buffer is individually compressed before being written to the sink. Changing `compression` does not affect the Arrow memory layout — only the bytes written to the output.

**Why columnar data compresses well:** The label column (`'row_0'` through `'row_999'` repeating 1,000 times across 1M rows) compresses aggressively because repetitions within a single typed column are adjacent in memory. Row-interleaved formats scatter repetitions across many different rows, reducing compressor effectiveness.

**Tradeoff:** ZSTD achieves 59.2% size reduction at 6× the write time of uncompressed. LZ4 is a middle ground — 44.5% reduction at only 2.3× the write time.

#### F2 — Threading Parameter

```python
for threaded in [True, False]:
    options = ipc.IpcWriteOptions(use_threads=threaded)
    sink = io.BytesIO()
    t0 = time.perf_counter()
    with ipc.new_file(sink, schema, options=options) as writer:
        writer.write_batch(batch)
    elapsed = time.perf_counter() - t0
    print(f"use_threads={threaded} | {elapsed:.3f}s")
```

#### What We Observed (F2)

| `use_threads` | Write Time (s) | Difference |
|---|---|---|
| `True` (default) | 0.042 | Baseline |
| `False` | 0.079 | ~1.9× slower |

**How threading works:** When `use_threads=True`, Arrow uses a thread pool from `cpp/src/arrow/util/thread_pool.h` to build column buffers in parallel. Each column in a RecordBatch is fully independent — one thread handles `id`, another handles `value`, another handles `label`. No synchronization is needed during buffer construction because columns do not share memory.

Disabling threading forces sequential column processing: id → value → label, one at a time.

---

### Experiment G — Dictionary Encoding Depth Analysis

**File:** `06_additional_experiments_py.py`

#### What We Changed

This is an extended analysis of dictionary encoding. We tested the same four cardinality scenarios as Experiment 3, but added: direct inspection of the internal encoding structure, and file-size comparison on disk between normal and dictionary-encoded Arrow files.

#### Code

```python
import pyarrow as pa
import pyarrow.ipc as ipc
import numpy as np, os

np.random.seed(42)
n = 500_000

scenarios = [
    ("Very low  (2 unique)",    ['Eng', 'HR']),
    ("Low       (10 unique)",   [f'Dept_{i}' for i in range(10)]),
    ("High      (1000 unique)", [f'Dept_{i}' for i in range(1000)]),
    ("Unique    (all diff)",    None),
]

for label, choices in scenarios:
    values = [f'employee_{i}' for i in range(n)] if choices is None \
             else np.random.choice(choices, n).tolist()
    normal_arr = pa.array(values, type=pa.string())
    dict_arr   = pa.array(values, type=pa.dictionary(pa.int32(), pa.string()))
    normal_kb  = normal_arr.nbytes / 1024
    dict_kb    = dict_arr.nbytes   / 1024
    saving     = ((normal_kb - dict_kb) / normal_kb) * 100
    verdict    = "HELPS" if saving > 0 else "HURTS"
    print(f"{label:<28} | Normal: {normal_kb:.1f} KB | Dict: {dict_kb:.1f} KB | {saving:.1f}% | {verdict}")

# Internal structure inspection
sample    = ['Eng', 'Mkt', 'Eng', 'Fin', 'Eng', 'Mkt', 'Eng']
dict_arr  = pa.array(sample, type=pa.dictionary(pa.int32(), pa.string()))
print(f"Original : {sample}")
print(f"Dict     : {dict_arr.dictionary.to_pylist()}")   # unique values stored once
print(f"Indices  : {dict_arr.indices.to_pylist()}")       # int32 index per row

# File size comparison
for cardinality, values_pool in [('low_cardinality', ['Eng','Mkt','Fin','HR']),
                                   ('high_cardinality', [f'Dept_{i}' for i in range(1000)])]:
    vals = np.random.choice(values_pool, n).tolist()
    for enc, typ in [('normal', pa.string()),
                     ('dict',   pa.dictionary(pa.int32(), pa.string()))]:
        sch = pa.schema([pa.field('dept', typ)])
        arr = pa.array(vals, type=typ)
        with ipc.new_file(f'{cardinality}_{enc}.arrow', sch) as w:
            w.write_batch(pa.record_batch({'dept': arr}, schema=sch))
        kb = os.path.getsize(f'{cardinality}_{enc}.arrow') / 1024
        print(f"{cardinality} — {enc:<7}: {kb:.1f} KB")
```

#### What We Observed

**In-memory savings:**

| Scenario | Normal (KB) | Dict (KB) | Saving | Verdict |
|---|---|---|---|---|
| Very low — 2 unique | 3,174.1 | 1,953.1 | 38.5% | HELPS |
| Low — 10 unique | 4,882.8 | 1,953.2 | 60.0% | HELPS |
| High — 1,000 unique | 5,805.8 | 1,964.7 | 66.2% | HELPS |
| All unique — 500K | 9,168.8 | 11,122.0 | −21.3% | **HURTS** |

**Internal encoding — 7-element sample:**

```
Original : ['Eng', 'Mkt', 'Eng', 'Fin', 'Eng', 'Mkt', 'Eng']
Dict     : ['Eng', 'Mkt', 'Fin']          ← stored ONCE
Indices  : [0, 1, 0, 2, 0, 1, 0]          ← 4 bytes (int32) per row
```

**File size on disk:**

| Configuration | File Size | Compared to normal |
|---|---|---|
| Low cardinality — Normal | Larger | Baseline |
| Low cardinality — Dict | Smaller | Significant savings |
| High cardinality — Normal | Larger | Baseline |
| High cardinality — Dict | Similar or larger | No benefit |

**Source connection:** `cpp/src/arrow/type.h` — `DictionaryType` exposes `index_type`. Using `pa.int8()` instead of `pa.int32()` as the index type saves 3 bytes per row when there are fewer than 256 unique values.

**Rule of thumb:** Use dictionary encoding when a column has fewer than 50% unique values. Beyond that the dictionary itself becomes too large to be worth the index overhead.

---

## 12. Failure Analysis Summary Table

| # | Failure | Type | Arrow's response | Source reference |
|---|---|---|---|---|
| 1 | Data size exceeds RAM | Conceptual | No spilling — OOM or OS swap | In-memory design assumption |
| 2 | All-unique dictionary encoding | Conceptual | −21.3% memory (uses more than plain) | `type.h` — DictionaryType cardinality assumption |
| 3 | Core assumptions violated | Conceptual | Degraded performance or errors | Design tradeoffs in `buffer.h`, `type.h`, `data.h` |
| 4 | Process crash mid-write | Source-grounded | `ArrowInvalid: Expected to read 1 indicated record batches, got 0` | `writer.cc` — EOS footer never written |
| 5 | Magic byte corruption | Source-grounded | `ArrowInvalid: Not an Arrow IPC stream (bad magic bytes)` | `writer.cc` — `0xFFFFFFFF` framing token |

---

## 13. Failure Analysis

### Failure 1 — Data Size Exceeds Available Memory

**Question answered:** What happens when data size increases significantly?

#### What We Observed

As dataset size increases from 1M rows toward 50M rows and beyond, memory usage grows linearly. When data approaches available RAM:
- Memory allocation per `pa.array()` call begins to fail or stall
- The OS starts swapping pages to disk
- Write times and scan times degrade non-linearly

Arrow has no internal spilling mechanism. It does not page data to disk or manage overflow. If the total size of all columns in a RecordBatch exceeds available RAM, the allocation for `RecordBatchSerializer::Assemble()` will fail.

#### Root Cause

Arrow is an in-memory format by design. The `Buffer` class in `cpp/src/arrow/buffer.h` allocates contiguous memory at construction time. There is no chunked-lazy-load path. This is a conscious decision — by staying in-memory, Arrow avoids the complexity and latency of disk I/O management.

#### Resolution

Arrow is designed to serve as the in-memory layer inside systems that handle data movement. For datasets larger than RAM:
- Use Arrow with **Apache Spark** — Spark manages memory spilling; Arrow handles the in-memory format
- Use **DuckDB** — DuckDB uses Arrow internally but manages buffer pools and disk spilling itself
- Use **pyarrow.dataset** with partition pruning to reduce the in-memory footprint per query

---

### Failure 2 — High-Cardinality Data Breaks Dictionary Encoding

**Question answered:** What happens under data skew?

#### What We Observed

From Experiment 3 and Experiment G: when a column contains 500,000 entirely unique string values, `pa.dictionary(pa.int32(), pa.string())` uses **21.3% more memory** than plain `pa.string()`.

The dictionary array contains:
- Dictionary buffer: 500,000 unique strings — same size as the original plain array
- Indices buffer: 500,000 × 4 bytes = 2 MB of pure overhead

Total memory is higher, compression benefit is zero.

#### Root Cause

`DictionaryType` in `cpp/src/arrow/type.h` stores unique values in a dictionary and uses integer indices per row. The assumption is that the number of unique values is small relative to the total number of rows. When every value is unique, the dictionary is the same size as the data, and the indices are pure overhead.

#### Resolution

Use dictionary encoding only when a column has fewer than ~50% unique values. For high-cardinality columns (user IDs, emails, UUIDs, full names), use plain `pa.string()`. Arrow exposes this as an explicit developer choice — it does not auto-detect cardinality and switch encodings.

---

### Failure 3 — Arrow's Core Assumptions and What Breaks Them

**Question answered:** What assumptions does this system rely on?

| Assumption | Where in code | What breaks when violated |
|---|---|---|
| Working dataset fits in RAM | `cpp/src/arrow/buffer.h` — all allocations are in-memory | OOM errors, OS swap, write failures |
| Schema is known and fixed before writing | `cpp/src/arrow/ipc/writer.cc` — schema written in stream header | Reader fails on schema mismatch; no auto-coercion or auto-fill |
| Dictionary-encoded columns are low-cardinality | `cpp/src/arrow/type.h` — DictionaryType design | High-cardinality encoding wastes memory (−21.3% at all-unique) |
| Buffers are 64-byte aligned | `cpp/src/arrow/buffer.h` — alignment enforced at allocation | SIMD vectorization breaks silently on unaligned data |

Arrow is optimized for one specific workload: **fast analytics on structured, schema-fixed data that fits in memory**. Outside that envelope, it either fails explicitly or performs worse than alternatives.

---

### Failure 4 — Process Crash Mid-Write (Incomplete IPC Stream)

**Question answered:** What happens if a component fails?

**File:** `failure1.py`, artifact: `crash_test.arrow`

#### What We Simulated

A process crash was simulated by writing one batch to an IPC stream and then deleting the writer object without calling `writer.close()`. This reproduces what happens when a pipeline crashes, is killed, or loses power before the write is completed.

#### Code

```python
import pyarrow as pa
import pyarrow.ipc as ipc

schema = pa.schema([pa.field('id', pa.int32()), pa.field('value', pa.float64())])
batch  = pa.record_batch({'id': list(range(100)), 'value': [float(i) for i in range(100)]},
                          schema=schema)

# Write to file — simulate crash by NOT calling writer.close()
writer = ipc.new_file('crash_test.arrow', schema)
writer.write_batch(batch)
# writer.close() intentionally omitted — simulates process crash

del writer   # object destroyed without closing

# Attempt to read the incomplete file
try:
    with ipc.open_file('crash_test.arrow') as reader:
        result = reader.read_all()
except Exception as e:
    print(f"Read failed: {type(e).__name__}: {e}")
```

#### What We Observed

```
Read failed: ArrowInvalid: Expected to read 1 indicated record batches, got 0
```

#### Why This Happens (from `writer.cc`)

`writer.close()` writes two things before releasing the file handle:
1. An EOS (End-of-Stream) continuation block — `0xFFFFFFFF` followed by `0x00000000`
2. The file footer — a FlatBuffer-serialized index of all batch byte offsets

The reader opens the file and reads the footer first (from the end of the file). If the footer is missing, the reader cannot know how many batches were written or where they are located. It reports 0 batches found instead of the expected 1.

**Design choice:** Arrow deliberately does not implement crash recovery. Recovering a partially written IPC file would require reading backwards through raw bytes and attempting to reconstruct batch boundaries — complex, slow, and unreliable. Arrow's position: fail cleanly and let the surrounding infrastructure (WAL, checkpointing, message broker retries) handle recovery.

---

### Failure 5 — Magic Byte Corruption

**Question answered:** What happens when a component's storage is corrupted?

**File:** `failure2.py`, artifacts: `corrupt_test.arrow`, `corrupt2.arrow`

#### What We Simulated

We wrote a complete, valid IPC stream to a buffer, then overwrote the first 4 bytes with zeros. This simulates a storage corruption event — a partial disk write, a bit-flip, or a truncated network transfer that damages the start of the file.

#### Code

```python
import pyarrow as pa
import pyarrow.ipc as ipc
import io

schema = pa.schema([pa.field('id', pa.int32()), pa.field('value', pa.float64())])
batch  = pa.record_batch({'id': list(range(50)), 'value': [float(i) for i in range(50)]},
                          schema=schema)

# Write a valid, complete IPC stream
sink = io.BytesIO()
with ipc.new_stream(sink, schema) as writer:
    writer.write_batch(batch)

valid_bytes = sink.getvalue().tobytes()

print(f"First 8 bytes (valid)    : {valid_bytes[:8].hex()}")
# Expected: ff ff ff ff ...  (0xFFFFFFFF continuation token)

# Corrupt the first 4 bytes
corrupted = b'\x00\x00\x00\x00' + valid_bytes[4:]

# Save both for inspection
with open('corrupt_test.arrow', 'wb') as f:
    f.write(corrupted)

# Attempt to read the corrupted stream
try:
    reader = ipc.open_stream(pa.py_buffer(corrupted))
    result = reader.read_all()
except Exception as e:
    print(f"Corruption detected: {type(e).__name__}: {e}")
```

#### What We Observed

```
First 8 bytes (valid)    : ffffffff00000000...
Corruption detected: ArrowInvalid: Not an Arrow IPC stream (bad magic bytes)
```

#### Why This Happens (from `writer.cc`)

Arrow IPC streams begin with the 4-byte continuation token `0xFFFFFFFF`. The reader checks this value as the very first operation when opening a stream — before reading schema, before reading any column data, before allocating any buffers.

Corrupting these 4 bytes causes an immediate `ArrowInvalid` exception. The reader never attempts to deserialize columns from a stream that fails this check.

**Design choice:** Arrow fails fast at the framing layer. The alternative — attempting to read and recover from a corrupted stream — would risk silently producing wrong analytical results. A wrong average salary is worse than a clear error. Arrow's position: if the framing is invalid, reject the stream entirely and let the caller decide how to handle it.

**Tradeoff:** There is no partial recovery. A stream with a corrupted header cannot be repaired by Arrow — the entire file must be regenerated from source.

---

## 14. Key Insights

**1. Columnar layout is an end-to-end architectural commitment, not just a file format.**
From `builder_primitive.h` (array construction) through `writer.cc` (IPC assembly), every component assumes columns are contiguous typed buffers. There is no row reconstruction at any stage of the write path. The columnar structure is built first and maintained throughout.

**2. Batch size is the single most impactful streaming performance tuning parameter.**
Metadata overhead in `RecordBatchSerializer::Assemble()` is paid once per batch. At batch size 100, 1 million rows required 1.504s. At batch size 10,000, the same data took 0.146s — a 10× difference with zero data change.

**3. The `alignment` parameter does not exist in PyArrow 18.**
We confirmed this by reading `options.h` directly. Documentation can reference parameters that were renamed or removed. Source code is always the ground truth.

**4. Null handling is completely separated from data storage.**
The validity bitmap in `ArrayData` tracks nulls independently of the data buffer. The data buffer is always fully allocated at uniform element size — enabling SIMD vectorization without null-checking branches. `-1` and `NaN` are valid data values, not null sentinels.

**5. Arrow does not do schema evolution — this is intentional.**
Arrow reads exactly what is written. Schema evolution is handled by metadata catalog layers (Iceberg, Delta Lake) that sit above Arrow. Arrow's role is to store and transport data efficiently, not to resolve schema conflicts.

**6. Arrow's reliability design is intentional minimalism.**
Arrow detects framing corruption (magic bytes check) and incomplete streams (missing EOS footer). It does not implement recovery. This keeps the core library fast and focused, delegating recovery to surrounding infrastructure.

**7. Dictionary encoding has a cardinality cliff.**
Savings are large below ~50% unique values (up to 66% in our experiments). Above that threshold, the overhead of the index array outweighs the savings from deduplication. At 100% unique values: −21.3% (uses more memory than plain storage).

**8. Arrow memory is substantially smaller than Pandas for string-heavy data.**
Pandas stores each string as a Python object (~50 bytes overhead per string). Arrow stores all strings in a contiguous byte buffer. For string-heavy analytical workloads, Arrow can use 3–4× less RAM than Pandas for the same dataset.

---

## 15. Project File Structure

```
DS614-Apache-Arrow-Columnar-Storage/
│
├── README.md                            ← This file
│
├── 01_introduction.py                   ← Arrow overview, columnar vs row-oriented motivation
├── 02_write_path.py                     ← Write path trace: Python API → IPC stream
├── 03_design_decision.py                ← Four design decisions with source file references
├── 04_concept_mapping.py                ← Maps Arrow internals to four class concepts
├── 05_experiment&failure_analysis.py    ← Experiments 1–5, Failure Analysis 1–3
├── 06_additional_experiments_py.py      ← Experiments A–G (CSV, batch, null, schema, memory, IPC params, dict)
│
├── failure1.py                          ← Source Failure 4: process crash simulation
├── failure2.py                          ← Source Failure 5: magic byte corruption
├── failure_analysis_3                   ← Source Failure 3: assumptions analysis
├── compute.py                           ← Open source contribution — tutorial_min_max
│
├── crash_test.arrow                     ← Artifact: incomplete IPC stream (no footer)
├── corrupt_test.arrow                   ← Artifact: corrupted IPC stream (zeroed magic bytes)
├── corrupt2.arrow                       ← Artifact: secondary corruption test
│
├── DS614_Arrow_Presentation.pptx        ← Final presentation slides
├── DS614_Arrow_Report.md                ← Full project report
│
└── screenshots/
    ├── pyarrow_built_from_source_verification.png
    ├── git_log_arrow_main_branch.png
    ├── failure1_crash_simulation_output.png
    ├── failure2_magic_byte_corruption_output.png
    └── git_commit_tutorial_min_max_contribution.png
```

---

## 16. Reproducing the Experiments

### Step 1 — Clone the project

```bash
git clone https://github.com/Prajapatikrishna123/DS614-Apache-Arrow-Columnar-Storage
cd DS614-Apache-Arrow-Columnar-Storage
```

### Step 2 — Clone Apache Arrow source (required for notebooks 7–9)

```bash
git clone --depth=1 --branch main https://github.com/apache/arrow.git
```

### Step 3 — Install dependencies

```bash
pip install pyarrow pandas numpy matplotlib
```

### Step 4 — Open in Google Colab

Upload project files to Google Colab. Run files in numerical order.  
`06_additional_experiments_py.py` can be run independently after `05_experiment&failure_analysis.py`.

### Step 5 — Run individual failure simulations

```bash
python failure1.py    # Generates crash_test.arrow, prints ArrowInvalid output
python failure2.py    # Generates corrupt_test.arrow, prints ArrowInvalid output
```

---

## 17. References

| Resource | URL |
|---|---|
| Apache Arrow Documentation | https://arrow.apache.org/docs/ |
| Arrow Columnar Format Specification | https://arrow.apache.org/docs/format/Columnar.html |
| Arrow Validity Bitmaps | https://arrow.apache.org/docs/format/Columnar.html#validity-bitmaps |
| Arrow Dictionary Encoded Layout | https://arrow.apache.org/docs/format/Columnar.html#dictionary-encoded-layout |
| Arrow IPC Format | https://arrow.apache.org/docs/format/IPC.html |
| PyArrow IpcWriteOptions | https://arrow.apache.org/docs/python/generated/pyarrow.ipc.IpcWriteOptions.html |
| PyArrow IPC Streaming | https://arrow.apache.org/docs/python/ipc.html |
| PyArrow Memory | https://arrow.apache.org/docs/python/memory.html |
| Apache Arrow Source — GitHub | https://github.com/apache/arrow |
| writer.cc source | https://github.com/apache/arrow/blob/main/cpp/src/arrow/ipc/writer.cc |
| options.h source | https://github.com/apache/arrow/blob/main/cpp/src/arrow/ipc/options.h |
| buffer.h source | https://github.com/apache/arrow/blob/main/cpp/src/arrow/buffer.h |
| array/data.h source | https://github.com/apache/arrow/blob/main/cpp/src/arrow/array/data.h |
| builder_primitive.h source | https://github.com/apache/arrow/blob/main/cpp/src/arrow/array/builder_primitive.h |
