// Lightweight UI regression checks: node frontend/test_ui.cjs
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync("frontend/app.js", "utf8");

function makeElement() {
    return {
        value: "", textContent: "", hidden: true, disabled: false, className: "", children: [],
        files: [{name: "guide.pdf"}], events: {}, attributes: {},
        addEventListener(name, handler) { this.events[name] = handler; },
        append(...items) { this.children.push(...items); },
        replaceChildren(...items) { this.children = items; },
        setAttribute(name, value) { this.attributes[name] = value; },
    };
}
const jsonResponse = (data, ok = true) => ({ok, headers: {get: () => "application/json"}, json: async () => data});

async function createUi(storedTheme = "auto") {
    const elements = new Map();
    const element = id => {
        if (!elements.has(id)) elements.set(id, makeElement());
        return elements.get(id);
    };
    const calls = [];
    const storage = new Map([["qds-theme", storedTheme]]);
    let records = [{document_id: "doc-one", file_name: "guide.pdf", status: "Ready"},
        {document_id: "doc-two", file_name: "processing.pdf", status: "Embedding"}];
    let response = jsonResponse({});
    const document = {documentElement: {dataset: {}}, getElementById: element, createElement: makeElement};
    const context = vm.createContext({
        document, localStorage: {getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value)},
        window: {confirm: () => true}, FormData: class {append() {}}, TextDecoder, console: {error() {}},
        fetch: async (url, options = {}) => {
            calls.push({url, options});
            if (url.endsWith("/documents")) return jsonResponse(records);
            if (options.method === "DELETE") {
                records = records.filter(item => !url.endsWith(item.document_id));
                return jsonResponse({status: "Deleted", message: "Document deleted.", blob_cleanup: {original: "deleted"}});
            }
            return response;
        },
    });
    vm.runInContext(source, context);
    await new Promise(resolve => setImmediate(resolve));
    return {element, context, document, calls, storage, setResponse: value => response = value,
        setRecords: value => records = value};
}

(async () => {
    const ui = await createUi("dark");
    const {element, context} = ui;
    assert.ok(ui.calls[0].url.endsWith("/documents"));
    assert.equal(ui.calls[0].options.cache, "no-store");
    assert.equal(element("documentList").children.length, 2);
    assert.equal(element("documentList").children[1].children[1].children[1].disabled, true);
    const actions = element("documentList").children[0].children[1];
    assert.equal(actions.className, "document-actions");
    assert.equal(actions.children.length, 2);
    const view = actions.children[0];
    assert.equal(view.href, "http://127.0.0.1:8000/documents/doc-one/view");
    assert.equal(view.target, "_blank");
    assert.equal(view.rel, "noopener noreferrer");
    assert.equal(view.title, "View document");
    assert.match(view.attributes["aria-label"], /guide.pdf/);
    const trash = actions.children[1];
    assert.equal(trash.title, "Delete document");
    assert.match(trash.attributes["aria-label"], /Delete guide.pdf/);
    assert.equal(trash.children[0].attributes["aria-hidden"], "true");
    assert.equal(trash.textContent, "");
    assert.match(trash.children[0].innerHTML, /^<svg /);
    assert.match(view.children[0].innerHTML, /^<svg /);
    assert.equal(ui.document.documentElement.dataset.theme, "dark");
    for (const theme of ["light", "dark", "auto"]) {
        element("themeSelect").value = theme;
        element("themeSelect").events.change();
        assert.equal(ui.document.documentElement.dataset.theme, theme);
        assert.equal(ui.storage.get("qds-theme"), theme);
    }
    assert.equal((await createUi("unrecognized")).document.documentElement.dataset.theme, "auto");
    for (const [status, percent] of [["Valid", 15], ["Extracting", 30], ["Chunking", 50], ["Embedding", 70], ["Indexing", 90], ["Ready", 100]]) {
        vm.runInContext(`showUpdate({status: '${status}', document_id: 'doc-one'})`, context);
        assert.equal(element("uploadProgress").value, percent);
    }
    assert.equal(element("questionPanel").hidden, false);
    vm.runInContext("showUpdate({status: 'Chunking', progress: 56})", context);
    assert.equal(element("uploadProgress").value, 56);
    vm.runInContext("showUpdate({status: 'Failed', failed_stage: 'Embedding', error_reason: 'Useful error'})", context);
    assert.equal(element("uploadProgress").value, 56);
    assert.equal(element("currentStage").textContent, "Embedding — Failed");
    assert.equal(element("result").textContent, "Useful error");

    // Select an existing ready document and query exactly its ID.
    element("documentList").children[0].children[0].events.click();
    element("questionInput").value = "What is in this document?";
    ui.setResponse(jsonResponse({document_id: "doc-one", results: [{score: 0.8, page_numbers: [1], section_heading: "<script>text only</script>", content: "x".repeat(600)}]}));
    await element("questionForm").events.submit({preventDefault() {}});
    const ask = ui.calls.find(call => call.url.endsWith("/ask"));
    assert.equal(JSON.parse(ask.options.body).document_id, "doc-one");
    assert.equal(element("retrievalResults").children[0].children[1].textContent.length, 501);

    // Render the returned count and preserve Search ranking without a UI cap.
    for (const count of [4, 8, 10]) {
        ui.setResponse(jsonResponse({document_id: "doc-one", results: Array.from({length: count}, (_, i) => ({
            score: 1 - i / 20, page_numbers: [i + 1], content: `Result ${i + 1}`
        }))}));
        await element("questionForm").events.submit({preventDefault() {}});
        assert.equal(element("retrievalResults").children.length, count);
        element("retrievalResults").children.forEach((item, i) => {
            assert.equal(item.children[1].textContent, `Result ${i + 1}`);
        });
    }

    // Delete resets the selected document and refreshes the metadata list.
    await element("documentList").children[0].children[1].children[1].events.click();
    assert.ok(ui.calls.some(call => call.options.method === "DELETE" && call.url.endsWith("/doc-one")));
    assert.equal(element("questionPanel").hidden, true);
    assert.equal(element("documentList").children.length, 1);

    // Real NDJSON boundaries, including the final line without a newline.
    const bytes = new TextEncoder().encode(["Extracting", "Chunking", "Embedding", "Indexing", "Ready"]
        .map(status => JSON.stringify({status, document_id: "fresh-upload", file_name: "guide.pdf", error_reason: null})).join("\r\n"));
    let offset = 0;
    ui.setResponse({ok: true, headers: {get: () => "application/x-ndjson"}, body: {getReader: () => ({read: async () => offset >= bytes.length ? {done: true} : {done: false, value: bytes.slice(offset, offset += 7)}})}});
    await element("uploadButton").events.click();
    assert.equal(element("uploadProgress").value, 100);
    assert.equal(element("currentStage").textContent, "Ready");
    assert.equal(vm.runInContext("currentDocumentId", context), "fresh-upload");
    assert.equal(element("uploadButton").disabled, false);
    // A fresh backend list excludes an original removed through the Azure portal.
    ui.setRecords([]);
    await element("refreshDocuments").events.click();
    assert.equal(element("documentList").children.length, 0);
    assert.equal(ui.calls.at(-1).options.cache, "no-store");
    console.log("PASS: automatic list, safe delete/selection reset, theme persistence, all progress stages, failure freeze, question retrieval, and streamed upload regression.");
})().catch(error => { console.error(error); process.exitCode = 1; });
