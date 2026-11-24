frappe.pages["notas-entrega"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: "Notas de Entrega",
		single_column: true,
	});
	new CimaNotasEntrega(page, wrapper);
};

class CimaNotasEntrega {
	constructor(page, wrapper) {
		this.page = page;
		this.wrapper = $(wrapper);
		this.data = null;
		this.notesRequest = 0;
		this.filters = {
			status: "",
			from_date: "",
			to_date: "",
			seller: "",
			search: "",
		};
		this.renderShell();
		this.bindEvents();
		this.loadNotes();
	}

	renderShell() {
		const html = `
			<div class="cima-ne-shell">
				<div class="cima-ne-hero">
					<div class="cima-hero-brand">
						<img src="/assets/cima_ve/images/cima-logo-blanco.png" alt="CIMA" class="cima-hero-logo" onerror="this.style.display='none'" />
						<div>
							<h2 id="ne-hero-title">Control de Notas de Entrega — Cima Electronic</h2>
							<p id="ne-hero-subtitle">Seguimiento de despachos pendientes de pago: registra pagos (con método e impuestos) o revierte una nota (solo admin).</p>
						</div>
					</div>
					<div class="d-flex" style="gap:8px;flex-wrap:wrap;align-items:center;">
						<button type="button" class="cima-btn-primary" data-action="go-pos">+ Nueva Nota de Entrega</button>
						<button type="button" class="cima-btn-ghost" data-action="calc-commission" id="ne-btn-calc-commission" style="display:none;">Calcular Comisiones</button>
						<button type="button" class="cima-btn-ghost" data-action="go-clients" id="ne-btn-clients" style="display:none;">Clientes y Consumo</button>
						<button type="button" class="cima-btn-ghost" data-action="refresh">Actualizar</button>
					</div>
				</div>

				<div class="cima-ne-card">
					<div class="cima-ne-filters">
						<div>
							<label class="small text-muted">Estado</label>
							<select id="ne-filter-status" class="form-control input-sm">
								<option value="">Todos</option>
								<option value="Pendiente" selected>Pendiente por Cobrar</option>
								<option value="Pago Parcial">Pago Parcial</option>
								<option value="Pagada">Pagada</option>
								<option value="Cancelada">Cancelada</option>
							</select>
						</div>
						<div id="ne-seller-filter-wrap">
							<label class="small text-muted">Vendedor</label>
							<select id="ne-filter-seller" class="form-control input-sm">
								<option value="">Todos</option>
							</select>
						</div>
						<div>
							<label class="small text-muted">Desde</label>
							<input type="date" id="ne-filter-from" class="form-control input-sm" />
						</div>
						<div>
							<label class="small text-muted">Hasta</label>
							<input type="date" id="ne-filter-to" class="form-control input-sm" />
						</div>
						<div>
							<label class="small text-muted">Buscar (N° o Cliente)</label>
							<input type="text" id="ne-filter-search" class="form-control input-sm" placeholder="MAT-DN o cliente..." />
						</div>
					</div>
				</div>

				<div class="cima-ne-kpis" id="ne-kpis"></div>

				<div id="ne-clients-container">
					<div class="cima-ne-card text-center text-muted p-4">Cargando notas de entrega...</div>
				</div>
			</div>
		`;
		this.wrapper.find(".layout-main-section").html(html);
		this.filters.status = "Pendiente";
	}

	bindEvents() {
		const root = this.wrapper;
		root.on("click", "[data-action='go-pos']", () => frappe.set_route("venta-rapida"));
		root.on("click", "[data-action='calc-commission']", () => this.openCommissionCalculator());
		root.on("click", "[data-action='go-clients']", () => frappe.set_route("clientes-consumo"));
		root.on("click", "[data-action='refresh']", () => this.loadNotes());
		root.on("click", "[data-notes-page]", (e) => this.loadNotes(Number(e.currentTarget.dataset.notesPage)));

		root.on("change", "#ne-filter-status, #ne-filter-seller, #ne-filter-from, #ne-filter-to", () => {
			this.filters.status = root.find("#ne-filter-status").val() || "";
			this.filters.seller = root.find("#ne-filter-seller").val() || "";
			this.filters.from_date = root.find("#ne-filter-from").val() || "";
			this.filters.to_date = root.find("#ne-filter-to").val() || "";
			this.loadNotes();
		});

		let timer = null;
		root.on("input", "#ne-filter-search", (e) => {
			clearTimeout(timer);
			timer = setTimeout(() => {
				this.filters.search = $(e.target).val() || "";
				this.loadNotes();
			}, 300);
		});

		root.on("click", "[data-pay-client]", (e) => {
			const cust = $(e.currentTarget).attr("data-pay-client");
			const seller = this.filters.seller || "";
			frappe.call({
				method: "cima_ve.api.delivery_notes.get_client_payment_context",
				args: { customer: cust, seller },
				freeze: true,
				freeze_message: "Cargando saldo del cliente...",
				callback: (r) => {
					if (Number(r.message?.pending_amount || 0) > 0.01) {
						this.openClientPaymentDialog(r.message, seller);
					} else {
						frappe.msgprint("Este cliente no tiene Notas de Entrega pendientes por cobrar.");
						this.loadNotes(1);
					}
				},
			});
		});

		root.on("click", "[data-doc-client]", (e) => {
			const cust = $(e.currentTarget).attr("data-doc-client");
			if (cust) this.openDocumentPreview({ customer: cust });
		});

		root.on("click", "[data-doc-dn]", (e) => {
			const dnName = $(e.currentTarget).attr("data-doc-dn");
			if (dnName) this.openDocumentPreview({ delivery_note: dnName });
		});

		root.on("click", "[data-revert-dn]", (e) => {
			const dnName = $(e.currentTarget).attr("data-revert-dn");
			const note = (this.data?.notes || []).find((n) => n.name === dnName);
			if (note) this.openRevertDialog(note);
		});
	}

	loadNotes(page = 1) {
		const request = ++this.notesRequest;
		this.wrapper.find("#ne-clients-container").html(
			`<div class="cima-ne-card text-center text-muted p-4" role="status">Cargando notas de entrega...</div>`
		);
		frappe.call({
			method: "cima_ve.api.delivery_notes.get_delivery_notes",
			args: { ...this.filters, page, page_size: 25 },
			callback: (r) => {
				if (request !== this.notesRequest) return;
				if (!r.message) {
					this.wrapper.find("#ne-clients-container").html(
						`<div class="cima-ne-card text-center text-danger p-4">No se pudieron cargar las Notas de Entrega.</div>`
					);
					return;
				}
				this.data = r.message;
				this.applyRoleUi();
				this.renderSellerFilter();
				this.renderKpis();
				this.renderTable();
			},
			error: () => {
				if (request !== this.notesRequest) return;
				this.wrapper.find("#ne-clients-container").html(
					`<div class="cima-ne-card text-center text-danger p-4">Error al consultar Notas de Entrega. Verifica tu sesión o permisos.</div>`
				);
			},
		});
	}

	applyRoleUi() {
		const isAdmin = Boolean(this.data?.config?.is_admin);
		this.wrapper.find("#ne-btn-clients").toggle(isAdmin);
		this.wrapper.find("#ne-btn-calc-commission").toggle(isAdmin);
		this.wrapper.find("#ne-seller-filter-wrap").toggle(isAdmin);
		if (isAdmin) {
			this.wrapper.find("#ne-hero-title").text("Control de Notas de Entrega — Cima Electronic");
			this.wrapper.find("#ne-hero-subtitle").text(
				"Seguimiento de despachos pendientes de pago: registra pagos (con método e impuestos) o revierte una nota (solo admin)."
			);
		} else {
			this.wrapper.find("#ne-hero-title").text("Mis Notas de Entrega");
			this.wrapper.find("#ne-hero-subtitle").text(
				"Consulta las Notas de Entrega emitidas por tu usuario y registra tus pagos con comprobante."
			);
		}
	}

	renderSellerFilter() {
		const sel = this.wrapper.find("#ne-filter-seller");
		const cur = this.filters.seller || "";
		const sellers = this.data?.config?.sellers || [];
		const opts = [`<option value="">Todos</option>`].concat(
			sellers.map((s) => `<option value="${frappe.utils.escape_html(s.user)}">${frappe.utils.escape_html(s.fullname)}</option>`)
		);
		sel.html(opts.join("")).val(cur);
	}

	renderKpis() {
		const s = this.data?.summary || {};
		const fx = this.data?.config?.exchange_rate || {};
		const fxRate = Number(fx.adjusted_rate || fx.bcv_rate || 0);
		const fxCard =
			fxRate > 0
				? `<div class="cima-ne-kpi" style="border-left:3px solid #2563eb;">
					<div class="cima-ne-kpi-label">Tasa Ajustada (BCV + Brecha ${Number(fx.applied_brecha_pct || 0).toFixed(1)}%)</div>
					<div class="cima-ne-kpi-val" style="color:#1d4ed8;">Bs. ${fxRate.toFixed(2)}</div>
					<div class="cima-ne-kpi-sub">BCV ${Number(fx.bcv_rate || 0).toFixed(2)} · Binance ${Number(fx.binance_usdt_rate || 0).toFixed(2)}</div>
				</div>`
				: "";
		this.wrapper.find("#ne-kpis").html(`
			<div class="cima-ne-kpi">
				<div class="cima-ne-kpi-label">Pendientes por Cobrar</div>
				<div class="cima-ne-kpi-val" style="color:#b45309;">$${Number(s.pending_total || 0).toFixed(2)}</div>
				<div class="cima-ne-kpi-sub">${s.pending_count || 0} notas activas${Number(s.overdue_count || 0) > 0 ? ` · <span style="color:#b91c1c;font-weight:700;">${s.overdue_count} vencidas</span>` : ""}</div>
			</div>
			<div class="cima-ne-kpi">
				<div class="cima-ne-kpi-label">Pagadas</div>
				<div class="cima-ne-kpi-val" style="color:#15803d;">$${Number(s.paid_total || 0).toFixed(2)}</div>
				<div class="cima-ne-kpi-sub">${s.paid_count || 0} notas liquidadas</div>
			</div>
			<div class="cima-ne-kpi">
				<div class="cima-ne-kpi-label">Canceladas (Stock Devuelto)</div>
				<div class="cima-ne-kpi-val" style="color:#b91c1c;">$${Number(s.cancelled_total || 0).toFixed(2)}</div>
				<div class="cima-ne-kpi-sub">${s.cancelled_count || 0} notas anuladas</div>
			</div>
			<div class="cima-ne-kpi" style="border-left:3px solid #7c3aed;">
				<div class="cima-ne-kpi-label">Comisión Vendedor (Filtro)</div>
				<div class="cima-ne-kpi-val" style="color:#6d28d9;">$${Number(s.filtered_commission || 0).toFixed(2)}</div>
				<div class="cima-ne-kpi-sub">En $: $${Number(s.filtered_commission_usd || 0).toFixed(2)} · En Bs: ${this.formatBs(s.filtered_commission_bs || 0)}</div>
			</div>
			<div class="cima-ne-kpi">
				<div class="cima-ne-kpi-label">Total Notas</div>
				<div class="cima-ne-kpi-val">${s.total_count || 0}</div>
				<div class="cima-ne-kpi-sub">Margen filtro: $${Number(s.filtered_margin || 0).toFixed(2)}</div>
			</div>
			${fxCard}
		`);
	}

	renderTable() {
		const isAdmin = Boolean(this.data?.config?.is_admin);
		const clients = this.data?.clients || [];
		const container = this.wrapper.find("#ne-clients-container");
		if (!clients.length) {
			container.html(`<div class="cima-ne-card text-center text-muted p-4">No hay Notas de Entrega para los filtros seleccionados.</div>`);
			return;
		}

		const fx = this.data?.config?.exchange_rate || {};
		const fxRate = Number(fx.adjusted_rate || fx.bcv_rate || 0);
		const renderProofLink = (url, seenProofs) => {
			const href = String(url || "").trim();
			const isLocalPath = href.startsWith("/") && !href.startsWith("//") && !href.includes("\\");
			const isHttpUrl = /^https?:\/\/[^\\/?#\s]+(?:[/?#][^\s]*)?$/i.test(href);
			if (!href || /[\u0000-\u001f\u007f]/.test(href) || (!isLocalPath && !isHttpUrl) || seenProofs.has(href)) return "";
			seenProofs.add(href);
			return ` · <a href="${frappe.utils.escape_html(href)}" target="_blank" rel="noopener noreferrer">📎 Ver captura</a>`;
		};

		const cardsHtml = clients
			.map((client) => {
				const notes = client.notes || [];
				const pendingUsd = Number(client.pending_amount || 0);
				const pendingBsHtml = fxRate > 0 && pendingUsd > 0.01 ? ` <span class="text-muted small">(${this.formatBs(pendingUsd * fxRate)})</span>` : "";
				const overdueBadge =
					Number(client.overdue_count || 0) > 0
						? `<span class="label" style="background:#fee2e2;color:#991b1b;border:1px solid #fecaca;font-size:11px;font-weight:700;">${client.overdue_count} VENCIDA(S) · $${Number(client.overdue_total || 0).toFixed(2)}</span>`
						: "";
				const canPayClient = pendingUsd > 0.01;

				const rowsHtml = notes
					.map((n) => {
						const itemsText = (n.items || [])
							.slice(0, 4)
							.map((it) => {
								const orig = Number(it.price_list_rate || 0);
								const rate = Number(it.rate || 0);
								const hasDisc = Boolean(it.has_discount || Number(it.discount_percentage || 0) > 0 || (orig > 0 && rate < orig - 0.0001));
								const discHtml = hasDisc
									? ` <del class="text-muted">$${orig.toFixed(2)}</del> <strong>$${rate.toFixed(2)}</strong> <span class="label" style="background:#fef3c7;color:#92400e;border:1px solid #fde68a;font-size:10px;">Descuento</span>`
									: ` <span class="text-muted">($${rate.toFixed(2)})</span>`;
								return `${Number(it.qty)}x ${frappe.utils.escape_html(it.item_name || it.item_code)}${discHtml}`;
							})
							.join("<br>");
						const moreItems = (n.items || []).length > 4 ? `<div class="text-muted small">+${n.items.length - 4} más</div>` : "";
						const badgeClass =
							n.ve_ne_estado === "Pagada"
								? "cima-ne-badge-pagada"
								: n.ve_ne_estado === "Pago Parcial"
								? "cima-ne-badge-parcial"
								: n.ve_ne_estado === "Cancelada"
								? "cima-ne-badge-cancelada"
								: "cima-ne-badge-pendiente";

						const seenProofs = new Set();
						const noteProofLink = renderProofLink(n.ve_ne_comprobante_url, seenProofs);
						const history = Array.isArray(n.payment_history) ? n.payment_history : [];
						const historyHtml = history.length
							? `<div class="mt-1 pt-1" style="border-top:1px dashed #cbd5e1;">` +
							  history
									.map((h, idx) => {
										const proofLink = renderProofLink(h.screenshot_url, seenProofs);
										const hBs = Number(h.amount_bs || 0) > 0 ? ` (${this.formatBs(h.amount_bs)})` : "";
										return `<div class="text-muted" style="font-size:11px;">#${idx + 1}: <strong>$${Number(h.amount || 0).toFixed(2)}${hBs}</strong> · ${frappe.utils.escape_html(h.method || "")}${h.reference ? ` (Ref: ${frappe.utils.escape_html(h.reference)})` : ""}${proofLink}</div>`;
									})
									.join("") +
							  `</div>`
							: "";

						let resolutionHtml = `<span class="text-muted small">Sin procesar</span>`;
						if (n.ve_ne_estado === "Pagada" || n.ve_ne_estado === "Pago Parcial") {
							const paidBsText = Number(n.paid_bs || 0) > 0 ? ` (${this.formatBs(n.paid_bs)})` : "";
							resolutionHtml = `
								<div class="small">
									<div>Pagado: <strong>$${Number(n.paid_amount || 0).toFixed(2)}${paidBsText}</strong> · Resta: <strong>$${Number(n.remaining_amount || 0).toFixed(2)}</strong></div>
									<strong>${frappe.utils.escape_html(n.ve_ne_metodo_pago || "-")}</strong>
									${n.ve_ne_factura && isAdmin ? `<br><a href="/app/sales-invoice/${encodeURIComponent(n.ve_ne_factura)}">${frappe.utils.escape_html(n.ve_ne_factura)}</a>` : ""}
									${historyHtml}
								</div>
							`;
						} else if (n.ve_ne_estado === "Cancelada") {
							resolutionHtml = `<div class="small text-danger"><strong>Cancelada</strong>${n.ve_ne_motivo_cancelacion ? `<br>${frappe.utils.escape_html(n.ve_ne_motivo_cancelacion)}` : ""}</div>`;
						}
						if (noteProofLink) resolutionHtml += `<div class="small">${noteProofLink}</div>`;

						const canRevert = isAdmin && n.ve_ne_estado !== "Cancelada";
						const actionsHtml = `
							<div class="cima-ne-actions">
								<button type="button" class="btn btn-xs btn-default" data-doc-dn="${frappe.utils.escape_html(n.name)}" title="Generar documento imprimible">📄 Documento</button>
								${canRevert ? `<button type="button" class="btn btn-xs btn-danger" data-revert-dn="${frappe.utils.escape_html(n.name)}">Revertir</button>` : ""}
							</div>
						`;

						let commissionCellHtml = `<span class="text-muted small">Sin comisión</span>`;
						if (n.ve_ne_estado === "Cancelada") {
							commissionCellHtml = `<span class="text-muted small">Anulada ($0.00)</span>`;
						} else if (n.commission_excluded) {
							commissionCellHtml = `<span class="text-muted small">Sin comisión</span>`;
						} else if (n.commission_option || Number(n.commission_pct || 0) > 0) {
							const optLabel = n.commission_option || `${Number(n.commission_pct || 0).toFixed(0)}%`;
							const cUsd = Number(n.commission_usd || 0);
							const cBs = Number(n.commission_bs || 0);
							const splitLine = (cUsd > 0 || cBs > 0)
								? `<div style="font-size:11px;color:#0f766e;">$${cUsd.toFixed(2)} + ${this.formatBs(cBs)}</div>`
								: "";
							commissionCellHtml = `
								<div class="small">
									<span class="label" style="background:#ede9fe;color:#5b21b6;border:1px solid #ddd6fe;font-size:10px;">${frappe.utils.escape_html(optLabel)}</span>
									<div style="color:#5b21b6;font-weight:700;margin-top:2px;">$${Number(n.commission_amount || 0).toFixed(2)}</div>
									${splitLine}
								</div>
							`;
						}

						const dueDateStr = n.ve_ne_fecha_vencimiento || "";
						const dueHtml = dueDateStr
							? `<div class="small ${n.is_overdue ? "text-danger font-weight-bold" : "text-muted"}">Vence: ${frappe.utils.escape_html(dueDateStr)}${n.is_overdue ? ` (VENCIDA ${n.days_overdue}d)` : ""}</div>`
							: "";
						const originBadge =
							n.ve_ne_origen === "Tienda Web"
								? `<div><span class="label" style="background:#dbeafe;color:#1e40af;font-size:10px;">Tienda Web</span></div>`
								: n.ve_ne_origen === "MercadoLibre" || String(n.canal_venta || "").toLowerCase().includes("mercado")
									? `<div><span class="label" style="background:#fef9c3;color:#854d0e;font-size:10px;">MercadoLibre</span></div>`
									: "";
						const noteDiscBadge = n.has_discount
							? `<div class="mt-1"><span class="label" style="background:#fef3c7;color:#92400e;border:1px solid #fde68a;font-size:10px;">Con descuento</span></div>`
							: "";
						const frozenBs = Number(n.total_bs || n.paid_bs || 0);
						const histBsHtml = n.ve_ne_estado === "Pagada" && frozenBs > 0
							? `<div class="small text-muted" title="Referencia histórica congelada">${this.formatBs(frozenBs)} <span style="font-size:10px;">(hist.)</span></div>`
							: "";

						return `
							<tr>
								<td><a href="/app/delivery-note/${encodeURIComponent(n.name)}"><strong>${frappe.utils.escape_html(n.name)}</strong></a>${originBadge}</td>
								<td>${frappe.utils.escape_html(n.posting_date || "")}${dueHtml}</td>
								<td>${frappe.utils.escape_html(n.seller_name || n.seller_user || "")}</td>
								<td class="small">${itemsText}${moreItems}${noteDiscBadge}</td>
								<td class="text-right font-weight-bold">$${Number(n.total_to_pay || n.amount || 0).toFixed(2)}${histBsHtml}<div class="small text-muted">Resta: $${Number(n.remaining_amount || 0).toFixed(2)}</div></td>
								<td>${commissionCellHtml}</td>
								<td><span class="cima-ne-badge ${badgeClass}">${frappe.utils.escape_html(n.ve_ne_estado)}</span></td>
								<td>${resolutionHtml}</td>
								<td>${actionsHtml}</td>
							</tr>
						`;
					})
					.join("");

				return `
					<div class="cima-ne-card mb-3" style="border-left:4px solid ${Number(client.overdue_count || 0) > 0 ? "#dc2626" : canPayClient ? "#2563eb" : "#16a34a"};">
						<div class="d-flex justify-content-between align-items-center flex-wrap mb-2" style="gap:10px;">
							<div>
								<h4 style="margin:0;font-weight:700;">${frappe.utils.escape_html(client.customer_name || client.customer)} ${overdueBadge}</h4>
								<div class="small text-muted">
									${client.customer_email ? `📧 ${frappe.utils.escape_html(client.customer_email)} · ` : ""}
									${client.customer_phone ? `📱 ${frappe.utils.escape_html(client.customer_phone)} · ` : ""}
									${client.total_notes_count} nota(s) (${client.pending_notes_count} pendiente(s), ${client.paid_notes_count} pagada(s))
									${client.oldest_due_date ? ` · Vence más próxima: <strong>${frappe.utils.escape_html(client.oldest_due_date)}</strong>` : ""}
								</div>
							</div>
							<div class="d-flex align-items-center flex-wrap" style="gap:10px;">
								<div class="text-right">
									<div class="small text-muted">Saldo Pendiente Cliente</div>
									<div style="font-size:17px;font-weight:800;color:${canPayClient ? "#1d4ed8" : "#15803d"};">$${pendingUsd.toFixed(2)}${pendingBsHtml}</div>
								</div>
								<button type="button" class="btn btn-sm btn-default" data-doc-client="${frappe.utils.escape_html(client.customer)}">📄 Documento Cliente</button>
								${canPayClient ? `<button type="button" class="btn btn-sm btn-success" data-pay-client="${frappe.utils.escape_html(client.customer)}">💳 Registrar Pago Cliente</button>` : `<span class="label label-success">Al día</span>`}
							</div>
						</div>
						<div class="table-responsive">
							<table class="table table-bordered table-hover table-sm mb-0">
								<thead>
									<tr>
										<th>Nota (FIFO)</th>
										<th>Fecha</th>
										<th>Vendedor</th>
										<th>Productos</th>
										<th class="text-right">Monto / Saldo</th>
										<th>Comisión ($ / Bs)</th>
										<th>Estado</th>
										<th>Pagos / Factura</th>
										<th>Acciones</th>
									</tr>
								</thead>
								<tbody>${rowsHtml}</tbody>
							</table>
						</div>
					</div>
				`;
			})
			.join("");

		const { page = 1, pages = 1, total = 0 } = this.data?.pagination || {};
		const pagination = pages > 1 ? `
			<nav class="d-flex justify-content-center align-items-center flex-wrap mb-3" style="gap:10px;" aria-label="Páginas de Notas de Entrega">
				<button type="button" class="btn btn-sm btn-default" data-notes-page="${page - 1}" ${page <= 1 ? "disabled" : ""}>Anterior</button>
				<span class="text-muted" aria-live="polite">Página ${page} de ${pages} · ${total} notas</span>
				<button type="button" class="btn btn-sm btn-default" data-notes-page="${page + 1}" ${page >= pages ? "disabled" : ""}>Siguiente</button>
			</nav>` : "";
		container.html(pagination + cardsHtml);
	}

	formatBs(amount) {
		return `Bs. ${Number(amount || 0).toLocaleString("es-VE", {
			minimumFractionDigits: 2,
			maximumFractionDigits: 2,
		})}`;
	}

	openClientPaymentDialog(client, seller) {
		const cfg = this.data?.config || {};
		const fx = cfg.exchange_rate || {};
		const fxRate = Number(fx.adjusted_rate || fx.bcv_rate || 0);
		const ivaRate = Number(cfg.iva_rate || 16);
		const igtfRate = Number(cfg.igtf_rate || 3);
		const methods = cfg.payment_methods || ["Efectivo USD", "Pago Móvil / Bs", "Zelle", "Binance Pay"];
		const methodCurrencies = cfg.payment_method_currencies || {};
		const pendingNotes = (client.notes || []).filter(
			(n) => (n.ve_ne_estado === "Pendiente" || n.ve_ne_estado === "Pago Parcial") && Number(n.remaining_amount || 0) > 0.01
		);
		const totalPending = Number(client.pending_amount || 0);

		const d = new frappe.ui.Dialog({
			title: `Registrar Pago Cliente — ${client.customer_name || client.customer}`,
			size: "large",
			fields: [
				{
					fieldtype: "HTML",
					fieldname: "client_info",
					options: `<div class="mb-2"><strong>Cliente:</strong> ${frappe.utils.escape_html(client.customer_name || client.customer)} · <strong>Saldo pendiente total:</strong> $${totalPending.toFixed(2)} · <strong>${pendingNotes.length} nota(s) pendientes</strong> (se aplicará a las más antiguas primero).</div>`,
				},
				{
					label: "Método de Pago",
					fieldname: "payment_method",
					fieldtype: "Select",
					options: methods.join("\n"),
					default: methods[0],
					reqd: 1,
				},
				{
					label: `Aplica IVA (${ivaRate}%)`,
					fieldname: "apply_iva",
					fieldtype: "Check",
					default: 0,
				},
				{
					label: `Aplica IGTF (${igtfRate}% Divisas)`,
					fieldname: "apply_igtf",
					fieldtype: "Check",
					default: 0,
				},
				{
					label: "Monto pagado por el cliente ($)",
					fieldname: "paid_amount",
					fieldtype: "Float",
					reqd: 1,
					default: totalPending,
				},
				{
					label: "Número de Referencia",
					fieldname: "reference",
					fieldtype: "Data",
					description: "Número de referencia o confirmación del pago (no aplica para efectivo).",
				},
				{
					fieldtype: "HTML",
					fieldname: "screenshot_box",
					options: `
						<div class="form-group" id="ne-screenshot-group">
							<label class="control-label small">Foto / Captura de Pantalla / Comprobante</label>
							<input type="file" id="ne-payment-screenshot" class="form-control input-sm" accept="image/*,.pdf" />
							<div id="ne-screenshot-preview" class="small text-muted mt-1"></div>
						</div>
					`,
				},
				{
					fieldtype: "HTML",
					fieldname: "totals_preview",
					options: "",
				},
			],
			primary_action_label: "Registrar Pago (FIFO)",
			primary_action: async (values) => {
				const payAmt = Number(values.paid_amount);
				if (isNaN(payAmt) || payAmt <= 0) {
					frappe.msgprint("Ingresa un monto válido mayor que 0.");
					return;
				}
				const isCash = isCashMethod(values.payment_method);
				let screenshotUrl = "";
				const fileInput = d.$wrapper.find("#ne-payment-screenshot")[0];
				const file = fileInput && fileInput.files && fileInput.files[0];
				if (file && pendingNotes.length) {
					if (file.size > 5 * 1024 * 1024) {
						frappe.msgprint("El comprobante supera el tamaño máximo permitido de 5 MB.");
						return;
					}
					try {
						const b64 = await new Promise((resolve, reject) => {
							const reader = new FileReader();
							reader.onload = () => resolve(String(reader.result || ""));
							reader.onerror = () => reject(new Error("read_error"));
							reader.readAsDataURL(file);
						});
						const up = await frappe.call({
							method: "cima_ve.api.delivery_notes.upload_delivery_note_payment_proof",
							args: {
								delivery_note: pendingNotes[0].name,
								file_name: file.name,
								file_data_base64: b64,
							},
						});
						if (up && up.message && up.message.file_url) {
							screenshotUrl = up.message.file_url;
						}
					} catch (err) {
						const detail = err?.message || err?._server_messages || "";
						frappe.msgprint(`Error al subir la captura.${detail ? ` (${frappe.utils.escape_html(String(detail))})` : ""}`);
						return;
					}
				}
				frappe.call({
					method: "cima_ve.api.delivery_notes.register_client_payment",
					args: {
						customer: client.customer,
						payment_method: values.payment_method,
						paid_amount: payAmt,
						apply_iva: values.apply_iva ? 1 : 0,
						apply_igtf: values.apply_igtf ? 1 : 0,
						reference: isCash ? "" : values.reference || "",
						screenshot_url: screenshotUrl,
						seller,
					},
					freeze: true,
					freeze_message: "Aplicando pago a Notas de Entrega más antiguas...",
					callback: (r) => {
						if (r.message && r.message.ok) {
							d.hide();
							const cnt = (r.message.allocations || []).length;
							frappe.show_alert({
								message: `Pago de $${Number(r.message.payment_amount).toFixed(2)} aplicado a ${cnt} nota(s) de ${client.customer_name || client.customer}.`,
								indicator: "green",
							});
							this.loadNotes();
						}
					},
				});
			},
		});

		const isCashMethod = (method) => {
			const lower = String(method || "").trim().toLowerCase();
			return lower.includes("efectivo") || /\bcash\b/.test(lower);
		};

		const toggleProofFields = () => {
			const method = d.get_value("payment_method");
			const cash = isCashMethod(method);
			d.set_df_property("reference", "hidden", cash ? 1 : 0);
			d.$wrapper.find("#ne-screenshot-group").show();
		};

		const updatePreview = () => {
			const rawPay = parseFloat(d.get_value("paid_amount"));
			const payAmt = isNaN(rawPay) ? 0 : rawPay;
			const method = d.get_value("payment_method") || "";
			const curr = methodCurrencies[method] || "USD";
			let remPay = payAmt;
			const allocRows = pendingNotes.map((n) => {
				const rem = Number(n.remaining_amount || 0);
				const alloc = remPay > 0.01 ? Math.min(remPay, rem) : 0;
				remPay = Math.max(remPay - alloc, 0);
				const statusNext = alloc >= rem - 0.01 ? "<span class='text-success'>Pagada</span>" : alloc > 0 ? "<span class='text-primary'>Pago Parcial</span>" : "<span class='text-muted'>Sin cambio</span>";
				return `<tr><td>${frappe.utils.escape_html(n.name)}</td><td>${frappe.utils.escape_html(n.posting_date || "")}</td><td class="text-right">$${rem.toFixed(2)}</td><td class="text-right"><strong>$${alloc.toFixed(2)}</strong></td><td>${statusNext}</td></tr>`;
			}).join("");
			const bsInfo = fxRate > 0 ? ` (${this.formatBs(payAmt * fxRate)})` : "";
			d.fields_dict.totals_preview.$wrapper.html(`
				<div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:10px;margin-top:6px;">
					<div><strong>Método:</strong> ${frappe.utils.escape_html(method)} <span class="label label-default">${curr}</span> · <strong>Pago:</strong> $${payAmt.toFixed(2)}${bsInfo}</div>
					<table class="table table-bordered table-condensed table-sm mt-2 mb-0" style="font-size:12px;">
						<thead><tr><th>Nota (más antigua primero)</th><th>Fecha</th><th class="text-right">Pendiente</th><th class="text-right">Se aplica</th><th>Nuevo estado</th></tr></thead>
						<tbody>${allocRows}</tbody>
					</table>
				</div>
			`);
		};

		d.fields_dict.payment_method.$input.on("change", () => {
			toggleProofFields();
			updatePreview();
		});
		d.fields_dict.paid_amount.$input.on("input change", () => updatePreview());
		d.$wrapper.on("change", "#ne-payment-screenshot", (e) => {
			const file = e.target.files && e.target.files[0];
			d.$wrapper.find("#ne-screenshot-preview").text(file ? `Archivo seleccionado: ${file.name}` : "");
		});
		d.show();
		toggleProofFields();
		updatePreview();
		setTimeout(() => d.fields_dict.paid_amount.$input.focus().select(), 80);
	}

	openDocumentPreview({ delivery_note = "", customer = "" }) {
		const params = new URLSearchParams();
		if (delivery_note) params.set("delivery_note", delivery_note);
		if (!delivery_note && customer) params.set("customer", customer);
		const pdfUrl = `/api/method/cima_ve.api.delivery_notes.download_delivery_note_pdf?${params.toString()}`;

		// Open the tab synchronously during the user click so mobile browsers don't block it as a popup.
		const win = window.open("", "_blank");
		if (win) {
			try {
				win.document.open();
				win.document.write(
					'<!DOCTYPE html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Generando Documento...</title></head><body style="font-family:Arial,sans-serif;padding:24px;text-align:center;color:#334155;">Generando Nota de Entrega...</body></html>'
				);
				win.document.close();
			} catch (e) {}
		}

		frappe.call({
			method: "cima_ve.api.delivery_notes.get_delivery_note_document",
			args: { delivery_note, customer },
			freeze: !win,
			freeze_message: "Generando documento...",
			callback: (r) => {
				if (!r.message || !r.message.html) {
					if (win && !win.closed) win.close();
					return;
				}
				if (win && !win.closed) {
					win.document.open();
					win.document.write(r.message.html);
					win.document.close();
				} else {
					const targetPdf = r.message.pdf_url || pdfUrl;
					frappe.msgprint({
						title: "Documento Listo",
						message: `
							<p>Tu navegador bloqueó la ventana emergente automática. Usa el botón para abrir o descargar el PDF en una pestaña completa:</p>
							<div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:12px;">
								<a class="btn btn-primary btn-sm" href="${frappe.utils.escape_html(targetPdf)}" target="_blank" rel="noopener">Descargar PDF</a>
							</div>
						`,
					});
				}
			},
			error: () => {
				if (win && !win.closed) win.close();
			},
		});
	}

	openRevertDialog(note) {
		const hasPayments = Boolean(note.ve_ne_factura) || Number(note.paid_amount || 0) > 0;
		const accountingNote =
			"La reversión cancela los movimientos contables en ERPNext; no ejecuta una transferencia bancaria de reembolso.";
		const detailsMsg = hasPayments
			? `Al revertir <strong>${frappe.utils.escape_html(note.name)}</strong>, se devolverán los productos al inventario y se anularán automáticamente la factura (<strong>${frappe.utils.escape_html(note.ve_ne_factura || "asociada")}</strong>) y los pagos registrados. El registro se conservará en el historial como <strong>Cancelada</strong>.`
			: `Al revertir <strong>${frappe.utils.escape_html(note.name)}</strong>, todos los productos serán devueltos al inventario automáticamente y el registro se conservará en el historial como <strong>Cancelada</strong>.`;
		const d = new frappe.ui.Dialog({
			title: `Revertir Nota de Entrega — ${note.name}`,
			fields: [
				{
					fieldtype: "HTML",
					fieldname: "warn",
					options: `<p>${detailsMsg}</p><p class="text-muted small">${accountingNote}</p>`,
				},
				{
					label: "Motivo de Reversión (Opcional)",
					fieldname: "reason",
					fieldtype: "Small Text",
				},
			],
			primary_action_label: "Confirmar Reversión",
			primary_action: (values) => {
				frappe.call({
					method: "cima_ve.api.delivery_notes.revert_delivery_note",
					args: {
						delivery_note: note.name,
						reason: values.reason || "",
					},
					freeze: true,
					freeze_message: "Revirtiendo Nota de Entrega, pagos e inventario...",
					callback: (r) => {
						if (r.message && r.message.ok) {
							d.hide();
							frappe.show_alert({
								message: `Nota ${note.name} revertida y marcada como Cancelada.`,
								indicator: "orange",
							});
							this.loadNotes();
						}
					},
				});
			},
		});
		d.show();
	}

	openCommissionCalculator() {
		frappe.call({
			method: "cima_ve.api.delivery_notes.calculate_seller_commissions",
			args: {
				...this.filters,
				persist_missing: 1,
			},
			freeze: true,
			freeze_message: "Calculando comisiones de vendedores...",
			callback: (r) => {
				if (!r.message) return;
				const res = r.message;
				const bySeller = res.by_seller || [];
				if (res.persisted_count > 0) {
					this.loadNotes();
				}

				const sellerFilterLabel = this.filters.seller
					? frappe.utils.escape_html(this.wrapper.find("#ne-filter-seller option:selected").text() || this.filters.seller)
					: "Todos los vendedores";
				const dateRangeLabel =
					this.filters.from_date || this.filters.to_date
						? `${this.filters.from_date || "..."} → ${this.filters.to_date || "..."}`
						: "Todas las fechas";
				const statusLabel = this.filters.status || "Todos los estados";

				const rowsHtml = bySeller.length
					? bySeller
							.map(
								(b) => `
						<tr>
							<td><strong>${frappe.utils.escape_html(b.seller_name || b.seller_user)}</strong><div class="text-muted small">${frappe.utils.escape_html(b.seller_user)}</div></td>
							<td class="text-center">${b.notes_count} <div class="text-muted small">(${b.paid_notes_count} pag. / ${b.pending_notes_count} pend.)</div></td>
							<td class="text-right">$${Number(b.sales_total || 0).toFixed(2)}</td>
							<td class="text-right">$${Number(b.cost_total || 0).toFixed(2)}</td>
							<td class="text-right">$${Number(b.margin_total || 0).toFixed(2)}</td>
							<td class="text-right text-success">$${Number(b.commission_usd || 0).toFixed(2)}</td>
							<td class="text-right text-primary"><strong>${this.formatBs(b.commission_bs || 0)}</strong><div class="text-muted small">(equiv. $${Number(b.commission_bs_usd_equiv || 0).toFixed(2)})</div></td>
							<td class="text-right text-warning">$${Number(b.pending_commission || 0).toFixed(2)}</td>
							<td class="text-right" style="font-weight:700;color:#5b21b6;">$${Number(b.commission_total || 0).toFixed(2)}</td>
						</tr>`
							)
							.join("")
					: `<tr><td colspan="8" class="text-center text-muted p-3">No hay notas activas con comisión para los filtros seleccionados.</td></tr>`;

				const d = new frappe.ui.Dialog({
					title: "Cálculo de Comisiones por Vendedor",
					size: "large",
					fields: [
						{
							fieldtype: "HTML",
							fieldname: "commission_report",
							options: `
								<div class="mb-3 p-2 small" style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:6px;">
									<strong>Filtros aplicados:</strong> ${sellerFilterLabel} · ${frappe.utils.escape_html(statusLabel)} · ${frappe.utils.escape_html(dateRangeLabel)}
									${res.persisted_count > 0 ? `<span class="label label-info ml-2">${res.persisted_count} notas históricas actualizadas</span>` : ""}
								</div>
								<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-bottom:14px;">
									<div style="background:#fff;border:1px solid #e2e8f0;border-radius:6px;padding:10px;">
										<div class="small text-muted">Ventas Netas</div>
										<div style="font-size:16px;font-weight:700;">$${Number(res.total_sales || 0).toFixed(2)}</div>
									</div>
									<div style="background:#fff;border:1px solid #e2e8f0;border-radius:6px;padding:10px;">
										<div class="small text-muted">Costo Productos</div>
										<div style="font-size:16px;font-weight:700;">$${Number(res.total_cost || 0).toFixed(2)}</div>
									</div>
									<div style="background:#fff;border:1px solid #e2e8f0;border-radius:6px;padding:10px;">
										<div class="small text-muted">Margen de Ganancia</div>
										<div style="font-size:16px;font-weight:700;color:#0f766e;">$${Number(res.total_margin || 0).toFixed(2)}</div>
									</div>
									<div style="background:#f5f3ff;border:1px solid #ddd6fe;border-radius:6px;padding:10px;">
										<div class="small" style="color:#5b21b6;">Total Comisión a Pagar</div>
										<div style="font-size:18px;font-weight:800;color:#5b21b6;">$${Number(res.total_commission || 0).toFixed(2)}</div>
										<div class="small text-muted">Pagadas: $${Number(res.paid_commission || 0).toFixed(2)} · Pend: $${Number(res.pending_commission || 0).toFixed(2)}</div>
									</div>
								</div>
								<div class="table-responsive">
									<table class="table table-bordered table-sm">
										<thead>
											<tr>
												<th>Vendedor</th>
												<th class="text-center">Notas</th>
												<th class="text-right">Ventas Netas</th>
												<th class="text-right">Costo</th>
												<th class="text-right">Margen</th>
												<th class="text-right">A Pagar en $</th>
												<th class="text-right">A Pagar en Bs</th>
												<th class="text-right">Com. Pendientes</th>
												<th class="text-right">Total Comisión ($)</th>
											</tr>
										</thead>
										<tbody>${rowsHtml}</tbody>
									</table>
								</div>
							`,
						},
					],
					primary_action_label: "Cerrar",
					primary_action: () => d.hide(),
				});
				d.show();
			},
		});
	}
}
