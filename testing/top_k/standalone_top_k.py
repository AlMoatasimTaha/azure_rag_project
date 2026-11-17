"""Evaluate local PDFs without an app server or Azure Search, and export HTML."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import re
from collections import Counter
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from testing.top_k.evaluate_top_k import (
    HERE, KS, evaluate, judge, load_dataset, plot_summary, summarize, write_csv,
)
import numpy as np


from testing.top_k.report_top_k import render_report, decision_plots, summarize_negatives


def run():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--documents', type=Path, default=ROOT / 'documents')
    parser.add_argument('--results', type=Path, default=HERE / 'results/standalone')
    args = parser.parse_args()
    if json.loads((HERE / 'test_questions.json').read_text(encoding='utf-8')).get('schema_version') == 2:
        parser.error('The active AI dataset requires real RAG answer capture. Use evaluate_top_k.py; local lexical grading is disabled for this dataset.')
    args.results.mkdir(parents=True, exist_ok=True)
    stage = 'source validation'
    try:
        questions, files = load_dataset(HERE / 'test_questions.json', args.documents)
        from pypdf import PdfReader
        print('STANDALONE TOP-K: four local PDFs; fully offline TF-IDF cosine search', flush=True)
        documents, matrices, vocabularies = {}, {}, {}
        provenance = {'method': 'Offline TF-IDF cosine similarity; pypdf page extraction; 180-word chunks with 40-word overlap', 'sources': {}, 'ks': list(KS), 'seed': 42}
        stopwords = set('a an the of and or to in on at for from with is are was were be been as by it its this that these those what which how why who when where does do can each their them they we you your our into than then have has had about'.split())
        def terms(text):
            return [t for t in re.findall(r"[a-z0-9]+", text.casefold()) if t not in stopwords]
        start = time.perf_counter()
        for name, source in files.items():
            path = source['path']
            document_id = 'standalone-' + source['sha256'][:16]
            stage = f'local PDF extraction: {path.name}'
            chunks = []
            for page_number, page in enumerate(PdfReader(path).pages, 1):
                text = page.extract_text() or ''
                words = text.split()
                heading = next((line.strip() for line in text.splitlines() if line.strip()), '(no heading)')
                for offset in range(0, len(words), 140):
                    content = ' '.join(words[offset:offset+180])
                    if not content:
                        continue
                    chunks.append({'document_id': document_id, 'chunk_id': f'{document_id}-{len(chunks):03}', 'content': content, 'section_heading': heading, 'page_numbers': [page_number], 'file_name': path.name})
                    if offset+180 >= len(words):
                        break
            if not chunks:
                raise ValueError(f'No extractable text for {name}')
            counts = [Counter(terms(c['content'])) for c in chunks]
            vocabulary = sorted({term for count in counts for term in count})
            lookup = {term: i for i, term in enumerate(vocabulary)}
            idf = np.asarray([math.log((1+len(chunks))/(1+sum(t in c for c in counts)))+1 for t in vocabulary])
            def vectorize(count):
                vector = np.zeros(len(vocabulary), dtype=np.float64)
                for term, count_value in count.items():
                    if term in lookup:
                        vector[lookup[term]] = 1+math.log(count_value)
                return vector * idf
            matrix = np.stack([vectorize(count) for count in counts])
            norms = np.linalg.norm(matrix, axis=1, keepdims=True)
            matrices[document_id] = matrix / np.where(norms == 0, 1, norms)
            vocabularies[document_id] = (lookup, idf)
            documents[name] = chunks
            provenance['sources'][name] = {'file': path.name, 'sha256': source['sha256'], 'document_id': document_id, 'chunks': len(chunks)}
            print(f'{path.name}: {len(chunks)} local chunks', flush=True)
        setup_ms = (time.perf_counter() - start) * 1000
        provenance['corpus_evidence'] = {q['id']: judge(q, documents[q['document']]) for q in questions if q.get('answerable', True)}
        corpus_missing = [key for key, verdict in provenance['corpus_evidence'].items() if not verdict['evidence_found']]
        if corpus_missing:
            print(f'WARNING: literal evidence incomplete within chunk boundaries: {corpus_missing}. Retained in results.', flush=True)
        mapping = {name: chunks[0]['document_id'] for name, chunks in documents.items()}
        by_id = {chunks[0]['document_id']: chunks for chunks in documents.values()}
        def embed(question):
            # Carry vectors for every document so identical question text in different
            # documents cannot accidentally select the wrong document's vocabulary.
            counts = Counter(terms(question))
            query_vectors = {}
            for document_id, (lookup, idf) in vocabularies.items():
                vector = np.zeros(len(lookup), dtype=np.float64)
                for term, count in counts.items():
                    if term in lookup:
                        vector[lookup[term]] = (1+math.log(count)) * idf[lookup[term]]
                query_vectors[document_id] = vector
            return query_vectors
        def search(query_vectors, document_id, top_k):
            vector = query_vectors[document_id]
            norm = np.linalg.norm(vector)
            if norm == 0:
                return []  # No known terms; never rank arbitrary tied zero vectors.
            scores = matrices[document_id] @ (vector / norm)
            ranked = np.argsort(-scores, kind='stable')[:top_k]
            return [{**by_id[document_id][int(i)], 'score': float(scores[i])} for i in ranked]
        stage = 'local question vectorization and retrieval'
        rows = evaluate(questions, mapping, embed, search)
        summary = summarize(rows)
        negatives = summarize_negatives(rows)
        stage = 'report export'
        write_csv(args.results / 'top_k_results.csv', rows)
        write_csv(args.results / 'top_k_summary.csv', summary)
        write_csv(args.results / 'unanswerable_summary.csv', negatives)
        plot_summary(summary, args.results)
        decision_plots(rows, summary, negatives, documents, args.results)
        provenance.update(completed_utc=datetime.now(timezone.utc).isoformat(), status='complete', setup_ms=setup_ms,
                          dataset_sha256=hashlib.sha256((HERE/'test_questions.json').read_bytes()).hexdigest(),
                          token_method='sum(ceil(len(content)/4))', retrieval_requests=len(rows), questions=len(questions), answerable_questions=sum(q['answerable'] for q in questions), negative_questions=sum(not q['answerable'] for q in questions), chunk_words=180, overlap_words=40)
        (args.results / 'standalone_run.json').write_text(json.dumps(provenance, indent=2), encoding='utf-8')
        render_report(args.results, rows, summary, documents, provenance, setup_ms, questions)
        print('\nSUMMARY\n' + json.dumps(summary, indent=2), flush=True)
        print(f'\nHTML: {(args.results / "top_k_report.html").resolve()}', flush=True)
        return 0
    except Exception as error:
        print(f'FAILED during {stage}: {type(error).__name__}. ' + (str(error) if isinstance(error, (ValueError, RuntimeError, ModuleNotFoundError)) else 'Check local files, permissions, and offline Python dependencies.'), flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(run())
