"""Offline checks of evidence rules, metric denominators, and production reuse."""
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from testing.top_k.evaluate_top_k import (
    KS, ROOT, HERE, document_key, evaluate, judge, load_dataset, analyze_retrieval, validate_evidence,
    plot_summary, summarize, write_csv, load_questions, evaluate_answers, summarize_answers, validate_collection,
)
from testing.top_k.report_top_k import summarize_negatives


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.q = {"id": "q01", "document": "doc", "question": "Compare A and B",
                  "evidence_groups": [{"all_terms": ["alpha", "10 GB"]},
                                      {"all_terms": ["beta", "20 GB"]}]}

    def test_all_groups_required_across_chunks(self):
        verdict = judge(self.q, [{"content": "ALPHA 10  GB"},
                                 {"content": "beta 20 GB"}, {"content": "noise"}])
        self.assertTrue(verdict["evidence_found"])
        self.assertEqual(verdict["relevant_chunks"], 2)
        self.assertAlmostEqual(verdict["precision"], 2 / 3)

    def test_partial_group_cannot_be_assembled_from_unrelated_chunks(self):
        verdict = judge(self.q, [{"content": "alpha"}, {"content": "10 GB"}])
        self.assertFalse(verdict["evidence_found"])
        self.assertEqual(verdict["relevant_chunks"], 0)
        self.assertEqual(judge(self.q, [])["precision"], 0)

    def test_score_and_heading_do_not_define_relevance(self):
        verdict = judge(self.q, [{"content": "noise", "score": 1,
                                 "section_heading": "alpha 10 GB beta 20 GB"}])
        self.assertFalse(verdict["evidence_found"])
        self.assertEqual(verdict["irrelevant_chunks"], 1)

    def test_negative_control_cannot_pass_vacuously(self):
        q = {"answerable": False, "evidence_groups": []}
        for chunks in ([], [{"content": "unrelated material"}]):
            verdict = judge(q, chunks)
            self.assertIsNone(verdict["evidence_found"])
            self.assertIsNone(verdict["evidence_coverage"])
            self.assertEqual(verdict["relevant_chunks"], 0)
            self.assertEqual(verdict["irrelevant_chunks"], len(chunks))

    def test_negatives_do_not_inflate_answerable_success_or_precision(self):
        rows = []
        for k in KS:
            base = {"k": k, "retrieval_ms": 1, "approx_tokens": 30}
            rows.append({**base, "answerable": True, "evidence_found": True,
                         "evidence_coverage": 1, "retrieved_count": 2,
                         "relevant_chunks": 1, "irrelevant_chunks": 1})
            rows.append({**base, "answerable": False, "evidence_found": None,
                         "evidence_coverage": None, "retrieved_count": 3,
                         "relevant_chunks": 0, "irrelevant_chunks": 3})
        for item in summarize(rows):
            self.assertEqual(item['Questions'], 1)
            self.assertEqual(item['Retrieval Success %'], 100)
            self.assertEqual(item['Precision %'], 50)
        for item in summarize_negatives(rows):
            self.assertEqual(item['Unanswerable Questions'], 1)
            self.assertEqual(item['Returned Any Context %'], 100)
            self.assertEqual(item['Avg Irrelevant Chunks'], 3)

    def test_same_embedding_and_filter_used_for_every_k(self):
        vector = [1.0]
        embed = Mock(return_value=vector)
        search = Mock(return_value=[{"chunk_id": "one", "document_id": "uuid",
                                    "content": "alpha 10 GB beta 20 GB"}])
        with contextlib.redirect_stdout(io.StringIO()):
            rows = evaluate([self.q], {"doc": "uuid"}, embed, search)
        embed.assert_called_once_with(self.q["question"])
        self.assertEqual({c.kwargs["top_k"] for c in search.call_args_list}, set(KS))
        self.assertTrue(all(c.args[0] is vector and c.args[1] == "uuid" for c in search.call_args_list))
        self.assertTrue(all(r["retrieved_count"] == 1 and r["precision"] == 1 for r in rows))
        summary = summarize(rows)
        self.assertTrue(all(s["Precision %"] == 100 for s in summary))
        with tempfile.TemporaryDirectory(dir=HERE, prefix="tmp-test-") as directory:
            output = Path(directory)
            write_csv(output / "raw.csv", rows)
            write_csv(output / "summary.csv", summary)
            plot_summary(summary, output)
            self.assertEqual(len(list(output.glob("*.png"))), 4)

    def test_document_filter_violation_rejected(self):
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError, "outside"):
            evaluate([self.q], {"doc": "uuid"}, lambda _: [1.0],
                     lambda *a, **k: [{"chunk_id": "x", "document_id": "wrong", "content": "alpha"}])

    def test_active_questions_and_k_values(self):
        questions = load_questions(HERE / "test_questions.json")
        self.assertEqual(len(questions), 20)
        self.assertEqual(KS, tuple(range(1, 11)))
        self.assertEqual(questions[0]['question'], 'What is the difference between AI, machine learning, deep learning, and generative AI?')
        self.assertTrue(all(q['scope'] == 'collection' for q in questions))
        self.assertTrue(all(bool(q['evidence_groups']) == q['answerable'] for q in questions))
        self.assertFalse(any('document' in q for q in questions))

    def test_answer_collection_and_exports_without_network(self):
        questions = load_questions(HERE / 'test_questions.json')
        chunk = {'chunk_id': 'c1', 'document_id': 'new-id', 'file_name': 'AI.pdf',
                 'page_numbers': [2], 'score': .8, 'content': 'Full content <safe> & complete'}
        search = Mock(return_value=[chunk])
        generate = Mock(return_value='An actual mocked answer')
        def answer(q, chunks):
            if q == questions[0]['question']:
                raise RuntimeError('secret must not be exported')
            return generate(q, chunks)
        with contextlib.redirect_stdout(io.StringIO()):
            rows = evaluate_answers(questions, ['new-id'], lambda q: [1.0], search, answer)
        self.assertEqual(len(rows), 200)
        self.assertEqual(search.call_count, 200)
        self.assertEqual(sum(r['status'] == 'failed' for r in rows), 10)
        self.assertTrue(all(r['retrieved_contents'] == [chunk['content']] for r in rows))
        self.assertTrue(all(r['actual_k'] == r['k'] for r in rows))
        self.assertNotIn('secret', str(rows))
        self.assertTrue(all('evidence_found' not in r for r in rows))
        self.assertTrue(all(call.kwargs['document_ids'] == ['new-id'] for call in search.call_args_list))
        with tempfile.TemporaryDirectory(dir=HERE) as directory:
            import csv, json
            from testing.top_k.report_top_k import render_answers_report
            output = Path(directory)
            write_csv(output / 'top_k_results.csv', rows)
            summary = summarize_answers(rows)
            write_csv(output / 'top_k_summary.csv', summary)
            render_answers_report(output, rows, summary, {'AI.pdf': 'new-id'})
            with (output / 'top_k_results.csv').open(encoding='utf-8', newline='') as stream:
                saved = list(csv.DictReader(stream))
            self.assertEqual(len(saved), 200)
            self.assertEqual(json.loads(saved[0]['retrieved_chunks']), [chunk])
            self.assertIn('&lt;safe&gt;', (output / 'top_k_report.html').read_text(encoding='utf-8'))

    def test_full_corpus_recall_and_f1(self):
        q = {'id':'supported','question':'Find alpha','type':'single_document','answerable':True,
             'source_documents':['AI.pdf'],'evidence_groups':[{'file_name':'AI.pdf','source_page':1,'all_terms':['alpha']}]}
        negative = {'id':'negative','question':'Outside','type':'out_of_scope','answerable':False,
                    'source_documents':[],'evidence_groups':[]}
        def chunk(cid, content):
            return {'chunk_id':cid,'content':content,'file_name':'AI.pdf','page_numbers':[1],'document_id':'id'}
        c1,c2,noise=chunk('one','alpha'),chunk('two','alpha alternate'),chunk('noise','unrelated')
        corpus={'documents':{'AI.pdf':'id'},'chunks':[c1,c2,noise]}
        rows=[]
        for k in KS:
            chunks=[c1] if k==1 else [c1,noise] if k==2 else [c1,c2,noise]
            for question,retrieved in ((q,chunks),(negative,[noise])):
                rows.append({'question_id':question['id'],'question':question['question'],'k':k,'actual_k':k,
                             'retrieved_chunks':retrieved,'status':'success','response_ms':5,'retrieval_ms':1})
        evaluated,summary,decision=analyze_retrieval(rows,[q,negative],corpus)
        first=next(r for r in evaluated if r['question_id']=='supported' and r['k']==1)
        self.assertEqual(first['precision'],1)
        self.assertEqual(first['chunk_recall'],.5)
        self.assertAlmostEqual(first['retrieval_f1'],2/3)
        self.assertEqual(first['evidence_recall'],1)
        self.assertEqual(summary[0]['Scored Questions'],1)
        self.assertEqual(decision['best_k'],3)
        self.assertTrue(all(r['retrieval_f1'] is None for r in evaluated if r['question_id']=='negative'))
        with self.assertRaisesRegex(ValueError,'exactly one saved case'):
            analyze_retrieval(rows[:-1],[q,negative],corpus)
        stale={**corpus,'chunks':[c1,{**c2,'page_numbers':[2]},noise]}
        with self.assertRaisesRegex(ValueError,'differs'):
            analyze_retrieval(rows,[q,negative],stale)

    def test_active_evidence_matches_complete_source_snapshot(self):
        import json
        snapshot = HERE / 'results/ai_top_k/source_corpus.json'
        if not snapshot.exists():
            self.skipTest('Optional local source corpus is not committed')
        corpus=json.loads(snapshot.read_text(encoding='utf-8'))
        validate_evidence(load_questions(HERE/'test_questions.json'),corpus)

    def test_collection_preflight(self):
        records = [{'document_id': str(i), 'file_name': f'AI {i}.pdf', 'status': 'Ready'} for i in range(10)]
        self.assertEqual(len(validate_collection(records)), 10)
        filenames = [r['file_name'] for r in records]
        extra = [{'document_id': 'other', 'file_name': 'Unselected.pdf', 'status': 'Ready'}]
        self.assertEqual(len(validate_collection(records + extra, filenames=filenames)), 10)
        duplicate = {**records[0], 'document_id': 'duplicate'}
        with self.assertRaises(ValueError):
            validate_collection(records + [duplicate], filenames=filenames)
        with self.assertRaises(ValueError):
            validate_collection(records[:9])
        records[0]['file_name'] = 'Introduction_to_Islam.pdf'
        with self.assertRaises(ValueError):
            validate_collection(records)


if __name__ == "__main__":
    unittest.main()
