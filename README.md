# Apache Arrow — Columnar Storage Format

A systems-level reverse engineering and experimental analysis of Apache Arrow's columnar memory layout,
write path internals, buffer construction, IPC serialization, partitioning strategy, and reliability
behavior — using actual Apache Arrow C++ source code and instrumented Python experiments.

---

## Table of Contents

1. [Overview](#overview)
2. [Problem Statement](#problem-statement)
3. [Project Objectives](#project-objectives)
4. [System Under Study](#system-under-study)
5. [Execution Path Analysis](#execution-path-analysis)
6. [Source Code Exploration](#source-code-exploration)
7. [Systems Design Decisions](#systems-design-decisions)
8. [Concept Mapping](#concept-mapping)
9. [Experimental Setup](#experimental-setup)
10. [Core Experiments](#core-experiments)
11. [Core Failure Analysis](#core-failure-analysis)
12. [Additional Experiments A–G](#additional-experiments-ag)
13. [Source-Level Failure Analysis](#source-level-failure-analysis)
14. [Reproducing the Experiments](#reproducing-the-experiments)
15. [Project Structure](#project-structure)
16. [Key Insights](#key-insights)
17. [References](#references)

---

## Overview

This project reverse engineers Apache Arrow's columnar storage internals through:

- Source code analysis of the Apache Arrow C++ implementation cloned directly from `apache/arrow`
- Execution path tracing from the Python API down to low-level C++ buffer construction
- Five core experiments with real measured results covering scan speed, batch effects, and dictionary encoding
- Seven additional experiments (A–G) covering columnar vs CSV, batch overhead, null bitmaps, schema evolution, memory layout, IPC parameters, and dictionary encoding depth
- Three core failure analysis scenarios showing where and why Arrow breaks
- Source-level parameter modification of `IpcWriteOptions` (`compression`, `use_threads`) from `options.h`
- Two source-grounded failure simulations from `writer.cc`
- Concept mapping to four Big Data Engineering class topics

The objective is not to demonstrate PyArrow API usage, but to understand:
- How Arrow organizes columnar data in memory
- Why specific architectural decisions were made
- What tradeoffs those decisions introduce
- How the system behaves under stress and failure

---

## Problem Statement

Traditional row-oriented formats like CSV and JSON introduce significant overhead during analytical processing:

- Scanning one column requires reading every field of every row
- Type inference must run at read time — there is no embedded schema
- Memory layout is not cache-friendly for vectorized CPU operations
- Cross-language exchange requires expensive serialization and deserialization

**Row-oriented layout:**
```
Row 1: [name="Alice",  age=30, salary=90000]
Row 2: [name="Bob",    age=25, salary=75000]
Row 3: [name="Carol",  age=35, salary=110000]
```
A salary scan reads `name` and `age` on every row — wasted I/O and cache misses.

**Arrow columnar layout:**
```
name   buffer: ["Alice", "Bob", "Carol", ...]
age    buffer: [30, 25, 35, ...]
salary buffer: [90000, 75000, 110000, ...]
```
A salary scan reads one contiguous typed buffer — no skipping, no parsing, no type inference.

Apache Arrow solves this using columnar memory layout, contiguous typed buffers,
schema-aware serialization, and zero-copy semantics. This project investigates how these
mechanisms are implemented in the actual C++ source code.

---

## Project Objectives

1. Trace the complete Arrow write path from Python API to IPC stream output
2. Analyze columnar buffer construction from actual C++ source files
3. Study how Arrow handles streaming ingestion and batch-oriented transport
4. Measure real performance differences between columnar and row-oriented access
5. Modify source-level parameters (`compression`, `use_threads`) and observe behavior change
6. Simulate failure conditions and trace them back to source code behavior
7. Connect Arrow internals to four class concepts: columnar storage, partitioning, streaming/ingestion, reliability

---

## System Under Study

**Project Topic:** Apache Arrow — Columnar Storage Format

**Primary source files analyzed:**

```
cpp/src/arrow/ipc/writer.cc              — IPC write path, RecordBatchSerializer::Assemble()
cpp/src/arrow/ipc/options.h             — IpcWriteOptions: compression, use_threads
cpp/src/arrow/array/builder_primitive.h — Columnar array construction, ArrayBuilder::Finish()
cpp/src/arrow/array/data.h              — ArrayData internal structure, buffer references
cpp/src/arrow/buffer.h                  — Buffer memory layout, SliceBuffer(), 64-byte alignment
cpp/src/arrow/type.h                    — Type system, DataType hierarchy, DictionaryType
```

**Core functions analyzed:**

```cpp
RecordBatchSerializer::Assemble()    // writer.cc           — assembles columnar buffers into IPC payload
IpcWriteOptions                      // options.h           — write-time configuration parameters
ArrayBuilder::Finish()               // builder_primitive.h — finalizes typed column into ArrayData
SliceBuffer()                        // buffer.h            — zero-copy logical buffer slicing
```

---

## Execution Path Analysis

The complete write path was traced from the Python API down to C++ buffer assembly:

```
Python API
    ↓
pa.array() / pa.RecordBatch.from_arrays()
        → constructs columnar arrays in memory, one buffer per column
    ↓
pa.ipc.new_stream(sink, schema)
        → opens an IPC stream writer, writes schema message to sink
    ↓
writer.write_batch(record_batch)
        → triggers batch serialization pipeline
    ↓
SerializeRecordBatch()               [writer.cc]
        → C++ entry point, prepares IpcPayload structure
    ↓
RecordBatchSerializer::Assemble()    [writer.cc]
        → walks each column through VisitArray()
        → accumulates field_nodes_, buffer_meta_, body_buffers
    ↓
VisitArray() — recursive column traversal
        → primitive arrays  → 1 validity bitmap + 1 value buffer
        → string arrays     → 1 validity bitmap + 1 offsets buffer + 1 data buffer
        → nested arrays     → recurse into child arrays
    ↓
Buffer construction per column
        → contiguous typed memory blocks, 64-byte aligned
    ↓
Metadata generation
        → field nodes, buffer offsets, lengths, total body length
    ↓
IPC payload output
        → continuation token (0xFFFFFFFF) + metadata length + schema + buffers
```

**Key internal structures observed during tracing:**

| Structure | Location | Purpose |
|-----------|----------|---------|
| `field_nodes_` | writer.cc | Metadata about each column in the RecordBatch |
| `body_buffers` | writer.cc | The actual columnar data buffers |
| `buffer_meta_` | writer.cc | Offset and length metadata per buffer |
| `payload.body_length` | writer.cc | Total IPC payload body size |
| `IpcWriteOptions` | options.h | Write-time configuration (compression, threading) |
| `ArrayData` | array/data.h | Internal structure holding buffers for one column |

---

## Source Code Exploration

The Apache Arrow repository was cloned and explored at the source level:

```bash
git clone --depth=1 --branch main https://github.com/apache/arrow.git
```

### writer.cc — Core Write Path

`cpp/src/arrow/ipc/writer.cc` contains `RecordBatchSerializer::Assemble()` — the function
that walks each column and constructs the IPC payload. Key findings:
- Each column visited recursively through `VisitArray()`
- Every array contributes at least one validity bitmap + one value buffer
- String arrays contribute an additional offsets buffer
- Buffer metadata accumulates in `buffer_meta_`
- IPC stream begins with 4-byte continuation token (`0xFFFFFFFF`)

### options.h — Modifiable Parameters

`cpp/src/arrow/ipc/options.h` defines `IpcWriteOptions`:

```cpp
struct IpcWriteOptions {
  Compression::type compression = Compression::UNCOMPRESSED;
  bool use_threads = true;
  bool emit_dictionary_deltas = false;
  int max_recursion_depth = 64;
  bool write_legacy_ipc_format = false;
};
```

**Important:** The `alignment` parameter does not exist in PyArrow 18 — confirmed by reading
`options.h` directly. Source code is the real spec; documentation can lag behind.

### array/data.h — ArrayData Internal Structure

`cpp/src/arrow/array/data.h` defines the `ArrayData` struct which holds:
- `shared_ptr<Buffer>` for each column's memory buffers
- Multiple arrays can share the same underlying buffer without copying
- This is the zero-copy design that Arrow is built on

### buffer.h — Memory Layout and Zero-Copy

`cpp/src/arrow/buffer.h` defines the `Buffer` class:
- Contiguous block of raw memory, immutable once finalized
- Aligned to 64 bytes (for AVX-512 and SSE SIMD instructions)
- Reference-counted for zero-copy sharing across logical views
- `SliceBuffer()` creates logical views without copying

### type.h — Type System and DictionaryType

`cpp/src/arrow/type.h` defines the `DataType` hierarchy and `DictionaryType`:
- `index_type` → the integer type used for indices (int8, int16, int32)
- `value_type` → the type of values in the dictionary (string, etc.)
- Every type maps to a specific buffer layout driving all allocation decisions

---

## Systems Design Decisions

### Decision 1 — Columnar Memory Layout

**Where in code:** `cpp/src/arrow/array/data.h`, `cpp/src/arrow/buffer.h`

**What problem it solves:** Row-oriented layouts force column scans to read every field on
every row. Columnar layout stores each column as a contiguous typed buffer — a scan for
one column never touches other columns' memory, improving cache locality and enabling
vectorized CPU operations.

**Tradeoff:** Ideal for analytical read-heavy workloads but inefficient for transactional
systems that access full rows. Each data type needs its own buffer management logic.

---

### Decision 2 — Zero-Copy Slicing and IPC via Memory-Mapped Files

**Where in code:** `cpp/src/arrow/buffer.h` — `SliceBuffer()` and `cpp/src/arrow/ipc/writer.cc`, `reader.cc`

**What problem it solves:** Copying large arrays to create subsets is expensive. Arrow allows
logical slicing through offset adjustments without copying. Arrow places data at 8-byte
aligned offsets in IPC files so another process can memory-map the file and point directly
to the data — no deserialization step needed.

**Tradeoff:** Reduces allocation overhead but buffers can retain memory that is no longer
logically needed. Schema must be fixed upfront — schema evolution is not natively handled.

---

### Decision 3 — Batch-Oriented IPC Transport via RecordBatch

**Where in code:** `cpp/src/arrow/ipc/writer.cc` — `RecordBatchSerializer::Assemble()`

**What problem it solves:** Sending one row at a time produces massive metadata overhead.
RecordBatch groups many rows into one serialization unit, amortizing schema metadata and
buffer framing costs across thousands of rows.

**Tradeoff:** Small batches lose efficiency; large batches increase memory pressure.
Experiment B measured this — 1,000 small batches are measurably slower than 1 large batch.

---

### Decision 4 — Schema-Aware Typed Serialization and Validity Bitmaps

**Where in code:** `cpp/src/arrow/type.h`, `cpp/src/arrow/array/data.h`, `cpp/src/arrow/ipc/writer.cc`

**What problem it solves:** CSV requires type inference on every read. Arrow embeds the schema
directly, enabling readers to skip inference. Nulls are tracked using a separate 1-bit-per-value
validity bitmap — no sentinel values, no ambiguity between null and zero, no branching in hot
analytical loops.

**Tradeoff:** Schema embedding and bitmap overhead adds framing cost. Schema evolution
requires explicit handling by the developer — Arrow does not auto-cast or auto-coerce.

---

## Concept Mapping

### 1. Columnar Storage

**Class concept:** Columnar storage organizes data by column rather than row, improving
analytical performance through cache utilization and vectorized execution.

**How Arrow implements it:** Every `pa.Array` is backed by contiguous `Buffer` objects from
`cpp/src/arrow/buffer.h`. `RecordBatchSerializer::Assemble()` writes each column's buffer
independently into the IPC payload. A scan for one column never reads another column's memory.

**Evidence:** Experiment A — Arrow reads only the salary buffer directly. CSV must read and
parse all 5 columns to extract salary. Arrow was significantly faster for this single-column
analytical query on 500,000 rows.

---

### 2. Partitioning

**Class concept:** Partitioning divides large datasets into independently manageable segments,
enabling parallelism, pruning, and distributed processing.

**How Arrow implements it:**

**In-memory partitioning via ChunkedArray** — a column split across multiple independent memory
chunks, each at its own buffer address:
```
Chunk 0 → [1,2,3,4,5]   | address: 0x35fec2c00c0
Chunk 1 → [6,7,8,9,10]  | address: 0x35fec2c0140
Chunk 2 → [11,12,13,14,15] | address: 0x35fec2c01c0
```

**File-based partitioning** — via `pyarrow.dataset`, data is split into separate files by
partition key (e.g., `region=North/`, `region=South/`). Readers evaluate partition predicates
at the directory level and skip entire files without opening them.

---

### 3. Streaming / Ingestion

**Class concept:** Streaming ingestion processes data in bounded chunks with bounded memory,
enabling low-latency pipelines.

**How Arrow implements it:** `pa.ipc.new_stream()` writes RecordBatches incrementally. Unlike
the IPC File format (which writes a footer for random access), the IPC Stream format is read
top-to-bottom in order — exactly like a Kafka consumer reads messages.

**Evidence:** Experiment B — batch size directly controls streaming throughput. 1,000 small
batches are measurably slower than 1 large batch on the same total data because each batch
adds its own message header overhead in `writer.cc`.

---

### 4. Reliability / Fault Tolerance

**Class concept:** Reliable systems detect corruption, handle partial failures, and prevent
silent data corruption.

**How Arrow implements it:** Arrow uses magic byte framing (`0xFFFFFFFF` continuation token),
EOS footer markers, and the validity bitmap for explicit null tracking. Schema mismatches
raise immediate errors — Arrow never silently coerces or guesses.

**Evidence:** Experiment D — Arrow does not auto-cast int32 to int64, does not auto-fill
missing columns. Source Failure 2 — corrupting 4 bytes produces immediate
`ArrowInvalid: Not an Arrow IPC stream (bad magic bytes)` before any column data is read.

---

## Experimental Setup

### Environment

- Google Colab (primary execution environment)
- Python 3.11
- PyArrow built from source (version `25.0.0.dev7+gcc15bf1e8`)
- Apache Arrow C++ source cloned at main branch (April 2026)
- Conda environment: `pyarrow-dev`

### Technologies Used

| Component | Purpose |
|-----------|---------|
| Python | Experiment execution |
| PyArrow (source build) | Arrow Python bindings |
| Apache Arrow C++ source | Source code analysis |
| NumPy | Dataset generation |
| Pandas | Row-oriented comparison baseline |
| Matplotlib | Result visualization |
| Google Colab | Notebook execution |

---

## Core Experiments

Core experiments are in `05_experiment&failure_analysis.py`.

---

### Core Experiment 1 — Row-Based vs Columnar Scan Speed

**Setup:** 1 million rows, 4 columns (name, age, department, salary). Computed average salary
using Pandas vs Arrow. Run 5 times each, average taken.

**Results:**

| Format | Memory Used |
|--------|-------------|
| Pandas (row-based) | 116.83 MB |
| Arrow (columnar) | 31.00 MB |
| Advantage | Arrow 3.77x smaller |

Arrow was also faster on the scan — it accessed only the salary buffer. Pandas walked through
all 4 columns on every row even when only salary was needed.

**Systems Insight:** Directly proves the value of columnar layout from `cpp/src/arrow/buffer.h`.
Each column is a completely independent contiguous buffer — no row crossing in memory.

---

### Core Experiment 2 — RecordBatch Size Effect on Streaming Throughput

**Setup:** Write 1 million rows using 7 different RecordBatch sizes. Measure time and file size.

**Results:**

| Batch Size | Num Batches | Time (s) | File Size (KB) |
|------------|-------------|----------|----------------|
| 100        | 10,000      | 1.504    | 13,828.5       |
| 1,000      | 1,000       | 0.186    | 11,930.1       |
| **10,000** | **100**     | **0.146**| **11,740.2**   |
| 50,000     | 20          | 0.169    | 11,723.4       |
| 100,000    | 10          | 0.174    | 11,721.3       |
| 500,000    | 2           | 0.182    | 11,719.6       |
| 1,000,000  | 1           | 0.323    | 11,719.4       |

**Fastest batch size → 10,000 rows per batch**

**Systems Insight:** Arrow IPC emits schema metadata once per batch via `Assemble()`. At
batch size 100 vs 10,000, metadata overhead repeats 100x more — 10x slower for the same data.

---

### Core Experiment 3 — Dictionary Encoding Under Skew

**Setup:** 500,000 rows. Compared plain string array vs dictionary encoded array across
four cardinality levels.

**Results:**

| Scenario | Normal (KB) | Dict Encoded (KB) | Saving |
|----------|-------------|-------------------|--------|
| Very skewed (2 unique values) | 3,174.1 | 1,953.1 | 38.5% |
| Moderate (10 unique values) | 4,882.8 | 1,953.2 | 60.0% |
| High cardinality (1000 unique) | 5,805.8 | 1,964.7 | 66.2% |
| All unique (500K different) | 9,168.8 | **11,122.0** | **-21.3%** |

**Systems Insight:** Dictionary encoding defined in `cpp/src/arrow/type.h` as `DictionaryType`
stores unique values once. When all 500K values are unique, the index array adds overhead with
zero benefit — dictionary encoding uses 21.3% more memory than plain storage.

---

### Core Experiment 4 — Zero-Copy IPC Verification

**Setup:** Wrote a RecordBatch to an IPC file and read it back. Verified buffer addresses to
prove no deserialization happened.

**What we found:** Data returned correctly with no conversion step. Arrow memory-maps the file
and returns a pointer directly to the on-disk bytes. The bytes were transferred as-is at the
memory level — no copy, no deserialization.

**Systems Insight:** Arrow places data at 8-byte aligned offsets in `writer.cc`. The OS
memory-maps the file and the reader points directly to the mapped region.

---

### Core Experiment 5 — Streaming Ingestion (IPC Stream Format)

**Setup:** Wrote 5 batches of event data to an IPC Stream, then read them back one batch at
a time — simulating a real streaming pipeline.

**What we found:** All 5 batches written and read back correctly in order. The stream format
produced no random-access footer — it is read linearly, exactly like messages in Kafka.

**Systems Insight:** IPC Stream format intentionally omits the footer that IPC File format
uses. In `writer.cc`, stream mode skips footer writing because live stream consumers don't
need random batch access.

---

## Core Failure Analysis

These scenarios are in `05_experiment&failure_analysis.py`.

---

### Failure 1 — What Happens When Data Size Increases Significantly?

Arrow is fundamentally an in-memory format. Memory grows linearly as dataset size increases.
When data approaches available RAM, the OS begins swapping and performance degrades sharply.
Arrow has no internal spilling mechanism — it cannot handle out-of-memory conditions on its
own. For larger-than-memory data, Arrow is meant to serve as the in-memory representation
inside systems like Spark or DuckDB that manage data movement between memory and disk.

---

### Failure 2 — What Happens Under Skew?

High-cardinality data breaks dictionary encoding. At 500K unique values (Core Experiment 3),
dictionary encoding used 21.3% more memory than plain storage. The index array becomes the
dominant memory cost with zero compression benefit.

---

### Failure 3 — What Assumptions Does Arrow Rely On?

| Assumption | What breaks when violated |
|------------|--------------------------|
| Data fits in RAM | Memory pressure, OOM errors, OS swap degradation |
| Schema is fixed and known upfront | Readers fail on schema mismatch; no native schema evolution |
| Dictionary-encoded columns are low-cardinality | High-cardinality encoding uses more memory than plain storage |

---

## Additional Experiments A–G

All additional experiments are in `06_additional_experiments_py.py`.

---

### Experiment A — Row Format (CSV) vs Columnar Format (Arrow IPC)

**What we tested:** 500,000 rows, 5 columns. Wrote the same dataset to both Arrow IPC and CSV.
Measured time to read only the salary column from each format. Run 5 times each, average taken.

**Setup:**
```python
n = 500_000
# Arrow — read salary column directly
with ipc.open_file('experiment_a.arrow') as r:
    col = r.get_batch(0).column('salary')

# CSV — read salary column using pandas
df = pd.read_csv('experiment_a.csv', usecols=['salary'])
```

**Results:**

| Format | File Size | Read behavior |
|--------|-----------|---------------|
| Arrow IPC | smaller | Jumps directly to salary buffer using footer offset table |
| CSV | larger (text encoding) | Reads and parses every character of every row for all 5 columns |

**Systems Insight:** The Arrow reader uses the footer offset table to jump directly to the
salary buffer. This is the core design of `cpp/src/arrow/array/data.h` — each column
(`ArrayData`) has its own independent memory buffers with known offsets. CSV has no concept
of column offsets — every read is a full row scan.

**Tradeoff:** Arrow IPC is dramatically faster for analytical single-column queries. CSV
remains human-readable and universally supported but pays a heavy cost for column-selective reads.

---

### Experiment B — Batch Size Effect on IPC File Write Performance

**What we tested:** 100,000 rows written using 5 different batch configurations. Measured
file size and write time for each.

**Setup:**
```python
n = 100_000
batch_configs = [
    ('1 batch   (100k rows)', 1),
    ('5 batches  (20k rows)', 5),
    ('20 batches  (5k rows)', 20),
    ('100 batches (1k rows)', 100),
    ('1000 batches (100 rows)', 1000),
]
```

**Results:**

| Config | File (KB) | Time (ms) | Batches |
|--------|-----------|-----------|---------|
| 1 batch (100k rows) | smallest | fastest | 1 |
| 5 batches (20k rows) | slightly larger | slightly slower | 5 |
| 20 batches (5k rows) | larger | slower | 20 |
| 100 batches (1k rows) | larger | slower | 100 |
| 1000 batches (100 rows) | largest | slowest | 1,000 |

**Systems Insight:** Each RecordBatch written to a file adds its own message header via
`IpcFormatWriter::WriteRecordBatch()` in `writer.cc`. More batches = more message headers =
more file overhead. This is the same overhead we observed in Core Experiment 2 at scale.

**Real-world implication:** In Kafka → Arrow streaming pipelines, each arriving message
becomes one RecordBatch. Tiny messages produce thousands of small batches which is
inefficient. Production systems solve this with micro-batching — collecting small messages
and combining them into larger batches before writing.

---

### Experiment C — Null Handling and the Validity Bitmap

**What we tested:** Created arrays with different null percentages (0%, 25%, 50%, 75%, 100%)
and observed how buffer sizes change. This tests whether null values affect data buffer size.

**Setup:**
```python
n = 10_000
null_percentages = [0, 25, 50, 75, 100]
# For each percentage, created an array and measured data buffer size
```

**Results:**

| Null % | Null count | Data buf (bytes) | File (KB) |
|--------|------------|-----------------|-----------|
| 0%     | 0          | 80,000          | (constant) |
| 25%    | 2,500      | 80,000          | (constant) |
| 50%    | 5,000      | 80,000          | (constant) |
| 75%    | 7,500      | 80,000          | (constant) |
| 100%   | 10,000     | 80,000          | (constant) |

**The data buffer size stayed exactly 80,000 bytes regardless of null count.**

**Inside a null array — direct buffer inspection:**
```python
arr_with_nulls = pa.array([1.0, None, 3.0, None, 5.0], type=pa.float64())
# Buffer 0 (validity bitmap): 1=valid, 0=null
# Buffer 1 (data values):     always 5 × 8 bytes = 40 bytes (even for None positions)
```

**Systems Insight:** Arrow's null handling is defined in `cpp/src/arrow/array/data.h` in
the `ArrayData` struct. Every array has two separate components:
- **Buffer 0 (validity bitmap):** 1 bit per row — 1 means value present, 0 means null
- **Buffer 1 (data buffer):** all values including null positions at full data type size

This means nulls don't compress the data buffer — they are tracked separately in the bitmap.
No sentinel values (like -1 or NaN) are needed, so -1 and NaN remain valid real data values.
SIMD vectorized operations can still process the dense data buffer without checking for
special values mid-operation.

**Tradeoff:** The data buffer always allocates full memory even for null positions. A 100%
null array still uses full data buffer memory. The benefit is predictable memory layout and
safe SIMD operations.

---

### Experiment D — Schema Evolution

**What we tested:** Three schema evolution scenarios — adding a column, removing a column,
and changing a data type — to understand Arrow's behavior under real-world schema changes.

**Scenario 1 — Reader expects MORE columns than file has:**
```python
old_schema = pa.schema([('id', pa.int32()), ('name', pa.string()), ('salary', pa.float64())])
new_schema = pa.schema([('id', pa.int32()), ('name', pa.string()), ('salary', pa.float64()),
                        ('dept', pa.string())])  # new column not in file
```
**Result:** Arrow reads the file successfully with the old schema. The missing `dept` column
is not auto-filled — the developer must handle it in code.

**Scenario 2 — Reader expects FEWER columns than file has:**
```python
# File has 4 columns; select only 2
subset = batch.select(['id', 'salary'])
```
**Result:** Arrow successfully returns only the requested columns via `.select()`. Column
projection works correctly — unused columns are never deserialized.

**Scenario 3 — Type mismatch (int32 file vs int64 expected):**
```python
# File has int32; cast manually to int64
casted = pc.cast(batch.column('id'), pa.int64())
```
**Result:** Arrow reads the original int32 type. It does NOT auto-cast. The developer must
call `pc.cast()` explicitly.

**What this shows:**

| Scenario | Arrow behavior | Developer must do |
|----------|---------------|-------------------|
| Extra column in reader | Reads fine with old schema | Handle missing column explicitly |
| Extra column in file | Reads correctly | Use `.select()` to project columns |
| Type mismatch | Reads original type | Use `pc.cast()` to convert |

**Systems Insight:** Arrow does not do automatic schema evolution. This is a conscious design
decision prioritizing predictability over magic auto-conversion. Schema validation happens in
`cpp/src/arrow/ipc/reader.cc` at file open time. In production systems like Apache Iceberg
and Delta Lake, schema evolution is handled by a metadata layer on top of Arrow — not by
Arrow itself.

---

### Experiment E — Memory Usage: Arrow vs Pandas

**What we tested:** Loaded the same 200,000-row, 5-column dataset as a Pandas DataFrame and
as an Arrow Table. Measured peak memory usage using `tracemalloc` for both.

**Setup:**
```python
n = 200_000
# Same data: id (int32), name (string), salary (float64), dept (string), score (float64)

tracemalloc.start()
df = table.to_pandas()
pandas_current, pandas_peak = tracemalloc.get_traced_memory()
tracemalloc.stop()

tracemalloc.start()
with ipc.open_file('exp_e.arrow') as r:
    arrow_table = r.read_all()
arrow_current, arrow_peak = tracemalloc.get_traced_memory()
tracemalloc.stop()
```

**Column-level memory breakdown:**

| Column | Arrow (KB) | Pandas (KB) |
|--------|-----------|-------------|
| id (int32) | compact | larger |
| name (string) | compact buffer | high — Python object per string |
| salary (float64) | compact | similar |
| dept (string) | compact buffer | high — Python object per string |
| score (float64) | compact | similar |

**Why Arrow uses less memory:**
- Pandas stores strings as Python objects (~50 bytes overhead per string)
- Arrow stores strings in a contiguous byte buffer (just raw characters + offsets)
- `cpp/src/arrow/array/data.h` — `ArrayData` uses `shared_ptr<Buffer>` for memory buffers
- Multiple arrays can share the same underlying buffer without copying — true zero-copy

**Systems Insight:** When processing 100 million rows, Pandas might need 8–10 GB of RAM.
Arrow might need 2–3 GB for the same data. This is why DuckDB, Polars, and Spark 3.0+ use
Arrow as their internal memory format.

---

### Experiment F — IpcWriteOptions Parameter Modification (Source-Level)

**What we tested:** Modified `compression` and `use_threads` parameters sourced directly
from `cpp/src/arrow/ipc/options.h` and measured the effect on write time and file size.

**Source reference:**
```cpp
// options.h
Compression::type compression = Compression::UNCOMPRESSED;
bool use_threads = true;
```

#### F1 — Compression Parameter

```python
options_none = ipc.IpcWriteOptions(compression=None)
options_lz4  = ipc.IpcWriteOptions(compression='lz4')
options_zstd = ipc.IpcWriteOptions(compression='zstd')
```

**Results (1,000,000 rows, 3 columns):**

| Compression | Write Time (s) | File Size (MB) | Size Reduction |
|-------------|---------------|----------------|----------------|
| None        | 0.061         | 23.8           | 0%             |
| LZ4         | 0.143         | 13.2           | 44.5%          |
| ZSTD        | 0.387         | 9.7            | 59.2%          |

**Systems Insight:** Compression is applied as a post-processing layer after buffer construction
in `writer.cc`. The columnar layout is assembled first, then compressed per-buffer. LZ4 achieves
good ratios because repeated typed values in a single column compress better than interleaved
row data. ZSTD achieves the best compression (59.2%) at higher CPU cost.

#### F2 — Threading Parameter

```python
options_threaded   = ipc.IpcWriteOptions(use_threads=True)
options_unthreaded = ipc.IpcWriteOptions(use_threads=False)
```

**Results (500,000 rows, 6 columns):**

| Threading           | Write Time (s) |
|---------------------|---------------|
| `use_threads=True`  | 0.042         |
| `use_threads=False` | 0.079         |

**Systems Insight:** With threading enabled, Arrow parallelizes column buffer construction
across CPU cores. Each column is fully independent in the columnar model — one thread per
column, no synchronization needed during construction.

---

### Experiment G — Dictionary Encoding Depth Analysis

**What we tested:** A deeper analysis of dictionary encoding behavior including in-memory
savings, how the encoding works internally, and file size comparison on disk across
cardinality levels.

**Setup:**
```python
n = 500_000
scenarios = [
    ("Very low  (2 unique)",    ['Eng', 'HR']),
    ("Low       (10 unique)",   [f'Dept_{i}' for i in range(10)]),
    ("High      (1000 unique)", [f'Dept_{i}' for i in range(1000)]),
    ("Unique    (all diff)",    None)
]
```

**In-memory results:**

| Scenario | Normal (KB) | Dict (KB) | Saving | Verdict |
|----------|-------------|-----------|--------|---------|
| Very low (2 unique) | measured | smaller | positive | HELPS |
| Low (10 unique) | measured | smaller | positive | HELPS |
| High (1000 unique) | measured | smaller | small positive | HELPS |
| Unique (all diff) | measured | **larger** | **negative** | **HURTS** |

**Inside the encoding — direct inspection:**
```python
sample = ['Eng', 'Mkt', 'Eng', 'Fin', 'Eng', 'Mkt', 'Eng']
dict_arr = pa.array(sample, type=pa.dictionary(pa.int32(), pa.string()))

# Dictionary: ['Eng', 'Mkt', 'Fin']
# Indices:    [0, 1, 0, 2, 0, 1, 0]
# 'Eng' stored ONCE at index 0 — every occurrence just stores int32(0)
```

**File size on disk:**

| Config | File (KB) |
|--------|-----------|
| Low cardinality — Normal | larger |
| Low cardinality — Dict | smaller |
| High cardinality — Normal | larger |
| High cardinality — Dict | similar or larger |

**Systems Insight:** `cpp/src/arrow/type.h` defines `DictionaryType` with `index_type`
(int8/int16/int32) and `value_type`. Choosing `int8` indices instead of `int32` saves
even more memory when there are fewer than 256 unique values. The rule of thumb: use
dictionary encoding when a column has less than 50% unique values. Beyond that, the
dictionary itself becomes too large to be worth it.

---

## Source-Level Failure Analysis

Grounded in behavior from `cpp/src/arrow/ipc/writer.cc`. Full code in `failure1.py` and `failure2.py`.

---

### Source Failure 1 — Process Crash Mid-Write (Incomplete IPC Stream)

**Simulated:** Writing a partial stream without calling `writer.close()`.

```python
sink = pa.BufferOutputStream()
writer = ipc.new_stream(sink, batch.schema)
writer.write_batch(batch)
# writer.close() intentionally NOT called — simulates crash

try:
    reader = ipc.open_stream(sink.getvalue())
    while True:
        reader.read_next_batch()
except pa.lib.ArrowInvalid as e:
    print(f"Read failed: {e}")
```

**Observed output:**
```
Read failed: Expected to read 1 indicated record batches, got 0
```

**Systems Insight:** `writer.close()` writes the EOS footer token. When the writer crashes
before close, the footer is never written. The reader detects the missing EOS and raises
`ArrowInvalid`. Arrow deliberately does not implement crash recovery — this is left to
surrounding infrastructure (write-ahead logging, checkpointing, message brokers).

---

### Source Failure 2 — Magic Byte Corruption

**Simulated:** First 4 bytes of a valid IPC stream overwritten with zeros.

```python
# Corrupt the 4-byte continuation token (0xFFFFFFFF → 0x00000000)
corrupted = b'\x00\x00\x00\x00' + buf[4:]

try:
    reader = ipc.open_stream(pa.py_buffer(corrupted))
    reader.read_next_batch()
except Exception as e:
    print(f"Corruption detected: {type(e).__name__}: {e}")
```

**Observed output:**
```
Corruption detected: ArrowInvalid: Not an Arrow IPC stream (bad magic bytes)
```

**Systems Insight:** Arrow IPC streams begin with `0xFFFFFFFF` as a framing marker in
`writer.cc`. The reader checks this token before deserializing any column data —
immediate rejection rather than silent wrong results. Arrow fails fast at the framing
layer to protect data integrity.

---

## Reproducing the Experiments

All experiments were executed in Google Colab using a PyArrow source build.

### 1. Clone This Repository

```bash
git clone https://github.com/Prajapatikrishna123/DS614-Apache-Arrow-Columnar-Storage
cd DS614-Apache-Arrow-Columnar-Storage
```

### 2. Clone Apache Arrow Source

```bash
git clone --depth=1 --branch main https://github.com/apache/arrow.git
```

### 3. Install Dependencies

```bash
pip install pyarrow pandas numpy matplotlib
```

### 4. Run Experiments

| File | Contents |
|------|---------|
| `01_introduction.py` | Arrow overview, columnar vs row-oriented motivation |
| `02_write_path.py` | Trace write path from Python API to IPC stream |
| `03_design_decision.py` | Four design decisions with source code references |
| `04_concept_mapping.py` | Concept mapping to four class topics |
| `05_experiment&failure_analysis.py` | Core Experiments 1–5 + Core Failure Analysis 1–3 |
| `06_additional_experiments_py.py` | Experiments A–G (columnar vs CSV, batch overhead, null bitmap, schema evolution, memory, IPC parameters, dictionary encoding depth) |
| `failure1.py` | Process crash simulation |
| `failure2.py` | Magic byte corruption simulation |

---

## Project Structure

```
DS614-Apache-Arrow-Columnar-Storage/
│
├── README.md
│
├── 01_introduction.py
├── 02_write_path.py
├── 03_design_decision.py
├── 04_concept_mapping.py
├── 05_experiment&failure_analysis.py
├── 06_additional_experiments_py.py
│
├── failure1.py                  — process crash mid-write simulation
├── failure2.py                  — magic byte corruption simulation
├── failure_analysis_3           — assumptions failure analysis
├── compute.py                   — open source contribution (tutorial_min_max)
│
├── corrupt2.arrow               — corrupted IPC stream artifact
├── corrupt_test.arrow           — corruption test artifact
├── crash_test.arrow             — crash test artifact
│
├── DS614_Arrow_Presentation.pptx
├── DS614_Arrow_Report.md
│
└── screenshots/
    ├── pyarrow_built_from_source_verification.png
    ├── git_log_arrow_main_branch.png
    ├── failure1_crash_simulation_output.png
    ├── failure2_magic_byte_corruption_output.png
    └── git_commit_tutorial_min_max_contribution.png
```

---

## Key Insights

**1. Columnar layout is an end-to-end architectural commitment.**
From `builder_primitive.h` through `writer.cc`, every component assumes columns are
contiguous typed buffers. No row reconstruction happens at any stage of the write path.

**2. Batch size is the single most impactful streaming performance parameter.**
At batch size 100 vs 10,000, throughput differs by 10x for the same total data — because
metadata overhead in `RecordBatchSerializer::Assemble()` is paid once per batch.

**3. Null handling is completely separated from data storage.**
The validity bitmap in `ArrayData` tracks nulls independently of the data buffer. The data
buffer is always fully allocated — enabling SIMD operations without null-checking branches.

**4. Arrow does not do schema evolution — this is intentional.**
Arrow reads exactly what is in the file. Schema evolution is handled by metadata layers
(Iceberg, Delta Lake) built on top of Arrow, not by Arrow itself.

**5. The `alignment` parameter does not exist in PyArrow 18.**
Confirmed by reading `options.h` directly. Source code is always the ground truth.

**6. Arrow's reliability design is intentional minimalism.**
Arrow detects framing corruption (magic bytes) and incomplete streams (missing EOS).
It does not implement recovery — this is left to surrounding infrastructure.

**7. Dictionary encoding has a cardinality cliff.**
Below ~50% unique values: significant savings. At all-unique values: 21.3% more memory
wasted vs plain storage. Use it explicitly; Arrow does not enable it by default.

**8. Arrow memory is dramatically smaller than Pandas for string data.**
Pandas stores each string as a Python object (~50 bytes overhead). Arrow stores strings
in a contiguous byte buffer. For string-heavy workloads, Arrow uses 3–4x less RAM.

---

## References

- [Apache Arrow Documentation](https://arrow.apache.org/docs/)
- [Apache Arrow Columnar Format Specification](https://arrow.apache.org/docs/format/Columnar.html)
- [Apache Arrow Validity Bitmaps](https://arrow.apache.org/docs/format/Columnar.html#validity-bitmaps)
- [Apache Arrow Dictionary Encoded Layout](https://arrow.apache.org/docs/format/Columnar.html#dictionary-encoded-layout)
- [Apache Arrow IPC Format](https://arrow.apache.org/docs/format/IPC.html)
- [PyArrow IpcWriteOptions API](https://arrow.apache.org/docs/python/generated/pyarrow.ipc.IpcWriteOptions.html)
- [PyArrow IPC Streaming](https://arrow.apache.org/docs/python/ipc.html#writing-and-reading-streams)
- [PyArrow Memory](https://arrow.apache.org/docs/python/memory.html)
- [PyArrow Dataset Partitioning](https://arrow.apache.org/docs/python/dataset.html)
- [Apache Arrow Source Code — GitHub](https://github.com/apache/arrow)
- [Arrow C++ IPC Writer Source](https://github.com/apache/arrow/blob/main/cpp/src/arrow/ipc/writer.cc)
- [Arrow C++ IPC Options Source](https://github.com/apache/arrow/blob/main/cpp/src/arrow/ipc/options.h)
- [Arrow C++ Buffer Source](https://github.com/apache/arrow/blob/main/cpp/src/arrow/buffer.h)
- [Arrow C++ Array Data Source](https://github.com/apache/arrow/blob/main/cpp/src/arrow/array/data.h)
- [Arrow C++ Primitive Builder Source](https://github.com/apache/arrow/blob/main/cpp/src/arrow/array/builder_primitive.h)

---

## Authors

Project developed as part of DS614 — Big Data Engineering.

**Topic:** Apache Arrow — Columnar Storage Format
**Focus:** Systems-level reverse engineering, source code analysis, parameter modification
experiments, and failure analysis grounded in Apache Arrow IPC internals.
