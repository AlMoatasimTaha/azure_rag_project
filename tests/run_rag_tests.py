"""Run questions through the existing local FastAPI RAG pipeline."""
import argparse
import json
import math
from pathlib import Path
import sys
import time

import requests

HERE = Path(__file__).resolve().parent
BASE_URL = 'http://127.0.0.1:8000'
REPORT = HERE / 'rag_test_results.md'


def ask(session, question, timeout):
    result = dict(question=question, answer='', chunks=[], elapsed=0, status='FAILED', error='')
    started = time.perf_counter()
    try:
        # These are the exact ranked chunks passed to generate_answer by /ask.
        response = session.post(BASE_URL + '/ask', json={'question': question},
                                timeout=timeout, allow_redirects=False)
        try:
            data = response.json()
        except ValueError:
            raise ValueError(f'HTTP {response.status_code}: backend returned invalid JSON') from None
        if isinstance(data, dict):
            if isinstance(data.get('answer'), str):
                result['answer'] = data['answer']
            if isinstance(data.get('results'), list):
                result['chunks'] = data['results']
        error = None
        if isinstance(data, dict):
            for key in ('error', 'error_reason', 'detail'):
                if data.get(key):
                    error = f'{key}: {json.dumps(data[key], ensure_ascii=False)}'
                    break
            if data.get('success') is False or str(data.get('status', '')).lower() in {'error', 'failed', 'failure', 'invalid'}:
                error = error or str(data.get('message') or 'Backend reported an error')
        if response.status_code != 200:
            raise ValueError(f'HTTP {response.status_code}: {error or "Request failed"}')
        if error:
            raise ValueError('Application error in HTTP 200: ' + error)
        if not isinstance(data, dict) or not result['answer'].strip():
            raise ValueError('Backend returned a missing or empty answer')
        if data.get('question') != question or data.get('document_id') is not None:
            raise ValueError('Backend returned a different question or document scope')
        if data.get('top_k') != 4:
            raise ValueError('Backend did not report Top-K=4; no settings were changed')
        chunks = result['chunks']
        if (not isinstance(data.get('results'), list)
                or any(not isinstance(c, dict) or not isinstance(c.get('content'), str) for c in chunks)
                or type(data.get('count')) is not int or data['count'] != len(chunks)):
            raise ValueError('Backend returned invalid retrieval results or missing chunk text')
        if not any(c['content'].strip() for c in chunks):
            raise ValueError('No retrieved context: backend returned a fixed fallback without calling GPT-5 mini')
        result['status'] = 'SUCCESS'
    except requests.Timeout:
        result['error'] = f'Request timed out after {timeout:g} seconds; backend may still finish it.'
    except requests.ConnectionError:
        result['error'] = 'Cannot connect to backend. It must already be running at ' + BASE_URL
    except requests.RequestException as error:
        result['error'] = f'HTTP transport error ({type(error).__name__})'
    except ValueError as error:
        result['error'] = str(error)
    finally:
        result['elapsed'] = time.perf_counter() - started
    return result


def block(text):
    """Preserve full text, including embedded Markdown code fences."""
    text = str(text)
    fence = '```'
    while fence in text:
        fence += '`'
    return f'{fence}\n{text}\n{fence}'


def write_report(path, results, total, elapsed, note=''):
    lines = ['# RAG Test Results', '']
    if note:
        lines += [note, '']
    for number, result in enumerate(results, 1):
        lines += [f'## Question {number}', '', 'Question:', '', block(result['question']),
                  '', 'LLM Answer:', '', block(result['answer'] or 'No answer returned'),
                  '', 'Retrieved Chunks:', '']
        for rank, chunk in enumerate(result['chunks'], 1):
            if not isinstance(chunk, dict):
                lines += [f'Chunk {rank}: invalid metadata', '']
                continue
            pages = chunk.get('page_numbers') or chunk.get('page_number')
            if isinstance(pages, list):
                pages = ', '.join(str(page) for page in pages)
            lines += [f'Chunk {rank}:', f'- Document: {chunk.get("file_name") or "Not available"}',
                      f'- Page: {pages or "Not available"}',
                      f'- Chunk ID: {chunk.get("chunk_id") or "Not available"}',
                      f'- Retrieval rank: {rank}',
                      f'- Score: {chunk.get("score") if chunk.get("score") is not None else "Not available"}',
                      '', 'Text:', '', block(chunk.get('content', 'Text not available')), '']
        if not result['chunks']:
            lines += ['No chunks returned.', '']
        lines += ['Response Time:', f'{result["elapsed"]:.3f} seconds', '', 'Status:', result['status'], '']
        if result['error']:
            lines += ['Error:', '', block(result['error']), '']
        lines += ['---', '']
    successful = sum(r['status'] == 'SUCCESS' for r in results)
    lines += ['## Execution Summary', '', f'- Total questions: {total}',
              f'- Successful requests: {successful}', f'- Failed requests: {len(results) - successful}',
              f'- Questions not run: {total - len(results)}',
              f'- Total execution time: {elapsed:.3f} seconds', '']
    temporary = path.with_suffix('.md.tmp')
    temporary.write_text('\n'.join(lines), encoding='utf-8')
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--limit', type=int, help='Run only the first N questions')
    parser.add_argument('--timeout', type=float, default=120, help='Request timeout in seconds')
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error('--limit must be positive')
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error('--timeout must be positive and finite')
    try:
        questions = [line.strip() for line in (HERE / 'questions.txt').read_text(encoding='utf-8').splitlines() if line.strip()]
    except OSError as error:
        parser.error(f'Cannot read questions.txt: {error}')
    if not questions:
        parser.error('questions.txt contains no questions')
    questions = questions[:args.limit]
    print(f'Warning: {len(questions)} requests consume Azure resources and may incur API charges.', flush=True)
    if len(questions) > 1:
        try:
            confirmed = input(f'Run {len(questions)} questions? Type RUN to confirm: ').strip() == 'RUN'
        except EOFError:
            confirmed = False
        if not confirmed:
            print('Cancelled. No requests sent; existing report preserved.')
            return 2
    results, note = [], ''
    started = time.perf_counter()
    try:
        with requests.Session() as session:
            # Read-only readiness check; no backend imports or Azure writes.
            response = session.get(BASE_URL + '/documents', timeout=args.timeout, allow_redirects=False)
            if response.status_code != 200:
                raise ValueError(f'Document readiness check failed (HTTP {response.status_code})')
            documents = response.json()
            if not isinstance(documents, list) or not any(isinstance(d, dict) and d.get('status') == 'Ready' for d in documents):
                raise ValueError('No Ready/indexed documents available; no questions sent')
            for number, question in enumerate(questions, 1):
                result = ask(session, question, args.timeout)
                results.append(result)
                write_report(REPORT, results, len(questions), time.perf_counter() - started)
                print(f'Question {number}: {result["status"]} ({result["elapsed"]:.3f}s)', flush=True)
                if result['error']:
                    print(result['error'], flush=True)
    except requests.ConnectionError:
        note = 'Cannot connect to backend. It must already be running at ' + BASE_URL
    except requests.Timeout:
        note = 'Document readiness check timed out; no questions sent.'
    except (requests.RequestException, ValueError) as error:
        note = f'Document readiness check failed: {error}'
    except KeyboardInterrupt:
        note = 'Interrupted. Completed results saved; the in-flight request may still finish at the backend.'
    finally:
        write_report(REPORT, results, len(questions), time.perf_counter() - started, note)
    if note:
        print(note)
    print(f'Results saved to {REPORT}')
    return 0 if not note and all(r['status'] == 'SUCCESS' for r in results) else 1


if __name__ == '__main__':
    sys.exit(main())
