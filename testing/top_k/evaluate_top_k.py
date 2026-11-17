"""Read-only Top-K experiment using the production embedding and search functions."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import statistics
import sys
import time
import unicodedata
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE / "_deps"))
os.environ.setdefault("MPLCONFIGDIR", str(HERE / "_deps" / ".mplconfig"))
KS = tuple(range(1, 11))


def load_questions(path):
    """Validate the active collection-wide experiment without judging answers."""
    data = json.loads(path.read_text(encoding="utf-8"))
    questions = data.get("questions", [])
    if data.get("schema_version") != 2 or data.get("document_count") != 10:
        raise ValueError("Expected schema_version 2 and document_count 10")
    filenames = data.get("documents", [])
    if len(filenames) != 10 or len(set(filenames)) != 10 or any(not n.endswith(".pdf") for n in filenames):
        raise ValueError("Expected ten distinct source PDF filenames")
    if any(any(name not in filenames for name in q.get("source_documents", [])) for q in questions):
        raise ValueError("Question references a PDF outside the AI collection")
    expected_types = ["single_document"] * 10 + ["multi_document"] * 5 + ["out_of_scope"] * 3 + ["partially_answerable"] * 2
    if len(questions) != 20:
        raise ValueError("Exactly 20 questions are required")
    for i, (question, kind) in enumerate(zip(questions, expected_types), 1):
        if question.get("id") != f"Q{i:02}" or question.get("type") != kind:
            raise ValueError("Expected ordered Q01-Q20 with the supplied question categories")
        if not isinstance(question.get("question"), str) or not question["question"].strip():
            raise ValueError("Question text must be nonempty")
        groups = question.get("evidence_groups")
        if not isinstance(groups, list) or bool(groups) != (kind != "out_of_scope"):
            raise ValueError("Supported questions need evidence groups; negative controls must have none")
        if question.get("answerable") != (kind != "out_of_scope"):
            raise ValueError("Invalid answerable flag")
        for group in groups:
            if group.get("file_name") not in question.get("source_documents", []) or type(group.get("source_page")) is not int or group["source_page"] < 1 or not group.get("all_terms"):
                raise ValueError("Invalid evidence source/page/phrases")
        if question.get("scope") != "collection":
            raise ValueError("Every question must use the same ten-document collection")
    return questions


def normalize(text):
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def document_key(name):
    return re.sub(r"[^a-z0-9]", "", Path(name).stem.casefold())


def group_matches(group, content):
    """All literal evidence phrases must occur within a single chunk."""
    content = normalize(content)
    return all(normalize(term) in content for term in group["all_terms"])


def judge(question, chunks):
    groups = question["evidence_groups"]
    if question.get("answerable", True) is False:
        return {
            "evidence_found": None, "evidence_coverage": None,
            "matched_evidence_groups": [], "chunk_evidence_groups": [[] for _ in chunks],
            "relevant_chunks": 0, "irrelevant_chunks": len(chunks), "precision": 0.0,
        }
    matches = [[i for i, group in enumerate(groups) if group_matches(group, c["content"])]
               for c in chunks]
    found = set(i for items in matches for i in items)
    relevant = sum(bool(items) for items in matches)
    return {
        "evidence_found": len(found) == len(groups),
        "evidence_coverage": len(found) / len(groups),
        "matched_evidence_groups": sorted(found),
        "chunk_evidence_groups": matches,
        "relevant_chunks": relevant,
        "irrelevant_chunks": len(chunks) - relevant,
        "precision": relevant / len(chunks) if chunks else 0.0,
    }


def load_dataset(path, documents):
    from pypdf import PdfReader
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") == 2:
        questions = load_questions(path)
        paths = sorted(documents.glob("*.pdf"))
        if len(paths) != 10:
            raise ValueError(f"Select a folder containing exactly the ten new AI PDFs; found {len(paths)} in {documents}")
        files = {}
        for source in paths:
            if any(document_key(source.name) == document_key(name) for name in files):
                raise ValueError("PDF filenames must have distinct normalized stems")
            pages = [page.extract_text() or "" for page in PdfReader(source).pages]
            if not any(page.strip() for page in pages):
                raise ValueError(f"No extractable text in {source.name}")
            files[source.stem] = {"path": source, "pages": pages,
                                  "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
        return questions, files
    if data.get("schema_version") != 1 or not data.get("questions"):
        raise ValueError("Expected schema_version 1 and nonempty questions")
    files = {}
    ids = set()
    for q in data["questions"]:
        answerable = q.get("answerable", True)
        if type(answerable) is not bool:
            raise ValueError("answerable must be a boolean")
        if q["id"] in ids or not q["question"].strip():
            raise ValueError("Duplicate ID, empty question, or missing evidence groups")
        if answerable and not q["evidence_groups"]:
            raise ValueError(f"{q['id']}: answerable questions require evidence")
        if not answerable and (q["evidence_groups"] or not q.get("unanswerable_reason")):
            raise ValueError(f"{q['id']}: negative questions require no evidence and an absence rationale")
        ids.add(q["id"])
        name = q["document"]
        if name not in files:
            candidates = [p for p in documents.iterdir() if p.is_file()
                          and document_key(p.name) == document_key(name)]
            if len(candidates) != 1:
                raise ValueError(f"{name}: expected one local source, found {len(candidates)}")
            source = candidates[0]
            if source.suffix.lower() != ".pdf":
                raise ValueError(f"Unsupported source format: {source.suffix}; add a local reader first")
            pages = [p.extract_text() or "" for p in PdfReader(source).pages]
            files[name] = {"path": source, "pages": pages,
                           "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
        for group in q["evidence_groups"]:
            if not group["all_terms"] or any(not t.strip() for t in group["all_terms"]):
                raise ValueError(f"{q['id']}: empty evidence")
            page = group["source_page"]
            pages = files[name]["pages"]
            if type(page) is not int or not 1 <= page <= len(pages):
                raise ValueError(f"{q['id']}: invalid source page")
            if not group_matches(group, pages[page - 1]):
                raise ValueError(f"{q['id']}: evidence absent from source page {page}")
    return data["questions"], files


def discover(files, overrides):
    # Only Search reads. UUIDs are assigned at upload; local preview IDs are not real IDs.
    from backend.search_service import _configuration, SearchClient, RETRIEVAL_FIELDS
    endpoint, credential, index_name = _configuration()
    with SearchClient(endpoint=endpoint, credential=credential, index_name=index_name,
                      connection_timeout=10, read_timeout=20, retry_total=0) as client:
        metadata = list(client.search(search_text="*", select=["document_id", "file_name"]))
        mapping, corpus, issues = {}, {}, []
        for name, source in files.items():
            candidates = sorted({r["document_id"] for r in metadata
                                 if r.get("document_id") and r.get("file_name")
                                 and document_key(r["file_name"]) == document_key(source["path"].name)})
            chosen = overrides.get(name)
            if chosen and chosen not in candidates:
                issues.append(f"{name}: selected document_id is not indexed under this filename")
                continue
            if not chosen and len(candidates) != 1:
                issues.append(f"{name}: {'NOT INDEXED' if not candidates else 'AMBIGUOUS'}; IDs={candidates}")
                continue
            chosen = chosen or candidates[0]
            mapping[name] = chosen
            escaped = chosen.replace("'", "''")
            corpus[name] = list(client.search(search_text="*", filter=f"document_id eq '{escaped}'",
                                             select=list(RETRIEVAL_FIELDS)))
            if not corpus[name]:
                issues.append(f"{name}: no indexed chunks")
        if issues:
            raise ValueError("\n".join(issues) + "\nNo uploads or index changes were attempted.")
    return mapping, corpus, index_name


def summarize(rows):
    output = []
    for k in KS:
        selected = [r for r in rows if r["k"] == k and r.get("answerable", True)]
        if not selected:
            raise ValueError("A summary requires answerable questions at every K")
        total = sum(r["retrieved_count"] for r in selected)
        output.append({
            "K": k, "Questions": len(selected),
            "Retrieval Success %": 100 * statistics.mean(r["evidence_found"] for r in selected),
            "Evidence Coverage %": 100 * statistics.mean(r["evidence_coverage"] for r in selected),
            "Precision %": 100 * sum(r["relevant_chunks"] for r in selected) / total if total else 0,
            "Avg Noise Chunks": statistics.mean(r["irrelevant_chunks"] for r in selected),
            "Avg Tokens": statistics.mean(r["approx_tokens"] for r in selected),
            "Avg Retrieval Time ms": statistics.mean(r["retrieval_ms"] for r in selected),
        })
    return output


def write_csv(path, rows):
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        for row in rows:
            writer.writerow({key: json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict))
                             else value for key, value in row.items()})


def plot_summary(summary, output):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    for field, filename, title in (
        ("Retrieval Success %", "retrieval_success.png", "Complete evidence retrieved"),
        ("Precision %", "precision.png", "Evidence-based chunk precision"),
        ("Avg Tokens", "retrieved_tokens.png", "Retrieved context size (approximate)"),
        ("Avg Retrieval Time ms", "retrieval_time.png", "Retrieval latency (embedding excluded)"),
    ):
        fig, ax = plt.subplots(figsize=(7, 4.5), layout="constrained")
        ax.plot(KS, [r[field] for r in summary], marker="o", linewidth=2)
        ax.set(xlabel="Top-K", ylabel=field, title=title, xticks=KS)
        ax.set_ylim(bottom=0, top=100 if field.endswith("%") else None)
        ax.grid(alpha=.25)
        fig.savefig(output / filename, dpi=160)
        plt.close(fig)


def evaluate(questions, mapping, embed, search, seed=42):
    rows = []
    rng = random.Random(seed)
    for q in questions:
        print(f"\nDocument: {q['document']}\nQuestion: {q['question']}", flush=True)
        vector = embed(q["question"])  # One vector reused unchanged for all K.
        order = list(KS)
        rng.shuffle(order)  # Avoid always giving K=3 the cold request.
        for k in order:
            started = time.perf_counter()
            chunks = search(vector, mapping[q["document"]], top_k=k)
            elapsed = (time.perf_counter() - started) * 1000
            if len(chunks) > k or len({c['chunk_id'] for c in chunks}) != len(chunks):
                raise ValueError("Search returned too many or duplicate chunks")
            if any(c["document_id"] != mapping[q["document"]] for c in chunks):
                raise ValueError("Search returned a chunk outside the document filter")
            verdict = judge(q, chunks)
            # Explicit offline approximation; no tokenizer downloads or pipeline changes.
            tokens = sum(math.ceil(len(c["content"]) / 4) for c in chunks)
            row = {"question_id": q["id"], "document": q["document"],
                   "answerable": q.get("answerable", True), "question_type": q.get("type", "simple"),
                   "question": q["question"], "document_id": mapping[q["document"]],
                   "k": k, "retrieved_count": len(chunks),
                   "chunk_ids": [c["chunk_id"] for c in chunks],
                   "headings": [c.get("section_heading") for c in chunks],
                   "scores": [c.get("score") for c in chunks],
                   "page_numbers": [c.get("page_numbers") for c in chunks],
                   "retrieved_contents": [c["content"] for c in chunks],
                   **verdict, "approx_tokens": tokens, "retrieval_ms": elapsed}
            rows.append(row)
            outcome = 'N/A (unanswerable control)' if verdict['evidence_found'] is None else ('YES' if verdict['evidence_found'] else 'NO')
            print(f"K = {k}\nEvidence found: {outcome}\n"
                  f"Relevant chunks: {verdict['relevant_chunks']}\n"
                  f"Irrelevant chunks: {verdict['irrelevant_chunks']}\n"
                  f"Precision: {verdict['precision']:.1%}\nTokens (approx): {tokens}\n"
                  f"Retrieval time: {elapsed:.1f} ms", flush=True)
    return sorted(rows, key=lambda r: (r["question_id"], r["k"]))


def validate_collection(records, overrides=None, filenames=None):
    ready = {r["document_id"]: r for r in records if r.get("status") == "Ready"}
    if filenames:
        ready = {i: r for i, r in ready.items() if r.get("file_name") in filenames}
    if overrides:
        ids = list(overrides.values())
        if len(ids) != 10 or len(set(ids)) != 10 or any(i not in ready for i in ids):
            raise ValueError("Document map must select ten distinct Ready document IDs")
        ready = {i: ready[i] for i in ids}
    if len(ready) != 10:
        raise ValueError(f"Expected exactly ten Ready AI PDFs, found {len(ready)}; provide --document-map to select ten if necessary")
    old = {document_key(name) for name in ("hard_technology_guide.pdf", "INFS3104 Student Syllabus W25 v2.pdf", "Introduction_to_Azure_Cloud.pdf", "Introduction_to_Islam.pdf")}
    if any(not str(r.get("file_name", "")).lower().endswith(".pdf") or document_key(r["file_name"]) in old for r in ready.values()):
        raise ValueError("Collection must contain the new AI PDFs, without the previous dataset")
    mapping = {r["file_name"]: i for i, r in ready.items()}
    if len(mapping) != 10 or (filenames and set(mapping) != set(filenames)):
        raise ValueError("Expected one unique Ready document for each AI PDF filename")
    return mapping


def evaluate_answers(questions, document_ids, embed, search, generate, checkpoint=None, seed=42):
    """Collect exactly one case per question/K; never grade model answers."""
    rows = []
    rng = random.Random(seed)
    for q in questions:
        order = list(KS)
        rng.shuffle(order)
        for k in order:
            row = {"question_id": q["id"], "question_type": q["type"], "question": q["question"],
                   "k": k, "actual_k": None, "retrieved_count": 0, "answer": None,
                   "chunk_ids": [], "file_names": [], "page_numbers": [], "scores": [],
                   "headings": [], "retrieved_contents": [], "retrieved_chunks": [],
                   "embedding_ms": None, "retrieval_ms": None, "generation_ms": None,
                   "response_ms": None, "status": "failed", "error": None}
            started = time.perf_counter()
            stage = "embedding"
            try:
                mark = time.perf_counter()
                vector = embed(q["question"])
                row["embedding_ms"] = (time.perf_counter() - mark) * 1000
                stage = "retrieval"
                mark = time.perf_counter()
                chunks = search(vector, document_ids=list(document_ids), top_k=k)
                row["retrieval_ms"] = (time.perf_counter() - mark) * 1000
                row["actual_k"] = k  # Exact value passed to the production Search function.
                row["retrieved_count"] = len(chunks)
                row["retrieved_chunks"] = chunks
                for field, key in (("chunk_ids", "chunk_id"), ("file_names", "file_name"),
                                   ("page_numbers", "page_numbers"), ("scores", "score"),
                                   ("headings", "section_heading"), ("retrieved_contents", "content")):
                    row[field] = [c.get(key) for c in chunks]
                if len(chunks) > k or len(set(row["chunk_ids"])) != len(chunks):
                    raise ValueError("Unexpected chunk count or duplicate chunk IDs")
                if any(c.get("document_id") not in document_ids for c in chunks):
                    raise ValueError("Chunk outside the selected collection")
                stage = "generation"
                mark = time.perf_counter()
                row["answer"] = generate(q["question"], chunks)
                row["generation_ms"] = (time.perf_counter() - mark) * 1000
                if not isinstance(row["answer"], str) or not row["answer"].strip():
                    raise ValueError("Empty answer")
                row["status"] = "success"
            except Exception as error:
                row["error"] = {"stage": stage, "type": type(error).__name__,
                                "message": f"{stage.capitalize()} failed; inspect service configuration and availability."}
            row["response_ms"] = (time.perf_counter() - started) * 1000
            rows.append(row)
            print(f"{q['id']} K={k}: {row['status']}; {row['retrieved_count']} chunks; {row['response_ms']:.0f} ms", flush=True)
            if checkpoint:
                checkpoint(rows)
    return sorted(rows, key=lambda r: (r["question_id"], r["k"]))


def summarize_answers(rows):
    return [{"K": k, "Requests": len(selected),
             "Successful Requests": sum(r["status"] == "success" for r in selected),
             "Request Errors": sum(r["status"] != "success" for r in selected),
             "Avg Retrieved Chunks": statistics.mean(r["retrieved_count"] for r in selected),
             "Avg Response ms": statistics.mean(r["response_ms"] for r in selected)}
            for k in KS if (selected := [r for r in rows if r["k"] == k])]


def evidence_matches(group, chunk):
    return (chunk.get("file_name") == group["file_name"]
            and group["source_page"] in (chunk.get("page_numbers") or [])
            and group_matches(group, chunk.get("content", "")))


def validate_evidence(questions, corpus):
    chunks = corpus["chunks"]
    if set(corpus["documents"]) != {name for q in questions for name in q["source_documents"]}:
        raise ValueError("Evidence corpus does not match the ten selected AI PDFs")
    if len({c["chunk_id"] for c in chunks}) != len(chunks):
        raise ValueError("Duplicate corpus chunk IDs")
    for c in chunks:
        if c.get("document_id") != corpus["documents"].get(c.get("file_name")):
            raise ValueError("Corpus chunk is outside its declared document scope")
    for q in questions:
        groups = q.get("evidence_groups")
        if groups is None or bool(groups) != q["answerable"]:
            raise ValueError(f"{q['id']}: invalid evidence groups")
        for g in groups:
            if not g.get("all_terms") or any(not t.strip() for t in g["all_terms"]):
                raise ValueError("Empty evidence phrases are not allowed")
            if g["file_name"] not in q["source_documents"] or not any(evidence_matches(g, c) for c in chunks):
                raise ValueError(f"{q['id']}: evidence not found on cited source page: {g}")


def fetch_source_corpus(mapping):
    from backend.search_service import _configuration, SearchClient, RETRIEVAL_FIELDS
    endpoint, credential, index_name = _configuration()
    chunks = []
    with SearchClient(endpoint=endpoint, credential=credential, index_name=index_name,
                      connection_timeout=10, read_timeout=30, retry_total=0) as client:
        for name, docid in mapping.items():
            escaped = docid.replace("'", "''")
            matches = list(client.search(search_text="*", filter=f"document_id eq '{escaped}'", select=list(RETRIEVAL_FIELDS)))
            if not matches:
                raise ValueError(f"No indexed chunks for {name}")
            chunks.extend(dict(c) for c in matches)
    return {"fetched_utc": datetime.now(timezone.utc).isoformat(), "documents": mapping, "chunks": chunks}


def analyze_retrieval(rows, questions, corpus):
    validate_evidence(questions, corpus)
    expected = {(q["id"], k) for q in questions for k in KS}
    if len(rows) != len(expected) or {(r["question_id"], r["k"]) for r in rows} != expected:
        raise ValueError("A fair comparison requires exactly one saved case for every question/K (200 cases)")
    lookup = {q["id"]: q for q in questions}
    indexed = {c["chunk_id"]: c for c in corpus["chunks"]}
    output = []
    for row in rows:
        q = lookup[row["question_id"]]
        if row["question"] != q["question"]:
            raise ValueError("Saved question text differs from the active input")
        chunks = row["retrieved_chunks"]
        if row.get("actual_k") != row["k"] or row.get("retrieval_ms") is None and not chunks:
            raise ValueError("Retrieval is unavailable; do not treat a missing request as a zero-quality retrieval")
        if len(chunks) > row["k"] or len({c["chunk_id"] for c in chunks}) != len(chunks):
            raise ValueError("Invalid saved chunk count")
        for c in chunks:
            original = indexed.get(c["chunk_id"])
            if original is None or any(c.get(key) != original.get(key) for key in ("content", "file_name", "page_numbers", "document_id")):
                raise ValueError("Saved retrieval differs from the full source-corpus snapshot; refresh/recollect consistently")
        groups = q["evidence_groups"]
        matches = [[i for i, g in enumerate(groups) if evidence_matches(g, c)] for c in chunks]
        covered = {i for match in matches for i in match}
        relevant_ids = {c["chunk_id"] for c in corpus["chunks"] if any(evidence_matches(g, c) for g in groups)}
        relevant = sum(bool(match) for match in matches)
        precision = relevant / len(chunks) if chunks else 0.0
        recall = len(covered) / len(groups) if groups else None
        chunk_recall = len(relevant_ids.intersection(c["chunk_id"] for c in chunks)) / len(relevant_ids) if relevant_ids else None
        retrieval_f1 = 2*precision*chunk_recall/(precision+chunk_recall) if chunk_recall is not None and precision+chunk_recall else 0.0 if chunk_recall is not None else None
        f1 = 2*precision*recall/(precision+recall) if recall is not None and precision+recall else 0.0 if recall is not None else None
        output.append({**row, "question_type": q["type"], "answerable": q["answerable"],
                       "precision": precision if groups else None, "evidence_recall": recall,
                       "chunk_recall": chunk_recall, "retrieval_f1": retrieval_f1, "evidence_f1": f1,
                       "complete_evidence": len(covered)==len(groups) if groups else None,
                       "chunk_evidence_groups": matches, "matched_evidence_groups": sorted(covered),
                       "relevant_chunks": relevant, "irrelevant_chunks": len(chunks)-relevant,
                       "approx_tokens": sum(math.ceil(len(c["content"])/4) for c in chunks)})
    summary = []
    for k in KS:
        selected = [r for r in output if r["k"]==k]
        scored = [r for r in selected if r["answerable"]]
        negative = [r for r in selected if not r["answerable"]]
        rng = random.Random(42)
        samples = sorted(statistics.mean(scored[i]["retrieval_f1"] for i in [rng.randrange(len(scored)) for _ in scored]) * 100 for _ in range(2000))
        summary.append({"K": k, "Scored Questions": len(scored),
                        "Precision %": 100*statistics.mean(r["precision"] for r in scored),
                        "Evidence Recall %": 100*statistics.mean(r["evidence_recall"] for r in scored),
                        "Chunk Recall %": 100*statistics.mean(r["chunk_recall"] for r in scored),
                        "Retrieval F1 %": 100*statistics.mean(r["retrieval_f1"] for r in scored),
                        "Evidence F1 %": 100*statistics.mean(r["evidence_f1"] for r in scored),
                        "F1 CI Low %": samples[49], "F1 CI High %": samples[1949],
                        "Complete Evidence %": 100*statistics.mean(r["complete_evidence"] for r in scored),
                        "Avg Noise Chunks": statistics.mean(r["irrelevant_chunks"] for r in scored),
                        "Avg Approx Tokens": statistics.mean(r["approx_tokens"] for r in selected),
                        "Avg Response ms": statistics.mean(r["response_ms"] for r in selected),
                        "Generation Errors": sum(r["status"]!="success" for r in selected),
                        "Negative Context %": 100*statistics.mean(bool(r["retrieved_chunks"]) for r in negative) if negative else None})
    best = max(summary, key=lambda r: (round(r["Retrieval F1 %"],10), r["Complete Evidence %"], -r["Avg Approx Tokens"], -r["K"]))
    full = max(summary, key=lambda r: (r["Evidence Recall %"], r["Complete Evidence %"], r["Precision %"], -r["K"]))
    paired = []
    for q in questions:
        if q["answerable"]:
            a = next(r["retrieval_f1"] for r in output if r["question_id"]==q["id"] and r["k"]==best["K"])
            b = next(r["retrieval_f1"] for r in output if r["question_id"]==q["id"] and r["k"]==4)
            paired.append(100*(a-b))
    rng = random.Random(42)
    differences = sorted(statistics.mean(paired[rng.randrange(len(paired))] for _ in paired) for _ in range(2000))
    return output, summary, {"best_k": best["K"], "coverage_k": full["K"],
                              "f1_difference_vs_k4_pp": statistics.mean(paired),
                              "paired_difference_ci95_pp": [differences[49], differences[1949]],
                              "rule": "Highest mean per-question chunk F1 (harmonic mean of chunk precision and recall against all labeled relevant source chunks). Ties: complete evidence, fewer context tokens, lower K.",
                              "scope": "Exploratory best among these tested configurations; no held-out confirmation. LLM answers are not graded."}


def export_analysis(output, rows, questions, corpus, provenance):
    evaluated, summary, decision = analyze_retrieval(rows, questions, corpus)
    output.mkdir(parents=True, exist_ok=True)
    write_csv(output / "retrieval_results.csv", evaluated)
    write_csv(output / "retrieval_summary.csv", summary)
    metadata = {"generated_utc": datetime.now(timezone.utc).isoformat(), "decision": decision,
                "provenance": provenance, "corpus_chunks": len(corpus["chunks"]),
                "label_sha256": hashlib.sha256(json.dumps(questions,sort_keys=True).encode()).hexdigest()}
    (output / "analysis_status.json").write_text(json.dumps(metadata,indent=2),encoding="utf-8")
    from testing.top_k.report_top_k import render_retrieval_report
    render_retrieval_report(output, evaluated, summary, questions, corpus, metadata)
    report = (output / "top_k_report.html").read_text(encoding="utf-8")
    import html
    for name in ("retrieval_results.csv", "retrieval_summary.csv", "analysis_status.json"):
        target = Path(os.path.relpath(output / name, HERE)).as_posix()
        report = report.replace(f'href="{name}"', f'href="{html.escape(target, quote=True)}"')
    (HERE / "top_k_report.html").write_text(report, encoding="utf-8")
    print(f"Best chunk-F1 K={decision['best_k']}; fullest evidence K={decision['coverage_k']}. HTML: {output / 'top_k_report.html'}",flush=True)


def main():
    parser = argparse.ArgumentParser(description="Capture real RAG answers for 20 questions at K=1-10; no answer grading")
    parser.add_argument("--questions", type=Path, default=HERE / "test_questions.json")
    parser.add_argument("--documents", type=Path, help="Optional local folder containing exactly the ten AI PDFs")
    parser.add_argument("--results", type=Path, default=HERE / "results/ai_top_k")
    parser.add_argument("--document-map", type=Path, help="Optional JSON filename -> existing Ready document UUID mapping (ten entries)")
    parser.add_argument("--validate-only", action="store_true", help="Offline input validation; no service calls")
    parser.add_argument("--analyze-saved", action="store_true", help="Offline: analyze saved full cases and the cached corpus; no Azure or GPT calls")
    args = parser.parse_args()
    questions = load_questions(args.questions)
    if args.documents:
        load_dataset(args.questions, args.documents)
    print(f"Validated {len(questions)} questions; K={list(KS)}; {len(questions)*len(KS)} scheduled cases.", flush=True)
    if args.validate_only:
        snapshot = args.results / "source_corpus.json"
        if snapshot.exists():
            validate_evidence(questions, json.loads(snapshot.read_text(encoding="utf-8")))
            print("All evidence quotes verified against the cached complete corpus; no network calls.", flush=True)
        return 0
    if args.analyze_saved:
        capture = json.loads((args.results / "analysis/captured_cases.json").read_text(encoding="utf-8"))
        corpus = json.loads((args.results / "source_corpus.json").read_text(encoding="utf-8"))
        export_analysis(args.results / "analysis", capture["rows"], questions, corpus, capture["source"])
        return 0
    args.results.mkdir(parents=True, exist_ok=True)
    status = {"started_utc": datetime.now(timezone.utc).isoformat(), "status": "running",
              "dataset_sha256": hashlib.sha256(args.questions.read_bytes()).hexdigest(), "ks": list(KS),
              "scheduled_cases": 200, "answer_grading": False}
    stage = "document preflight"
    try:
        from backend.document_service import list_documents
        from backend.embedding_service import generate_embedding
        from backend.search_service import search_chunks
        from backend.llm_service import generate_answer
        overrides = json.loads(args.document_map.read_text(encoding="utf-8")) if args.document_map else None
        mapping = validate_collection(list_documents(), overrides, json.loads(args.questions.read_text(encoding="utf-8"))["documents"])
        if args.documents:
            _, files = load_dataset(args.questions, args.documents)
            if {document_key(n) for n in mapping} != {document_key(n) for n in files}:
                raise ValueError("Ready collection does not match the local ten PDFs")
        status["documents"] = mapping
        print("Selected collection: " + json.dumps(mapping, ensure_ascii=False), flush=True)
        stage = "source evidence preflight"
        corpus = fetch_source_corpus(mapping)
        validate_evidence(questions, corpus)
        (args.results / "source_corpus.json").write_text(json.dumps(corpus,ensure_ascii=False,indent=2),encoding="utf-8")
        stage = "RAG collection"
        rows = evaluate_answers(questions, list(mapping.values()), generate_embedding, search_chunks, generate_answer,
                                checkpoint=lambda items: write_csv(args.results / "top_k_results.csv", items))
        stage = "result export"
        write_csv(args.results / "top_k_results.csv", rows)
        summary = summarize_answers(rows)
        write_csv(args.results / "top_k_summary.csv", summary)
        from testing.top_k.report_top_k import render_answers_report
        render_answers_report(args.results, rows, summary, mapping)
        analysis_dir = args.results / "analysis"
        analysis_dir.mkdir(exist_ok=True)
        (analysis_dir / "captured_cases.json").write_text(json.dumps({"rows": rows, "documents": mapping, "source": "Original live runner capture; exact stage timings preserved."},ensure_ascii=False,indent=2),encoding="utf-8")
        export_analysis(analysis_dir, rows, questions, corpus, "Original live runner capture; exact stage timings preserved.")
        failures = sum(r["status"] != "success" for r in rows)
        status.update(status="complete" if not failures else "complete_with_errors", measured_rows=len(rows), request_errors=failures)
        print(f"Results: {args.results.resolve()} ({failures} request errors)", flush=True)
        return 0 if not failures else 1
    except Exception as error:
        status.update(status="failed", stage=stage, error_type=type(error).__name__,
                      detail=str(error) if isinstance(error, (ValueError, FileNotFoundError, ModuleNotFoundError)) else "Service or environment failure; check configuration and connectivity.")
        print(f"FAILED during {stage}: {status['detail']}", flush=True)
        return 1
    finally:
        status["finished_utc"] = datetime.now(timezone.utc).isoformat()
        (args.results / "run_status.json").write_text(json.dumps(status, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
