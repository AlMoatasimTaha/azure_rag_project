# Top-K retrieval comparison

## Repository contents and local data

The repository includes scripts, offline tests, plotting requirements, and the
20-question dataset with short evidence labels. PDFs, full extracted source
corpora, captured answers, reports, and generated results stay local and are not
included. The report and saved-run descriptions below refer to local artifacts;
they are not available in a fresh checkout.

Run `evaluate_top_k.py --validate-only` to validate the question dataset without
Azure calls. Offline tests use mocks; the full-corpus evidence check skips when
its optional local snapshot is absent. `--analyze-saved` requires a local saved
run and corpus. A new live run incurs Azure usage and must be started separately.

For plotting, install this folder's `requirements.txt` separately from production
dependencies. The existing scripts also support local packages under `_deps/`,
which is ignored by Git.

Open **top_k_report.html in this folder**. The self-contained HTML already
contains the measured results and six embedded line graphs; no server, internet
connection, plotting installation or Azure call is needed to view it.

## What the report shows

- The same 20 fixed questions across K=1 through 10 (200 captured cases).
- Exact AI PDFs 01–10; PDFs 11–20 and the old four-PDF dataset are excluded.
- Chunk precision, chunk recall and per-question F1, averaged over 17 questions
  with supported content. Three out-of-scope controls are separate.
- Required evidence-group coverage and complete-evidence success, preserving
  the previous experiment's evidence-coverage method.
- The best observed F1 configuration and the strongest evidence-coverage setting.
- F1 bootstrap intervals and a paired F1 comparison with production K=4.
- Category comparisons, context tokens, full response latency and negative noise.
- Every question, expected source quotes/pages, generated answer, retrieved chunk,
  filename, page, ID, score and request error.

This evaluates retrieved evidence, not GPT answer correctness. Production K stays
4. The highest F1 is a descriptive winner on these questions, not a validated
universal optimum. Labels were authored after the captured run. Confirm the
choice on new held-out questions before changing production.

## Evidence and metrics

The active test_questions.json includes literal evidence groups grounded in the
complete indexed source corpus (currently 128 chunks across ten PDFs). Every
phrase in a group must match one chunk after Unicode/case/whitespace normalization,
with the correct filename and page. A chunk matching any group is labeled relevant.
Full-corpus validation prevents unsupported quotes and incomplete recall denominators.
This is a literal evidence proxy: paraphrases and relevant passages outside the
labeled sources can be missed. The report exposes every label for review.

Precision = relevant retrieved chunks / actual retrieved chunks.
Chunk recall = relevant retrieved IDs / all labeled-relevant corpus IDs.
F1 is calculated per question, then averaged (macro).
Evidence recall = matched unique evidence groups / all required evidence groups.
Complete evidence means all required groups are covered.
For Q19/Q20, only supported parts enter these metrics; unsupported statistics
remain explicitly unavailable. Q16-Q18 have no recall/F1 and cannot pass vacuously.
Generation errors do not discard successful retrievals.

Bootstrap intervals resample the 17 whole questions 2,000 times with seed 42.
The paired K=4 comparison preserves each question's two measurements.
They do not measure repeated-generation variance or prove the labels are exhaustive.

## Commands from C:\rag-project

```powershell
# Rebuild all graphs/metrics/HTML from the saved 200 cases. Fully offline.
.\.venv\Scripts\python.exe testing/top_k/evaluate_top_k.py --analyze-saved

# Offline validation (also verifies quotes against the cached corpus)
.\.venv\Scripts\python.exe testing/top_k/evaluate_top_k.py --validate-only

# Offline tests: metrics, scope, source quotes, failure capture, exports
.\.venv\Scripts\python.exe -m unittest testing.top_k.test_evaluate_top_k -v

# Optional NEW live run: 200 RAG cases, followed by the complete report
.\.venv\Scripts\python.exe testing/top_k/evaluate_top_k.py
```

A live run uses the unchanged production embedding, search and generation functions
directly. /ask has fixed K=4 with no request-level override. Each case makes an
embedding request, retrieval request with explicit K and (unless context is empty)
a generation request. These are 200 RAG cases, not 200 total HTTP calls. No indexing,
uploading, frontend, embedding-deployment or generation configuration changes occur.
Preflight selects the exact ten Ready filenames and checks the source evidence.
Use --document-map PATH for duplicate upload IDs. Use --results PATH to preserve a
separate run. Current report and complete captured-case snapshot remain available
if a later collection is interrupted before its analysis export.

## Files

The root top_k_report.html embeds all graphs and all 200 cases. Supporting files
are under results/ai_top_k/analysis/: top_k_report.html, retrieval_results.csv,
retrieval_summary.csv, analysis_status.json, six PNG graphs and captured_cases.json.
The complete source snapshot is results/ai_top_k/source_corpus.json.
Raw runner CSVs and run_status.json remain under results/ai_top_k/.

For the current analysis, a later interrupted rerun had replaced the raw CSV with
47 cases. The original complete 200-case HTML was preserved as
analysis/captured_answers.html and recovered into captured_cases.json. No rows
were invented or mixed between runs. Response times from that HTML are rounded
to 1 ms; separate stage times are unavailable. The source hash is retained in the
capture snapshot. The report includes the original two generation errors.

Historical results/standalone is retained unchanged for the old four-PDF lexical
benchmark. standalone_top_k.py refuses the active AI dataset because it cannot
collect real GPT answers or provide this full-corpus Azure retrieval comparison.
