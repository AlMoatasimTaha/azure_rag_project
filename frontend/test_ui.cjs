// Offline DOM, network, and clock fixtures; never calls the running backend.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync("frontend/app.js", "utf8");
const html = fs.readFileSync("frontend/index.html", "utf8");
const json = (data, ok = true) => ({ok, status: ok ? 200 : 502, headers: {get: () => "application/json"}, json: async () => data});
const turn = () => new Promise(resolve => setImmediate(resolve));
function makeElement() {
    return {value: "", textContent: "", hidden: true, disabled: false, className: "", children: [], files: [], events: {}, attributes: {},
        addEventListener(name, callback) { this.events[name] = callback; },
        append(...items) { for (const item of items) { item.parent = this; this.children.push(item); } },
        replaceChildren(...items) { this.children = []; this.append(...items); },
        setAttribute(name, value) { this.attributes[name] = value; },
        removeAttribute(name) { delete this.attributes[name]; delete this[name]; },
        remove() { if (this.parent) this.parent.children = this.parent.children.filter(item => item !== this); },
        focus() {}, showModal() { this.open = true; }, close() { this.open = false; this.events.close?.(); },
    };
}
async function createUi(storage = new Map()) {
    const elements = new Map();
    const element = id => { if (!elements.has(id)) elements.set(id, makeElement()); return elements.get(id); };
    let records = [{document_id: "one", file_name: "same.pdf", status: "Ready"}, {document_id: "two", file_name: "same.pdf", status: "Ready"}];
    let now = 1700000000000, timerId = 0, confirmation = true, failedId = null, response = json({}), viewResponse = null;
    const calls = [], revoked = [], timers = new Map(), session = new Map([["qds-notifications", "old private answer"]]);
    const dialog = element("deleteConfirmation");
    dialog.showModal = () => { dialog.open = true; if (confirmation !== null) queueMicrotask(() => { dialog.returnValue = confirmation ? "confirm" : "cancel"; dialog.close(); }); };
    const document = {visibilityState: "visible", events: {}, addEventListener(name, handler) { this.events[name] = handler; },
        documentElement: {dataset: {}}, getElementById: element, createElement: makeElement};
    const context = vm.createContext({document, Date: class extends Date { static now() { return now; } },
        localStorage: {getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value)},
        sessionStorage: {removeItem: key => session.delete(key)},
        setTimeout: (callback, delay) => { timers.set(++timerId, {at: now + delay, callback}); return timerId; },
        clearTimeout: id => timers.delete(id),
        URL: {createObjectURL: () => `blob:test-${calls.length}`, revokeObjectURL: url => revoked.push(url)},
        FormData: class {constructor() { this.entries = []; } append(...args) { this.entries.push(args); }}, TextDecoder, TypeError,
        fetch: async (url, options = {}) => {
            calls.push({url, options});
            if (url.endsWith("/documents/delete-batch")) {
                const {document_ids} = JSON.parse(options.body);
                assert.ok(Array.isArray(document_ids));
                const results = document_ids.map(document_id => ({document_id, status: document_id === failedId ? "Failed" : "Deleted", message: "Cleanup could not finish. Please retry."}));
                records = records.filter(item => !results.some(r => r.document_id === item.document_id && r.status === "Deleted"));
                return json({results});
            }
            if (url.endsWith("/documents/capacity")) {
                const active = records.filter(r => !["Failed", "Invalid", "FAILED_CLEANUP", "PENDING_CLEANUP", "Deleted"].includes(r.status));
                return json({current_count: active.length, remaining_slots: Math.max(0, 20 - active.length)});
            }
            if (url.endsWith("/documents")) return json(records);
            if (url.endsWith("/view")) return viewResponse ? viewResponse() : {ok: true, headers: {get: () => "application/pdf"}, blob: async () => ({type: "application/pdf"})};
            return typeof response === "function" ? response() : response;
        },
    });
    vm.runInContext(source, context);
    await turn();
    function advance(ms) {
        const target = now + ms;
        while (true) {
            const next = [...timers].filter(([,t]) => t.at <= target).sort((a,b) => a[1].at - b[1].at)[0];
            if (!next) break;
            now = next[1].at; timers.delete(next[0]); next[1].callback();
        }
        now = target;
    }
    return {element, context, document, calls, storage, session, revoked, advance,
        setRecords: value => records = value, setResponse: value => response = value,
        setConfirm: value => confirmation = value, setFailedId: value => failedId = value, setViewResponse: value => viewResponse = value,
        select(index, checked = true) { const checkbox = element("documentList").children[index].children[0]; checkbox.checked = checked; checkbox.events.change(); },
        selectAll(checked = true) { element("selectAllDocuments").checked = checked; element("selectAllDocuments").events.change(); },
        reload: () => element("refreshDocuments").events.click(),
        notify(kind, reason = "message", subject = "Safe fixture") {
            context.fixture = {kind, reason, ...(kind === "llm" ? {question: subject} : {filename: subject})};
            vm.runInContext("addNotification(fixture)", context);
        },
    };
}
const persisted = ui => JSON.parse(ui.storage.get("qds-notifications-v2"));
const historyText = ui => ui.element("notificationList").children.map(row => row.children[1].children[1].textContent).join("\n");
(async () => {
    assert.doesNotMatch(html, /id="deleteAll"|>Delete All</);
    assert.match(html, /Confirm Delete/);
    const ui = await createUi(new Map([["qds-theme", "dark"]]));
    assert.equal(ui.document.documentElement.dataset.theme, "dark");
    assert.equal(ui.element("documentList").children.length, 2);
    assert.equal(ui.element("deleteSelected").disabled, true);
    assert.equal(ui.element("viewSelected").disabled, true);
    assert.equal(ui.session.has("qds-notifications"), false);
    for (const kind of ["success", "failed", "llm", "processed"]) ui.notify(kind);
    assert.equal(ui.element("notificationCount").textContent, "4");
    assert.equal(ui.element("notificationToast").children.length, 3);
    assert.equal(ui.element("notificationToast").children[0].children[0].attributes.role, "status");
    assert.equal(ui.element("notificationToast").children[1].children[0].attributes.role, "alert");
    ui.advance(4999);
    assert.equal(ui.element("notificationToast").children.length, 3);
    ui.advance(1);
    assert.equal(ui.element("notificationToast").children.length, 1); // queued fourth starts now
    ui.advance(4999);
    assert.equal(ui.element("notificationToast").children.length, 1);
    ui.advance(1);
    assert.equal(ui.element("notificationToast").hidden, true);
    assert.equal(ui.element("notificationCount").textContent, "4");
    ui.notify("failed", "maximum 2 pages allowed.", "personal-name.pdf");
    ui.element("notificationToast").children[0].children[1].events.click();
    assert.equal(ui.element("notificationToast").hidden, true);
    assert.equal(ui.element("notificationCount").textContent, "5"); // X is not a read action
    assert.match(historyText(ui), /maximum 2 pages/);
    assert.equal(persisted(ui).at(-1).filename, "personal-name.pdf");
    assert.match(ui.element("notificationList").children[0].children[1].children[0].textContent, /personal-name.pdf/);
    ui.context.fixture = {kind: "llm", question: "What is Azure Blob Storage?", reason: "Your answer is ready.", answer: {answer: "secret answer", question: "not the asked question"}, contexts: ["secret context"], apiKey: "secret key"};
    vm.runInContext("addNotification(fixture)", ui.context);
    assert.doesNotMatch(ui.storage.get("qds-notifications-v2"), /secret answer|secret context|secret key|not the asked question/);
    assert.equal(persisted(ui).at(-1).question, "What is Azure Blob Storage?");
    const restored = await createUi(ui.storage);
    assert.equal(restored.element("notificationList").children.length, 6);
    assert.equal(restored.element("notificationCount").textContent, "6");
    assert.equal(persisted(restored).at(-1).question, "What is Azure Blob Storage?");
    assert.equal(persisted(restored).at(-2).filename, "personal-name.pdf");
    assert.equal(restored.element("notificationToast").hidden, true);
    restored.element("notificationButton").events.click();
    assert.equal(restored.element("notificationCount").textContent, "0");
    assert.ok(persisted(restored).every(item => item.read));
    restored.element("notificationPanel").events.keydown({key: "Escape"});
    restored.notify("failed", "Connection lost.");
    restored.element("notificationList").children[0].children[1].events.click();
    assert.equal(restored.element("notificationCount").textContent, "0");
    for (let index = 0; index < 110; index++) restored.notify("processed", "Processing completed.");
    assert.equal(persisted(restored).length, 100);
    assert.ok(persisted(restored).every(item => Object.keys(item).sort().join() === "code,createdAt,filename,kind,operation,question,read,reason,result"));
    restored.element("clearNotifications").events.click();
    assert.deepEqual(persisted(restored), []);
    assert.equal(restored.element("notificationCount").hidden, true);

    // Only visible active records are selectable, including actual in-progress records.
    const bulk = await createUi();
    bulk.setRecords([{document_id: "one", file_name: "same.pdf", status: "Ready"},
        {document_id: "two", file_name: "same.pdf", status: "Ready"},
        {document_id: "processing", file_name: "working.pdf", status: "Embedding"},
        {document_id: "failed", status: "Failed"}, {document_id: "pending", status: "FAILED_CLEANUP"}]);
    await bulk.reload();
    assert.equal(bulk.element("documentList").children.length, 3);
    bulk.selectAll();
    assert.equal(bulk.element("selectionCount").textContent, "3 documents selected");
    assert.equal(bulk.element("deleteSelected").disabled, false);
    assert.equal(bulk.element("viewSelected").disabled, false);
    bulk.select(2, false);
    assert.equal(bulk.element("selectAllDocuments").indeterminate, true);
    bulk.setConfirm(false);
    const beforeCancel = bulk.calls.length;
    await bulk.element("deleteSelected").events.click();
    assert.equal(bulk.calls.length, beforeCancel);
    assert.equal(bulk.element("selectionCount").textContent, "2 documents selected");
    bulk.setConfirm(true);
    bulk.setFailedId("two");
    await bulk.element("deleteSelected").events.click();
    const request = bulk.calls.find(call => call.url.endsWith("delete-batch"));
    assert.deepEqual(JSON.parse(request.options.body), {document_ids: ["one", "two"]});
    assert.equal(bulk.element("selectionCount").textContent, "1 document selected");
    assert.equal(bulk.element("documentList").children[0].children[0].checked, true);
    assert.match(bulk.element("documentsStatus").textContent, /2 \/ 20/);
    assert.match(historyText(bulk), /Deleted successfully/);
    assert.match(historyText(bulk), /Cleanup could not finish/);
    bulk.setFailedId(null);
    await bulk.element("deleteSelected").events.click();
    assert.equal(bulk.element("selectionCount").textContent, "0 documents selected");
    assert.equal(bulk.element("deleteSelected").disabled, true);
    bulk.selectAll();
    await bulk.element("deleteSelected").events.click();
    assert.deepEqual(JSON.parse(bulk.calls.filter(c => c.url.endsWith("delete-batch")).at(-1).options.body), {document_ids: ["processing"]});
    assert.equal(bulk.element("documentList").children.length, 0);
    assert.match(bulk.element("uploadCapacity").textContent, /0 \/ 20/);

    // A single viewer switches IDs; no window.open, extraction, upload or tabs.
    const viewer = await createUi();
    viewer.select(0);
    await viewer.element("viewSelected").events.click();
    assert.equal(viewer.element("documentViewer").open, true);
    assert.equal(viewer.element("viewerDocument").children.length, 1);
    assert.ok(viewer.calls.at(-1).url.endsWith("/one/view"));
    assert.equal(viewer.element("viewerFrame").hidden, false);
    viewer.element("closeViewer").events.click();
    assert.equal(viewer.element("documentViewer").open, false);
    assert.equal(viewer.revoked.length, 1);
    viewer.select(1);
    await viewer.element("viewSelected").events.click();
    assert.equal(viewer.element("viewerDocument").children.length, 2);
    viewer.element("viewerDocument").value = "two";
    await viewer.element("viewerDocument").events.change();
    assert.ok(viewer.calls.at(-1).url.endsWith("/two/view"));
    assert.ok(viewer.revoked.length >= 2);
    viewer.element("closeViewer").events.click();
    viewer.setRecords([{document_id: "docx", file_name: "unsupported.docx", status: "Ready"}]);
    await viewer.reload(); viewer.select(0);
    const beforeDocx = viewer.calls.length;
    await viewer.element("viewSelected").events.click();
    assert.equal(viewer.calls.length, beforeDocx);
    assert.match(viewer.element("viewerStatus").textContent, /cannot be previewed/);
    assert.equal(viewer.element("viewerFrame").hidden, true);
    assert.ok(viewer.calls.every(call => !["POST", "DELETE"].includes(call.options.method)));

    // PDF/size validation messages reach toasts unchanged; failures stay out of list/count.
    const uploads = await createUi();
    uploads.setRecords([]); await uploads.reload();
    for (const [name, status, message, expected] of [
        ["valid.pdf", "Ready", "Uploaded successfully", /processing completed/i],
        ["large.pdf", "Invalid", "File exceeds 10 MB.", /10 MB/],
        ["three.pdf", "Invalid", "File contains 3 pages. Maximum allowed is 2 pages.", /3 pages/],
        ["failed.pdf", "Failed", "Document extraction failed: the service rate limit was reached (HTTP 429).", /rate limit.*HTTP 429/],
    ]) {
        uploads.element("clearNotifications").events.click();
        uploads.element("fileInput").files = [{name}];
        uploads.setResponse(json({results: [{document_id: name, status, message, cleanup_pending: status === "Failed"}]}));
        await uploads.element("uploadButton").events.click();
        assert.equal(uploads.element("notificationToast").children.length, 1); // one terminal event
        assert.match(uploads.element("notificationToast").children[0].children[0].textContent, expected);
        if (status === "Failed") assert.match(uploads.element("notificationToast").children[0].children[0].textContent, /Cleanup pending/);
        assert.equal(persisted(uploads)[0].kind, status === "Ready" ? "processed" : "failed");
    }
    uploads.setRecords([{document_id: "valid", file_name: "valid.pdf", status: "Ready"}, {document_id: "failed", status: "PENDING_CLEANUP"}]);
    await uploads.reload();
    assert.equal(uploads.element("documentList").children.length, 1);
    assert.match(uploads.element("documentsStatus").textContent, /1 \/ 20/);
    uploads.element("fileInput").files = Array.from({length: 21}, () => ({name: "x.pdf"}));
    const beforeBatch = uploads.calls.length;
    await uploads.element("uploadButton").events.click();
    assert.equal(uploads.calls.length, beforeBatch);
    assert.match(uploads.element("result").textContent, /maximum of 20/);

    // Keep existing answer rendering/ranking and session question history.
    const ask = await createUi();
    ask.element("documentList").children[0].children[1].events.click();
    ask.element("questionInput").value = "private question";
    ask.setResponse(json({document_id: "one", answer: "private answer", results: [{score: .9, page_numbers: [1], content: "private passage"}]}));
    await ask.element("questionForm").events.submit({preventDefault() {}});
    assert.equal(ask.element("answerText").textContent, "private answer");
    assert.equal(ask.element("retrievalResults").children.length, 1);
    assert.equal(ask.element("historyList").children.length, 1);
    assert.equal(ask.element("notificationCount").textContent, "1");
    assert.equal(persisted(ask)[0].kind, "llm");
    assert.equal(persisted(ask)[0].code, "answer_ready");
    assert.equal(persisted(ask)[0].question, "private question");
    assert.doesNotMatch(ask.storage.get("qds-notifications-v2"), /private answer|private passage|same.pdf/);
    const stages = await createUi();
    stages.context.stageEntry = {file: {name: "fixture.pdf"}, status: "Processing"};
    vm.runInContext("applyUploadStage(stageEntry, {status:'Extracting'}); applyUploadStage(stageEntry, {status:'Extracting'}); stageEntry.status='Ready'; finishUploadEntry(stageEntry)", stages.context);
    assert.deepEqual(persisted(stages).map(item => item.kind), ["success", "processed"]);
    ask.setResponse(json({detail: "Unable to generate an answer."}, false));
    await ask.element("questionForm").events.submit({preventDefault() {}});
    assert.equal(persisted(ask).at(-1).code, "answer_failed");
    assert.equal(persisted(ask).at(-1).question, "private question");
    assert.equal(persisted(ask).at(-1).result, "failure");
    assert.match(persisted(ask).at(-1).reason, /Unable to generate an answer/);
    assert.equal(ask.element("askButton").disabled, false);

    // Each deletion keeps its own original filename, even after list removal.
    const singleDelete = await createUi();
    singleDelete.setRecords([{document_id: "islam", file_name: "Islam.pdf", status: "Ready"}]);
    await singleDelete.reload(); singleDelete.select(0);
    await singleDelete.element("deleteSelected").events.click();
    assert.equal(persisted(singleDelete).length, 1);
    assert.equal(persisted(singleDelete)[0].filename, "Islam.pdf");
    assert.equal(persisted(singleDelete)[0].operation, "delete");
    assert.equal(persisted(singleDelete)[0].result, "success");
    assert.equal(singleDelete.element("documentList").children.length, 0);
    const batchDelete = await createUi();
    const deletionFixtures = ["Azure_Cloud.pdf", "University_Syllabus.pdf", "Technology.pdf"].map((file_name, index) => ({document_id: `file-${index}`, file_name, status: "Ready"}));
    batchDelete.setRecords(deletionFixtures); await batchDelete.reload(); batchDelete.selectAll();
    batchDelete.setFailedId("file-1");
    await batchDelete.element("deleteSelected").events.click();
    assert.equal(persisted(batchDelete).length, 3);
    assert.deepEqual(persisted(batchDelete).map(item => item.filename), deletionFixtures.map(item => item.file_name));
    assert.deepEqual(persisted(batchDelete).map(item => item.result), ["success", "failure", "success"]);
    assert.ok(persisted(batchDelete).every(item => item.operation === "delete"));
    assert.equal(persisted(batchDelete)[1].reason, "Cleanup could not finish. Please retry.");
    const deletionRestored = await createUi(batchDelete.storage);
    assert.deepEqual(persisted(deletionRestored).map(item => item.filename), deletionFixtures.map(item => item.file_name));
    assert.equal(deletionRestored.element("notificationCount").textContent, "3");

    // Terminal upload messages retain exact reasons and distinct stage context.
    assert.deepEqual(persisted(stages).map(item => item.filename), ["fixture.pdf", "fixture.pdf"]);
    assert.deepEqual(persisted(stages).map(item => item.operation), ["upload", "processing"]);
    assert.match(persisted(stages)[1].reason, /Extraction, chunking, and indexing completed/);
    const extractionFailure = await createUi();
    extractionFailure.setRecords([]); await extractionFailure.reload();
    extractionFailure.element("fileInput").files = [{name: "University_Syllabus.pdf"}];
    const actualReason = "Document extraction failed: the service rate limit was reached (HTTP 429).";
    extractionFailure.setResponse(json({results: [{status: "Failed", message: actualReason, cleanup_pending: true}]}));
    await extractionFailure.element("uploadButton").events.click();
    extractionFailure.advance(5000);
    assert.equal(extractionFailure.element("notificationToast").hidden, true);
    assert.equal(extractionFailure.element("notificationCount").textContent, "1");
    const failedRestored = await createUi(extractionFailure.storage);
    assert.equal(persisted(failedRestored)[0].filename, "University_Syllabus.pdf");
    assert.equal(persisted(failedRestored)[0].operation, "extraction");
    assert.ok(persisted(failedRestored)[0].reason.includes(actualReason));
    assert.match(historyText(failedRestored), /HTTP 429/);
    const uploadBatch = await createUi();
    uploadBatch.setRecords([]); await uploadBatch.reload();
    const uploadFixtures = [
        {file_name: "Azure_Cloud.pdf", status: "Ready", message: "Uploaded successfully."},
        {file_name: "Large.pdf", status: "Invalid", message: "File exceeds 10 MB."},
        {file_name: "University_Syllabus.pdf", status: "Failed", message: "Document extraction failed: maximum 2 pages allowed."},
    ];
    uploadBatch.element("fileInput").files = uploadFixtures.map(item => ({name: item.file_name}));
    let uploadOutcomeIndex = 0;
    uploadBatch.setResponse(() => json({results: [uploadFixtures[uploadOutcomeIndex++]]}));
    await uploadBatch.element("uploadButton").events.click();
    assert.deepEqual(persisted(uploadBatch).map(item => item.filename), uploadFixtures.map(item => item.file_name));
    assert.deepEqual(persisted(uploadBatch).map(item => item.operation), ["processing", "upload", "extraction"]);
    assert.ok(persisted(uploadBatch)[2].reason.includes(uploadFixtures[2].message));
    uploadBatch.advance(5000);
    assert.equal(uploadBatch.element("notificationCount").textContent, "3");
    assert.equal(uploadBatch.element("notificationToast").hidden, true);

    // Untrusted filenames/questions remain literal text through save and reload.
    const injection = await createUi();
    const hostileFilename = '<img src=x onerror="alert(1)">.pdf';
    injection.notify("failed", "Maximum 2 pages allowed.", hostileFilename);
    const hostileQuestion = '<script>alert("question")</script> ' + "What is Azure Blob Storage? ".repeat(18);
    injection.context.fixture = {kind: "llm", question: hostileQuestion, operation: "answer", result: "success",
        reason: "Answer generated successfully.", answer: "SECRET_ANSWER", results: [{content: "SECRET_CONTEXT"}], api_key: "SECRET_KEY"};
    vm.runInContext("addNotification(fixture)", injection.context);
    const toastQuestion = injection.element("notificationToast").children[1].children[0].textContent;
    assert.ok(toastQuestion.includes("…"));
    assert.ok(!toastQuestion.includes(hostileQuestion));
    const injectionRestored = await createUi(injection.storage);
    assert.equal(persisted(injectionRestored)[1].question, hostileQuestion);
    assert.equal(persisted(injectionRestored)[0].filename, hostileFilename);
    const questionContent = injectionRestored.element("notificationList").children[0].children[1];
    assert.ok(questionContent.children[0].textContent.includes(hostileQuestion));
    assert.equal(questionContent.children[0].innerHTML, undefined);
    const filenameContent = injectionRestored.element("notificationList").children[1].children[1];
    assert.ok(filenameContent.children[0].textContent.includes(hostileFilename));
    assert.equal(filenameContent.children[0].innerHTML, undefined);
    assert.equal(questionContent.children[2].attributes.datetime, new Date(persisted(injectionRestored)[1].createdAt).toISOString());
    assert.ok(questionContent.children[2].textContent.length > 0);
    assert.doesNotMatch(injection.storage.get("qds-notifications-v2"), /SECRET_ANSWER|SECRET_CONTEXT|SECRET_KEY/);

    // Older code-only history migrates without losing its read count or date.
    const legacy = await createUi(new Map([["qds-notifications-v2", JSON.stringify([
        {kind: "failed", code: "page_limit", createdAt: 1700000000000, read: false},
        {kind: "success", code: "uploaded", createdAt: 1700000001000, read: true},
        {kind: "unknown", createdAt: 1700000001000, read: false},
    ])]]));
    assert.equal(persisted(legacy).length, 2);
    assert.equal(legacy.element("notificationCount").textContent, "1");
    assert.equal(persisted(legacy)[0].createdAt, 1700000000000);
    assert.match(historyText(legacy), /Maximum 2 pages/);

    // An answer notification uses the submitted question even if UI text changes.
    const pendingQuestion = await createUi();
    pendingQuestion.element("questionInput").value = "Explain cloud computing.";
    let finishRequest;
    pendingQuestion.setResponse(() => new Promise(resolve => { finishRequest = resolve; }));
    const pendingAsk = pendingQuestion.element("questionForm").events.submit({preventDefault() {}});
    pendingQuestion.element("questionInput").value = "A different question";
    finishRequest(json({detail: "Answer service rate limit reached. Please retry."}, false));
    await pendingAsk;
    assert.equal(persisted(pendingQuestion)[0].question, "Explain cloud computing.");
    assert.match(persisted(pendingQuestion)[0].reason, /rate limit/);

    // New entries are unread even while history is already open.
    pendingQuestion.element("notificationButton").events.click();
    assert.equal(pendingQuestion.element("notificationCount").textContent, "0");
    pendingQuestion.notify("success", "Uploaded successfully.", "New.pdf");
    assert.equal(pendingQuestion.element("notificationCount").textContent, "1");
    pendingQuestion.element("notificationToast").children.at(-1).children[1].events.click();
    assert.equal(pendingQuestion.element("notificationCount").textContent, "1");
    console.log("PASS: contextual upload/failure/extraction/processing, single/multiple deletion, LLM success/failure/question snapshot, full history and timestamps, code-only migration, literal-text injection checks, answer/context/key exclusion, and unread persistence.");
    console.log("PASS: four categories, exact 5-second queue, manual close keeps unread, safe localStorage/100-entry history, read/clear, active-only selection, confirmation/cancel, ID-based deletion/partial retry/counts, PDF viewer/switcher/DOCX fallback, upload errors and LLM regression (offline).");
})().catch(error => { console.error(error); process.exitCode = 1; });
