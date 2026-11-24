const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

function renderNotes(notes) {
	const frappe = {
		pages: { "notas-entrega": {} },
		utils: { escape_html: (value) => String(value).replaceAll("&", "&amp;").replaceAll('"', "&quot;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll("'", "&#39;") },
	};
	const context = vm.createContext({ frappe });
	vm.runInContext(
		readFileSync(join(__dirname, "..", "cima_ve", "cima_ve", "page", "notas_entrega", "notas_entrega.js"), "utf8"),
		context
	);
	const page = vm.runInContext("Object.create(CimaNotasEntrega.prototype)", context);
	let html = "";
	page.wrapper = { find: () => ({ html: (value) => { html = value; } }) };
	page.data = { clients: [{ customer: "Customer", customer_name: "Customer", notes }] };
	page.renderTable();
	return html;
}

function note(name, status, proof, history = []) {
	return {
		name,
		ve_ne_estado: status,
		ve_ne_comprobante_url: proof,
		payment_history: history,
	};
}

test("delivery note payment proofs are user-activated links and duplicate or unsafe URLs are omitted", () => {
	const localProof = "/private/files/proof%20one.png?download=1&source=note";
	const historyProof = "https://cdn.example.test/payments/proof%20two.jpg";
	const html = renderNotes([
		note("PENDING-1", "Pendiente", localProof),
		note("PAID-1", "Pagada", "", [{ amount: 25, method: "Zelle", screenshot_url: historyProof }]),
		note("PAID-2", "Pagada", localProof, [{ amount: 10, method: "Pago Móvil", screenshot_url: localProof }]),
		note("PAID-3", "Pagada", "", [{ amount: 5, method: "Tarjeta", screenshot_url: "http://legacy.example.test/proof.pdf" }]),
		note("PENDING-2", "Pendiente", "javascript:alert(1)"),
		note("PENDING-3", "Pendiente", ""),
	]);

	const links = [...html.matchAll(/<a\b[^>]*>(.*?)<\/a>/g)].filter(([, label]) => /Ver (?:captura|comprobante)/.test(label));
	assert.equal(links.length, 4);
	assert.ok(links.some(([link]) => link.includes('href="/private/files/proof%20one.png?download=1&amp;source=note"')));
	assert.ok(links.some(([link]) => link.includes('href="https://cdn.example.test/payments/proof%20two.jpg"')));
	assert.ok(links.some(([link]) => link.includes('href="http://legacy.example.test/proof.pdf"')));
	assert.equal(links.filter(([link]) => link.includes("proof%20one.png")).length, 2);
	const duplicateNoteRow = html.match(/<tr>(?:(?!<\/tr>)[\s\S])*PAID-2(?:(?!<\/tr>)[\s\S])*<\/tr>/)?.[0] || "";
	assert.equal((duplicateNoteRow.match(/Ver captura/g) || []).length, 1);
	assert.ok(links.every(([link]) => link.includes('target="_blank"') && link.includes('rel="noopener noreferrer"')));
	assert.doesNotMatch(html, /<(?:img|iframe|object|embed)\b/i);
	assert.doesNotMatch(html, /javascript:/i);
});