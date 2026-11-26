frappe.pages["bi-cima"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: "Inteligencia de Negocios",
		single_column: true,
	});
	new CimaBiPage(page, wrapper);
};

class CimaBiPage {
	constructor(page, wrapper) {
		this.page = page;
		this.wrapper = $(wrapper);
		this.allItems = [];
		this.selectedItemCode = null;
		this.renderShell();
		this.bindEvents();
		this.loadBi();
	}

	renderShell() {
		this.wrapper.find(".layout-main-section").html(`
			<div class="cima-shell">
				<div class="cima-hero">
					<div class="cima-hero-brand">
						<img src="/assets/cima_ve/images/cima-logo-blanco.png" alt="CIMA" class="cima-hero-logo" />
						<div class="cima-hero-title">
							<h2>Inteligencia de Negocios e Importación China</h2>
							<p>Predicción de agotamiento, clasificación ABC, rotación rápida y plan óptimo de compra.</p>
						</div>
					</div>
					<div class="cima-hero-actions">
						<button type="button" class="cima-btn-primary" id="bi-send-alert">Enviar Alerta Semanal Ahora</button>
						<button type="button" class="cima-btn-ghost" id="bi-open-settings">Configurar Días China</button>
					</div>
				</div>

				<div class="cima-kpi-grid" id="bi-kpis"></div>

				<div class="cima-card mb-3" id="bi-product-explorer">
					<div class="cima-card-title">
						<span>Analítica Detallada por Producto</span>
						<span class="cima-badge cima-badge-teal">Busca o haz clic en cualquier producto</span>
					</div>
					<div class="d-flex flex-wrap align-items-center" style="gap: 10px;">
						<input type="text" id="bi-product-search" class="form-control" style="flex: 1; min-width: 240px;"
							placeholder="Buscar por nombre, código o categoría (ej: Cargador, Audífono, Cable)..." />
						<button type="button" class="btn btn-default" id="bi-product-clear">Limpiar</button>
					</div>
					<div id="bi-product-results"></div>
					<div id="bi-product-detail"></div>
				</div>

				<div class="cima-card mb-3">
					<div class="cima-card-title">
						<span>Alertas Críticas: Stock se agota antes de llegar de China</span>
						<span class="cima-badge cima-badge-red" id="bi-lead-badge">Lead time China: 45 días</span>
					</div>
					<div id="bi-table-critical" class="table-responsive"></div>
				</div>

				<div class="row mb-3">
					<div class="col-md-6 mb-3">
						<div class="cima-card">
							<div class="cima-card-title"><span>Top Productos por Ingresos (ABC)</span></div>
							<div id="bi-table-revenue" class="table-responsive"></div>
						</div>
					</div>
					<div class="col-md-6 mb-3">
						<div class="cima-card">
							<div class="cima-card-title"><span>Productos de Rotación Más Rápida</span></div>
							<div id="bi-table-fastest" class="table-responsive"></div>
						</div>
					</div>
				</div>

				<div class="cima-card mb-3">
					<div class="cima-card-title"><span>Plan Recomendado de Compra a China</span></div>
					<div id="bi-table-purchase" class="table-responsive"></div>
				</div>

				<div class="cima-card">
					<div class="cima-card-title"><span>Inventario Sin Movimiento (Capital Inmovilizado)</span></div>
					<div id="bi-table-dead" class="table-responsive"></div>
				</div>
			</div>
		`);
	}

	bindEvents() {
		const root = this.wrapper;
		root.on("click", "#bi-open-settings", () => frappe.set_route("Form", "Cima VE Settings"));
		root.on("input", "#bi-product-search", () => {
			this.renderSearchResults();
		});
		root.on("click", "#bi-product-clear", () => {
			root.find("#bi-product-search").val("");
			this.selectedItemCode = null;
			root.find("#bi-product-results").empty();
			root.find("#bi-product-detail").empty();
		});
		root.on("click", "[data-bi-item]", (e) => {
			const code = $(e.currentTarget).attr("data-bi-item");
			if (code) {
				this.loadProductAnalytics(code);
			}
		});
		root.on("click", "#bi-detail-close", () => {
			this.selectedItemCode = null;
			root.find("#bi-product-detail").empty();
			root.find(".cima-bi-search-item").removeClass("active");
		});
		root.on("click", "#bi-send-alert", () => {
			frappe.call({
				method: "cima_ve.api.bi.send_bi_stockout_alert_now",
				callback: (r) => {
					if (r.message && r.message.sent) {
						frappe.msgprint(`Alerta enviada a: ${(r.message.recipients || []).join(", ")}`);
					} else {
						frappe.msgprint((r.message && r.message.message) || "No hay productos críticos para alertar.");
					}
				},
			});
		});
	}

	loadBi() {
		frappe.call({
			method: "cima_ve.api.bi.get_bi_dashboard",
			callback: (r) => {
				if (!r.message) return;
				this.renderBi(r.message);
			},
		});
	}

	renderBi(data) {
		this.allItems = data.all_items || [];
		const k = data.kpis;
		const cfg = data.config;
		this.wrapper.find("#bi-lead-badge").text(`Lead time China: ${cfg.china_lead_time_days} días · Seguridad: ${cfg.safety_stock_days}d`);
		this.wrapper.find("#bi-kpis").html(`
			<div class="cima-kpi-card danger">
				<div class="cima-kpi-label">Críticos (&lt; ${cfg.china_lead_time_days}d China)</div>
				<div class="cima-kpi-value">${k.critical_count}</div>
				<div class="cima-kpi-sub">${k.warning_count} en alerta preventiva</div>
			</div>
			<div class="cima-kpi-card emerald">
				<div class="cima-kpi-label">Ventas Ventana (${cfg.analysis_window_days}d)</div>
				<div class="cima-kpi-value">$${Number(k.total_revenue_window).toFixed(2)}</div>
				<div class="cima-kpi-sub">${k.total_items_analyzed} ítems analizados</div>
			</div>
			<div class="cima-kpi-card">
				<div class="cima-kpi-label">Inversión Sugerida China</div>
				<div class="cima-kpi-value">$${Number(k.total_recommended_investment).toFixed(2)}</div>
				<div class="cima-kpi-sub">Valor stock actual: $${Number(k.total_inventory_value).toFixed(2)}</div>
			</div>
			<div class="cima-kpi-card amber">
				<div class="cima-kpi-label">Stock Sin Movimiento</div>
				<div class="cima-kpi-value">${k.dead_stock_count}</div>
				<div class="cima-kpi-sub">Candidatos a promoción / Meta Ads</div>
			</div>
		`);

		this.wrapper.find("#bi-table-critical").html(`
			<table class="table table-sm table-bordered">
				<thead><tr><th>Código</th><th>Producto</th><th class="text-right">Stock</th><th class="text-right">Vel./Día</th><th class="text-right">Días Rest.</th><th>Agotamiento Est.</th><th class="text-right">Pedir (China)</th></tr></thead>
				<tbody>${(data.critical_items || []).map((i) => `<tr class="cima-bi-clickable-row" data-bi-item="${frappe.utils.escape_html(i.item_code)}" title="Ver analítica completa"><td><code>${frappe.utils.escape_html(i.item_code)}</code></td><td><strong>${frappe.utils.escape_html(i.item_name)}</strong></td><td class="text-right">${i.current_stock}</td><td class="text-right">${i.daily_velocity.toFixed(2)}</td><td class="text-right cima-text-danger"><strong>${i.days_until_stockout} d</strong></td><td>${frappe.utils.escape_html(i.est_stockout_date || "-")}</td><td class="text-right"><strong>${i.recommended_order_qty} uds</strong></td></tr>`).join("") || `<tr><td colspan="7" class="text-muted text-center">Ningún producto se agota antes del tiempo de llegada desde China.</td></tr>`}</tbody>
			</table>
		`);

		this.wrapper.find("#bi-table-revenue").html(`
			<table class="table table-sm table-bordered">
				<thead><tr><th>ABC</th><th>Producto</th><th class="text-right">Uds.</th><th class="text-right">Ingresos</th></tr></thead>
				<tbody>${(data.top_revenue_items || []).map((i) => `<tr class="cima-bi-clickable-row" data-bi-item="${frappe.utils.escape_html(i.item_code)}" title="Ver analítica completa"><td><span class="cima-badge cima-badge-teal">${i.abc_class}</span></td><td>${frappe.utils.escape_html(i.item_name)}</td><td class="text-right">${i.units_sold_window}</td><td class="text-right"><strong>$${Number(i.revenue_window).toFixed(2)}</strong></td></tr>`).join("") || `<tr><td colspan="4" class="text-muted text-center">Sin ventas</td></tr>`}</tbody>
			</table>
		`);

		this.wrapper.find("#bi-table-fastest").html(`
			<table class="table table-sm table-bordered">
				<thead><tr><th>Producto</th><th class="text-right">Uds./Día</th><th class="text-right">Uds./Mes</th><th class="text-right">Días Stock</th></tr></thead>
				<tbody>${(data.fastest_selling_items || []).map((i) => `<tr class="cima-bi-clickable-row" data-bi-item="${frappe.utils.escape_html(i.item_code)}" title="Ver analítica completa"><td>${frappe.utils.escape_html(i.item_name)}</td><td class="text-right"><strong>${i.daily_velocity.toFixed(2)}</strong></td><td class="text-right">${i.monthly_velocity}</td><td class="text-right">${i.days_until_stockout ?? "∞"}</td></tr>`).join("") || `<tr><td colspan="4" class="text-muted text-center">Sin datos</td></tr>`}</tbody>
			</table>
		`);

		this.wrapper.find("#bi-table-purchase").html(`
			<table class="table table-sm table-bordered">
				<thead><tr><th>Estado</th><th>Código</th><th>Producto</th><th class="text-right">Stock + Tránsito</th><th class="text-right">Cant. Sugerida</th><th class="text-right">Costo Est.</th></tr></thead>
				<tbody>${(data.purchase_plan || []).slice(0, 30).map((i) => {
					const badge = i.alert_level === "CRÍTICO" ? "cima-badge-red" : i.alert_level === "ALERTA" ? "cima-badge-amber" : "cima-badge-green";
					return `<tr class="cima-bi-clickable-row" data-bi-item="${frappe.utils.escape_html(i.item_code)}" title="Ver analítica completa"><td><span class="cima-badge ${badge}">${i.alert_level}</span></td><td><code>${frappe.utils.escape_html(i.item_code)}</code></td><td>${frappe.utils.escape_html(i.item_name)}</td><td class="text-right">${i.current_stock + i.incoming_qty}</td><td class="text-right"><strong>${i.recommended_order_qty}</strong></td><td class="text-right">$${Number(i.est_order_cost).toFixed(2)}</td></tr>`;
				}).join("") || `<tr><td colspan="6" class="text-muted text-center">Inventario cubierto</td></tr>`}</tbody>
			</table>
		`);

		this.wrapper.find("#bi-table-dead").html(`
			<table class="table table-sm table-bordered">
				<thead><tr><th>Código</th><th>Producto</th><th class="text-right">Stock</th><th class="text-right">Capital Inmovilizado</th></tr></thead>
				<tbody>${(data.slow_dead_stock || []).map((i) => `<tr class="cima-bi-clickable-row" data-bi-item="${frappe.utils.escape_html(i.item_code)}" title="Ver analítica completa"><td><code>${frappe.utils.escape_html(i.item_code)}</code></td><td>${frappe.utils.escape_html(i.item_name)}</td><td class="text-right">${i.current_stock}</td><td class="text-right">$${Number(i.stock_value).toFixed(2)}</td></tr>`).join("") || `<tr><td colspan="4" class="text-muted text-center">Todo el inventario tiene rotación</td></tr>`}</tbody>
			</table>
		`);
	}

	alertBadgeClass(level) {
		if (level === "CRÍTICO") return "cima-badge-red";
		if (level === "ALERTA") return "cima-badge-amber";
		if (level === "SALUDABLE") return "cima-badge-green";
		return "cima-badge-dark";
	}

	renderSearchResults() {
		const box = this.wrapper.find("#bi-product-results");
		const query = (this.wrapper.find("#bi-product-search").val() || "").trim().toLowerCase();
		if (!query) {
			box.empty();
			return;
		}
		const matches = (this.allItems || [])
			.filter((i) => {
				const hay = `${i.item_name || ""} ${i.item_code || ""} ${i.item_group || ""}`.toLowerCase();
				return hay.includes(query);
			})
			.slice(0, 12);

		if (!matches.length) {
			box.html(`<div class="text-muted small mt-2">No se encontraron productos que coincidan con "${frappe.utils.escape_html(query)}".</div>`);
			return;
		}

		box.html(`
			<div class="cima-bi-search-results">
				${matches
					.map((i) => {
						const activeCls = this.selectedItemCode === i.item_code ? " active" : "";
						const badgeCls = this.alertBadgeClass(i.alert_level);
						return `
							<div class="cima-bi-search-item${activeCls}" data-bi-item="${frappe.utils.escape_html(i.item_code)}">
								<div class="d-flex justify-content-between align-items-start" style="gap:6px;">
									<strong>${frappe.utils.escape_html(i.item_name)}</strong>
									<span class="cima-badge ${badgeCls}">${frappe.utils.escape_html(i.alert_level)}</span>
								</div>
								<div class="small text-muted mt-1">
									<code>${frappe.utils.escape_html(i.item_code)}</code> · ${frappe.utils.escape_html(i.item_group || "General")}
								</div>
								<div class="small mt-1">
									Stock: <strong>${i.current_stock}</strong> · Vendidas: <strong>${i.units_sold_window}</strong> ($${Number(i.revenue_window || 0).toFixed(2)})
								</div>
							</div>
						`;
					})
					.join("")}
			</div>
		`);
	}

	loadProductAnalytics(itemCode) {
		this.selectedItemCode = itemCode;
		this.wrapper.find(".cima-bi-search-item").removeClass("active");
		this.wrapper.find(`.cima-bi-search-item[data-bi-item="${CSS.escape(itemCode)}"]`).addClass("active");

		const detailBox = this.wrapper.find("#bi-product-detail");
		detailBox.html(`<div class="cima-bi-detail-box text-muted">Cargando analítica completa de <code>${frappe.utils.escape_html(itemCode)}</code>...</div>`);

		const el = detailBox.get(0);
		if (el && el.scrollIntoView) {
			el.scrollIntoView({ behavior: "smooth", block: "nearest" });
		}

		frappe.call({
			method: "cima_ve.api.bi.get_product_bi_analytics",
			args: { item_code: itemCode },
			callback: (r) => {
				if (!r.message) return;
				this.renderProductAnalytics(r.message);
			},
			error: () => {
				detailBox.html(`<div class="cima-bi-detail-box cima-text-danger">No se pudo cargar la analítica de este producto.</div>`);
			},
		});
	}

	renderProductAnalytics(d) {
		const it = d.item || {};
		const s = d.sales_summary || {};
		const v = d.velocity_and_trend || {};
		const p = d.predictions || {};
		const cfg = d.config || {};
		const badgeCls = this.alertBadgeClass(p.alert_level);

		const trendBadge =
			v.trend_direction === "up"
				? `<span class="cima-badge cima-badge-green">▲ +${Number(v.trend_pct).toFixed(1)}% vs 30d ant.</span>`
				: v.trend_direction === "down"
					? `<span class="cima-badge cima-badge-red">▼ ${Number(v.trend_pct).toFixed(1)}% vs 30d ant.</span>`
					: `<span class="cima-badge cima-badge-teal">● Demanda estable (${Number(v.trend_pct).toFixed(1)}%)</span>`;

		const orderUrgency =
			p.recommended_order_date === "INMEDIATO"
				? `<span class="cima-text-danger"><strong>¡ORDENAR INMEDIATO!</strong></span>`
				: p.recommended_order_date
					? `Pedir antes de: <strong>${frappe.utils.escape_html(p.recommended_order_date)}</strong>`
					: "Sin urgencia de reposición";

		const stockoutLine =
			p.days_until_stockout !== null && p.days_until_stockout !== undefined
				? `${p.days_until_stockout} días (${frappe.utils.escape_html(p.est_stockout_date || "-")})`
				: "Sin rotación reciente";

		const projections = [
			{ label: "Próximos 30 días", qty: p.projected_demand_30d, rev: p.projected_revenue_30d, prof: p.projected_profit_30d },
			{ label: `Lead Time China (${cfg.china_lead_time_days}d)`, qty: p.projected_demand_lead_time, rev: p.projected_revenue_lead_time, prof: Number(p.projected_demand_lead_time || 0) * Number(it.margin_per_unit || 0) },
			{ label: "Próximos 60 días", qty: p.projected_demand_60d, rev: p.projected_revenue_60d, prof: p.projected_profit_60d },
			{ label: "Próximos 90 días", qty: p.projected_demand_90d, rev: p.projected_revenue_90d, prof: p.projected_profit_90d },
		];

		const series = d.monthly_series || [];
		const maxMonthUnits = Math.max(1, ...series.map((m) => Number(m.units_sold || 0)));

		this.wrapper.find("#bi-product-detail").html(`
			<div class="cima-bi-detail-box">
				<div class="d-flex flex-wrap justify-content-between align-items-center mb-3" style="gap: 10px;">
					<div>
						<h4 class="mb-1" style="font-weight: 800;">
							${frappe.utils.escape_html(it.item_name)}
							<span class="cima-badge ${badgeCls} ml-1">${frappe.utils.escape_html(p.alert_level)}</span>
							${trendBadge}
						</h4>
						<div class="small text-muted">
							Código: <code>${frappe.utils.escape_html(it.item_code)}</code> · Categoría: <strong>${frappe.utils.escape_html(it.item_group || "General")}</strong>
							${s.last_sale_date ? ` · Última venta: <strong>${frappe.utils.escape_html(s.last_sale_date)}</strong>` : ""}
						</div>
					</div>
					<button type="button" class="btn btn-sm btn-default" id="bi-detail-close">Cerrar detalle</button>
				</div>

				<div class="cima-kpi-grid">
					<div class="cima-kpi-card">
						<div class="cima-kpi-label">Stock Actual</div>
						<div class="cima-kpi-value">${it.current_stock} uds</div>
						<div class="cima-kpi-sub">
							En tránsito: ${it.incoming_qty} · Notas pend.: ${it.pending_dn_qty}<br/>
							Valor costo: $${Number(it.stock_value || 0).toFixed(2)}
						</div>
					</div>
					<div class="cima-kpi-card emerald">
						<div class="cima-kpi-label">Vendido (Histórico / ${cfg.analysis_window_days}d)</div>
						<div class="cima-kpi-value">${s.units_all_time} uds</div>
						<div class="cima-kpi-sub">
							Total: $${Number(s.revenue_all_time || 0).toFixed(2)} (${s.invoices_all_time} fact.)<br/>
							Últimos ${cfg.analysis_window_days}d: ${s.units_window} uds ($${Number(s.revenue_window || 0).toFixed(2)})
						</div>
					</div>
					<div class="cima-kpi-card">
						<div class="cima-kpi-label">Velocidad de Venta</div>
						<div class="cima-kpi-value">${Number(v.daily_velocity || 0).toFixed(2)} / día</div>
						<div class="cima-kpi-sub">
							7d: ${s.units_7d} uds · 30d: ${s.units_30d} uds<br/>
							Ritmo mensual: ${v.monthly_velocity} uds/mes
						</div>
					</div>
					<div class="cima-kpi-card ${p.will_stockout_before_china ? "danger" : "amber"}">
						<div class="cima-kpi-label">Agotamiento y Reposición</div>
						<div class="cima-kpi-value">${p.recommended_order_qty > 0 ? `Pedir ${p.recommended_order_qty} uds` : "Cubierto"}</div>
						<div class="cima-kpi-sub">
							Agotamiento: ${stockoutLine}<br/>
							${orderUrgency}${p.est_order_cost > 0 ? ` · Costo: $${Number(p.est_order_cost).toFixed(2)}` : ""}
						</div>
					</div>
					<div class="cima-kpi-card dark">
						<div class="cima-kpi-label">Precio, Costo y Margen</div>
						<div class="cima-kpi-value">$${Number(it.unit_price || it.avg_selling_price || 0).toFixed(2)}</div>
						<div class="cima-kpi-sub">
							Costo: $${Number(it.unit_cost || 0).toFixed(2)} · Margen: $${Number(it.margin_per_unit || 0).toFixed(2)} (${Number(it.margin_pct || 0).toFixed(1)}%)<br/>
							Ganancia ${cfg.analysis_window_days}d: $${Number(s.profit_window || 0).toFixed(2)}
						</div>
					</div>
				</div>

				<div class="row mb-3">
					<div class="col-md-6 mb-3">
						<h5 class="font-weight-bold mb-2">Predicción de Ventas Esperadas</h5>
						<div class="table-responsive">
							<table class="table table-sm table-bordered">
								<thead>
									<tr>
										<th>Horizonte</th>
										<th class="text-right">Demanda Est.</th>
										<th class="text-right">Ingresos Est.</th>
										<th class="text-right">Ganancia Est.</th>
										<th class="text-right">Balance Stock</th>
									</tr>
								</thead>
								<tbody>
									${projections
										.map((row) => {
											const diff = Number(it.effective_stock || 0) - Number(row.qty || 0);
											const balHtml =
												diff >= 0
													? `<span class="cima-text-ok">+${diff.toFixed(1)} uds</span>`
													: `<span class="cima-text-danger"><strong>${diff.toFixed(1)} uds</strong></span>`;
											return `
												<tr>
													<td><strong>${frappe.utils.escape_html(row.label)}</strong></td>
													<td class="text-right">${Number(row.qty || 0).toFixed(1)} uds</td>
													<td class="text-right">$${Number(row.rev || 0).toFixed(2)}</td>
													<td class="text-right">$${Number(row.prof || 0).toFixed(2)}</td>
													<td class="text-right">${balHtml}</td>
												</tr>
											`;
										})
										.join("")}
								</tbody>
							</table>
						</div>
					</div>
					<div class="col-md-6 mb-3">
						<h5 class="font-weight-bold mb-2">Tendencia Mensual (Últimos 6 Meses)</h5>
						<div class="cima-bi-month-bars">
							${series
								.map((m) => {
									const pct = Math.max(4, Math.round((Number(m.units_sold || 0) / maxMonthUnits) * 100));
									return `
										<div class="cima-bi-month-col" title="${frappe.utils.escape_html(m.month)}: ${m.units_sold} uds · $${Number(m.revenue || 0).toFixed(2)}">
											<div class="cima-bi-month-val">${m.units_sold}</div>
											<div class="cima-bi-month-bar" style="height: ${m.units_sold > 0 ? pct : 3}%;"></div>
											<div class="cima-bi-month-label">${frappe.utils.escape_html(m.month)}</div>
										</div>
									`;
								})
								.join("")}
						</div>
					</div>
				</div>

				<div class="row">
					<div class="col-md-5 mb-2">
						<h5 class="font-weight-bold mb-2">Stock por Almacén</h5>
						<div class="table-responsive">
							<table class="table table-sm table-bordered">
								<thead><tr><th>Almacén</th><th class="text-right">Disponible</th><th class="text-right">En Tránsito</th></tr></thead>
								<tbody>
									${(d.warehouse_breakdown || [])
										.map((w) => `<tr><td>${frappe.utils.escape_html(w.warehouse)}</td><td class="text-right"><strong>${w.actual_qty}</strong></td><td class="text-right">${w.ordered_qty}</td></tr>`)
										.join("") || `<tr><td colspan="3" class="text-muted text-center">Sin stock en almacenes</td></tr>`}
								</tbody>
							</table>
						</div>
					</div>
					<div class="col-md-7 mb-2">
						<h5 class="font-weight-bold mb-2">Últimas Ventas Facturadas</h5>
						<div class="table-responsive">
							<table class="table table-sm table-bordered">
								<thead><tr><th>Fecha</th><th>Factura</th><th>Cliente</th><th class="text-right">Cant.</th><th class="text-right">Total</th></tr></thead>
								<tbody>
									${(d.recent_sales || [])
										.map((r) => `<tr><td>${frappe.utils.escape_html(r.posting_date)}</td><td><code>${frappe.utils.escape_html(r.invoice_name)}</code></td><td>${frappe.utils.escape_html(r.customer_name)}</td><td class="text-right">${r.qty}</td><td class="text-right"><strong>$${Number(r.amount || 0).toFixed(2)}</strong></td></tr>`)
										.join("") || `<tr><td colspan="5" class="text-muted text-center">Sin ventas facturadas registradas</td></tr>`}
								</tbody>
							</table>
						</div>
					</div>
				</div>
			</div>
		`);
	}
}
