"""Offline HTML and decision plots for answerable and negative retrieval controls."""
import base64
import html
import statistics


def render_answers_report(output, rows, summary, documents):
    """Present captured answers and evidence without scoring their correctness."""
    esc = lambda value: html.escape(str(value))
    page = ['<!doctype html><html lang="en"><meta charset="utf-8"><title>AI Top-K results</title>',
            '<style>body{max-width:1200px;margin:32px auto;padding:16px;font:16px/1.6 system-ui}table{border-collapse:collapse}td,th{padding:8px;border:1px solid #ccc}pre{white-space:pre-wrap;overflow-wrap:anywhere}details{margin:12px 0;padding:12px;border:1px solid #ccc}</style>',
            '<h1>AI Top-K experiment: captured answers</h1><p>20 questions × K=1–10. No automatic answer grading. Response time includes embedding, search and generation.</p>',
            '<h2>Selected PDFs</h2><ul>' + ''.join(f'<li>{esc(name)}: {esc(value)}</li>' for name, value in documents.items()) + '</ul>',
            '<h2>Request summary</h2><table><tr>' + ''.join(f'<th>{esc(key)}</th>' for key in summary[0]) + '</tr>']
    for item in summary:
        page.append('<tr>' + ''.join(f'<td>{esc(round(value, 2) if isinstance(value, float) else value)}</td>' for value in item.values()) + '</tr>')
    page.append('</table><h2>Answers and full retrieved chunks</h2>')
    for row in rows:
        page.append(f'<details><summary>{esc(row["question_id"])} · K={row["k"]} · {esc(row["status"])} · {row["response_ms"]:.0f} ms</summary><p>{esc(row["question"])}</p><p>Actual K: {esc(row["actual_k"])}; chunks: {row["retrieved_count"]}</p><h3>LLM answer</h3><pre>{esc(row["answer"])}</pre>')
        if row['error']:
            page.append(f'<pre>Request error: {esc(row["error"])}</pre>')
        for rank, chunk in enumerate(row['retrieved_chunks'], 1):
            page.append(f'<details><summary>Chunk {rank} · {esc(chunk.get("file_name"))} · pages {esc(chunk.get("page_numbers"))}</summary><p>ID: {esc(chunk.get("chunk_id"))}; score: {esc(chunk.get("score"))}</p><pre>{esc(chunk.get("content"))}</pre></details>')
        page.append('</details>')
    page.append('</html>')
    (output / 'top_k_report.html').write_text('\n'.join(page), encoding='utf-8')

from testing.top_k.evaluate_top_k import KS, summarize


def summarize_negatives(rows):
    result = []
    for k in KS:
        selected = [r for r in rows if r['k'] == k and not r['answerable']]
        if not selected:
            continue
        result.append({
            'K': k, 'Unanswerable Questions': len(selected),
            'Returned Any Context %': 100 * statistics.mean(r['retrieved_count'] > 0 for r in selected),
            'Avg Irrelevant Chunks': statistics.mean(r['irrelevant_chunks'] for r in selected),
            'Avg Irrelevant Tokens': statistics.mean(r['approx_tokens'] for r in selected),
            'Avg Local Retrieval ms': statistics.mean(r['retrieval_ms'] for r in selected),
        })
    return result


def decision_plots(rows, summary, negatives, documents, output):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    colors = ['#2964b7', '#d57414', '#27806c', '#8b55ad']
    with plt.rc_context({'font.size': 10, 'axes.spines.top': False, 'axes.spines.right': False}):
        fig, axes = plt.subplots(1, 2, figsize=(13, 5.2), layout='constrained')
        axes[0].plot(KS, [r['Retrieval Success %'] for r in summary], 'o-', label='Complete evidence success', color=colors[0], linewidth=2)
        axes[0].plot(KS, [r['Precision %'] for r in summary], 's-', label='Relevant chunk precision', color=colors[1], linewidth=2)
        axes[0].set(title='Answerable questions: coverage vs noise', xlabel='Top-K', ylabel='Percent', xticks=KS, ylim=(0, 100))
        axes[0].legend(loc='lower left')
        for s in summary:
            axes[0].annotate(f"{s['Retrieval Success %']:.1f}%", (s['K'], s['Retrieval Success %']), xytext=(0, -17), textcoords='offset points', ha='center', fontsize=9)
        # Co-located K values are grouped honestly, rather than visually jittered.
        points = {}
        for s in summary:
            points.setdefault((s['Avg Tokens'], s['Retrieval Success %']), []).append(s)
        point_items = list(points.items())
        for index, ((tokens, success), items) in enumerate(point_items):
            axes[1].scatter(tokens, success, s=80, color=colors[0])
            label = 'K=' + ','.join(str(s['K']) for s in items)
            label += f"\nprecision {items[0]['Precision %']:.1f}%"
            offset, align = ((25, -30), 'left') if index == len(point_items)-1 else ((0, -32-36*index), 'center')
            axes[1].annotate(label, (tokens, success), xytext=offset, textcoords='offset points', ha=align, fontsize=9,
                             arrowprops={'arrowstyle': '-', 'color': '#718096', 'linewidth': .8})
        axes[1].set(title='Answerable questions: evidence gained per context cost', xlabel='Average retrieved tokens (approximate)', ylabel='Complete evidence success %', ylim=(0, 100), xlim=(0, max(s['Avg Tokens'] for s in summary)*1.4))
        for ax in axes:
            ax.grid(alpha=.2)
        fig.suptitle('Top-K decision guide | 60 answerable questions', fontsize=15)
        fig.savefig(output/'decision_tradeoff.png', dpi=170)
        plt.close(fig)

        fig, ax = plt.subplots(figsize=(9, 5.1), layout='constrained')
        for color, name in zip(colors, documents):
            values = summarize([r for r in rows if r['document'] == name])
            ax.plot(KS, [r['Retrieval Success %'] for r in values], marker='o', label=name, color=color)
        ax.set(title='Evidence success by document | 15 answerable questions each', xlabel='Top-K', ylabel='Complete evidence success %', xticks=KS, ylim=(0,100))
        ax.grid(alpha=.2)
        ax.legend(loc='lower left', fontsize=9)
        fig.savefig(output/'success_by_document.png', dpi=160)
        plt.close(fig)

        fig, axes = plt.subplots(1, 2, figsize=(12, 4.8), layout='constrained')
        axes[0].bar([str(s['K']) for s in negatives], [s['Avg Irrelevant Chunks'] for s in negatives], color=colors[1])
        axes[0].set(title='Irrelevant context on unanswerable questions', xlabel='Top-K', ylabel='Average irrelevant chunks')
        axes[0].set_ylim(bottom=0)
        axes[1].bar([str(s['K']) for s in negatives], [s['Returned Any Context %'] for s in negatives], color=colors[2])
        axes[1].set(title='Unanswerable queries returning any context', xlabel='Top-K', ylabel='Percent of 20 negative questions', ylim=(0,100))
        fig.suptitle('Negative controls | 8 related-but-unsupported + 12 out-of-topic', fontsize=13)
        fig.savefig(output/'unanswerable_noise.png', dpi=160)
        plt.close(fig)


def render_report(output, rows, summary, documents, provenance, setup_ms, questions):
    esc = lambda value: html.escape(str(value))
    negatives = summarize_negatives(rows)
    lookup = {q['id']: q for q in questions}
    def table(items):
        if not items:
            return '<p>No measurements.</p>'
        return '<div class="scroll"><table><thead><tr>' + ''.join(f'<th>{esc(f)}</th>' for f in items[0]) + '</tr></thead><tbody>' + ''.join(
            '<tr>' + ''.join(f'<td>{v:.2f}</td>' if isinstance(v, float) else f'<td>{esc(v)}</td>' for v in item.values()) + '</tr>' for item in items) + '</tbody></table></div>'
    def figure(file, caption, large=False):
        data = base64.b64encode((output/file).read_bytes()).decode()
        return f'<figure class="{"wide" if large else ""}"><img alt="{esc(caption)}" src="data:image/png;base64,{data}"><figcaption>{esc(caption)}</figcaption></figure>'
    panels = []
    for question in questions:
        selected = [r for r in rows if r['question_id'] == question['id']]
        kind = 'answerable' if question['answerable'] else question['type']
        expected = '<ul>' + ''.join(f'<li>PDF page {g["source_page"]}: ' + '; '.join(esc(t) for t in g['all_terms']) + '</li>' for g in question['evidence_groups']) + '</ul>' if question['answerable'] else f'<p>{esc(question["unanswerable_reason"])}</p>'
        blocks = []
        for r in selected:
            outcome = ('PASS' if r['evidence_found'] else 'FAIL') if r['answerable'] else ('NO CONTEXT RETURNED' if r['retrieved_count'] == 0 else 'UNSUPPORTED CONTEXT RETURNED')
            chunk_blocks = []
            for rank, (cid, score, content, groups, pages) in enumerate(zip(r['chunk_ids'], r['scores'], r['retrieved_contents'], r['chunk_evidence_groups'], r['page_numbers']), 1):
                match = 'evidence groups ' + ', '.join(str(i+1) for i in groups) if groups else 'no expected evidence'
                chunk_blocks.append(f'<details class="chunk"><summary>Rank {rank} · cosine {score:.4f} · {esc(match)}</summary><small>{esc(cid)} · PDF pages {esc(pages)}</small><pre>{esc(content)}</pre></details>')
            blocks.append(f'<section class="kblock"><h4>Top-{r["k"]}: {esc(outcome)}</h4><p>{r["retrieved_count"]} chunks returned · {r["relevant_chunks"]} relevant · {r["irrelevant_chunks"]} irrelevant · {r["precision"]:.1%} precision · {r["approx_tokens"]} approximate tokens · {r["retrieval_ms"]:.4f} ms</p>{"".join(chunk_blocks)}</section>')
        panels.append(f'<details class="question" data-document="{esc(question["document"])}" data-type="{esc(kind)}"><summary><span class="badge">{esc(kind.replace("_", " "))}</span> {esc(question["id"])} · {esc(question["question"])}</summary><p>{esc(question["document"])}</p><div class="expected"><b>{"Expected evidence" if question["answerable"] else "Why no answer is supported"}</b>{expected}</div>{"".join(blocks)}</details>')
    doc_options = ''.join(f'<option>{esc(name)}</option>' for name in documents)
    count_rows = []
    per_doc = []
    for name, chunks in documents.items():
        source = provenance['sources'][name]
        count_rows.append({'Document': name, 'Questions': sum(q['document']==name for q in questions), 'Answerable': 15, 'Related unsupported': 2, 'Out of topic': 3, 'Chunks': len(chunks)})
        per_doc.append(f'<details class="panel"><summary>{esc(name)} — {len(chunks)} chunks</summary><h4>Answerable questions</h4>{table(summarize([r for r in rows if r["document"]==name]))}<h4>Unanswerable controls</h4>{table(summarize_negatives([r for r in rows if r["document"]==name]))}<p><small>Source SHA-256: {esc(source["sha256"])}</small></p></details>')
    maximum = max(len(chunks) for chunks in documents.values())
    failed = [r['question_id'] for r in rows if r['k']==max(KS) and r['answerable'] and not r['evidence_found']]
    page = '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Top-K decision report — 80 questions</title><style>
    *{box-sizing:border-box}body{margin:0;background:#f2f5fa;color:#17283e;font:16px/1.6 system-ui,sans-serif}main{max-width:1250px;margin:auto;padding:32px 24px}header,.panel,.question{background:#fff;padding:24px;border:1px solid #dbe3ef;border-radius:14px;margin-bottom:16px}header{border-top:5px solid #2964b7}h1{font-size:40px;line-height:1.15;margin:12px 0}h2{margin:36px 0 16px}h3,h4{margin-bottom:8px}.eyebrow{font-size:12px;font-weight:750;letter-spacing:.13em;color:#2964b7}.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:20px 0}.card{background:#edf3fb;padding:16px;border-radius:9px}.card b{display:block;font-size:30px;line-height:1.2}.card span,small{color:#52647b}.notice{padding:16px 20px;background:#fff5df;border-left:4px solid #cd942c;border-radius:4px;margin:16px 0}.graphs{display:grid;grid-template-columns:1fr 1fr;gap:18px}figure{margin:0;border:1px solid #dbe3ef;background:white;border-radius:12px;padding:12px}figure.wide{grid-column:1/-1}img{width:100%;height:auto}figcaption{font-size:14px;color:#52647b;padding:4px 10px 10px}.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:14px}th,td{text-align:left;padding:12px;border-bottom:1px solid #dbe3ef;white-space:nowrap}th{background:#edf3fb}summary{cursor:pointer;font-weight:650}.badge{font-size:11px;text-transform:uppercase;background:#eaf0f8;padding:4px 7px;border-radius:4px;display:inline-block}.kblock{background:#f4f7fb;padding:16px;border-radius:8px;margin-top:14px}.chunk{padding:10px;border-bottom:1px solid #dbe3ef}.expected{border-left:3px solid #2964b7;padding:10px 18px;margin-top:12px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:14px/1.6 system-ui}.filters{display:flex;gap:12px;flex-wrap:wrap;margin:16px 0}select,input{font:inherit;padding:10px;border:1px solid #bdcce0;border-radius:6px;max-width:100%}a{color:#245caa}@media(max-width:760px){main{padding:18px 12px}h1{font-size:30px}.graphs{grid-template-columns:1fr}.cards{grid-template-columns:1fr 1fr}}[hidden]{display:none!important}
    </style></head><body><main>'''
    page += f'<header><div class="eyebrow">STANDALONE LOCAL RETRIEVAL · TOP-{" / ".join(str(k) for k in KS)}</div><h1>Which K gives enough evidence?</h1><p>Compare evidence coverage against precision, extra context and unanswerable-query noise.</p><div class="cards"><div class="card"><b>{len(questions)}</b><span>Questions across 4 PDFs</span></div><div class="card"><b>60</b><span>Answerable questions</span></div><div class="card"><b>20</b><span>Unanswerable controls</span></div><div class="card"><b>{len(rows)}</b><span>Measured question/K runs</span></div></div><small>Generated {esc(provenance["completed_utc"])} · Fully offline TF-IDF cosine retrieval</small></header>'
    page += '<div class="notice"><b>What this measures:</b> local PDF text, 180-word chunks with 40-word overlap and per-document TF-IDF cosine search. No Azure services or application server were used. These results apply to this local method; they do not validate production Azure embedding search.</div>'
    page += '<h2>Decision graph</h2>' + figure('decision_tradeoff.png', 'Use the left panel to compare success and precision. Use the right panel to see whether extra context adds evidence. Overlapping K points are grouped.', True)
    page += f'<div class="notice"><b>Comparison limit:</b> these documents contain only 5–{maximum} chunks each with the current settings. Top-6 and Top-7 retrieve identical sets. More questions improve topic coverage but cannot make those K values distinguishable on this corpus. Tiny timing differences are not a useful basis for choosing K.</div>'
    page += '<h2>Answerable questions only</h2><div class="panel"><p>60 questions: 15 per document. Success requires every expected evidence group. Precision counts relevant chunks among the chunks actually returned. Negative controls are excluded from both metrics.</p>' + table(summary) + '</div>'
    page += '<div class="graphs">' + figure('success_by_document.png', 'Per-document success prevents the overall average from hiding a weak document.', True) + '</div>'
    page += '<h2>Related unsupported and out-of-topic questions</h2><div class="panel"><p>20 controls: each document has two related questions with missing information and three questions outside its topic. None has expected answer evidence in its selected document. Any returned chunks count as irrelevant context. No answer generation or rejection threshold is implemented, so this is not an answer-accuracy or hallucination test. Empty context means no vocabulary match, not a validated semantic rejection.</p>' + table(negatives) + '</div>' + figure('unanswerable_noise.png', 'More K can add unsupported context. A larger K alone cannot decide whether a question is answerable.', True)
    page += '<h2>Additional measurements</h2><div class="graphs">' + ''.join(figure(file,caption) for file,caption in [
        ('retrieval_success.png','Evidence success across the 60 answerable questions.'),('precision.png','Relevant chunk precision across answerable questions.'),('retrieved_tokens.png','Average context size on answerable questions; character-based token estimate.'),('retrieval_time.png','Local search wall time on answerable questions; excludes PDF preparation and question vectorization.')]) + '</div>'
    page += '<h2>Dataset and document breakdown</h2><div class="panel">' + table(count_rows) + '</div>' + ''.join(per_doc)
    page += '<h2>Inspect all 80 questions and their retrieved chunks</h2><div class="filters"><select id="document-filter"><option value="">All documents</option>' + doc_options + '</select><select id="type-filter"><option value="">All question types</option><option value="answerable">Answerable</option><option value="related_unanswerable">Related but unsupported</option><option value="out_of_topic">Out of topic</option></select><input id="search-filter" type="search" placeholder="Search question text" aria-label="Search questions"></div><p id="visible-count">80 questions shown</p>' + ''.join(panels)
    page += f'<h2>How to interpret and reproduce</h2><div class="panel"><p>No final K is selected automatically. Look for where success stops improving, then compare precision and context size. Review failed questions and negative controls before choosing. Questions still missing complete evidence at K={max(KS)}: {esc(", ".join(failed) or "None")}.</p><p>Every phrase in an evidence group must occur in one chunk after Unicode, case and whitespace normalization. Groups may be covered by different chunks. A chunk matching at least one complete group is relevant. Scores never determine the ground-truth label. Literal matching can miss paraphrases and evidence split between chunks; overlapping chunks may both be counted as relevant. Unanswerable labels were reviewed against the complete target PDFs and carry an absence rationale.</p><p>Tokens use ceil(characters/4) per chunk, excluding prompts and metadata. Retrieval timing covers cosine ranking and result construction only; corpus fitting and query vectorization are excluded. There is one measurement per question/K with seeded shuffled order. Preparation took {setup_ms/1000:.3f} seconds. Do not interpret these local timings as Azure latency.</p><pre>.\\.venv\\Scripts\\python.exe testing/top_k/standalone_top_k.py</pre><p>Sources: documents/ · Dataset: testing/top_k/test_questions.json · Dataset SHA-256: <small>{esc(provenance["dataset_sha256"])}</small></p><p>Saved beside this HTML: top_k_results.csv, top_k_summary.csv, unanswerable_summary.csv, standalone_run.json and seven PNG charts. This HTML embeds every graph and works offline. Production code and settings were not changed.</p></div>'
    page += '''</main><script>
    function filterQuestions(){let doc=document.getElementById('document-filter').value;let kind=document.getElementById('type-filter').value;let query=document.getElementById('search-filter').value.toLowerCase();let count=0;document.querySelectorAll('.question').forEach(q=>{let match=(!doc||q.dataset.document===doc)&&(!kind||q.dataset.type===kind)&&(!query||q.querySelector('summary').textContent.toLowerCase().includes(query));q.hidden=!match;if(match)count++;});document.getElementById('visible-count').textContent=count+' questions shown';}document.querySelectorAll('select,input').forEach(control=>control.addEventListener('input',filterQuestions));
    </script></body></html>'''
    (output/'top_k_report.html').write_text(page, encoding='utf-8')


def render_retrieval_report(output, rows, summary, questions, corpus, metadata):
    """Self-contained evidence dashboard; no generated-answer grading."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    esc = lambda value: html.escape(str(value))
    ks = [s["K"] for s in summary]
    decision = metadata["decision"]
    best = decision["best_k"]
    selected = next(s for s in summary if s["K"] == best)
    with plt.rc_context({"font.size": 10, "axes.spines.top": False, "axes.spines.right": False}):
        specs = [
            ("retrieval_quality.png", "Chunk retrieval: precision, recall and F1", [("Precision %", "Precision", "#c77522"), ("Chunk Recall %", "Recall", "#16816a"), ("Retrieval F1 %", "F1", "#315fce")], "Percent", True),
            ("evidence_coverage.png", "Required evidence retrieved", [("Evidence Recall %", "Evidence-group coverage", "#16816a"), ("Complete Evidence %", "All required groups found", "#315fce")], "Percent", True),
            ("context_cost.png", "Context size across all 20 questions", [("Avg Approx Tokens", "Approximate context tokens", "#315fce")], "Approximate tokens", False),
            ("response_time.png", "Full pipeline response time, including failed requests", [("Avg Response ms", "Mean response time", "#315fce")], "Milliseconds", False),
            ("negative_context.png", "Out-of-scope questions: returned context", [("Negative Context %", "Queries receiving context", "#c77522")], "Percent of 3 questions", True),
        ]
        for filename, title, series, ylabel, percent in specs:
            fig, ax = plt.subplots(figsize=(8.5,4.5), layout="constrained")
            for field, label, color in series:
                ax.plot(ks, [s[field] for s in summary], marker="o", label=label, color=color, linewidth=2)
            if filename == "retrieval_quality.png":
                ax.fill_between(ks, [s["F1 CI Low %"] for s in summary], [s["F1 CI High %"] for s in summary], color="#315fce", alpha=.12, label="F1 bootstrap 95% interval")
            ax.axvline(best, color="#6d7280", linestyle="--", alpha=.6, label=f"Best observed F1: K={best}")
            ax.set(title=title,xlabel="Top-K chunks",ylabel=ylabel,xticks=ks)
            ax.set_ylim(bottom=0,top=100 if percent else None)
            ax.grid(alpha=.2)
            ax.legend(fontsize=9)
            fig.savefig(output/filename,dpi=160)
            plt.close(fig)
        fig, ax = plt.subplots(figsize=(8.5,4.5),layout="constrained")
        for kind, label in (("single_document","Single-document (10)"),("multi_document","Multi-document (5)"),("partially_answerable","Supported parts (2)")):
            values = [100*statistics.mean(r["retrieval_f1"] for r in rows if r["k"]==k and r["question_type"]==kind) for k in ks]
            ax.plot(ks,values,marker="o",label=label)
        ax.set(title="F1 by question category",xlabel="Top-K chunks",ylabel="Mean chunk F1 %",xticks=ks,ylim=(0,100))
        ax.grid(alpha=.2);ax.legend()
        fig.savefig(output/"category_f1.png",dpi=160);plt.close(fig)

    def figure(filename, caption):
        encoded = base64.b64encode((output/filename).read_bytes()).decode()
        return f'<figure><img src="data:image/png;base64,{encoded}" alt="{esc(caption)}"><figcaption>{esc(caption)}</figcaption></figure>'
    def table(items):
        return '<div class="scroll"><table><thead><tr>' + ''.join(f'<th>{esc(k)}</th>' for k in items[0]) + '</tr></thead><tbody>' + ''.join(('<tr class="best">' if item.get('K')==best else '<tr>') + ''.join(f'<td>{v:.2f}</td>' if isinstance(v,float) else f'<td>{esc(v)}</td>' for v in item.values()) + '</tr>' for item in items) + '</tbody></table></div>'
    page = ['<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Top-K evidence comparison</title><style>']
    page.append('body{margin:0;background:#f2f5fa;color:#17283e;font:16px/1.6 system-ui}main{max-width:1250px;margin:auto;padding:28px}header,.panel,details.question{background:white;border:1px solid #dce4ef;border-radius:12px;padding:22px;margin:16px 0}h1{font-size:36px;line-height:1.2}h2{margin-top:32px}.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.card{padding:18px;background:#eaf0fa;border-radius:10px}.card b{display:block;font-size:32px}.notice{background:#fff2d9;border-left:4px solid #b47c16;padding:18px;margin:18px 0}.graphs{display:grid;grid-template-columns:1fr 1fr;gap:16px}figure{margin:0;background:white;padding:12px;border:1px solid #dce4ef;border-radius:12px}img{width:100%;height:auto}figcaption,small{font-size:13px;color:#50637a}.wide{grid-column:1/-1}.scroll{overflow:auto}table{border-collapse:collapse;width:100%;font-size:13px}td,th{padding:10px;border-bottom:1px solid #dce4ef;white-space:nowrap;text-align:left}th{background:#eaf0fa}.best{background:#e2f3ec}summary{cursor:pointer;font-weight:650}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:14px/1.6 system-ui}details.case{background:#f3f6fc;margin:12px 0;padding:16px;border-radius:8px}details.chunk{padding:10px;border-bottom:1px solid #dce4ef}.error{color:#9b271f}input,select{font:inherit;padding:10px;margin:6px;border:1px solid #bbc9d9;border-radius:6px}a{color:#315fce}[hidden]{display:none!important}@media(max-width:800px){main{padding:14px}.graphs{grid-template-columns:1fr}.cards{grid-template-columns:1fr 1fr}h1{font-size:28px}}')
    page.append('</style></head><body><main>')
    page.append(f'<header><small>AZURE RETRIEVAL · SAME QUESTIONS · K=1–10</small><h1>Which chunk count gives the best retrieval?</h1><p>Compare relevant evidence against extra context. This report evaluates retrieved chunks; GPT answers are included for inspection.</p><div class="cards"><div class="card"><b>K={best}</b>Best observed chunk F1</div><div class="card"><b>{selected["Retrieval F1 %"]:.1f}%</b>Mean F1 at K={best}</div><div class="card"><b>200</b>Captured question/K cases</div><div class="card"><b>{sum(r["status"]!="success" for r in rows)}</b>Generation request errors</div></div></header>')
    page.append(f'<div class="panel"><h2>Measured recommendation: K={best}</h2><p>{esc(decision["rule"])} At this K: precision {selected["Precision %"]:.1f}%, chunk recall {selected["Chunk Recall %"]:.1f}%, required-evidence coverage {selected["Evidence Recall %"]:.1f}%, and complete evidence on {selected["Complete Evidence %"]:.1f}% of the 17 supported questions.</p><p>The strongest evidence-coverage/precision tradeoff is K={decision["coverage_k"]}. Use the coverage graph to see what a larger K adds. Production K remains 4.</p></div>')
    low, high = decision["paired_difference_ci95_pp"]
    page.append(f'<div class="panel"><p><b>Comparison with production K=4:</b> the observed mean F1 difference is {decision["f1_difference_vs_k4_pp"]:+.2f} percentage points; paired bootstrap 95% interval [{low:+.2f}, {high:+.2f}]. An interval including zero does not establish a reliable improvement. This is an exploratory comparison on the same questions used to select K.</p></div>')
    page.append('<div class="notice"><b>What “best” means here:</b> highest measured F1 on these 20 fixed questions using source-quote labels. This is an exploratory comparison, not a universal optimum or an answer-quality score. Labels were authored after the run; new held-out questions should confirm the choice. Overlapping bootstrap intervals do not establish a significant winner. Three out-of-scope questions are shown separately, not counted as zero-recall questions.</div>')
    page.append('<div class="graphs"><div class="wide">' + figure('retrieval_quality.png','Macro means on 17 supported questions: 10 single-document, 5 multi-document and 2 supported parts. The interval resamples whole questions, preserving their pairing across K.') + '</div>')
    for filename, caption in [('evidence_coverage.png','Evidence coverage measures unique required facts; complete evidence requires every labeled group.'),('category_f1.png','Category means prevent one overall score from hiding differences between question types.'),('context_cost.png','Approximate tokens = sum of ceil(content characters / 4), excluding prompt overhead.'),('response_time.png','One observation per question/K; includes generation errors. These are noisy pipeline timings, not pure Search latency.'),('negative_context.png','Returning context to an unsupported question is a noise measure; it does not prove that the generated answer hallucinated.')]:
        page.append(figure(filename,caption))
    page.append('</div><h2>All K values</h2><div class="panel">' + table(summary) + '</div>')
    page.append('<h2>How the measurements work</h2><div class="panel"><ul><li>Precision = retrieved labeled-relevant chunks / actual retrieved chunks.</li><li>Chunk recall = retrieved labeled-relevant chunk IDs / all labeled-relevant chunk IDs in the full selected source corpus.</li><li>F1 = harmonic mean of precision and chunk recall, calculated per question and then averaged. The mean F1 is not necessarily the harmonic mean of the displayed aggregate means.</li><li>Evidence recall = required evidence groups found / all required groups. This is the previous experiment’s evidence-coverage proxy.</li><li>A chunk is relevant if it contains every quoted phrase in at least one group, on the specified PDF page. Unicode, case and whitespace are normalized. Scores and headings do not determine relevance.</li><li>Partial questions measure only their supported parts. Missing job-placement rates and tool-user counts remain explicitly unsupported.</li><li>Generation failures do not remove successful retrievals from the metrics. Answers are not automatically graded.</li><li>F1 intervals use 2,000 seeded bootstrap samples of the 17 whole questions. They express sample uncertainty, not label correctness or repeated-request variability.</li></ul><p>Literal labels can miss paraphrases and relevant alternatives in other passages. They measure this audited label set, not exhaustive semantic relevance. No new held-out set or repeated generation experiment was run.</p></div>')
    page.append('<h2>Questions, evidence labels and captured answers</h2><label>Question type <select id="type"><option value="">All types</option><option>single_document</option><option>multi_document</option><option>out_of_scope</option><option>partially_answerable</option></select></label><label>Search <input id="search" type="search" placeholder="Question ID or text"></label><p id="count">20 questions shown</p>')
    for q in questions:
        page.append(f'<details class="question" data-type="{esc(q["type"])}"><summary>{esc(q["id"])} · {esc(q["question"])}</summary><p>{esc(q["type"])}</p>')
        if q.get('unsupported_part'): page.append('<p><b>Unsupported part:</b> '+esc(q['unsupported_part'])+'</p>')
        if q['evidence_groups']:
            page.append('<details><summary>Audit expected evidence and source pages</summary><ol>')
            for group in q['evidence_groups']:
                page.append(f'<li>{esc(group["file_name"])} · page {group["source_page"]}<pre>{esc("; ".join(group["all_terms"]))}</pre></li>')
            page.append('</ol></details>')
        else: page.append('<p>'+esc(q['unanswerable_reason'])+'</p>')
        for r in [r for r in rows if r['question_id']==q['id']]:
            metrics = f'Precision {r["precision"]:.1%} · recall {r["chunk_recall"]:.1%} · F1 {r["retrieval_f1"]:.1%} · evidence coverage {r["evidence_recall"]:.1%}' if r['answerable'] else 'Unsupported question: no precision/recall/F1 assigned'
            page.append(f'<details class="case"><summary>K={r["k"]} · {r["retrieved_count"]} chunks · {esc(r["status"])} · {r["response_ms"]/1000:.2f} s</summary><p>{esc(metrics)}</p><h3>Captured GPT answer</h3><pre>{esc(r["answer"] if r["answer"] is not None else "No answer captured")}</pre>')
            if r['error']: page.append('<p class="error">'+esc(r['error'])+'</p>')
            for rank,(chunk,matches) in enumerate(zip(r['retrieved_chunks'],r['chunk_evidence_groups']),1):
                label = 'matches groups '+','.join(str(i+1) for i in matches) if matches else 'no labeled evidence match'
                page.append(f'<details class="chunk"><summary>Rank {rank} · {esc(chunk["file_name"])} · pages {esc(chunk["page_numbers"])} · {esc(label)}</summary><p>ID: {esc(chunk["chunk_id"])} · score: {esc(chunk.get("score"))}</p><pre>{esc(chunk["content"])}</pre></details>')
            page.append('</details>')
        page.append('</details>')
    page.append('<h2>Provenance and files</h2><div class="panel"><p>'+esc(metadata['provenance'])+'</p><p>Corpus: '+str(metadata['corpus_chunks'])+' chunks across 10 PDFs. Generated '+esc(metadata['generated_utc'])+'. Label SHA-256: '+esc(metadata['label_sha256'])+'</p><p><a href="retrieval_results.csv">Per-case metrics CSV</a> · <a href="retrieval_summary.csv">Summary CSV</a> · <a href="analysis_status.json">Analysis metadata</a></p><p>Rebuild this report offline:</p><pre>./.venv/Scripts/python.exe testing/top_k/evaluate_top_k.py --analyze-saved</pre></div>')
    page.append("""<script>function filter(){const type=document.getElementById('type').value;const text=document.getElementById('search').value.toLowerCase();let count=0;document.querySelectorAll('details.question').forEach(q=>{const show=(!type||q.dataset.type===type)&&(!text||q.querySelector('summary').textContent.toLowerCase().includes(text));q.hidden=!show;if(show)count++;});document.getElementById('count').textContent=count+' questions shown';}document.querySelectorAll('input,select').forEach(e=>e.addEventListener('input',filter));</script></main></body></html>""")
    (output/'top_k_report.html').write_text(''.join(page),encoding='utf-8')
